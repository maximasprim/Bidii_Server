"""
Sends outbound emails via SMTP using only the Python standard library
(smtplib + email.mime) - no new pip dependency needed for this feature.

This is the only module in the codebase that knows how to actually send
an email; app/services/notifications.py is the only caller, and it always
goes through is_email_configured()/send_email() here rather than touching
smtplib directly, mirroring how app/services/ai_providers/factory.py is
the only place that reads AI API keys.
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import get_settings


class EmailError(Exception):
    """Base class for every email-sending failure."""


class EmailNotConfiguredError(EmailError):
    """No SMTP host is configured server-side."""


def is_email_configured() -> bool:
    settings = get_settings()
    return bool(settings.smtp_host and settings.smtp_from_email)


def send_email(*, to_email: str, subject: str, body_text: str) -> None:
    """
    Raises EmailNotConfiguredError if SMTP isn't set up, or EmailError on
    any send failure. Callers that don't want a failed/unconfigured send
    to interrupt whatever else they're doing (e.g. an application status
    update) should catch these - see app/services/notifications.py, which
    is the only intended caller and already does this.
    """
    settings = get_settings()
    if not is_email_configured():
        raise EmailNotConfiguredError(
            "No SMTP server is configured (SMTP_HOST/SMTP_FROM_EMAIL). Add them to .env and restart the backend."
        )

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    message["To"] = to_email
    # Plain text only, deliberately - templates are authored as plain text
    # (see app/models/notification.py), so there's no HTML source to send
    # alongside it. A simple, readable plain-text email is also the safest
    # default for a company that hasn't set up its own HTML email styling.
    message.attach(MIMEText(body_text, "plain"))

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_username and settings.smtp_password:
                server.login(settings.smtp_username, settings.smtp_password)
            server.sendmail(settings.smtp_from_email, [to_email], message.as_string())
    except smtplib.SMTPException as exc:
        raise EmailError(f"SMTP send failed: {exc}") from exc
    except OSError as exc:  # connection refused, DNS failure, timeout, etc.
        raise EmailError(f"Couldn't reach the SMTP server: {exc}") from exc
