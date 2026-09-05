"""Verify the links you placed: is it still on the page, and is it dofollow?""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.parser import HTMLParser
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.checkers.live_checker import MAX_BODY_BYTES, DomainThrottle
from app.config import settings
from app.db.models import (
    LINK_LIVE,
    LINK_MISSING,
    LINK_PENDING,
    LINK_UNREACHABLE,
    Backlink,
    utcnow,
)
from app.processors.url_cleaner import canonical_url, host_of, root_domain

log = logging.getLogger(__name__)


@dataclass
class LinkOutcome:
    backlink_id: int
    status: str
    http_status: int | None = None
    is_dofollow: bool | None = None
    rel: str | None = None
    anchor: str | None = None
    note: str | None = None


def add_backlink(
    session: Session,
    source_url: str,
    target_url: str,
    anchor: str | None = None,
    project: str | None = None,
    notes: str | None = None,
) -> Backlink | None:
    """Register a placed link. Idempotent on (source_url, target_url)."""
    src = canonical_url(source_url) or source_url.strip()
    tgt = canonical_url(target_url) or target_url.strip()
    if not src or not tgt:
        return None

    existing = session.execute(
        select(Backlink).where(Backlink.source_url == src, Backlink.target_url == tgt)
    ).scalar_one_or_none()
    if existing:
        return existing

    row = Backlink(
        source_url=src,
        source_domain=root_domain(host_of(src)),
        target_url=tgt,
        target_domain=root_domain(host_of(tgt)),
        anchor_expected=anchor,
        project=project,
        notes=notes,
        status=LINK_PENDING,
    )
    session.add(row)
    session.flush()
    return row


def verify_batch(session: Session, limit: int | None = None) -> dict:
    """Re-check the next batch of tracked links."""
    limit = limit or settings.tracker_batch_size
    cutoff = utcnow() - timedelta(days=settings.tracker_recheck_days)

    rows = list(
        session.execute(
            select(Backlink)
            .where(or_(Backlink.last_checked.is_(None), Backlink.last_checked < cutoff))
            .order_by(Backlink.last_checked.is_(None).desc(), Backlink.last_checked.asc())
            .limit(limit)
        ).scalars()
    )
    if not rows:
        return {"checked": 0, "live": 0, "missing": 0, "lost": 0, "unreachable": 0}

    jobs = [(r.id, r.source_url, r.target_url, r.target_domain) for r in rows]
    throttle = DomainThrottle(settings.per_domain_delay)
    outcomes: list[LinkOutcome] = []

    with httpx.Client(
        timeout=settings.request_timeout,
        headers={"User-Agent": settings.user_agent, "Accept": "text/html"},
        follow_redirects=True,
        max_redirects=5,
        verify=False,
    ) as client:
        with ThreadPoolExecutor(max_workers=settings.checker_concurrency) as pool:
            futures = [pool.submit(_verify_one, client, throttle, *job) for job in jobs]
            for fut in futures:
                try:
                    outcomes.append(fut.result())
                except Exception as exc:
                    log.warning("link verification crashed: %s", exc)

    summary = {"checked": len(outcomes), "live": 0, "missing": 0, "lost": 0, "unreachable": 0}
    by_id = {r.id: r for r in rows}
    newly_lost: list[Backlink] = []

    for out in outcomes:
        row = by_id.get(out.backlink_id)
        if row is None:
            continue
        was_live = row.status == LINK_LIVE

        row.status = out.status
        row.http_status = out.http_status
        row.last_checked = utcnow()
        row.check_count += 1
        if out.rel is not None:
            row.rel = out.rel[:128]
        if out.is_dofollow is not None:
            row.is_dofollow = out.is_dofollow
        if out.anchor:
            row.anchor_found = out.anchor[:255]

        if out.status == LINK_LIVE:
            summary["live"] += 1
            if row.first_seen_live is None:
                row.first_seen_live = utcnow()
            row.lost_at = None
        elif out.status == LINK_MISSING:
            summary["missing"] += 1
            if was_live:
                row.lost_at = utcnow()
                newly_lost.append(row)
                summary["lost"] += 1
        else:
            summary["unreachable"] += 1

    session.flush()
    summary["lost_links"] = [
        {"source": r.source_url, "target": r.target_url, "project": r.project} for r in newly_lost
    ]
    return summary


def _verify_one(
    client: httpx.Client,
    throttle: DomainThrottle,
    link_id: int,
    source_url: str,
    target_url: str,
    target_domain: str,
) -> LinkOutcome:
    host = root_domain(urlsplit(source_url).hostname or "")
    lock = throttle.acquire(host)
    try:
        try:
            with client.stream("GET", source_url) as resp:
                code = resp.status_code
                if code >= 400:
                    return LinkOutcome(link_id, LINK_UNREACHABLE, http_status=code)
                body = b""
                for chunk in resp.iter_bytes(32_768):
                    body += chunk
                    if len(body) >= MAX_BODY_BYTES * 3:
                        break
        except Exception as exc:
            return LinkOutcome(link_id, LINK_UNREACHABLE, note=f"{type(exc).__name__}: {exc}"[:200])
    finally:
        throttle.release(host, lock)

    html = body.decode("utf-8", "ignore")
    found = find_link(html, source_url, target_url, target_domain)
    if not found:
        return LinkOutcome(link_id, LINK_MISSING, http_status=code)

    rel = found["rel"]
    return LinkOutcome(
        link_id,
        LINK_LIVE,
        http_status=code,
        is_dofollow=found["dofollow"],
        rel=rel,
        anchor=found["anchor"],
    )


def find_link(html: str, source_url: str, target_url: str, target_domain: str | None = None) -> dict | None:
    """Locate the exact tracked target URL.

    Domain-only matching is intentionally not a fallback: a page can keep
    linking to the same website while the specific tracked backlink disappears.
    """
    tree = HTMLParser(html or "")
    target_norm = canonical_url(target_url) or target_url

    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        norm = canonical_url(urljoin(source_url, href))
        if not norm or norm != target_norm:
            continue

        rel = (a.attributes.get("rel") or "").lower().strip()
        return {
            "anchor": a.text(strip=True)[:255],
            "rel": rel or None,
            "dofollow": not any(t in rel.split() for t in ("nofollow", "ugc", "sponsored")),
        }

    return None


def import_rows(session: Session, rows: list[dict], project: str | None = None) -> tuple[int, int]:
    """Bulk-register links from a CSV: source_url,target_url,anchor[,project]."""
    seen = added = 0
    for r in rows:
        source = (r.get("source_url") or r.get("source") or "").strip()
        target = (r.get("target_url") or r.get("target") or "").strip()
        if not source or not target:
            continue
        seen += 1

        src = canonical_url(source) or source
        tgt = canonical_url(target) or target
        before = session.execute(
            select(Backlink.id).where(Backlink.source_url == src, Backlink.target_url == tgt)
        ).scalar_one_or_none()

        link = add_backlink(
            session,
            source,
            target,
            anchor=(r.get("anchor") or r.get("anchor_text") or "").strip() or None,
            project=(r.get("project") or project or "").strip() or None,
        )
        if link is not None and before is None:
            added += 1
    return seen, added
