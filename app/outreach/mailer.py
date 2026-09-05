"""SMTP sending with the safety rails that cold outreach needs.

Four independent gates, all of which must pass before anything leaves:
  1. OUTREACH_ENABLED must be true          (master switch, off by default)
  2. the message must be approved           (human review, on by default)
  3. the address must not be suppressed     (opt-outs, bounces, manual blocks)
  4. the daily limit must not be reached    (volume cap)

Without SMTP credentials the mailer runs in dry-run mode: drafts are marked as
if sent and written to the log, so a campaign can be rehearsed end to end.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import OutreachMessage, Suppression, utcnow
from app.processors.url_cleaner import root_domain

log = logging.getLogger(__name__)


class SendBlocked(RuntimeError):
    """Raised when a gate refuses a send - the reason is the message."""


def is_suppressed(session: Session, email: str) -> bool:
    domain = root_domain(email.split("@")[-1])
    hit = session.execute(
        select(Suppression.id).where(
            or_(Suppression.value == email.lower(), Suppression.value == domain)
        )
    ).first()
    return hit is not None


def suppress(session: Session, value: str, reason: str = "manual") -> bool:
    value = value.strip().lower()
    if not value:
        return False
    exists = session.execute(
        select(Suppression.id).where(Suppression.value == value)
    ).first()
    if exists:
        return False
    session.add(Suppression(value=value, reason=reason))
    session.flush()
    return True


def sent_today(session: Session) -> int:
    since = utcnow() - timedelta(days=1)
    return (
        session.scalar(
            select(func.count(OutreachMessage.id)).where(
                OutreachMessage.sent.is_(True), OutreachMessage.sent_at >= since
            )
        )
        or 0
    )


def remaining_today(session: Session) -> int:
    return max(0, settings.outreach_daily_limit - sent_today(session))


def dry_run() -> bool:
    """No SMTP host configured means rehearse, never send."""
    return not (settings.smtp_host and settings.outreach_from_email)


def check_gates(session: Session, message: OutreachMessage) -> None:
    if not settings.outreach_enabled:
        raise SendBlocked("OUTREACH_ENABLED is false")
    if settings.outreach_require_approval and not message.approved:
        raise SendBlocked("message not approved")
    if is_suppressed(session, message.to_email):
        raise SendBlocked(f"{message.to_email} is suppressed")
    if remaining_today(session) <= 0:
        raise SendBlocked(f"daily limit reached ({settings.outreach_daily_limit})")


def build_message(msg: OutreachMessage, in_reply_to: str | None = None) -> EmailMessage:
    email = EmailMessage()
    email["From"] = formataddr((settings.outreach_from_name or "", settings.outreach_from_email))
    email["To"] = msg.to_email
    email["Subject"] = msg.subject
    email["Message-ID"] = make_msgid()
    if settings.outreach_reply_to:
        email["Reply-To"] = settings.outreach_reply_to
    if in_reply_to:
        email["In-Reply-To"] = in_reply_to
        email["References"] = in_reply_to
    email.set_content(msg.body)
    return email


def send(session: Session, msg: OutreachMessage, in_reply_to: str | None = None) -> bool:
    """Send one drafted message. Returns True if it actually went out."""
    check_gates(session, msg)
    email = build_message(msg, in_reply_to)

    if dry_run():
        log.info(
            "DRY RUN - would send to %s | subject: %s | %s chars",
            msg.to_email,
            msg.subject,
            len(msg.body),
        )
        msg.sent = True
        msg.sent_at = utcnow()
        msg.error = "dry-run (no SMTP configured)"
        return False

    try:
        context = ssl.create_default_context()
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
        with server:
            if settings.smtp_port != 465 and settings.smtp_starttls:
                server.starttls(context=context)
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(email)
    except Exception as exc:
        msg.error = f"{type(exc).__name__}: {exc}"[:1000]
        log.warning("send failed to %s: %s", msg.to_email, exc)
        raise

    msg.sent = True
    msg.sent_at = utcnow()
    msg.error = None
    log.info("sent outreach to %s", msg.to_email)
    return True
