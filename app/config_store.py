#!/usr/bin/env python3
"""
config_store.py

Centralized configuration storage for the Processing Status Report tool,
backed by a single MSSQL table (``dbo.report_config``) that lives ON the
Prod SQL Server instance -- alongside the abhi_mask* reporting databases.

WHAT CHANGED FROM THE SQLITE VERSION
-------------------------------------------------------------------------
Configuration no longer lives in a local ``config_store.sqlite3`` file
encrypted with a Fernet key. It now lives in ONE row of a table on the
Prod DB server, so every host that runs the report reads the same central
config, and a DBA can inspect/change it with plain SQL.

BOOTSTRAP (chicken-and-egg)
-------------------------------------------------------------------------
The config table is on the Prod SQL instance, so we need connection
details for that instance BEFORE we can read the config. Those bootstrap
details cannot live in the table they point to -- they come from
environment variables:

    CONFIG_DB_SERVER   host,port of the Prod SQL instance (e.g. 10.21.42.17,7865)
    CONFIG_DB_NAME     database that holds the report_config table (e.g. master
                       or a dedicated 'ops' database)
    CONFIG_DB_USER     SQL login used to read config AND (by default) to run
                       the reporting queries
    CONFIG_DB_PWD      password for that login (plaintext env var -- keep the
                       env/service account locked down)
    CONFIG_DB_DRIVER   (optional) ODBC driver name;
                       defaults to "ODBC Driver 18 for SQL Server"

The same login is reused to connect to each reporting database in
``db_list`` (they are on the same instance), unless the config row
overrides report_server / report_user / report_pwd_b64.

THE CONFIG ROW (single global config, id = 1)
-------------------------------------------------------------------------
    start_date      report range start (inclusive) for the pivot table
    end_date        report range end (exclusive)
    db_list         comma-separated database names to run against
    prod_db         which database in db_list is PROD (day-wise summary)
    report_server   optional override for the reporting-DB server
    report_user     optional override for the reporting-DB login
    report_pwd_b64  optional override password (base64) for that login
    from_mail       "from" mail id
    from_name       display name for the "from" address
    mail_pwd_b64    mail password, BASE64-ENCODED
    to_mails        semicolon-separated To recipients
    cc_mails        semicolon-separated Cc recipients
    smtp_server     SMTP host
    smtp_port       SMTP port
    triggers        comma-separated HH:MM (24h) trigger times, e.g. "09:30,13:30,18:30"
    last_run_marker internal de-dup marker ("YYYY-MM-DD HH:MM" of last fired trigger)

SECURITY NOTE ON base64
-------------------------------------------------------------------------
``mail_pwd_b64`` / ``report_pwd_b64`` are BASE64 -- that is ENCODING, not
encryption. Anyone who can read the row can decode the password in one
line. This obfuscates it from a casual glance only. Protect it with SQL
Server permissions on the report_config table (grant SELECT only to the
service account). If you need real secrecy, use a secrets manager or
SQL Server Always Encrypted on these columns.
"""

import base64
import binascii
import os
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

try:
    import pyodbc
except ImportError:
    raise ImportError("Missing dependency: pip install pyodbc")


DEFAULT_ODBC_DRIVER = "ODBC Driver 18 for SQL Server"
CONFIG_ROW_ID = 1


# ------------------------------------------------------------------ #
# Dataclasses -- the shapes the rest of the app depends on.
# DbConfig / MailConfig are unchanged so db.py / mailer.py / report.py
# keep working; they are DERIVED from AppConfig below.
# ------------------------------------------------------------------ #
@dataclass
class DbConfig:
    key: str
    label: str
    server: str
    database: str
    username: str
    password: str
    is_prod: bool = False


@dataclass
class MailConfig:
    smtp_server: str
    smtp_port: int
    mail_from: str
    mail_from_name: str
    mail_password: str
    default_to: str
    default_cc: str


@dataclass
class AppConfig:
    start_date: Optional[str]      # 'YYYY-MM-DD' or None
    end_date: Optional[str]        # 'YYYY-MM-DD' or None
    db_list: List[str]             # database names
    prod_db: Optional[str]
    report_server: Optional[str]   # override; None -> use bootstrap server
    report_user: Optional[str]     # override; None -> use bootstrap user
    report_pwd: Optional[str]      # override (already-decoded); None -> bootstrap pwd
    from_mail: str
    from_name: str
    mail_password: str             # already base64-decoded
    to_mails: str
    cc_mails: str
    smtp_server: str
    smtp_port: int
    triggers: List[str]            # ['09:30', '13:30', '18:30']
    last_run_marker: str


# ------------------------------------------------------------------ #
# base64 helpers (encoding, NOT encryption -- see module docstring)
# ------------------------------------------------------------------ #
def b64_encode(plaintext: str) -> str:
    return base64.b64encode((plaintext or "").encode("utf-8")).decode("ascii")


def b64_decode(encoded: str) -> str:
    if not encoded:
        return ""
    try:
        return base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        # Tolerate a value that was stored in plaintext by mistake, so a
        # hand-edited row doesn't hard-fail the whole run.
        return encoded


# ------------------------------------------------------------------ #
# Bootstrap connection to the Prod SQL instance (env-driven)
# ------------------------------------------------------------------ #
def _bootstrap_env() -> Dict[str, str]:
    missing = [
        name for name in ("CONFIG_DB_SERVER", "CONFIG_DB_NAME", "CONFIG_DB_USER", "CONFIG_DB_PWD")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s) for the config DB: "
            + ", ".join(missing)
            + ".\nSet CONFIG_DB_SERVER, CONFIG_DB_NAME, CONFIG_DB_USER, CONFIG_DB_PWD "
            "(optionally CONFIG_DB_DRIVER) before running. Example (PowerShell):\n"
            '  $env:CONFIG_DB_SERVER = "10.21.42.17,7865"\n'
            '  $env:CONFIG_DB_NAME   = "master"\n'
            '  $env:CONFIG_DB_USER   = "ABHIMASK"\n'
            '  $env:CONFIG_DB_PWD    = "***"'
        )
    return {
        "server": os.environ["CONFIG_DB_SERVER"],
        "database": os.environ["CONFIG_DB_NAME"],
        "user": os.environ["CONFIG_DB_USER"],
        "pwd": os.environ["CONFIG_DB_PWD"],
        "driver": os.environ.get("CONFIG_DB_DRIVER", DEFAULT_ODBC_DRIVER),
    }


def _connect_config_db() -> "pyodbc.Connection":
    env = _bootstrap_env()
    conn_str = (
        f"DRIVER={{{env['driver']}}};"
        f"SERVER={env['server']};"
        f"DATABASE={env['database']};"
        f"UID={env['user']};"
        f"PWD={env['pwd']};"
        "TrustServerCertificate=yes;"
        "Encrypt=yes;"
    )
    return pyodbc.connect(conn_str, timeout=15)


# ------------------------------------------------------------------ #
# Schema + seeding (idempotent: creates table + singleton row if absent)
# ------------------------------------------------------------------ #
_CREATE_TABLE_SQL = """
IF OBJECT_ID('dbo.report_config', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.report_config (
        id              INT           NOT NULL PRIMARY KEY,
        start_date      DATE          NULL,
        end_date        DATE          NULL,
        db_list         NVARCHAR(MAX) NOT NULL CONSTRAINT DF_report_config_db_list DEFAULT '',
        prod_db         NVARCHAR(256) NULL,
        report_server   NVARCHAR(256) NULL,
        report_user     NVARCHAR(256) NULL,
        report_pwd_b64  NVARCHAR(MAX) NULL,
        from_mail       NVARCHAR(256) NOT NULL CONSTRAINT DF_report_config_from_mail DEFAULT '',
        from_name       NVARCHAR(256) NOT NULL CONSTRAINT DF_report_config_from_name DEFAULT '',
        mail_pwd_b64    NVARCHAR(MAX) NOT NULL CONSTRAINT DF_report_config_mail_pwd DEFAULT '',
        to_mails        NVARCHAR(MAX) NOT NULL CONSTRAINT DF_report_config_to DEFAULT '',
        cc_mails        NVARCHAR(MAX) NOT NULL CONSTRAINT DF_report_config_cc DEFAULT '',
        smtp_server     NVARCHAR(256) NOT NULL CONSTRAINT DF_report_config_smtp_srv DEFAULT 'smtp.office365.com',
        smtp_port       INT           NOT NULL CONSTRAINT DF_report_config_smtp_port DEFAULT 587,
        triggers        NVARCHAR(256) NOT NULL CONSTRAINT DF_report_config_triggers DEFAULT '',
        last_run_marker NVARCHAR(64)  NOT NULL CONSTRAINT DF_report_config_last_run DEFAULT '',
        CONSTRAINT CK_report_config_singleton CHECK (id = 1)
    );
END
"""

# Seed values -- only inserted if the singleton row is missing. Mirrors
# the original hardcoded defaults so the tool works out of the box; edit
# afterwards with edit_config.py (do not expect changes here to take
# effect once the row exists).
_SEED_DB_LIST = "abhi_mask,abhi_maskv2,abhi_maskv3,abhi_maskv4,abhi_maskv5,abhi_maskv6"
_SEED_PROD_DB = "abhi_mask"
_SEED_FROM_MAIL = "nv@ctdtechs.com"
_SEED_FROM_NAME = "Processing Status Report"
_SEED_MAIL_PWD = "NiveM#31"
_SEED_TO_MAILS = "vn@ctdtechs.com"
_SEED_TRIGGERS = "09:30,13:30,18:30"


def _seed_if_empty(conn: "pyodbc.Connection"):
    exists = conn.execute(
        "SELECT COUNT(*) FROM dbo.report_config WHERE id = ?", CONFIG_ROW_ID
    ).fetchone()[0]
    if exists:
        return
    conn.execute(
        """
        INSERT INTO dbo.report_config
            (id, start_date, end_date, db_list, prod_db,
             from_mail, from_name, mail_pwd_b64, to_mails, cc_mails,
             smtp_server, smtp_port, triggers, last_run_marker)
        VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, '', 'smtp.office365.com', 587, ?, '')
        """,
        CONFIG_ROW_ID,
        _SEED_DB_LIST,
        _SEED_PROD_DB,
        _SEED_FROM_MAIL,
        _SEED_FROM_NAME,
        b64_encode(_SEED_MAIL_PWD),
        _SEED_TO_MAILS,
        _SEED_TRIGGERS,
    )


def _ensure_ready(conn: "pyodbc.Connection"):
    conn.execute(_CREATE_TABLE_SQL)
    _seed_if_empty(conn)
    conn.commit()


# ------------------------------------------------------------------ #
# Small parse helpers
# ------------------------------------------------------------------ #
def _split_list(raw: Optional[str], sep: str) -> List[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.split(sep) if p.strip()]


def _fmt_date(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (date,)):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


# ------------------------------------------------------------------ #
# Public API
# ------------------------------------------------------------------ #
def load_app_config() -> AppConfig:
    """Read the single config row (creating/seeding the table if needed)."""
    with closing(_connect_config_db()) as conn:
        _ensure_ready(conn)
        row = conn.execute(
            """
            SELECT start_date, end_date, db_list, prod_db,
                   report_server, report_user, report_pwd_b64,
                   from_mail, from_name, mail_pwd_b64, to_mails, cc_mails,
                   smtp_server, smtp_port, triggers, last_run_marker
            FROM dbo.report_config WHERE id = ?
            """,
            CONFIG_ROW_ID,
        ).fetchone()

    if row is None:
        raise RuntimeError("report_config row (id=1) not found even after seeding.")

    (start_date, end_date, db_list, prod_db, report_server, report_user, report_pwd_b64,
     from_mail, from_name, mail_pwd_b64, to_mails, cc_mails,
     smtp_server, smtp_port, triggers, last_run_marker) = row

    return AppConfig(
        start_date=_fmt_date(start_date),
        end_date=_fmt_date(end_date),
        db_list=_split_list(db_list, ","),
        prod_db=(prod_db or None),
        report_server=(report_server or None),
        report_user=(report_user or None),
        report_pwd=(b64_decode(report_pwd_b64) if report_pwd_b64 else None),
        from_mail=from_mail or "",
        from_name=from_name or "",
        mail_password=b64_decode(mail_pwd_b64),
        to_mails=to_mails or "",
        cc_mails=cc_mails or "",
        smtp_server=smtp_server or "smtp.office365.com",
        smtp_port=int(smtp_port) if smtp_port is not None else 587,
        triggers=_split_list(triggers, ","),
        last_run_marker=last_run_marker or "",
    )


def load_db_configs(app_cfg: Optional[AppConfig] = None) -> Dict[str, DbConfig]:
    """Build a {db_name: DbConfig} map from the config row's db_list.

    The reporting databases live on the same instance as the config table,
    so by default they reuse the bootstrap login. report_server /
    report_user / report_pwd_b64 override that if set.
    """
    if app_cfg is None:
        app_cfg = load_app_config()

    env = _bootstrap_env()
    server = app_cfg.report_server or env["server"]
    username = app_cfg.report_user or env["user"]
    password = app_cfg.report_pwd if app_cfg.report_pwd is not None else env["pwd"]

    result: Dict[str, DbConfig] = {}
    for name in app_cfg.db_list:
        is_prod = (app_cfg.prod_db is not None and name == app_cfg.prod_db)
        label = f"{name} (PROD)" if is_prod else f"{name} (Historical)"
        result[name] = DbConfig(
            key=name,
            label=label,
            server=server,
            database=name,
            username=username,
            password=password,
            is_prod=is_prod,
        )
    return result


def get_prod_db_key(db_configs: Dict[str, DbConfig]) -> Optional[str]:
    for key, cfg in db_configs.items():
        if cfg.is_prod:
            return key
    return None


def load_mail_config(app_cfg: Optional[AppConfig] = None) -> MailConfig:
    if app_cfg is None:
        app_cfg = load_app_config()
    return MailConfig(
        smtp_server=app_cfg.smtp_server,
        smtp_port=app_cfg.smtp_port,
        mail_from=app_cfg.from_mail,
        mail_from_name=app_cfg.from_name,
        mail_password=app_cfg.mail_password,
        default_to=app_cfg.to_mails,
        default_cc=app_cfg.cc_mails,
    )


# ------------------------------------------------------------------ #
# Writers -- update individual columns of the singleton row.
# `password_field` columns are base64-encoded on write.
# ------------------------------------------------------------------ #
_UPDATABLE_COLUMNS = {
    "start_date", "end_date", "db_list", "prod_db",
    "report_server", "report_user", "report_pwd_b64",
    "from_mail", "from_name", "mail_pwd_b64", "to_mails", "cc_mails",
    "smtp_server", "smtp_port", "triggers", "last_run_marker",
}


def set_config_field(column: str, value):
    """Update one column of the singleton config row.

    Pass base64-encoded values for the *_b64 columns (use b64_encode()).
    """
    if column not in _UPDATABLE_COLUMNS:
        raise ValueError(f"Unknown / non-updatable config column: {column}")
    with closing(_connect_config_db()) as conn:
        _ensure_ready(conn)
        conn.execute(
            f"UPDATE dbo.report_config SET {column} = ? WHERE id = ?",
            value, CONFIG_ROW_ID,
        )
        conn.commit()


def set_mail_password(plaintext: str):
    set_config_field("mail_pwd_b64", b64_encode(plaintext))


def set_report_password(plaintext: str):
    set_config_field("report_pwd_b64", b64_encode(plaintext))


def mark_trigger_fired(marker: str):
    """Record the last fired trigger ('YYYY-MM-DD HH:MM') for de-dup."""
    set_config_field("last_run_marker", marker)
