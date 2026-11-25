"""SMTP mail sending through the user's own preferred mailbox (or the optional
system SMTP for password-reset mail)."""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import aiosmtplib

from app.config import settings
from app.models import UserSettings
from app.security.crypto import decrypt


class MailError(Exception):
    pass


def _clean(h: str) -> str:
    return (h or "").replace("\r", " ").replace("\n", " ").strip()[:300]


def user_smtp_configured(s: UserSettings | None) -> bool:
    return bool(s and s.smtp_host and s.smtp_from_email)


def system_smtp_configured() -> bool:
    return bool(settings.system_smtp_host and settings.system_smtp_from)


async def send_mail(s: UserSettings | None, to_email: str, subject: str, body: str, *, to_name: str = "",
                    from_name: str = "Deminic", allow_system: bool = False) -> str:
    """Send a plain-text mail. Returns the Message-ID. Raises MailError."""
    if user_smtp_configured(s):
        host, port = s.smtp_host, int(s.smtp_port or 587)
        user, pwd, sender, sec = s.smtp_username, decrypt(s.smtp_password_enc), s.smtp_from_email, s.smtp_security
    elif allow_system and system_smtp_configured():
        host, port = settings.system_smtp_host, settings.system_smtp_port
        user, pwd, sender = settings.system_smtp_username, settings.system_smtp_password, settings.system_smtp_from
        sec = "ssl" if port == 465 else "starttls"
    else:
        raise MailError("SMTP is not configured - add your SMTP server, SMTP key and preferred mail in Settings → Email")
    to_email = _clean(to_email)
    if "@" not in to_email:
        raise MailError("invalid recipient address")
    msg = EmailMessage()
    msg["From"] = formataddr((_clean(from_name), sender))
    msg["To"] = formataddr((_clean(to_name), to_email)) if to_name else to_email
    msg["Subject"] = _clean(subject) or "(no subject)"
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or "deminic.local")
    msg.set_content(body or "")
    try:
        await aiosmtplib.send(
            msg,
            hostname=host,
            port=port,
            username=user or None,
            password=pwd or None,
            start_tls=(sec == "starttls"),
            use_tls=(sec == "ssl"),
            timeout=25,
        )
    except Exception as e:  # noqa: BLE001
        raise MailError(f"{type(e).__name__}: {str(e)[:200]}") from e
    return msg["Message-ID"]
