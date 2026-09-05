"""Preflight checks: is this install actually capable of discovering anything?

Every dependency that can fail silently gets one live probe. Run it right after
deploying, and any time the database stops growing:

    docker compose exec worker python -m app.cli doctor
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select

from app.config import settings
from app.db.models import Footprint, Opportunity, SeedSource, utcnow
from app.db.session import session_scope
from app.metrics import get_metrics_provider
from app.search import get_provider

log = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str


def run_all(probe_network: bool = True) -> list[Check]:
    checks: list[Check] = []
    checks.append(_database())
    checks.append(_config_loaded())
    if probe_network:
        checks.append(_search_backend())
        checks.append(_github())
        checks.append(_metrics_backend())
    checks.append(_discovery_progress())
    checks.append(_outreach_readiness())
    checks.append(_telegram())
    return checks


def _database() -> Check:
    try:
        with session_scope() as s:
            count = s.scalar(select(func.count(Opportunity.id))) or 0
        engine = settings.database_url.split("@")[-1]
        return Check("database", OK, f"reachable ({engine}), {count:,} opportunities stored")
    except Exception as exc:
        return Check("database", FAIL, f"{type(exc).__name__}: {exc}"[:200])


def _config_loaded() -> Check:
    try:
        with session_scope() as s:
            footprints = s.scalar(select(func.count(Footprint.id))) or 0
            seeds = s.scalar(
                select(func.count(SeedSource.id)).where(SeedSource.enabled.is_(True))
            ) or 0
    except Exception as exc:
        return Check("config", FAIL, str(exc)[:200])

    if footprints == 0:
        return Check("config", FAIL, "no footprints loaded - run `blf sync-config`")
    if seeds == 0:
        return Check("config", WARN, f"{footprints} footprints, but 0 enabled seed sources")
    return Check("config", OK, f"{footprints} footprints, {seeds} enabled seed sources")


def _search_backend() -> Check:
    provider = get_provider()
    try:
        ok, detail = provider.healthcheck()
    finally:
        provider.close()

    if provider.name == "none":
        return Check(
            "search backend",
            FAIL,
            "SEARCH_PROVIDER=none - footprint discovery is disabled entirely",
        )
    if not ok:
        return Check(
            "search backend",
            FAIL,
            f"{provider.name}: {detail}. Self-host SearXNG (it ships in "
            "docker-compose) or set SERPER_API_KEY / BRAVE_API_KEY.",
        )
    return Check("search backend", OK, f"{provider.name}: {detail}")


def _github() -> Check:
    import httpx

    headers = {"User-Agent": settings.user_agent}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        r = httpx.get("https://api.github.com/rate_limit", headers=headers, timeout=15)
        r.raise_for_status()
        core = r.json()["resources"]["search"]
    except Exception as exc:
        return Check("github api", FAIL, f"{type(exc).__name__}: {exc}"[:160])

    limit, remaining = core.get("limit", 0), core.get("remaining", 0)
    if not settings.github_token:
        return Check(
            "github api",
            WARN,
            f"unauthenticated: {remaining}/{limit} search requests left. "
            "Set GITHUB_TOKEN to lift the limit.",
        )
    return Check("github api", OK, f"authenticated: {remaining}/{limit} search requests left")


def _metrics_backend() -> Check:
    provider = get_metrics_provider()
    if provider.name == "none":
        return Check("metrics", WARN, "not configured - authority stays empty (optional)")

    # Metrics are an optional enrichment: a missing key is a warning, never a
    # failure. Only a key that is present and broken counts as a failure.
    missing_key = (
        provider.name == "openpagerank" and not settings.openpagerank_api_key
    ) or (
        provider.name == "dataforseo"
        and not (settings.dataforseo_login and settings.dataforseo_password)
    )
    if missing_key:
        return Check(
            "metrics",
            WARN,
            f"{provider.name} selected but no key set - authority stays empty "
            "(optional; discovery is unaffected)",
        )
    try:
        result = provider.fetch(["github.com"])
    except Exception as exc:
        return Check("metrics", FAIL, f"{provider.name}: {exc}"[:160])
    finally:
        provider.close()

    if not result:
        return Check("metrics", FAIL, f"{provider.name}: no data returned - check the API key")
    first = result[0]
    if first.error:
        return Check("metrics", FAIL, f"{provider.name}: {first.error}")
    return Check("metrics", OK, f"{provider.name}: github.com -> page_rank {first.page_rank}")


def _discovery_progress() -> Check:
    """Is the database actually growing, or has it silently stalled?"""
    from datetime import timedelta

    try:
        with session_scope() as s:
            total = s.scalar(select(func.count(Opportunity.id))) or 0
            day = utcnow() - timedelta(days=1)
            week = utcnow() - timedelta(days=7)
            last_day = s.scalar(
                select(func.count(Opportunity.id)).where(Opportunity.first_seen >= day)
            ) or 0
            last_week = s.scalar(
                select(func.count(Opportunity.id)).where(Opportunity.first_seen >= week)
            ) or 0
            unchecked = s.scalar(
                select(func.count(Opportunity.id)).where(Opportunity.last_checked.is_(None))
            ) or 0
    except Exception as exc:
        return Check("discovery", FAIL, str(exc)[:200])

    if total == 0:
        return Check("discovery", FAIL, "database is empty - run `blf discover && blf harvest`")
    if last_week == 0:
        return Check(
            "discovery",
            FAIL,
            f"{total:,} URLs stored but nothing new in 7 days - discovery has stalled",
        )
    return Check(
        "discovery",
        OK,
        f"{total:,} stored, +{last_day:,} in 24h, +{last_week:,} in 7d, "
        f"{unchecked:,} awaiting verification",
    )


def _outreach_readiness() -> Check:
    if not settings.outreach_enabled:
        return Check("outreach", OK, "disabled (OUTREACH_ENABLED=false) - drafts only, nothing sends")
    missing = []
    if not settings.smtp_host:
        missing.append("SMTP_HOST")
    if not settings.outreach_from_email:
        missing.append("OUTREACH_FROM_EMAIL")
    if missing:
        return Check("outreach", WARN, f"enabled but {', '.join(missing)} unset - dry-run only")
    return Check(
        "outreach",
        WARN,
        f"LIVE: will send up to {settings.outreach_daily_limit}/day from "
        f"{settings.outreach_from_email}"
        + (" (approval required)" if settings.outreach_require_approval else " WITHOUT approval"),
    )


def _telegram() -> Check:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return Check("telegram", WARN, "not configured - no digest, no failure alerts")

    import httpx

    try:
        r = httpx.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe", timeout=15
        )
        r.raise_for_status()
        name = r.json()["result"]["username"]
    except Exception as exc:
        return Check("telegram", FAIL, f"{type(exc).__name__}: {exc}"[:160])
    return Check("telegram", OK, f"@{name} reachable")
