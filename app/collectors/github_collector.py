"""Harvest public backlink-site lists that people publish on GitHub.

Two jobs live here:
  * `discover_lists`   - GitHub repo search -> register new seed sources
  * `harvest_sources`  - fetch every enabled seed source and extract URLs
"""
from __future__ import annotations

import hashlib
import logging
import time

import httpx
from sqlalchemy import select

from app.config import settings
from app.db.models import SeedSource, utcnow
from app.db.repo import add_opportunities
from app.processors.url_cleaner import extract_urls

log = logging.getLogger(__name__)

API = "https://api.github.com"
SEARCH_QUERIES = [
    "backlink sites list",
    "high da backlink list",
    "profile creation sites list",
    "web directory submission list",
    "free directory submission sites",
    "guest posting sites list",
    "social bookmarking sites list",
    "dofollow backlink sites",
    "seo backlink resources",
    "awesome directories submit",
]
README_CANDIDATES = ("README.md", "readme.md", "README.MD", "Readme.md")


def _client() -> httpx.Client:
    headers = {
        "User-Agent": settings.user_agent,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return httpx.Client(timeout=settings.request_timeout, headers=headers, follow_redirects=True)


def discover_lists(session, per_query: int = 10) -> int:
    """Search GitHub for list repositories and register their READMEs as seeds."""
    registered = 0
    with _client() as client:
        for q in SEARCH_QUERIES:
            try:
                r = client.get(
                    f"{API}/search/repositories",
                    params={"q": q, "sort": "stars", "order": "desc", "per_page": per_query},
                )
                if r.status_code == 403:
                    log.warning("github search rate-limited; stopping this run")
                    break
                r.raise_for_status()
                items = r.json().get("items", [])
            except Exception as exc:
                log.warning("github search failed for %r: %s", q, exc)
                continue

            for repo in items:
                full = repo.get("full_name")
                branch = repo.get("default_branch") or "main"
                if not full:
                    continue
                raw = f"https://raw.githubusercontent.com/{full}/{branch}/README.md"
                exists = session.execute(
                    select(SeedSource.id).where(SeedSource.url == raw)
                ).scalar_one_or_none()
                if exists:
                    continue
                session.add(
                    SeedSource(
                        url=raw,
                        label=f"github:{full}",
                        kind_hint=None,
                        enabled=True,
                    )
                )
                registered += 1
            time.sleep(2)  # stay well inside the search rate limit
    session.flush()
    log.info("discover_lists registered %s new seed sources", registered)
    return registered


def harvest_sources(session, limit: int = 40) -> tuple[int, int]:
    """Fetch seed sources, extract URLs, insert new opportunities."""
    sources = list(
        session.execute(
            select(SeedSource)
            .where(SeedSource.enabled.is_(True))
            .order_by(SeedSource.last_fetched.is_(None).desc(), SeedSource.last_fetched.asc())
            .limit(limit)
        ).scalars()
    )
    seen_total = 0
    new_total = 0

    with httpx.Client(
        timeout=settings.request_timeout,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    ) as client:
        for src in sources:
            text = _fetch_source(client, src)
            src.last_fetched = utcnow()
            if text is None:
                continue

            digest = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
            if digest == src.last_hash:
                src.error = None
                continue  # unchanged since last run

            urls = extract_urls(text)
            seen, created = add_opportunities(
                session,
                urls,
                source="github",
                source_detail=src.label or src.url,
                kind_hint=src.kind_hint,
            )
            src.last_hash = digest
            src.urls_found = seen
            src.urls_new = created
            src.error = None
            seen_total += seen
            new_total += created
            log.info("seed %s -> %s urls, %s new", src.label or src.url, seen, created)
            time.sleep(0.5)

    session.flush()
    return seen_total, new_total


def _fetch_source(client: httpx.Client, src: SeedSource) -> str | None:
    """Fetch a raw list, retrying the usual README filename variants."""
    candidates = [src.url]
    if src.url.endswith("/README.md"):
        stem = src.url.rsplit("/", 1)[0]
        candidates += [f"{stem}/{n}" for n in README_CANDIDATES[1:]]
        candidates.append(src.url.replace("/main/", "/master/"))

    for url in candidates:
        try:
            r = client.get(url)
        except Exception as exc:
            src.error = f"{type(exc).__name__}: {exc}"[:500]
            continue
        if r.status_code == 200 and r.text.strip():
            return r.text
        src.error = f"HTTP {r.status_code}"

    # Nothing worked: disable permanently-missing sources so we stop retrying.
    if (src.error or "").startswith("HTTP 404"):
        src.enabled = False
    return None
