"""
Sends outbound emails via SMTP using only the Python standard library
(smtplib + email.mime) - no new pip dependency needed for this feature.

This is the only module in the codebase that knows how to actually send
an email. It supports two SMTP "identities" (EmailKind, below):
"candidate" for career-application emails and "loans" for
loan-application emails to branch/ops admins - so the two can be
configured to send from different mailboxes (see app/config.py's
SMTP_LOANS_* settings). app/services/notifications.py (candidate) and
app/services/internal_notifications.py + app/services/product_routing.py
(loans) are the only callers, and they always go through
is_email_configured()/send_email() here rather than touching smtplib
directly, mirroring how app/services/ai_providers/factory.py is the
only place that reads AI API keys.
"""

import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Literal

from app.config import get_settings

EmailKind = Literal["candidate", "loans"]


class EmailError(Exception):
    """Base class for every email-sending failure."""


class EmailNotConfiguredError(EmailError):
    """No SMTP host is configured server-side."""


@dataclass(frozen=True)
class _SmtpProfile:
    host: str | None
    port: int
    username: str | None
    password: str | None
    use_tls: bool
    from_email: str | None
    from_name: str


def _resolve_profile(kind: EmailKind) -> _SmtpProfile:
    """
    "candidate" always uses the primary SMTP_* settings. "loans" uses the
    optional SMTP_LOANS_* overrides - falling back field-by-field to the
    primary SMTP_* settings for anything left unset, so a deployment that
    hasn't configured SMTP_LOANS_* at all gets byte-for-byte the same
    behaviour it had before this existed.
    """
    settings = get_settings()
    if kind == "candidate":
        return _SmtpProfile(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_tls=settings.smtp_use_tls,
            from_email=settings.smtp_from_email,
            from_name=settings.smtp_from_name,
        )

    return _SmtpProfile(
        host=settings.smtp_loans_host or settings.smtp_host,
        port=settings.smtp_loans_port or settings.smtp_port,
        username=settings.smtp_loans_username or settings.smtp_username,
        password=settings.smtp_loans_password or settings.smtp_password,
        use_tls=settings.smtp_loans_use_tls if settings.smtp_loans_use_tls is not None else settings.smtp_use_tls,
        from_email=settings.smtp_loans_from_email or settings.smtp_from_email,
        # Note: from_name has its own default ("Bidii Credit Loans"), so
        # unlike the other fields it does NOT fall back to smtp_from_name -
        # that default is the whole point of a separate loans identity.
        from_name=settings.smtp_loans_from_name,
    )


def is_email_configured(kind: EmailKind = "candidate") -> bool:
    profile = _resolve_profile(kind)
    return bool(profile.host and profile.from_email)


def send_email(*, to_email: str, subject: str, body_text: str, kind: EmailKind = "candidate") -> None:
    """
    Raises EmailNotConfiguredError if SMTP isn't set up for `kind`, or
    EmailError on any send failure. `kind` picks which SMTP identity to
    send from - defaults to "candidate" (career-application emails) for
    backward compatibility; pass kind="loans" for loan-application
    emails. Callers that don't want a failed/unconfigured send to
    interrupt whatever else they're doing (e.g. an application status
    update, or a new loan application being submitted) should catch
    these - see app/services/notifications.py and
    app/services/internal_notifications.py / product_routing.py, which
    already do this.
    """
    profile = _resolve_profile(kind)
    if not (profile.host and profile.from_email):
        raise EmailNotConfiguredError(
            f"No SMTP server is configured for {kind!r} emails. Add "
            + ("SMTP_HOST/SMTP_FROM_EMAIL" if kind == "candidate" else "SMTP_LOANS_* (or SMTP_*)")
            + " to .env and restart the backend."
        )

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = f"{profile.from_name} <{profile.from_email}>"
    message["To"] = to_email
    # Plain text only, deliberately - templates are authored as plain text
    # (see app/models/notification.py), so there's no HTML source to send
    # alongside it. A simple, readable plain-text email is also the safest
    # default for a company that hasn't set up its own HTML email styling.
    message.attach(MIMEText(body_text, "plain"))

    try:
        with smtplib.SMTP(profile.host, profile.port, timeout=15) as server:
            if profile.use_tls:
                server.starttls()
            if profile.username and profile.password:
                server.login(profile.username, profile.password)
            server.sendmail(profile.from_email, [to_email], message.as_string())
    except smtplib.SMTPException as exc:
        raise EmailError(f"SMTP send failed: {exc}") from exc
    except OSError as exc:  # connection refused, DNS failure, timeout, etc.
        raise EmailError(f"Couldn't reach the SMTP server: {exc}") from exc

