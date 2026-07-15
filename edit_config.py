#!/usr/bin/env python3
"""
edit_config.py

Tiny console tool to view and edit the encrypted configuration store
(config_store.sqlite3) used by status_report.py -- so rotating a
password or adding a new historical database doesn't require touching
any code.

Usage:
    python3 edit_config.py list
    python3 edit_config.py add
    python3 edit_config.py update <key>
    python3 edit_config.py remove <key>
    python3 edit_config.py mail
"""

import sys
import getpass

from app import config_store as cs


def cmd_list():
    dbs = cs.load_db_configs()
    if not dbs:
        print("No databases configured yet.")
        return
    print(f"{'KEY':<14}{'LABEL':<28}{'SERVER':<22}{'DATABASE':<16}{'USERNAME':<14}{'PROD':<6}")
    for cfg in dbs.values():
        print(f"{cfg.key:<14}{cfg.label:<28}{cfg.server:<22}{cfg.database:<16}"
              f"{cfg.username:<14}{'YES' if cfg.is_prod else '':<6}")


def _prompt_db_config(existing: cs.DbConfig = None) -> cs.DbConfig:
    def ask(label, default=None):
        suffix = f" [{default}]" if default else ""
        val = input(f"{label}{suffix}: ").strip()
        return val or default or ""

    key = existing.key if existing else ask("Key (e.g. abhi_maskv7)")
    label = ask("Display label", existing.label if existing else f"{key} (Historical)")
    server = ask("Server (host,port)", existing.server if existing else "10.21.42.17,1433")
    database = ask("Database name", existing.database if existing else key)
    username = ask("Username", existing.username if existing else "")
    pwd_prompt = "Password (leave blank to keep current)" if existing else "Password"
    password = getpass.getpass(f"{pwd_prompt}: ")
    if not password and existing:
        password = existing.password
    is_prod_raw = ask("Is this the PROD database? (y/n)", "y" if (existing and existing.is_prod) else "n")
    is_prod = is_prod_raw.strip().lower().startswith("y")
    return cs.DbConfig(key, label, server, database, username, password, is_prod)


def cmd_add():
    cfg = _prompt_db_config()
    cs.upsert_db_config(cfg)
    print(f"Added/updated '{cfg.key}'.")


def cmd_update(key: str):
    dbs = cs.load_db_configs()
    if key not in dbs:
        print(f"No such key: {key}")
        sys.exit(1)
    cfg = _prompt_db_config(dbs[key])
    cs.upsert_db_config(cfg)
    print(f"Updated '{key}'.")


def cmd_remove(key: str):
    dbs = cs.load_db_configs()
    if key not in dbs:
        print(f"No such key: {key}")
        sys.exit(1)
    confirm = input(f"Delete config for '{key}' ({dbs[key].label})? (y/n): ").strip().lower()
    if confirm == "y":
        cs.delete_db_config(key)
        print("Deleted.")


def cmd_mail():
    mc = cs.load_mail_config()
    print(f"SMTP server : {mc.smtp_server}")
    print(f"SMTP port   : {mc.smtp_port}")
    print(f"Mail from   : {mc.mail_from}")
    print(f"Default To  : {mc.default_to}")
    print(f"Default Cc  : {mc.default_cc}")
    print()
    if input("Update any of these? (y/n): ").strip().lower() != "y":
        return

    smtp_server = input(f"SMTP server [{mc.smtp_server}]: ").strip() or mc.smtp_server
    smtp_port = input(f"SMTP port [{mc.smtp_port}]: ").strip() or str(mc.smtp_port)
    mail_from = input(f"Mail from [{mc.mail_from}]: ").strip() or mc.mail_from
    default_to = input(f"Default To [{mc.default_to}]: ").strip() or mc.default_to
    default_cc = input(f"Default Cc [{mc.default_cc}]: ").strip() or mc.default_cc
    new_password = getpass.getpass("Mail password (leave blank to keep current): ")

    cs.set_mail_setting("smtp_server", smtp_server)
    cs.set_mail_setting("smtp_port", smtp_port)
    cs.set_mail_setting("mail_from", mail_from)
    cs.set_mail_setting("default_to", default_to)
    cs.set_mail_setting("default_cc", default_cc)
    if new_password:
        cs.set_mail_setting("mail_password", new_password, encrypt=True)
    print("Mail config updated.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "list":
        cmd_list()
    elif cmd == "add":
        cmd_add()
    elif cmd == "update":
        if len(sys.argv) < 3:
            print("Usage: python3 edit_config.py update <key>")
            sys.exit(1)
        cmd_update(sys.argv[2])
    elif cmd == "remove":
        if len(sys.argv) < 3:
            print("Usage: python3 edit_config.py remove <key>")
            sys.exit(1)
        cmd_remove(sys.argv[2])
    elif cmd == "mail":
        cmd_mail()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
