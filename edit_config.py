#!/usr/bin/env python3
"""
edit_config.py

Console tool to view and edit the configuration used by status_report.py,
stored in MSSQL on the Prod SQL instance:
  - dbo.report_config      one row: mail settings, recipients, triggers,
                           optional global default date range
  - dbo.report_databases   one row PER database, each with its OWN date range

Connection details for the config instance come from environment variables
(a .env file in the project root is loaded automatically -- see .env.example):

    CONFIG_DB_SERVER  CONFIG_DB_NAME  CONFIG_DB_USER  CONFIG_DB_PWD
    (optional) CONFIG_DB_DRIVER

Usage:
    python edit_config.py show                       # print all config
    python edit_config.py edit                       # guided edit of mail/global settings
    python edit_config.py mailpwd                    # rotate just the mail password

    python edit_config.py db-list                    # list databases + their ranges
    python edit_config.py db-set  <name> <start> <end>   # set a DB's date range
                                                         # (use '-' for a date to clear it)
    python edit_config.py db-add  <name> [start] [end]   # add a database
    python edit_config.py db-prod <name>             # mark this DB as PROD
    python edit_config.py db-remove <name>           # remove a database
"""

import getpass
import sys

from app import config_store as cs


def _mask(secret: str) -> str:
    if not secret:
        return "(not set)"
    return f"{'*' * max(len(secret) - 2, 0)}{secret[-2:]}" if len(secret) > 2 else "**"


def _print_db_table():
    entries = cs.load_db_entries()
    if not entries:
        print("  (no databases configured -- add one with: python edit_config.py db-add <name>)")
        return
    print(f"  {'DATABASE':<20}{'START':<13}{'END (excl)':<13}{'PROD':<6}")
    print(f"  {'-' * 18:<20}{'-' * 11:<13}{'-' * 11:<13}{'-' * 4:<6}")
    for e in entries:
        print(f"  {e.name:<20}{(e.start_date or '(default)'):<13}"
              f"{(e.end_date or '(default)'):<13}{'YES' if e.is_prod else '':<6}")


def cmd_show():
    cfg = cs.load_app_config()
    print("Databases (dbo.report_databases) -- each with its own date range")
    print("-" * 64)
    _print_db_table()
    print()
    print("Global / mail settings (dbo.report_config, id=1)")
    print("-" * 64)
    print(f"default start_date : {cfg.start_date or '(none -> current month to date)'}")
    print(f"default end_date   : {cfg.end_date or '(none -> current month to date)'}")
    print(f"  (used only for databases whose own range is unset)")
    print(f"report_server      : {cfg.report_server or '(use CONFIG_DB_SERVER)'}")
    print(f"report_user        : {cfg.report_user or '(use CONFIG_DB_USER)'}")
    print(f"report_pwd         : {_mask(cfg.report_pwd) if cfg.report_pwd else '(use CONFIG_DB_PWD)'}")
    print(f"from_mail          : {cfg.from_mail or '(none)'}")
    print(f"from_name          : {cfg.from_name or '(none)'}")
    print(f"mail_pwd           : {_mask(cfg.mail_password)}  (stored base64)")
    print(f"to_mails           : {cfg.to_mails or '(none)'}")
    print(f"cc_mails           : {cfg.cc_mails or '(none)'}")
    print(f"smtp_server        : {cfg.smtp_server}")
    print(f"smtp_port          : {cfg.smtp_port}")
    print(f"triggers           : {', '.join(cfg.triggers) or '(none)'}")
    print(f"last_run           : {cfg.last_run_marker or '(never)'}")


def _ask(label: str, current) -> str:
    shown = current if current not in (None, "") else "(empty)"
    val = input(f"{label} [{shown}]: ").strip()
    return val  # empty string means "keep current"


def cmd_edit():
    """Guided edit of the mail/global settings. Per-DB date ranges are managed
    with the db-* commands (each database has its own range)."""
    cfg = cs.load_app_config()
    print("Guided edit -- press Enter to keep the current value.")
    print("(To set a database's date range, use: python edit_config.py db-set <name> <start> <end>)\n")

    updates = {}

    v = _ask("Default start date (YYYY-MM-DD, '-' to clear)", cfg.start_date)
    if v:
        updates["start_date"] = None if v == "-" else v
    v = _ask("Default end date (YYYY-MM-DD, exclusive, '-' to clear)", cfg.end_date)
    if v:
        updates["end_date"] = None if v == "-" else v

    v = _ask("Reporting server override ('-' to clear, blank to keep)", cfg.report_server)
    if v:
        updates["report_server"] = None if v == "-" else v
    v = _ask("Reporting login override ('-' to clear)", cfg.report_user)
    if v:
        updates["report_user"] = None if v == "-" else v

    v = _ask("From mail id", cfg.from_mail)
    if v:
        updates["from_mail"] = v
    v = _ask("From display name", cfg.from_name)
    if v:
        updates["from_name"] = v
    v = _ask("To recipients (semicolon-separated)", cfg.to_mails)
    if v:
        updates["to_mails"] = v
    v = _ask("Cc recipients (semicolon-separated, '-' to clear)", cfg.cc_mails)
    if v:
        updates["cc_mails"] = "" if v == "-" else v
    v = _ask("SMTP server", cfg.smtp_server)
    if v:
        updates["smtp_server"] = v
    v = _ask("SMTP port", cfg.smtp_port)
    if v:
        if v.isdigit():
            updates["smtp_port"] = int(v)
        else:
            print("  Ignoring non-numeric SMTP port.")
    v = _ask("Trigger times (comma-separated HH:MM, e.g. 09:30,13:30,18:30)",
             ",".join(cfg.triggers))
    if v:
        updates["triggers"] = ",".join(p.strip() for p in v.split(",") if p.strip())

    for column, value in updates.items():
        cs.set_config_field(column, value)

    # Passwords handled separately (base64-encoded, never echoed).
    new_mail_pwd = getpass.getpass("Mail password (Enter to keep current): ")
    if new_mail_pwd:
        cs.set_mail_password(new_mail_pwd)
        updates["mail_pwd_b64"] = "(updated)"
    new_report_pwd = getpass.getpass("Reporting-DB password override (Enter to keep, '-' to clear): ")
    if new_report_pwd == "-":
        cs.set_config_field("report_pwd_b64", "")
        updates["report_pwd_b64"] = "(cleared)"
    elif new_report_pwd:
        cs.set_report_password(new_report_pwd)
        updates["report_pwd_b64"] = "(updated)"

    if updates:
        print(f"\nUpdated: {', '.join(updates.keys())}")
    else:
        print("\nNo changes made.")


def cmd_mailpwd():
    pwd = getpass.getpass("New mail password: ")
    if not pwd:
        print("Empty -- no change.")
        return
    cs.set_mail_password(pwd)
    print("Mail password updated (stored base64).")


# --------------------------------------------------------------------- #
# Per-database commands
# --------------------------------------------------------------------- #
def _date_arg(value: str):
    """'-' clears the date (-> None); otherwise return the string as-is."""
    return None if value == "-" else value


def cmd_db_list():
    print("Databases (dbo.report_databases):")
    _print_db_table()


def cmd_db_set(argv):
    if len(argv) < 3:
        name = input("Database name: ").strip()
        start = input("Start date (YYYY-MM-DD, '-' to clear): ").strip()
        end = input("End date (YYYY-MM-DD, exclusive, '-' to clear): ").strip()
    else:
        name, start, end = argv[0], argv[1], argv[2]
    cs.set_db_range(name, _date_arg(start), _date_arg(end))
    print(f"Set range for '{name}': {start} -> {end}")


def cmd_db_add(argv):
    name = argv[0] if argv else input("Database name: ").strip()
    start = argv[1] if len(argv) > 1 else input("Start date (YYYY-MM-DD, blank = default): ").strip()
    end = argv[2] if len(argv) > 2 else input("End date (YYYY-MM-DD, exclusive, blank = default): ").strip()
    # Place new DB after existing ones.
    existing = cs.load_db_entries()
    sort_order = (max((e.sort_order for e in existing), default=-1)) + 1
    cs.upsert_db_entry(name, _date_arg(start) if start else None,
                       _date_arg(end) if end else None,
                       is_prod=False, enabled=True, sort_order=sort_order)
    print(f"Added '{name}'.")


def cmd_db_prod(argv):
    if not argv:
        print("Usage: python edit_config.py db-prod <name>")
        sys.exit(1)
    cs.set_prod_db(argv[0])
    print(f"'{argv[0]}' is now the PROD database (day-wise summary).")


def cmd_db_remove(argv):
    if not argv:
        print("Usage: python edit_config.py db-remove <name>")
        sys.exit(1)
    name = argv[0]
    if input(f"Remove database '{name}' from config? (y/n): ").strip().lower() == "y":
        cs.delete_db_entry(name)
        print("Removed.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    rest = sys.argv[2:]
    try:
        if cmd == "show":
            cmd_show()
        elif cmd == "edit":
            cmd_edit()
        elif cmd == "mailpwd":
            cmd_mailpwd()
        elif cmd == "db-list":
            cmd_db_list()
        elif cmd == "db-set":
            cmd_db_set(rest)
        elif cmd == "db-add":
            cmd_db_add(rest)
        elif cmd == "db-prod":
            cmd_db_prod(rest)
        elif cmd == "db-remove":
            cmd_db_remove(rest)
        else:
            print(__doc__)
            sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
