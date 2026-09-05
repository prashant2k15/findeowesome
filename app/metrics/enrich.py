"""Fill in domain authority for stored opportunities.

Scores stay deliberately separate:
  * `score`      - can I actually get a link here? (page signals)
  * `page_rank`  - is this domain worth the effort? (authority)

Mixing them into one number hides which half is missing, so exports and the
dashboard filter on both instead.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import (
    STATUS_LIVE,
    STATUS_REDIRECT,
    DomainMetrics,
    Opportunity,
    Prospect,
    utcnow,
)
from app.metrics import get_metrics_provider
from app.metrics.base import DomainMetric

log = logging.getLogger(__name__)


def domains_needing_metrics(session: Session, limit: int) -> list[str]:
    """Live opportunities first - never spend quota on URLs that may be dead."""
    cutoff = utcnow() - timedelta(days=settings.metrics_refresh_days)

    stmt = (
        select(Opportunity.root_domain)
        .where(
            Opportunity.status.in_([STATUS_LIVE, STATUS_REDIRECT]),
            or_(Opportunity.metrics_at.is_(None), Opportunity.metrics_at < cutoff),
        )
        .group_by(Opportunity.root_domain)
        .limit(limit)
    )
    return [d for (d,) in session.execute(stmt).all() if d]


def store_metrics(session: Session, metrics: list[DomainMetric], provider: str) -> int:
    """Upsert domain_metrics rows and denormalise onto opportunities/prospects."""
    stored = 0
    now = utcnow()

    for m in metrics:
        row = session.execute(
            select(DomainMetrics).where(DomainMetrics.root_domain == m.root_domain)
        ).scalar_one_or_none()
        if row is None:
            row = DomainMetrics(root_domain=m.root_domain)
            session.add(row)

        row.page_rank = m.page_rank
        row.global_rank = m.global_rank
        row.domain_rating = m.domain_rating
        row.backlinks = m.backlinks
        row.referring_domains = m.referring_domains
        row.organic_traffic = m.organic_traffic
        row.spam_score = m.spam_score
        row.provider = provider
        row.raw = m.raw or {}
        row.fetched_at = now
        row.error = m.error
        stored += 1

        session.execute(
            update(Opportunity)
            .where(Opportunity.root_domain == m.root_domain)
            .values(page_rank=m.page_rank, metrics_at=now)
        )
        session.execute(
            update(Prospect)
            .where(Prospect.root_domain == m.root_domain)
            .values(page_rank=m.page_rank)
        )

    session.flush()
    return stored


def enrich(session: Session, limit: int | None = None) -> tuple[int, int]:
    """Fetch metrics for the next batch of domains. Returns (requested, stored)."""
    limit = limit or settings.metrics_batch_domains
    domains = domains_needing_metrics(session, limit)
    if not domains:
        return 0, 0

    provider = get_metrics_provider()
    if provider.name == "none":
        log.warning("METRICS_PROVIDER not configured; authority stays empty")
        return len(domains), 0

    try:
        metrics = provider.fetch(domains)
    finally:
        provider.close()

    stored = store_metrics(session, metrics, provider.name)

    # domains the provider silently skipped: stamp them so they do not block
    # the queue forever, but leave page_rank NULL so they sort last
    returned = {m.root_domain for m in metrics}
    missing = [d for d in domains if d not in returned]
    if missing:
        session.execute(
            update(Opportunity)
            .where(Opportunity.root_domain.in_(missing))
            .values(metrics_at=utcnow())
        )

    log.info("metrics: requested %s domains, stored %s", len(domains), stored)
    return len(domains), stored
