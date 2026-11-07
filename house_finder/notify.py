"""Notifications for new listings: email (SMTP) and webhook (Discord/Slack)."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx

from .config import get_settings
from .models import Listing

log = logging.getLogger(__name__)


def _format_listing(listing: Listing) -> str:
    price = f"£{listing.price_pcm:,} pcm" if listing.price_pcm else "price n/a"
    beds = f"{listing.beds} bed" if listing.beds else "beds n/a"
    return f"• [{listing.title or 'Listing'}]({listing.url}) — {price}, {beds} ({listing.source})"


def _build_message(listings: list[Listing]) -> str:
    lines = [f"{len(listings)} new Bath student listing(s):", ""]
    lines.extend(_format_listing(x) for x in listings[:25])
    if len(listings) > 25:
        lines.append(f"…and {len(listings) - 25} more.")
    return "\n".join(lines)


def notify_new_listings(listings: list[Listing]) -> None:
    if not listings:
        return
    settings = get_settings()
    body = _build_message(listings)
    if settings.webhook_url:
        _send_webhook(settings.webhook_url, body)
    if settings.smtp_host and settings.email_to:
        _send_email(body)


def _send_webhook(url: str, body: str) -> None:
    # Discord and Slack both accept {"content": ...} / {"text": ...}.
    payload = {"content": body, "text": body}
    try:
        resp = httpx.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        log.info("webhook notification sent")
    except httpx.HTTPError as exc:  # pragma: no cover
        log.warning("webhook notification failed: %s", exc)


def _send_email(body: str) -> None:
    settings = get_settings()
    msg = EmailMessage()
    msg["Subject"] = "House Finder: new listings"
    msg["From"] = settings.email_from or settings.smtp_user
    msg["To"] = settings.email_to
    msg.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
        log.info("email notification sent")
    except (smtplib.SMTPException, OSError) as exc:  # pragma: no cover
        log.warning("email notification failed: %s", exc)
