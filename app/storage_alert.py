#!/usr/bin/env python3
"""
storage_alert.py

A SEPARATE alert email (never combined with the processing-status report):
server storage breakdown + CPU utilization + pipeline file sizes.

Threshold-driven: when the configured mount (default /data) crosses
STORAGE_THRESHOLD_PCT, it emails a newsletter-style alert, then RE-ALERTS at
most once every STORAGE_REMINDER_HOURS while still over the threshold (a small
JSON state file de-dups reminders across cron runs). `--force` sends it
immediately regardless of the threshold (handy for testing).

Sizes (Input File Size / Aadhaar not found / Aadhaar found) are computed by
taking the file paths from the reporting database (files.file_path and
extractionDetails.{extractedFilePath,pickleInputPath,pickleOutputPath}) over
the PROD DB's configured date range and summing each file's on-disk size.

Recipients come from the SAME report_config table as the processing-status
report (to_mails / cc_mails) -- both support multiple addresses.

Threshold/mount/reminder settings come from environment / the .env file:
    STORAGE_ALERT_ENABLED    1/0 (default 1)
    STORAGE_MOUNT            mount to watch (default /data)
    STORAGE_THRESHOLD_PCT    used %% that triggers the alert (default 80)
    STORAGE_REMINDER_HOURS   re-alert interval while over (default 2)
    STORAGE_STATE_FILE       reminder state file (default <root>/.psr_storage_state.json)
"""

import json
import logging
import os
import shutil
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from . import config_store as cs
from . import db
from . import report
from .mailer import parse_addr_list, send_report_email

log = logging.getLogger("processing_status_report")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GB = 1024 ** 3

try:
    import psutil  # optional but recommended
except ImportError:
    psutil = None


# ------------------------------------------------------------------ #
# Config (from .env / environment)
# ------------------------------------------------------------------ #
@dataclass
class StorageConfig:
    enabled: bool
    mount: str
    threshold_pct: float
    reminder_hours: float
    state_file: str

    @staticmethod
    def from_env() -> "StorageConfig":
        cs.load_dotenv()  # ensure .env is applied before reading os.environ
        g = os.environ.get
        return StorageConfig(
            enabled=g("STORAGE_ALERT_ENABLED", "1").strip() not in ("0", "false", "False", ""),
            mount=g("STORAGE_MOUNT", "/data"),
            threshold_pct=float(g("STORAGE_THRESHOLD_PCT", "80") or 80),
            reminder_hours=float(g("STORAGE_REMINDER_HOURS", "2") or 2),
            state_file=g("STORAGE_STATE_FILE", "").strip()
            or os.path.join(_PROJECT_ROOT, ".psr_storage_state.json"),
        )


# ------------------------------------------------------------------ #
# System metrics
# ------------------------------------------------------------------ #
def _local_ip() -> str:
    """Best-effort primary IP (the outbound interface address)."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packets sent; just picks the route
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "N/A"
    finally:
        if s is not None:
            s.close()


def _disk_usage(mount: str) -> Tuple[float, float, float, float]:
    """(total_gb, used_gb, free_gb, used_pct) for the mount."""
    if psutil is not None:
        u = psutil.disk_usage(mount)
        total, used, free = u.total, u.used, u.free
    else:
        u = shutil.disk_usage(mount)
        total, used, free = u.total, u.used, u.free
    used_pct = (used / total * 100.0) if total else 0.0
    return total / _GB, used / _GB, free / _GB, used_pct


def _cpu_metrics():
    """(overall_pct, per_core_list_or_None, logical_cores, physical_cores)."""
    if psutil is not None:
        per_core = psutil.cpu_percent(interval=1, percpu=True)
        overall = round(sum(per_core) / len(per_core), 1) if per_core else 0.0
        return overall, [round(p, 1) for p in per_core], \
            psutil.cpu_count(logical=True), psutil.cpu_count(logical=False)
    return _cpu_from_proc(), None, os.cpu_count(), None


def _read_proc_stat_total():
    with open("/proc/stat", "r") as f:
        parts = f.readline().split()[1:]
    vals = [int(x) for x in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)


def _cpu_from_proc() -> float:
    try:
        idle1, total1 = _read_proc_stat_total()
        time.sleep(1)
        idle2, total2 = _read_proc_stat_total()
        dt, di = total2 - total1, idle2 - idle1
        return round((1 - di / dt) * 100.0, 1) if dt else 0.0
    except (OSError, ValueError, ZeroDivisionError):
        return 0.0


def _load_avg():
    try:
        return os.getloadavg()
    except (OSError, AttributeError):
        return None


def collect_metrics(cfg: StorageConfig) -> dict:
    total_gb, used_gb, free_gb, used_pct = _disk_usage(cfg.mount)
    overall, per_core, logical, physical = _cpu_metrics()
    return {
        "hostname": socket.gethostname(),
        "ip": _local_ip(),
        "mount": cfg.mount,
        "total_gb": total_gb,
        "occupied_gb": used_gb,
        "free_gb": free_gb,
        "used_pct": used_pct,
        "cpu_overall_pct": overall,
        "per_core": per_core,
        "load_avg": _load_avg(),
        "logical_cores": logical,
        "physical_cores": physical,
        "psutil_available": psutil is not None,
    }


# ------------------------------------------------------------------ #
# File-size computation (sum on-disk size of DB-referenced paths)
# ------------------------------------------------------------------ #
def _sum_path_sizes(paths: List[str]) -> Tuple[float, int]:
    """Return (total_gb, missing_count) for a list of file paths."""
    total = 0
    missing = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            missing += 1
    return total / _GB, missing


def _compute_sizes(app_cfg):
    """Run the path queries on the PROD DB and sum on-disk sizes.
    Returns (sizes_dict, range_start, range_end). sizes_dict entries are
    {'gb': float, 'files': int, 'missing': int}; empty dict if unavailable."""
    db_configs = cs.load_db_configs(app_cfg)
    prod_key = cs.get_prod_db_key(db_configs)
    if not prod_key:
        log.warning("[storage] no PROD database flagged -- skipping file sizes.")
        return {}, None, None

    entries = {e.name: e for e in cs.load_db_entries()}
    start, end = cs.resolve_date_range(entries.get(prod_key), app_cfg)
    data = db.run_storage_file_paths(db_configs[prod_key], start, end)

    sizes = {}
    total_missing = 0
    for key in ("input", "notfound", "found", "extracted"):
        gb, missing = _sum_path_sizes(data[key]["paths"])
        sizes[key] = {"gb": gb, "files": data[key]["rows"], "missing": missing}
        total_missing += missing
    if total_missing:
        log.warning(f"[storage] {total_missing} DB-referenced file(s) not found on "
                    f"disk and skipped in size totals.")
    return sizes, start, end


# ------------------------------------------------------------------ #
# Reminder state (JSON file; de-dups reminders across cron runs)
# ------------------------------------------------------------------ #
def _read_last_alert(cfg: StorageConfig) -> Optional[datetime]:
    try:
        with open(cfg.state_file, "r", encoding="utf-8") as f:
            ts = json.load(f).get("last_alert_iso")
        return datetime.fromisoformat(ts) if ts else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _write_last_alert(cfg: StorageConfig, when: Optional[datetime]):
    try:
        with open(cfg.state_file, "w", encoding="utf-8") as f:
            json.dump({"last_alert_iso": when.isoformat() if when else None}, f)
    except OSError as e:
        log.warning(f"[storage] could not write state file {cfg.state_file}: {e}")


def _should_send(cfg: StorageConfig, used_pct: float, force: bool) -> Tuple[bool, str]:
    if force:
        return True, "forced"
    if used_pct < cfg.threshold_pct:
        _write_last_alert(cfg, None)  # recovered -> next breach alerts immediately
        return False, f"under threshold ({used_pct:.1f}% < {cfg.threshold_pct:.0f}%)"
    last = _read_last_alert(cfg)
    if last is None:
        return True, "first breach"
    elapsed = datetime.now() - last
    if elapsed >= timedelta(hours=cfg.reminder_hours):
        return True, f"reminder ({elapsed} since last alert)"
    return False, f"within reminder window (last alert {elapsed} ago)"


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #
def run_storage_alert(force: bool = False) -> bool:
    """Collect metrics, decide whether to alert, and send the storage email.
    Returns True on success or a legitimate no-op; False on a real failure."""
    cfg = StorageConfig.from_env()

    if not cfg.enabled and not force:
        log.info("[storage] STORAGE_ALERT_ENABLED=0 -- skipping storage alert.")
        return True

    # Cheap check first: disk usage decides whether to do the expensive work.
    total_gb, used_gb, free_gb, used_pct = _disk_usage(cfg.mount)
    log.info(f"[storage] {cfg.mount} at {used_pct:.1f}% ({used_gb:.1f}/{total_gb:.1f} GB)")

    send, reason = _should_send(cfg, used_pct, force)
    if not send:
        log.info(f"[storage] not sending -- {reason}.")
        return True
    log.info(f"[storage] sending alert -- {reason}.")

    # Recipients come from the report_config table (shared with the status
    # report). Both to_mails and cc_mails allow multiple ids (',' or ';').
    app_cfg = cs.load_app_config()
    mail_cfg = cs.load_mail_config(app_cfg)
    to_list = parse_addr_list(mail_cfg.default_to)
    cc_list = [a for a in parse_addr_list(mail_cfg.default_cc) if a not in to_list]
    if not to_list:
        log.error("[storage] report_config.to_mails is empty -- cannot send storage alert. "
                  "Set it with: python edit_config.py edit")
        return False

    metrics = collect_metrics(cfg)
    sizes, start, end = _compute_sizes(app_cfg)

    over = used_pct >= cfg.threshold_pct
    html = report.build_storage_email_html(metrics, sizes, start, end,
                                           cfg.threshold_pct, over)

    date_str = datetime.now().strftime("%d-%b-%Y %H:%M")
    if over:
        subject = f"[STORAGE ALERT] {cfg.mount} at {used_pct:.0f}% on {metrics['hostname']} - {date_str}"
    else:
        subject = f"Storage & System Report - {metrics['hostname']} - {date_str}"

    log.info(f"[storage] emailing {', '.join(to_list)}"
             + (f" | Cc: {', '.join(cc_list)}" if cc_list else ""))
    try:
        send_report_email(mail_cfg, html, subject, to_list, cc_list)
        _write_last_alert(cfg, datetime.now())
        log.info("[storage] alert email sent.")
        return True
    except Exception as e:
        log.error(f"[storage] failed to send alert email: {e}")
        return False
