"""Import URLs from local files (imports/*.txt|csv|md) or a raw URL."""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.config import ROOT, settings
from app.db.repo import add_opportunities
from app.processors.url_cleaner import extract_urls

log = logging.getLogger(__name__)

IMPORT_DIR = ROOT / "imports"
SUFFIXES = {".txt", ".csv", ".md", ".list"}


def import_local_files(session, directory: Path | None = None) -> tuple[int, int]:
    d = directory or IMPORT_DIR
    d.mkdir(parents=True, exist_ok=True)
    seen_total = new_total = 0
    for path in sorted(d.iterdir()):
        if path.suffix.lower() not in SUFFIXES or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        seen, created = add_opportunities(
            session, extract_urls(text), source="import", source_detail=path.name
        )
        log.info("import %s -> %s urls, %s new", path.name, seen, created)
        seen_total += seen
        new_total += created
        # mark as processed so the next run does not re-read it
        path.rename(path.with_suffix(path.suffix + ".done"))
    return seen_total, new_total


def import_remote_list(session, url: str, kind_hint: str | None = None) -> tuple[int, int]:
    with httpx.Client(
        timeout=settings.request_timeout,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    ) as client:
        r = client.get(url)
        r.raise_for_status()
    return add_opportunities(
        session, extract_urls(r.text), source="import", source_detail=url, kind_hint=kind_hint
    )
