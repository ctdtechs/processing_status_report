#!/usr/bin/env python3
"""
edit_config.py

Console tool to view and edit the single configuration row in the MSSQL
``dbo.report_config`` table used by status_report.py -- so changing the
date range, database list, mail settings, recipients or trigger times
doesn't require touching any code or hand-writing SQL.

The config table lives on the Prod SQL instance; connection details come
from environment variables (see app/config_store.py):

    CONFIG_DB_SERVER  CONFIG_DB_NAME  CONFIG_DB_USER  CONFIG_DB_PWD
    (optional) CONFIG_DB_DRIVER

Usage:
    python edit_config.py show      # print current config (passwords masked)
    python edit_config.py edit      # guided edit of every field
    python edit_config.py mailpwd   # rotate just the mail password
"""

import getpass
import sys

from app import config_store as cs


def _mask(secret: str) -> str:
    if not secret:
        return "(not set)"
    return f"{'*' * max(len(secret) - 2, 0)}{secret[-2:]}" if len(secret) > 2 else "**"


def cmd_show():
    cfg = cs.load_app_config()
    print("Current configuration (dbo.report_config, id=1)")
    print("-" * 60)
    print(f"start_date    : {cfg.start_date or '(not set -> current month to date)'}")
    print(f"end_date      : {cfg.end_date or '(not set -> current month to date)'}")
    print(f"db_list       : {', '.join(cfg.db_list) or '(none)'}")
    print(f"prod_db       : {cfg.prod_db or '(none)'}")
    print(f"report_server : {cfg.report_server or '(use CONFIG_DB_SERVER)'}")
    print(f"report_user   : {cfg.report_user or '(use CONFIG_DB_USER)'}")
    print(f"report_pwd    : {_mask(cfg.report_pwd) if cfg.report_pwd else '(use CONFIG_DB_PWD)'}")
    print(f"from_mail     : {cfg.from_mail or '(none)'}")
    print(f"from_name     : {cfg.from_name or '(none)'}")
    print(f"mail_pwd      : {_mask(cfg.mail_password)}  (stored base64)")
    print(f"to_mails      : {cfg.to_mails or '(none)'}")
    print(f"cc_mails      : {cfg.cc_mails or '(none)'}")
    print(f"smtp_server   : {cfg.smtp_server}")
    print(f"smtp_port     : {cfg.smtp_port}")
    print(f"triggers      : {', '.join(cfg.triggers) or '(none)'}")
    print(f"last_run      : {cfg.last_run_marker or '(never)'}")


def _ask(label: str, current) -> str:
    shown = current if current not in (None, "") else "(empty)"
    val = input(f"{label} [{shown}]: ").strip()
    return val  # empty string means "keep current"


def cmd_edit():
    cfg = cs.load_app_config()
    print("Guided edit -- press Enter to keep the current value.\n")

    updates = {}

    v = _ask("Start date (YYYY-MM-DD, '-' to clear)", cfg.start_date)
    if v:
        updates["start_date"] = None if v == "-" else v
    v = _ask("End date (YYYY-MM-DD, exclusive, '-' to clear)", cfg.end_date)
    if v:
        updates["end_date"] = None if v == "-" else v

    v = _ask("Database list (comma-separated)", ",".join(cfg.db_list))
    if v:
        updates["db_list"] = ",".join(p.strip() for p in v.split(",") if p.strip())
    v = _ask("PROD database (for day-wise summary)", cfg.prod_db)
    if v:
        updates["prod_db"] = v

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


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "show":
        cmd_show()
    elif cmd == "edit":
        cmd_edit()
    elif cmd == "mailpwd":
        cmd_mailpwd()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
