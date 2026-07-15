#!/usr/bin/env python3
"""
db.py

Connection handling + retry/backoff logic for the Processing Status
Report. Credentials come from config_store (encrypted SQLite), never
from hardcoded values in this file.
"""

import logging
import time

try:
    import pyodbc
except ImportError:
    raise ImportError("Missing dependency: pip install pyodbc")

from . import config_store as cs
from . import queries

log = logging.getLogger("processing_status_report")

QUERY_TIMEOUT_SECONDS = 180     # per-attempt query timeout (headroom beyond lock timeout)
LOCK_TIMEOUT_MS = 60000         # fail fast on blocking locks instead of hanging
CONNECT_TIMEOUT_SECONDS = 15
MAX_RETRIES = 4
RETRY_BACKOFF_SECONDS = 10      # multiplied by attempt number

# SQLSTATE prefixes worth retrying (transient): timeouts, deadlocks, dropped
# connections, general connection failures.
TRANSIENT_SQLSTATES = ("HYT00", "HYT01", "40001", "08S01", "08001", "08004")

# SQL Server sometimes reports a genuinely transient condition (lock timeout,
# deadlock victim, network blip) under a generic SQLSTATE like 42000, with the
# real reason only visible in the message text / native error number. Catch
# those here so they still get retried instead of being treated as a
# permission/syntax error.
TRANSIENT_MESSAGE_MARKERS = (
    "lock request time out period exceeded",  # native error 1222
    "deadlock",                                # native error 1205
    "timeout expired",
    "communication link failure",
    "general network error",
    "transport-level error",
    "connection is busy",
)


def is_transient_error(exc: "pyodbc.Error") -> bool:
    sqlstate = str(exc.args[0]).upper() if exc.args else ""
    if sqlstate in TRANSIENT_SQLSTATES:
        return True
    message = str(exc).lower()
    return any(marker in message for marker in TRANSIENT_MESSAGE_MARKERS)


def get_connection(cfg: cs.DbConfig):
    """Open a connection with a bounded login timeout, using credentials
    decrypted from the config store (never hardcoded)."""
    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={cfg.server};"
        f"DATABASE={cfg.database};"
        f"UID={cfg.username};"
        f"PWD={cfg.password};"
        "TrustServerCertificate=yes;"
        "Encrypt=yes;"
    )
    return pyodbc.connect(conn_str, timeout=CONNECT_TIMEOUT_SECONDS)


def _run_with_retry(cfg: cs.DbConfig, label: str, run_fn):
    """Shared retry/backoff wrapper. `run_fn(cursor)` does the actual
    execute+fetch and returns the result; retried on transient errors."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        conn = None
        try:
            t0 = time.time()
            conn = get_connection(cfg)
            conn.timeout = QUERY_TIMEOUT_SECONDS
            cursor = conn.cursor()
            result = run_fn(cursor)
            elapsed = time.time() - t0
            log.info(f"[{cfg.key}] {label} completed in {elapsed:.2f}s (attempt {attempt})")
            return result
        except pyodbc.Error as e:
            last_err = e
            log.warning(f"[{cfg.key}] {label} attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if not is_transient_error(e):
                log.error(
                    f"[{cfg.key}] non-transient error (permission/syntax/object-not-found) "
                    f"-- not retrying. Check that the login has SELECT on files/documents/"
                    f"extractionDetails and CREATE TABLE permission in tempdb for this database."
                )
                break
            if attempt < MAX_RETRIES:
                sleep_for = RETRY_BACKOFF_SECONDS * attempt
                log.info(f"[{cfg.key}] retrying in {sleep_for}s "
                         f"(transient: blocking lock or timeout, not a script bug)")
                time.sleep(sleep_for)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    log.error(f"[{cfg.key}] {label}: gave up after {MAX_RETRIES} attempts: {last_err}")
    return None


def run_status_query(cfg: cs.DbConfig, start_date: str, end_date: str) -> dict:
    """Executes the optimized 'Processing Status' pivot query. Returns a
    dict of {field_name: count}, or {} on failure."""

    def _run(cursor):
        sql = queries.PROCESSING_STATUS_SQL.format(lock_timeout_ms=LOCK_TIMEOUT_MS)
        cursor.execute(sql, start_date, end_date)

        row1 = cursor.fetchone()
        cols1 = [c[0] for c in cursor.description]
        result = dict(zip(cols1, row1)) if row1 else {}

        if cursor.nextset():
            row2 = cursor.fetchone()
            cols2 = [c[0] for c in cursor.description]
            result.update(dict(zip(cols2, row2)) if row2 else {})
        return result

    result = _run_with_retry(cfg, "processing-status query", _run)
    return result if result is not None else {}


def run_daily_status_query(cfg: cs.DbConfig, start_date: str, end_date: str) -> list:
    """Executes the day-wise execution summary query (PROD). Returns a
    list of row-dicts ordered by date, or [] on failure."""

    def _run(cursor):
        sql = queries.DAILY_STATUS_SQL.format(lock_timeout_ms=LOCK_TIMEOUT_MS)
        cursor.execute(sql, start_date, end_date)
        cols = [c[0] for c in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    result = _run_with_retry(cfg, "daily-status query", _run)
    return result if result is not None else []


def run_storage_file_paths(cfg: cs.DbConfig, start_date: str, end_date: str) -> dict:
    """Fetches the file paths used to compute storage sizes. Returns:
        {
          'input':    {'rows': n, 'paths': [...]},
          'notfound': {'rows': n, 'paths': [...]},
          'found':    {'rows': n, 'paths': [...]},
        }
    'rows' is the number of DB rows (files); 'paths' is every non-empty path
    cell across those rows (a not-found/found row contributes up to 3 paths).
    The caller stats each path on disk to sum sizes."""

    def _make_run(sql):
        def _run(cursor):
            cursor.execute(sql.format(lock_timeout_ms=LOCK_TIMEOUT_MS), start_date, end_date)
            paths, rows = [], 0
            for row in cursor.fetchall():
                rows += 1
                for cell in row:
                    if cell and str(cell).strip():
                        paths.append(str(cell).strip())
            return {"rows": rows, "paths": paths}
        return _run

    result = {}
    for key, sql in (
        ("input", queries.STORAGE_INPUT_PATHS_SQL),
        ("notfound", queries.STORAGE_NOTFOUND_PATHS_SQL),
        ("found", queries.STORAGE_FOUND_PATHS_SQL),
    ):
        r = _run_with_retry(cfg, f"storage-{key}-paths query", _make_run(sql))
        result[key] = r if r is not None else {"rows": 0, "paths": []}
    return result
