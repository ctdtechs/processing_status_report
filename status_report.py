#!/usr/bin/env python3
"""
status_report.py

Interactive console tool that:
  1. Lets the user pick one or more databases (abhi_mask = PROD,
     abhi_maskv2..v6 = historical) -- config now comes from the local
     encrypted config store (config_store.py), not hardcoded credentials.
  2. Lets the user enter an upload-date range per database and runs the
     optimized "Processing Status" pivot query (Stage x Database).
  3. Automatically runs the "Day-wise Execution Summary" query against
     the PROD database for the current month to date (matching the
     reference report's second table) -- no separate prompting needed.
  4. Renders both as console tables and, optionally, emails them
     together as a single HTML report matching the reference format.

-------------------------------------------------------------------------
WHY THE ORIGINAL PIVOT QUERY WAS SLOW / SOMETIMES HUNG
-------------------------------------------------------------------------
The original query used 9 separate SELECT...UNION ALL branches, each
re-running the SAME correlated subquery against `files`/`documents`.
This script's PROCESSING_STATUS_SQL (see queries.py) fixes that by
loading `files`/`documents` ONCE into indexed temp tables for the date
range, then deriving every stage count from those -- single scan per
source table, single join to extractionDetails -- plus an explicit
LOCK_TIMEOUT and pyodbc query timeout so a blocked query fails fast and
retries with backoff instead of hanging forever.

The stage DEFINITIONS are unchanged from the original -- only the
execution plan is optimized. The "Day-wise Execution Summary" query is
the one supplied for the daily PROD report, kept logically identical
(same stage definitions) and just parameterized + given the same
LOCK_TIMEOUT / READ UNCOMMITTED treatment for consistency.

-------------------------------------------------------------------------
RECOMMENDED INDEXES (ask your DBA to add these if not present -- they
matter more than anything in this script for actually fixing slowness):
-------------------------------------------------------------------------
    CREATE NONCLUSTERED INDEX IX_files_uploaded_at
        ON dbo.files (uploaded_at) INCLUDE (id, processing_status, Upload_Status);

    CREATE NONCLUSTERED INDEX IX_documents_uploaddate
        ON dbo.documents (UploadDate) INCLUDE (DownloadStatus);

    CREATE NONCLUSTERED INDEX IX_extractionDetails_fileId
        ON dbo.extractionDetails (fileId)
        INCLUDE (identificationStatus, maskingStatus, processingStatus, outputFilePrepration);
-------------------------------------------------------------------------

-------------------------------------------------------------------------
CONFIGURATION
-------------------------------------------------------------------------
Databases, date range, mail settings, recipients and trigger times all
live in ONE row of the MSSQL table dbo.report_config on the Prod SQL
instance (see config_store.py). Bootstrap connection details for that
instance come from environment variables:

    CONFIG_DB_SERVER  CONFIG_DB_NAME  CONFIG_DB_USER  CONFIG_DB_PWD
    (optional) CONFIG_DB_DRIVER

The table is auto-created/seeded on first connect. View/edit it with:

    python edit_config.py show
    python edit_config.py edit
    python edit_config.py mailpwd

RUN MODES
-------------------------------------------------------------------------
    python status_report.py            # interactive (manual, ad-hoc)
    python status_report.py --auto     # config-driven; runs only if a
                                       # configured trigger time is due now
    python status_report.py --force    # config-driven; runs immediately

    # --auto / --force run BOTH emails by default. Restrict with:
    python status_report.py --auto --status     # processing-status email only
    python status_report.py --auto --storage    # storage/system alert only
        (the storage alert is threshold-driven: it only sends when the
         configured mount crosses STORAGE_THRESHOLD_PCT, re-alerting at most
         once every STORAGE_REMINDER_HOURS -- see app/storage_alert.py)

Requirements:
    pip install pyodbc psutil
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta

from app import config_store as cs
from app import db
from app import report
from app.report import C
from app.mailer import parse_addr_list, send_report_email

# Minutes after a configured trigger time within which --auto will still
# send. Lets an external scheduler (cron) poll every few minutes without
# needing to hit the exact HH:MM. Keep this >= the cron interval so a
# trigger is never skipped between two runs.
DEFAULT_TRIGGER_GRACE_MIN = 15

LOG_FILE = "status_report.log"

console_handler = logging.StreamHandler(sys.stdout)


class ColorConsoleFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.INFO: C.GREEN,
        logging.WARNING: C.YELLOW,
        logging.ERROR: C.RED,
        logging.CRITICAL: C.RED + C.BOLD,
    }

    def format(self, record):
        color = self.LEVEL_COLORS.get(record.levelno, "")
        base = super().format(record)
        return f"{color}{base}{C.RESET}"


console_handler.setFormatter(ColorConsoleFormatter("%(asctime)s [%(levelname)s] %(message)s"))

file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[console_handler, file_handler])
log = logging.getLogger("processing_status_report")


# ------------------------------------------------------------------------- #
# Console interaction -- database + date-range selection (pivot table)
# ------------------------------------------------------------------------- #
def prompt_database_selection(db_configs: dict) -> list:
    print(f"\n{C.CYAN}{C.BOLD}Available databases:{C.RESET}")
    keys = list(db_configs.keys())
    for i, k in enumerate(keys, start=1):
        print(f"  {C.GREEN}{i}.{C.RESET} {db_configs[k].label}")
    print(f"  {C.GREEN}{len(keys) + 1}.{C.RESET} ALL")

    raw = input(f"\n{C.CYAN}Select database(s) by number (comma-separated, or 'all'): {C.RESET}").strip().lower()
    if raw in ("all", str(len(keys) + 1)):
        return keys

    selected = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or not (1 <= int(part) <= len(keys)):
            print(f"{C.YELLOW}Ignoring invalid selection: '{part}'{C.RESET}")
            continue
        selected.append(keys[int(part) - 1])

    if not selected:
        print(f"{C.RED}No valid database selected. Exiting.{C.RESET}")
        sys.exit(1)
    return selected


def prompt_date(label: str) -> str:
    while True:
        raw = input(f"  {label} (YYYY-MM-DD): ").strip()
        try:
            datetime.strptime(raw, "%Y-%m-%d")
            return raw
        except ValueError:
            print("  Invalid date format, try again (e.g. 2026-05-01).")


def prompt_date_range_for(db_configs: dict, db_key: str) -> tuple:
    print(f"\nUpload date range for {db_configs[db_key].label}:")
    start_date = prompt_date("Start date (inclusive)")
    end_date = prompt_date("End date (exclusive)")
    return start_date, end_date


# ------------------------------------------------------------------------- #
# Day-wise PROD summary -- automatic range: 1st of current month -> today
# (inclusive), matching the reference report (generated mid-month, shows
# every day from the 1st through the generation date).
# ------------------------------------------------------------------------- #
def current_month_to_date_range() -> tuple:
    today = datetime.now().date()
    month_start = today.replace(day=1)
    end_exclusive = today + timedelta(days=1)
    return month_start.strftime("%Y-%m-%d"), end_exclusive.strftime("%Y-%m-%d")


def run_daily_prod_summary(db_configs: dict):
    prod_key = cs.get_prod_db_key(db_configs)
    if not prod_key:
        log.warning("No database is flagged is_prod=True in config -- skipping "
                     "Day-wise Execution Summary. Set one with: "
                     "python edit_config.py db-prod <name>")
        return [], ""

    start_date, end_date = current_month_to_date_range()
    period_label = report.compute_period_label(start_date, end_date)
    print(f"\n{C.YELLOW}Running Day-wise Execution Summary on {db_configs[prod_key].label} "
          f"[{start_date} -> {end_date}] ...{C.RESET}")
    daily_rows = db.run_daily_status_query(db_configs[prod_key], start_date, end_date)
    return daily_rows, period_label


# ------------------------------------------------------------------------- #
# Email
# ------------------------------------------------------------------------- #
def prompt_send_email(selected_dbs, db_configs, results, date_ranges, daily_rows, daily_period_label):
    choice = input(
        f"\n{C.CYAN}Do you want to send this report via email? (y/n): {C.RESET}"
    ).strip().lower()
    if choice != "y":
        return

    mail_cfg = cs.load_mail_config()
    default_to = parse_addr_list(mail_cfg.default_to)
    default_cc = parse_addr_list(mail_cfg.default_cc)

    print(f"{C.GRAY}Default To: {'; '.join(default_to) or '(none)'}{C.RESET}")
    print(f"{C.GRAY}Default Cc: {'; '.join(default_cc) or '(none)'}{C.RESET}")

    extra_to_raw = input(
        f"{C.CYAN}Additional To recipients, semicolon-separated (Enter to skip): {C.RESET}"
    ).strip()
    extra_cc_raw = input(
        f"{C.CYAN}Additional Cc recipients, semicolon-separated (Enter to skip): {C.RESET}"
    ).strip()

    to_list = default_to + parse_addr_list(extra_to_raw)
    cc_list = default_cc + parse_addr_list(extra_cc_raw)

    to_list = list(dict.fromkeys(to_list))
    cc_list = [addr for addr in dict.fromkeys(cc_list) if addr not in to_list]

    if not to_list:
        print(f"{C.RED}No To recipients configured or entered -- not sending.{C.RESET}")
        return

    subject = f"Processing Status Report - {datetime.now().strftime('%d-%b-%Y')}"
    html_body = report.build_email_html(
        selected_dbs, db_configs, results, date_ranges, daily_rows, daily_period_label
    )

    print(f"{C.YELLOW}Sending email to: {', '.join(to_list)}"
          f"{' | Cc: ' + ', '.join(cc_list) if cc_list else ''} ...{C.RESET}")
    try:
        send_report_email(mail_cfg, html_body, subject, to_list, cc_list)
        print(f"{C.GREEN}Email sent successfully.{C.RESET}")
    except Exception as e:
        _report_send_error(e)


def _report_send_error(e: Exception):
    import smtplib
    if isinstance(e, smtplib.SMTPAuthenticationError):
        print(f"{C.RED}Mail auth failed: {e}{C.RESET}")
        print(f"{C.GRAY}-> Check mail settings via 'python3 edit_config.py mail'. "
              f"If MFA is enabled, use an App Password.{C.RESET}")
    elif isinstance(e, smtplib.SMTPException):
        print(f"{C.RED}SMTP error: {e}{C.RESET}")
    else:
        print(f"{C.RED}Unexpected error sending mail: {e}{C.RESET}")


# ------------------------------------------------------------------------- #
# Trigger evaluation (--auto). The trigger TIMES live in the config table
# (triggers column), NOT in cron -- change them there any time. cron just
# runs this checker every few minutes; a report is sent when a configured
# time has arrived and hasn't been sent yet today.
#
# A trigger is "due" when now is AT or just past its HH:MM -- specifically
# within [trigger_time, trigger_time + grace_min] -- and it hasn't already
# fired today (tracked via last_run_marker so a polling scheduler won't
# send duplicates). Fire at/just-after (never early), which is what you'd
# expect from "send at 09:30". Keep grace_min >= your cron interval so a
# trigger is never skipped between two cron runs.
# ------------------------------------------------------------------------- #
def _parse_hhmm(raw: str):
    try:
        h, m = raw.strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (ValueError, AttributeError):
        pass
    return None


def select_due_trigger(app_cfg: cs.AppConfig, now: datetime, grace_min: int):
    """Return (trigger_str, marker) for a trigger due right now, or (None, None)."""
    now_minutes = now.hour * 60 + now.minute
    today = now.strftime("%Y-%m-%d")

    best = None  # (minutes_since_trigger, trigger_str) -- prefer most recent
    for raw in app_cfg.triggers:
        parsed = _parse_hhmm(raw)
        if parsed is None:
            log.warning(f"Ignoring malformed trigger time in config: '{raw}'")
            continue
        h, m = parsed
        since = now_minutes - (h * 60 + m)          # >=0 means the time has passed today
        if 0 <= since <= grace_min and (best is None or since < best[0]):
            best = (since, f"{h:02d}:{m:02d}")

    if best is None:
        return None, None

    trigger_str = best[1]
    marker = f"{today} {trigger_str}"
    if app_cfg.last_run_marker == marker:
        log.info(f"Trigger {trigger_str} already sent today ({marker}) -- skipping.")
        return None, None
    return trigger_str, marker


# ------------------------------------------------------------------------- #
# Config-driven (non-interactive) run -- used by --auto / --force. Reads
# databases, date range and recipients entirely from the config table; no
# prompts; emails the configured To/Cc automatically.
# ------------------------------------------------------------------------- #
def run_config_driven(app_cfg: cs.AppConfig, db_configs: dict):
    if not db_configs:
        log.error("No enabled databases configured -- nothing to run. "
                  "Add one with: python edit_config.py db-add")
        return False

    # Per-DB date range: each DB uses its own range from dbo.report_databases.
    # Each bound resolves independently -- a blank end_date rolls to today,
    # a blank start_date falls back to the global default / 1st of the month.
    entries = {e.name: e for e in cs.load_db_entries()}

    selected_dbs = list(db_configs.keys())
    date_ranges = {k: cs.resolve_date_range(entries.get(k), app_cfg) for k in selected_dbs}

    results = {}
    for k in selected_dbs:
        start_date, end_date = date_ranges[k]
        log.info(f"Running query on {db_configs[k].label} [{start_date} -> {end_date}] ...")
        results[k] = db.run_status_query(db_configs[k], start_date, end_date)

    failed_dbs = [k for k in selected_dbs if not results.get(k)]
    title = "Processing Status" if not failed_dbs else "Processing Status (partial -- some DBs failed)"
    report.print_status_report(
        selected_dbs, db_configs, results, date_ranges,
        f"{title} ({datetime.now().strftime('%d-%b-%Y')})",
    )
    if failed_dbs:
        log.warning("Failed to fetch results for: "
                    + ", ".join(db_configs[k].label for k in failed_dbs))

    daily_rows, daily_period_label = run_daily_prod_summary(db_configs)
    report.print_daily_report(
        daily_rows, f"Day-wise {daily_period_label} Execution Summary".strip()
    )

    return send_config_email(app_cfg, selected_dbs, db_configs, results,
                             date_ranges, daily_rows, daily_period_label)


def send_config_email(app_cfg, selected_dbs, db_configs, results,
                      date_ranges, daily_rows, daily_period_label) -> bool:
    mail_cfg = cs.load_mail_config(app_cfg)
    to_list = list(dict.fromkeys(parse_addr_list(mail_cfg.default_to)))
    cc_list = [a for a in dict.fromkeys(parse_addr_list(mail_cfg.default_cc)) if a not in to_list]

    if not to_list:
        log.error("No To recipients configured (to_mails) -- not sending email.")
        return False

    subject = f"Processing Status Report - {datetime.now().strftime('%d-%b-%Y')}"
    html_body = report.build_email_html(
        selected_dbs, db_configs, results, date_ranges, daily_rows, daily_period_label
    )
    log.info(f"Sending email to: {', '.join(to_list)}"
             + (f" | Cc: {', '.join(cc_list)}" if cc_list else ""))
    try:
        send_report_email(mail_cfg, html_body, subject, to_list, cc_list)
        log.info("Email sent successfully.")
        return True
    except Exception as e:
        log.error(f"Failed to send report email: {e}")
        return False


def run_auto(force: bool, grace_min: int) -> int:
    """Non-interactive entry point for the scheduler.

    --auto : run only if a configured trigger time has just arrived.
    --force: run immediately regardless of triggers (still config-driven).
    Returns a process exit code.
    """
    app_cfg = cs.load_app_config()

    # Honour the configured start/end as an overall active window: if today
    # is outside [start_date, end_date] (both set), don't run in --auto.
    if not force and app_cfg.start_date and app_cfg.end_date:
        today = datetime.now().strftime("%Y-%m-%d")
        if not (app_cfg.start_date <= today < app_cfg.end_date):
            log.info(f"Today ({today}) is outside the configured active range "
                     f"[{app_cfg.start_date} -> {app_cfg.end_date}) -- not running.")
            return 0

    marker = None
    if not force:
        trigger_str, marker = select_due_trigger(app_cfg, datetime.now(), grace_min)
        if trigger_str is None:
            log.info("No trigger due right now -- exiting without running.")
            return 0
        log.info(f"Trigger {trigger_str} is due -- running report.")

    db_configs = cs.load_db_configs(app_cfg)
    ok = run_config_driven(app_cfg, db_configs)

    # Only mark the trigger as sent once the email actually went out, so a
    # transient failure lets the next cron run retry within the grace window.
    if ok and marker:
        cs.mark_trigger_fired(marker)
    return 0 if ok else 1


# ------------------------------------------------------------------------- #
# Main
# ------------------------------------------------------------------------- #
def main_interactive():
    print(f"{C.BLUE}{C.BOLD}{'=' * 70}{C.RESET}")
    print(f"{C.BLUE}{C.BOLD} Processing Status Report{C.RESET}")
    print(f"{C.BLUE}{C.BOLD}{'=' * 70}{C.RESET}")

    db_configs = cs.load_db_configs()
    if not db_configs:
        print(f"{C.RED}No databases configured. Run: python edit_config.py db-add <name>{C.RESET}")
        sys.exit(1)

    selected_dbs = prompt_database_selection(db_configs)

    same_range = "y"
    if len(selected_dbs) > 1:
        same_range = input(
            f"\n{C.CYAN}Use the SAME upload-date range for all selected databases? (y/n): {C.RESET}"
        ).strip().lower() or "y"

    date_ranges = {}
    if same_range == "y":
        start_date, end_date = prompt_date_range_for(db_configs, selected_dbs[0])
        for k in selected_dbs:
            date_ranges[k] = (start_date, end_date)
    else:
        for k in selected_dbs:
            date_ranges[k] = prompt_date_range_for(db_configs, k)

    results = {}
    for k in selected_dbs:
        start_date, end_date = date_ranges[k]
        print(f"\n{C.YELLOW}Running query on {db_configs[k].label} "
              f"[{start_date} -> {end_date}] ...{C.RESET}")
        results[k] = db.run_status_query(db_configs[k], start_date, end_date)

    failed_dbs = [k for k in selected_dbs if not results.get(k)]

    title = "Processing Status" if not failed_dbs else "Processing Status (partial -- some DBs failed)"
    report.print_status_report(
        selected_dbs, db_configs, results, date_ranges,
        f"{title} ({datetime.now().strftime('%d-%b-%Y')})",
    )

    if failed_dbs:
        failed_labels = ", ".join(db_configs[k].label for k in failed_dbs)
        print(f"\n{C.RED}{C.BOLD}Failed to fetch results for: {failed_labels}{C.RESET}")
        print(f"{C.GRAY}(see {LOG_FILE} for the full error on each){C.RESET}")

        retry_choice = input(
            f"\n{C.CYAN}Retry just the failed database(s) once? (y/n): {C.RESET}"
        ).strip().lower()

        if retry_choice == "y":
            still_failed = []
            for k in failed_dbs:
                start_date, end_date = date_ranges[k]
                print(f"\n{C.YELLOW}Retrying {db_configs[k].label} "
                      f"[{start_date} -> {end_date}] ...{C.RESET}")
                new_result = db.run_status_query(db_configs[k], start_date, end_date)
                if new_result:
                    results[k] = new_result
                    print(f"{C.GREEN}[{k}] retry succeeded.{C.RESET}")
                else:
                    still_failed.append(k)
                    print(f"{C.RED}[{k}] retry failed again -- leaving as N/A.{C.RESET}")

            final_title = "Processing Status (final, after retry)"
            if still_failed:
                still_failed_labels = ", ".join(db_configs[k].label for k in still_failed)
                final_title += f" -- still failed: {still_failed_labels}"
            report.print_status_report(
                selected_dbs, db_configs, results, date_ranges,
                f"{final_title} ({datetime.now().strftime('%d-%b-%Y')})",
            )
        else:
            print(f"{C.GRAY}Skipping retry -- final result above includes N/A for failed DB(s).{C.RESET}")

    # Day-wise PROD execution summary -- automatic, no extra prompts,
    # matching the reference report's second table.
    daily_rows, daily_period_label = run_daily_prod_summary(db_configs)
    report.print_daily_report(
        daily_rows, f"Day-wise {daily_period_label} Execution Summary".strip()
    )

    prompt_send_email(selected_dbs, db_configs, results, date_ranges, daily_rows, daily_period_label)


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Processing Status Report -- interactive by default; "
                    "--auto/--force for scheduled, config-driven runs."
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--auto", action="store_true",
                      help="Config-driven run, but only if a configured trigger "
                           "time is due right now (for use from a scheduler).")
    mode.add_argument("--force", action="store_true",
                      help="Config-driven run immediately, ignoring trigger times.")
    p.add_argument("--grace", type=int, default=DEFAULT_TRIGGER_GRACE_MIN,
                   help=f"Minutes after a configured trigger time within which "
                        f"--auto will still send (set >= your cron interval; "
                        f"default {DEFAULT_TRIGGER_GRACE_MIN}).")
    # Which email(s) to run. If neither is given, BOTH run (each still gated
    # by its own rules -- status by triggers, storage by threshold).
    p.add_argument("--status", action="store_true",
                   help="Run only the processing-status report email.")
    p.add_argument("--storage", action="store_true",
                   help="Run only the storage & system alert email.")
    return p.parse_args(argv)


def main():
    args = parse_args(sys.argv[1:])
    if not (args.auto or args.force):
        main_interactive()
        return

    # Selectors: neither flag -> both; one flag -> just that one.
    want_status = args.status or not (args.status or args.storage)
    want_storage = args.storage or not (args.status or args.storage)

    rc = 0
    if want_status:
        try:
            rc |= run_auto(force=args.force, grace_min=args.grace)
        except Exception as e:
            log.error(f"Status report run failed: {e}")
            rc |= 1
    if want_storage:
        try:
            from app import storage_alert
            ok = storage_alert.run_storage_alert(force=args.force)
            rc |= 0 if ok else 1
        except Exception as e:
            log.error(f"Storage alert run failed: {e}")
            rc |= 1

    sys.exit(rc)


if __name__ == "__main__":
    main()
