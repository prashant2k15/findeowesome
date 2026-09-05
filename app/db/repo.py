"""All database writes go through here so de-duplication stays in one place."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    LINK_LIVE,
    LINK_MISSING,
    PROSPECT_CONTACTED,
    PROSPECT_REPLIED,
    STATUS_BLOCKED,
    STATUS_DEAD,
    STATUS_LIVE,
    STATUS_NEW,
    STATUS_REDIRECT,
    Backlink,
    Footprint,
    JobRun,
    Opportunity,
    OutreachMessage,
    Prospect,
    SeedSource,
    utcnow,
)
from app.processors.classifier import classify_url
from app.processors.url_cleaner import host_of, normalize_url, root_domain

# One site should not be allowed to flood the master database.
MAX_URLS_PER_ROOT_DOMAIN = 60


def add_opportunities(
    session: Session,
    urls: list[str],
    source: str,
    source_detail: str | None = None,
    kind_hint: str | None = None,
    max_per_domain: int = MAX_URLS_PER_ROOT_DOMAIN,
) -> tuple[int, int]:
    """Insert normalised URLs, skipping duplicates. Returns (seen, inserted)."""
    normalised: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        n = normalize_url(raw)
        if n and n not in seen:
            seen.add(n)
            normalised.append(n)

    if not normalised:
        return 0, 0

    existing: set[str] = set()
    for chunk in _chunks(normalised, 400):
        rows = session.execute(
            select(Opportunity.url).where(Opportunity.url.in_(chunk))
        ).scalars()
        existing.update(rows)

    fresh = [u for u in normalised if u not in existing]
    if not fresh:
        return len(normalised), 0

    # per-domain cap, counting what is already stored
    roots = {root_domain(host_of(u)) for u in fresh}
    counts: dict[str, int] = {}
    for chunk in _chunks(sorted(roots), 400):
        rows = session.execute(
            select(Opportunity.root_domain, func.count(Opportunity.id))
            .where(Opportunity.root_domain.in_(chunk))
            .group_by(Opportunity.root_domain)
        ).all()
        counts.update({r: c for r, c in rows})

    objects: list[Opportunity] = []
    for url in fresh:
        host = host_of(url)
        root = root_domain(host)
        if counts.get(root, 0) >= max_per_domain:
            continue
        counts[root] = counts.get(root, 0) + 1
        kind, score = classify_url(url, kind_hint)
        objects.append(
            Opportunity(
                url=url,
                domain=host,
                root_domain=root,
                kind=kind,
                score=score,
                source=source,
                source_detail=(source_detail or "")[:512] or None,
                status=STATUS_NEW,
            )
        )

    if objects:
        session.add_all(objects)
        session.flush()
    return len(normalised), len(objects)


def due_for_check(session: Session, limit: int, recheck_days: int) -> list[Opportunity]:
    """New URLs first, then anything not checked within `recheck_days`."""
    cutoff = utcnow() - timedelta(days=recheck_days)
    stmt = (
        select(Opportunity)
        .where(
            (Opportunity.last_checked.is_(None))
            | (
                (Opportunity.last_checked < cutoff)
                & (Opportunity.status != STATUS_DEAD)
            )
        )
        .order_by(Opportunity.last_checked.is_(None).desc(), Opportunity.last_checked.asc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def stats(session: Session) -> dict:
    total = session.scalar(select(func.count(Opportunity.id))) or 0
    by_status = dict(
        session.execute(
            select(Opportunity.status, func.count(Opportunity.id)).group_by(Opportunity.status)
        ).all()
    )
    by_kind = dict(
        session.execute(
            select(Opportunity.kind, func.count(Opportunity.id))
            .where(Opportunity.status.in_([STATUS_LIVE, STATUS_REDIRECT]))
            .group_by(Opportunity.kind)
        ).all()
    )
    day_ago = utcnow() - timedelta(days=1)
    new_today = (
        session.scalar(
            select(func.count(Opportunity.id)).where(Opportunity.first_seen >= day_ago)
        )
        or 0
    )
    domains = session.scalar(select(func.count(func.distinct(Opportunity.root_domain)))) or 0
    pending = by_status.get(STATUS_NEW, 0)
    return {
        "total": total,
        "domains": domains,
        "tracker": tracker_stats(session),
        "outreach": outreach_stats(session),
        "new_today": new_today,
        "live": by_status.get(STATUS_LIVE, 0) + by_status.get(STATUS_REDIRECT, 0),
        "dead": by_status.get(STATUS_DEAD, 0),
        "blocked": by_status.get(STATUS_BLOCKED, 0),
        "pending": pending,
        "by_status": by_status,
        "by_kind": by_kind,
    }


def recent_jobs(session: Session, limit: int = 12) -> list[JobRun]:
    return list(
        session.execute(select(JobRun).order_by(JobRun.started_at.desc()).limit(limit)).scalars()
    )


def start_job(session: Session, job: str) -> JobRun:
    run = JobRun(job=job, started_at=utcnow())
    session.add(run)
    session.flush()
    return run


def finish_job(
    session: Session, run: JobRun, ok: bool, processed: int = 0, created: int = 0, message: str = ""
) -> None:
    run.finished_at = utcnow()
    run.ok = ok
    run.processed = processed
    run.created = created
    run.message = (message or "")[:4000] or None
    session.add(run)


def sync_seed_sources(session: Session, entries: list[dict]) -> int:
    """Upsert the seed-source list from config/seed_sources.yaml."""
    added = 0
    for e in entries:
        url = (e.get("url") or "").strip()
        if not url:
            continue
        existing = session.execute(
            select(SeedSource).where(SeedSource.url == url)
        ).scalar_one_or_none()
        if existing:
            existing.label = e.get("label") or existing.label
            existing.kind_hint = e.get("kind") or existing.kind_hint
            continue
        session.add(
            SeedSource(url=url, label=e.get("label"), kind_hint=e.get("kind"), enabled=True)
        )
        added += 1
    session.flush()
    return added


def sync_footprints(session: Session, entries: list[dict]) -> int:
    added = 0
    for e in entries:
        q = (e.get("query") or "").strip()
        if not q:
            continue
        existing = session.execute(
            select(Footprint).where(Footprint.query == q)
        ).scalar_one_or_none()
        if existing:
            existing.kind_hint = e.get("kind") or existing.kind_hint
            continue
        session.add(Footprint(query=q, kind_hint=e.get("kind") or "unknown"))
        added += 1
    session.flush()
    return added


def next_footprints(session: Session, limit: int) -> list[Footprint]:
    """Least-recently-run enabled footprints - a rotating queue."""
    stmt = (
        select(Footprint)
        .where(Footprint.enabled.is_(True))
        .order_by(Footprint.last_run.is_(None).desc(), Footprint.last_run.asc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def tracker_stats(session: Session) -> dict:
    """Health of the links you placed."""
    total = session.scalar(select(func.count(Backlink.id))) or 0
    if not total:
        return {"total": 0}
    by_status = dict(
        session.execute(
            select(Backlink.status, func.count(Backlink.id)).group_by(Backlink.status)
        ).all()
    )
    return {
        "total": total,
        "live": by_status.get(LINK_LIVE, 0),
        "missing": by_status.get(LINK_MISSING, 0),
        "lost": session.scalar(
            select(func.count(Backlink.id)).where(Backlink.lost_at.isnot(None))
        )
        or 0,
        "dofollow": session.scalar(
            select(func.count(Backlink.id)).where(
                Backlink.status == LINK_LIVE, Backlink.is_dofollow.is_(True)
            )
        )
        or 0,
        "by_status": by_status,
    }


def outreach_stats(session: Session) -> dict:
    """Where every prospect currently sits in the pipeline."""
    total = session.scalar(select(func.count(Prospect.id))) or 0
    if not total:
        return {"total": 0}
    by_status = dict(
        session.execute(
            select(Prospect.status, func.count(Prospect.id)).group_by(Prospect.status)
        ).all()
    )
    return {
        "total": total,
        "contacted": by_status.get(PROSPECT_CONTACTED, 0),
        "replied": by_status.get(PROSPECT_REPLIED, 0),
        "with_email": session.scalar(
            select(func.count(Prospect.id)).where(Prospect.email.isnot(None))
        )
        or 0,
        "pending_approval": session.scalar(
            select(func.count(OutreachMessage.id)).where(
                OutreachMessage.sent.is_(False), OutreachMessage.approved.is_(False)
            )
        )
        or 0,
        "sent": session.scalar(
            select(func.count(OutreachMessage.id)).where(OutreachMessage.sent.is_(True))
        )
        or 0,
        "by_status": by_status,
    }
