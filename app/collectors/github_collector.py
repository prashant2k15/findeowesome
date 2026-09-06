"""Harvest public backlink-site lists published on GitHub.

Discovery intentionally does not assume useful lists live only in README files.
Many repositories store their data in CSV/TXT/JSON/Markdown files, so a bounded
repository-tree scan registers likely list files as independent seed sources.
"""
from __future__ import annotations

import hashlib
import logging
import time
from urllib.parse import quote

import httpx
from sqlalchemy import select

from app.config import settings
from app.db.models import SeedSource, utcnow
from app.db.repo import add_opportunities
from app.processors.url_cleaner import URL_RE, extract_urls, spam_ratio

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
LIST_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".yaml", ".yml"}
LIST_NAME_HINTS = (
    "backlink", "directory", "directories", "submit", "profile", "guest",
    "bookmark", "seo", "sites", "resources", "list", "web2", "forum",
)
MAX_FILES_PER_REPO = 12
MAX_TREE_ITEMS = 4000
MAX_FILE_BYTES = 2_000_000
MAX_SPAM_RATIO = 0.5


def _client() -> httpx.Client:
    headers = {
        "User-Agent": settings.user_agent,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return httpx.Client(timeout=settings.request_timeout, headers=headers, follow_redirects=True)


def _raw_url(full: str, branch: str, path: str) -> str:
    """Build a raw.githubusercontent.com URL without mangling branch names."""
    return f"https://raw.githubusercontent.com/{full}/{quote(branch, safe='/')}/{quote(path, safe='/')}"


def _likely_list_file(path: str, size: int | None = None) -> bool:
    lower = path.lower()
    if "." + lower.rsplit(".", 1)[-1] not in LIST_EXTENSIONS:
        return False
    if size is not None and size > MAX_FILE_BYTES:
        return False
    name = lower.rsplit("/", 1)[-1]
    return name.startswith("readme") or any(hint in lower for hint in LIST_NAME_HINTS)


def _candidate_files(client: httpx.Client, full: str, branch: str) -> list[str]:
    """Return a bounded set of likely public list files from a repository."""
    candidates = ["README.md"]
    try:
        # Git trees require a tree SHA, not a branch name. Resolve the branch
        # ref first so repositories whose default branch is not `main` work.
        ref = client.get(f"{API}/repos/{full}/git/ref/heads/{quote(branch, safe='')}")
        if ref.status_code != 200:
            return candidates
        tree_sha = ((ref.json().get("object") or {}).get("sha"))
        if not tree_sha:
            return candidates

        tree_url = f"{API}/repos/{full}/git/trees/{tree_sha}?recursive=1"
        r = client.get(tree_url)
        if r.status_code != 200:
            return candidates
        payload = r.json()
        items = payload.get("tree") or []
        if payload.get("truncated"):
            log.info("GitHub tree truncated for %s; using first %s items", full, MAX_TREE_ITEMS)
        if len(items) > MAX_TREE_ITEMS:
            items = items[:MAX_TREE_ITEMS]
        ranked: list[tuple[int, str]] = []
        for item in items:
            if item.get("type") != "blob":
                continue
            path = item.get("path") or ""
            size = item.get("size")
            if not _likely_list_file(path, size):
                continue
            lower = path.lower()
            score = 0
            if lower.startswith("readme"):
                score += 100
            score += sum(10 for hint in LIST_NAME_HINTS if hint in lower)
            if lower.endswith(".csv"):
                score += 5
            ranked.append((-score, path))
        ranked.sort()
        for _, path in ranked[:MAX_FILES_PER_REPO]:
            if path not in candidates:
                candidates.append(path)
    except Exception as exc:
        log.debug("could not inspect GitHub tree %s: %s", full, exc)
    return candidates[:MAX_FILES_PER_REPO]


def _register_seed(session, url: str, label: str) -> bool:
    exists = session.execute(select(SeedSource.id).where(SeedSource.url == url)).scalar_one_or_none()
    if exists:
        return False
    session.add(SeedSource(url=url, label=label, kind_hint=None, enabled=True))
    return True


def discover_lists(session, per_query: int = 10) -> int:
    """Search GitHub and register likely README/list data files as seeds."""
    registered = 0
    inspected_repos: set[str] = set()
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
                if not full or full in inspected_repos:
                    continue
                inspected_repos.add(full)
                for path in _candidate_files(client, full, branch):
                    raw = _raw_url(full, branch, path)
                    if _register_seed(session, raw, f"github:{full}:{path}"):
                        registered += 1
            time.sleep(1)
    session.flush()
    log.info("discover_lists registered %s new GitHub list files", registered)
    return registered


def harvest_sources(session, limit: int = 40) -> tuple[int, int]:
    sources = list(
        session.execute(
            select(SeedSource)
            .where(SeedSource.enabled.is_(True))
            .order_by(SeedSource.last_fetched.is_(None).desc(), SeedSource.last_fetched.asc())
            .limit(limit)
        ).scalars()
    )
    seen_total = new_total = 0

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

            raw_urls = _raw_links(text)
            ratio = spam_ratio(raw_urls)
            if len(raw_urls) >= 50 and ratio > MAX_SPAM_RATIO:
                src.enabled = False
                src.error = f"disabled: {ratio:.0%} open-redirect spam"
                log.warning("seed %s disabled - %.0f%% spam", src.label or src.url, ratio * 100)
                continue

            digest = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
            if digest == src.last_hash:
                src.error = None
                continue

            urls = extract_urls(text)
            seen, created = add_opportunities(
                session, urls, source="github",
                source_detail=src.label or src.url, kind_hint=src.kind_hint,
            )
            src.last_hash, src.urls_found, src.urls_new, src.error = digest, seen, created, None
            seen_total += seen
            new_total += created
            log.info("seed %s -> %s urls, %s new", src.label or src.url, seen, created)
            time.sleep(0.3)

    session.flush()
    return seen_total, new_total


def _fetch_source(client: httpx.Client, src: SeedSource) -> str | None:
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

    if (src.error or "").startswith("HTTP 404"):
        src.enabled = False
    return None


def _raw_links(text: str) -> list[str]:
    return URL_RE.findall(text or "")
