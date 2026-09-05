"""Telegram reporting - daily digest plus failure alerts."""
from __future__ import annotations

import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str, silent: bool = False) -> bool:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.debug("telegram not configured; message dropped")
        return False
    try:
        r = httpx.post(
            API.format(token=settings.telegram_bot_token),
            json={
                "chat_id": settings.telegram_chat_id,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": silent,
            },
            timeout=20,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        log.warning("telegram send failed: %s", exc)
        return False


def daily_report(stats: dict, jobs: list) -> str:
    kinds = stats.get("by_kind", {})
    kind_lines = "\n".join(
        f"  - {k}: <b>{v:,}</b>" for k, v in sorted(kinds.items(), key=lambda x: -x[1])
    ) or "  - (nothing classified yet)"

    job_lines = "\n".join(
        f"  {'OK ' if j.ok else 'ERR'} {j.job}: {j.processed} processed, {j.created} new"
        for j in jobs[:6]
    ) or "  (no jobs yet)"

    return (
        "<b>BingLinkFinder daily report</b>\n"
        f"\nNew URLs (24h): <b>{stats['new_today']:,}</b>"
        f"\nLive: <b>{stats['live']:,}</b>   Dead: {stats['dead']:,}   "
        f"Blocked: {stats['blocked']:,}"
        f"\nPending check: {stats['pending']:,}"
        f"\nTotal database: <b>{stats['total']:,}</b> URLs across "
        f"{stats['domains']:,} domains"
        f"\n\n<b>Live by type</b>\n{kind_lines}"
        f"\n\n<b>Recent jobs</b>\n<pre>{job_lines}</pre>"
    )


def alert(job: str, error: str) -> None:
    send(f"<b>BingLinkFinder job failed</b>\nJob: <code>{job}</code>\n<pre>{error[:800]}</pre>")
