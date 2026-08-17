from __future__ import annotations

import email
import imaplib
import re
from email.message import Message


DEFAULT_OTP_REGEX = r"\b(\d{6})\b"


def extract_otp(text: str, pattern: str = DEFAULT_OTP_REGEX) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def message_text(message: Message) -> str:
    if message.is_multipart():
        parts: list[str] = []
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_type() not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, errors="replace"))
        return "\n".join(parts)
    payload = message.get_payload(decode=True)
    if not payload:
        return str(message.get_payload())
    charset = message.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def latest_email_otp(
    username: str,
    app_password: str,
    mailbox: str = "INBOX",
    sender_filter: str = "",
    subject_filter: str = "",
    otp_regex: str = DEFAULT_OTP_REGEX,
    host: str = "imap.gmail.com",
    limit: int = 10,
) -> str:
    with imaplib.IMAP4_SSL(host) as client:
        client.login(username, app_password)
        client.select(mailbox)
        criteria = ["UNSEEN"]
        if sender_filter:
            criteria.extend(["FROM", f'"{sender_filter}"'])
        if subject_filter:
            criteria.extend(["SUBJECT", f'"{subject_filter}"'])
        status, data = client.search(None, *criteria)
        if status != "OK" or not data or not data[0]:
            status, data = client.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            raise RuntimeError("No email messages found for OTP lookup")
        message_ids = data[0].split()[-limit:]
        for message_id in reversed(message_ids):
            status, payload = client.fetch(message_id, "(RFC822)")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            message = email.message_from_bytes(payload[0][1])
            text = "\n".join([str(message.get("Subject", "")), str(message.get("From", "")), message_text(message)])
            otp = extract_otp(text, otp_regex)
            if otp:
                return otp
    raise RuntimeError("OTP not found in recent email messages")
