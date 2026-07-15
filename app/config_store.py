#!/usr/bin/env python3
"""
config_store.py

Centralized, encrypted configuration storage for the Processing Status
Report tool.

WHY THIS EXISTS
-------------------------------------------------------------------------
Database and mail credentials used to live in plaintext (or weak base64)
directly inside the report script. That means:
  - credentials sit in source control / a plaintext file
  - rotating a password means editing and redeploying code
  - anyone who can read the .py file can read every password

This module replaces that with a local SQLite "configuration table"
(config_store.sqlite3) plus a separate encryption key file
(config_store.key). Passwords are encrypted at rest with Fernet
(AES-128-CBC + HMAC) from the `cryptography` package -- this is the
standard pattern for "handful of services, no external secrets manager"
setups. The plaintext password is only ever held in memory, for the
duration of a connection.

FIRST RUN
-------------------------------------------------------------------------
If config_store.sqlite3 doesn't exist yet, this module creates it and
seeds it from DEFAULT_DB_CONFIG / DEFAULT_MAIL_CONFIG below (so the tool
keeps working out of the box). After that first run, the SQLite file is
the source of truth -- use edit_config.py to change servers, add a new
historical database, or rotate a password; don't edit the defaults here
and expect them to take effect on an existing install.

FILES CREATED (next to this module)
-------------------------------------------------------------------------
  config_store.sqlite3   the configuration tables: db_config (server,
                          database, username, ENCRYPTED password, is_prod
                          flag per entry) and mail_config (SMTP settings +
                          ENCRYPTED mail password).
  config_store.key       the Fernet key used to encrypt/decrypt those
                          passwords. Restricted to owner-read/write
                          (chmod 600) automatically on creation. Losing
                          this file means stored passwords can't be
                          decrypted and must be re-entered via
                          edit_config.py.

SECURITY NOTES
-------------------------------------------------------------------------
  - This protects credentials from casual disclosure (grep-ing source,
    an accidental git commit, a screen-shared code review) -- it is NOT
    a substitute for a real secrets manager (Azure Key Vault, AWS
    Secrets Manager, HashiCorp Vault) if one is available in your org.
    If it is, prefer it; the DbConfig/MailConfig dataclasses below are
    the target shape to fill in from that source instead.
  - Both config_store.sqlite3 and config_store.key MUST be excluded from
    version control (see .gitignore) and readable only by the service
    account that runs this script.
"""

import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from typing import Dict, Optional

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    print("Missing dependency: pip install cryptography")
    raise

# app/config_store.py -> project root is one level up -> config/ lives
# at <project_root>/config/, kept separate from source so it's easy to
# exclude from version control / back up / lock down permissions on.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_DIR = os.path.join(_PROJECT_ROOT, "config")
os.makedirs(_CONFIG_DIR, exist_ok=True)

DB_PATH = os.environ.get("STATUS_REPORT_CONFIG_DB", os.path.join(_CONFIG_DIR, "config_store.sqlite3"))
KEY_PATH = os.environ.get("STATUS_REPORT_CONFIG_KEY", os.path.join(_CONFIG_DIR, "config_store.key"))


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
    mail_password: str
    default_to: str
    default_cc: str


# ------------------------------------------------------------------ #
# Seed defaults -- only used the FIRST time config_store.sqlite3 is
# created on a machine. After that, the sqlite file is authoritative;
# use edit_config.py to change values rather than editing these.
# ------------------------------------------------------------------ #
_DEFAULT_SERVER = "10.21.42.17,7865"
_DEFAULT_USERNAME = "ABHIMASK"
_DEFAULT_PASSWORD = "abhiM@4312"

DEFAULT_DB_CONFIG = [
    DbConfig("abhi_mask", "abhi_mask (PROD)", _DEFAULT_SERVER, "abhi_mask",
             _DEFAULT_USERNAME, _DEFAULT_PASSWORD, is_prod=True),
    DbConfig("abhi_maskv2", "abhi_maskv2 (Historical)", _DEFAULT_SERVER, "abhi_maskv2",
             _DEFAULT_USERNAME, _DEFAULT_PASSWORD),
    DbConfig("abhi_maskv3", "abhi_maskv3 (Historical)", _DEFAULT_SERVER, "abhi_maskv3",
             _DEFAULT_USERNAME, _DEFAULT_PASSWORD),
    DbConfig("abhi_maskv4", "abhi_maskv4 (Historical)", _DEFAULT_SERVER, "abhi_maskv4",
             _DEFAULT_USERNAME, _DEFAULT_PASSWORD),
    DbConfig("abhi_maskv5", "abhi_maskv5 (Historical)", _DEFAULT_SERVER, "abhi_maskv5",
             _DEFAULT_USERNAME, _DEFAULT_PASSWORD),
    DbConfig("abhi_maskv6", "abhi_maskv6 (Historical)", _DEFAULT_SERVER, "abhi_maskv6",
             _DEFAULT_USERNAME, _DEFAULT_PASSWORD),
]

DEFAULT_MAIL_CONFIG = MailConfig(
    smtp_server="smtp.office365.com",
    smtp_port=587,
    mail_from="nv@ctdtechs.com",
    mail_password="NiveM#31",
    default_to="vn@ctdtechs.com",
    default_cc="",
)


# ------------------------------------------------------------------ #
# Key + encryption helpers
# ------------------------------------------------------------------ #
def _get_or_create_key() -> bytes:
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "rb") as f:
            return f.read()
    key = Fernet.generate_key()
    with open(KEY_PATH, "wb") as f:
        f.write(key)
    try:
        os.chmod(KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 600, owner-only
    except OSError:
        pass  # best-effort -- some filesystems (e.g. Windows) don't support this
    return key


_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_get_or_create_key())
    return _fernet


def _encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def _decrypt(ciphertext: str) -> str:
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise RuntimeError(
            "Could not decrypt a stored credential -- config_store.key doesn't match "
            "config_store.sqlite3 (wrong key file, or the DB was copied from another "
            "machine without its matching key). Re-enter credentials via edit_config.py."
        )


# ------------------------------------------------------------------ #
# Schema + seeding
# ------------------------------------------------------------------ #
def _create_schema(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS db_config (
            key                 TEXT PRIMARY KEY,
            label               TEXT NOT NULL,
            server              TEXT NOT NULL,
            database            TEXT NOT NULL,
            username            TEXT NOT NULL,
            password_encrypted  TEXT NOT NULL,
            is_prod             INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mail_config (
            setting_key    TEXT PRIMARY KEY,
            setting_value  TEXT NOT NULL
        )
    """)


def _seed_if_empty(conn: sqlite3.Connection):
    if conn.execute("SELECT COUNT(*) FROM db_config").fetchone()[0] == 0:
        for cfg in DEFAULT_DB_CONFIG:
            conn.execute(
                "INSERT INTO db_config "
                "(key, label, server, database, username, password_encrypted, is_prod) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cfg.key, cfg.label, cfg.server, cfg.database, cfg.username,
                 _encrypt(cfg.password), int(cfg.is_prod)),
            )

    if conn.execute("SELECT COUNT(*) FROM mail_config").fetchone()[0] == 0:
        mc = DEFAULT_MAIL_CONFIG
        plain = {
            "smtp_server": mc.smtp_server,
            "smtp_port": str(mc.smtp_port),
            "mail_from": mc.mail_from,
            "default_to": mc.default_to,
            "default_cc": mc.default_cc,
        }
        for k, v in plain.items():
            conn.execute(
                "INSERT INTO mail_config (setting_key, setting_value) VALUES (?, ?)", (k, v)
            )
        conn.execute(
            "INSERT INTO mail_config (setting_key, setting_value) VALUES (?, ?)",
            ("mail_password_encrypted", _encrypt(mc.mail_password)),
        )


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _create_schema(conn)
    _seed_if_empty(conn)  # no-op once tables already have rows
    conn.commit()
    return conn


# ------------------------------------------------------------------ #
# Public API
# ------------------------------------------------------------------ #
def load_db_configs() -> Dict[str, DbConfig]:
    """Reads every row from db_config, decrypting passwords in memory only."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT key, label, server, database, username, password_encrypted, is_prod "
            "FROM db_config ORDER BY rowid"
        ).fetchall()
    result: Dict[str, DbConfig] = {}
    for r in rows:
        result[r["key"]] = DbConfig(
            key=r["key"],
            label=r["label"],
            server=r["server"],
            database=r["database"],
            username=r["username"],
            password=_decrypt(r["password_encrypted"]),
            is_prod=bool(r["is_prod"]),
        )
    return result


def get_prod_db_key(db_configs: Dict[str, DbConfig]) -> Optional[str]:
    for key, cfg in db_configs.items():
        if cfg.is_prod:
            return key
    return None


def load_mail_config() -> MailConfig:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT setting_key, setting_value FROM mail_config").fetchall()
    values = {r["setting_key"]: r["setting_value"] for r in rows}
    return MailConfig(
        smtp_server=values.get("smtp_server", DEFAULT_MAIL_CONFIG.smtp_server),
        smtp_port=int(values.get("smtp_port", DEFAULT_MAIL_CONFIG.smtp_port)),
        mail_from=values.get("mail_from", DEFAULT_MAIL_CONFIG.mail_from),
        mail_password=(_decrypt(values["mail_password_encrypted"])
                       if "mail_password_encrypted" in values
                       else DEFAULT_MAIL_CONFIG.mail_password),
        default_to=values.get("default_to", DEFAULT_MAIL_CONFIG.default_to),
        default_cc=values.get("default_cc", DEFAULT_MAIL_CONFIG.default_cc),
    )


def upsert_db_config(cfg: DbConfig):
    """Add a new database entry, or update an existing one (password re-encrypted)."""
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO db_config (key, label, server, database, username, password_encrypted, is_prod) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  label=excluded.label, server=excluded.server, database=excluded.database, "
            "  username=excluded.username, password_encrypted=excluded.password_encrypted, "
            "  is_prod=excluded.is_prod",
            (cfg.key, cfg.label, cfg.server, cfg.database, cfg.username,
             _encrypt(cfg.password), int(cfg.is_prod)),
        )
        conn.commit()


def delete_db_config(key: str):
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM db_config WHERE key = ?", (key,))
        conn.commit()


def set_mail_setting(setting_key: str, value, encrypt: bool = False):
    stored_key = f"{setting_key}_encrypted" if encrypt else setting_key
    stored_value = _encrypt(str(value)) if encrypt else str(value)
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO mail_config (setting_key, setting_value) VALUES (?, ?) "
            "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
            (stored_key, stored_value),
        )
        conn.commit()
