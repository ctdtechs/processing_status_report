#!/usr/bin/env python3
"""
mailer.py

Sends the report email. SMTP settings and the mail password come from
config_store (encrypted at rest) rather than being hardcoded/base64'd
in source.
"""

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from . import config_store as cs


def parse_addr_list(raw: str) -> list:
    """Splits a semicolon-separated address string into a clean list."""
    return [a.strip() for a in raw.split(";") if a.strip()]


def send_report_email(mail_cfg: cs.MailConfig, html_body: str, subject: str, to_list: list, cc_list: list):
    msg = MIMEMultipart("alternative")
    # "Display Name <addr>" when a from_name is configured; bare address otherwise.
    from_name = getattr(mail_cfg, "mail_from_name", "") or ""
    msg["From"] = formataddr((from_name, mail_cfg.mail_from)) if from_name else mail_cfg.mail_from
    msg["To"] = "; ".join(to_list)
    if cc_list:
        msg["Cc"] = "; ".join(cc_list)
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    all_recipients = to_list + cc_list

    context = ssl.create_default_context()
    with smtplib.SMTP(mail_cfg.smtp_server, mail_cfg.smtp_port) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(mail_cfg.mail_from, mail_cfg.mail_password)
        server.sendmail(mail_cfg.mail_from, all_recipients, msg.as_string())
