from __future__ import annotations

import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path


def _mail_settings(config: dict | None = None) -> tuple[str, list[str], str, int, Path]:
    config = config or {}
    active_id = config.get("active_sender", "default")
    profile = next((item for item in config.get("senders", []) if item.get("id") == active_id and item.get("enabled", True)), {})
    sender = os.getenv(profile.get("username_env", "CLIPPERS_SMTP_SENDER"), "")
    recipients_config = [item for item in config.get("recipients", []) if item.get("enabled", True)]
    recipients = [str(item.get("email", "")).strip() for item in recipients_config if item.get("email")]
    if not recipients:
        recipients = [x.strip() for x in os.getenv("CLIPPERS_RECIPIENTS", "").split(",") if x.strip()]
    host = profile.get("host") or os.getenv("CLIPPERS_SMTP_HOST", "smtp.qq.com")
    port = int(profile.get("port") or os.getenv("CLIPPERS_SMTP_PORT", "465"))
    password_file = Path(profile.get("password_file") or os.getenv("CLIPPERS_SMTP_PASSWORD_FILE", "/nonexistent"))
    if not sender or not recipients or not password_file.is_file():
        raise RuntimeError("SMTP 发件人、收件人或密码文件未配置")
    return sender, recipients, host, port, password_file


def send_html(subject: str, html_path: Path, config: dict | None = None) -> str:
    sender, recipients, host, port, password_file = _mail_settings(config)
    password = password_file.read_text().strip()
    html = html_path.read_text(encoding="utf-8")
    message_id = make_msgid(domain=sender.split("@")[-1])
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Message-ID"] = sender, ", ".join(recipients), subject, message_id
    msg.set_content(re.sub(r"<[^>]+>", "", html))
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)
    return message_id


def test_connection(config: dict | None = None) -> dict:
    sender, recipients, host, port, password_file = _mail_settings(config)
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as smtp:
        smtp.login(sender, password_file.read_text().strip())
    return {"ok": True, "sender": sender, "recipient_count": len(recipients), "host": host, "port": port}
