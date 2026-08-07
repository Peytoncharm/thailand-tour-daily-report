"""
email_sender.py
───────────────
Minimal SMTP sender for customer-facing mail. The service had no
existing mail path, so this follows the spec's fallback: plain
smtplib with env-var config.

Env vars:
  SMTP_HOST  — e.g. smtp.zoho.eu
  SMTP_PORT  — default 587 (STARTTLS)
  SMTP_USER  — login user
  SMTP_PASS  — login password / app password
  SMTP_FROM  — default booking@peytonandcharmed.com
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "booking@peytonandcharmed.com")


def send_email(to_addr: str, subject: str, html_body: str) -> bool:
    """Send a plain-HTML email. Returns True only on successful handoff."""
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        logger.error("[EMAIL] SMTP_HOST/SMTP_USER/SMTP_PASS not configured — cannot send")
        return False
    if not to_addr:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_addr
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [to_addr], msg.as_string())
        logger.info(f"[EMAIL] Sent to {to_addr}: {subject}")
        return True
    except Exception as e:
        logger.error(f"[EMAIL] Send to {to_addr} failed: {e}")
        return False
