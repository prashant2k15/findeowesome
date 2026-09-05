"""Search-footprint hunter: rotate queries, harvest result URLs."""
from __future__ import annotations

import logging
import random
import time

from app.db.models import utcnow
from app.db.repo import add_opportunities, next_footprints
from app.search import get_provider

log = logging.getLogger(__name__)


class SearchProviderDown(RuntimeError):
    """The configured search backend answered nothing at all."""


def run_footprints(
    session,
    batch: int = 25,
    pages: int = 2,
    delay: float = 3.0,
    provider_name: str | None = None,
) -> tuple[int, int]:
    """Run the least-recently-used footprints. Returns (results, new URLs)."""
    footprints = next_footprints(session, batch)
    if not footprints:
        log.warning("no footprints configured - run `blf sync-config` first")
        return 0, 0

    provider = get_provider(provider_name)
    if provider.name == "none":
        log.warning("SEARCH_PROVIDER not configured; footprint hunt skipped")
        return 0, 0

    total = 0
    new_total = 0
    empty_queries = 0
    try:
        for fp in footprints:
            results = provider.search(fp.query, pages=pages)
            if not results:
                empty_queries += 1
            urls = [r.url for r in results]
            seen, created = add_opportunities(
                session,
                urls,
                source="footprint",
                source_detail=fp.query,
                kind_hint=fp.kind_hint,
            )
            fp.last_run = utcnow()
            fp.run_count += 1
            fp.results_total += seen
            fp.results_new += created
            total += seen
            new_total += created
            log.info("footprint %r -> %s results, %s new", fp.query[:70], seen, created)
            session.flush()
            time.sleep(delay + random.uniform(0, delay * 0.5))
    finally:
        provider.close()

    # A backend that silently stops answering is the failure mode that kills
    # this system quietly: jobs stay green while the database stops growing.
    # Raise so run_job records a failure and Telegram alerts.
    if empty_queries == len(footprints) and len(footprints) >= 3:
        raise SearchProviderDown(
            f"{provider.name} returned 0 results for all {len(footprints)} queries - "
            "check SEARCH_PROVIDER / SEARXNG_URL / API key"
        )

    return total, new_total
