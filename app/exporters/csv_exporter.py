"""CSV exports of the master database."""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.db.models import STATUS_LIVE, STATUS_REDIRECT, Backlink, Opportunity, Prospect

log = logging.getLogger(__name__)

COLUMNS = [
    "url", "domain", "root_domain", "kind", "submission_url", "status",
    "http_status", "score", "page_rank", "title", "source", "source_detail",
    "first_seen", "last_checked", "used",
]
BACKLINK_COLUMNS = [
    "source_url", "target_url", "anchor_found", "anchor_expected", "status",
    "is_dofollow", "rel", "http_status", "project", "first_seen_live",
    "lost_at", "last_checked",
]
PROSPECT_COLUMNS = [
    "root_domain", "url", "kind", "email", "contact_page", "status",
    "page_rank", "score", "project", "template", "contacted_at", "follow_ups",
]
LIVE_STATUSES = (STATUS_LIVE, STATUS_REDIRECT)


def _row(o: Opportunity) -> dict:
    return {
        "url": o.final_url or o.url,
        "domain": o.domain,
        "root_domain": o.root_domain,
        "kind": o.kind,
        "submission_url": o.submission_url or "",
        "status": o.status,
        "http_status": o.http_status or "",
        "score": o.score,
        "page_rank": o.page_rank if o.page_rank is not None else "",
        "title": (o.title or "").replace("\n", " ")[:200],
        "source": o.source,
        "source_detail": o.source_detail or "",
        "first_seen": o.first_seen.isoformat() if o.first_seen else "",
        "last_checked": o.last_checked.isoformat() if o.last_checked else "",
        "used": int(bool(o.used)),
    }


def export_all(session, out_dir: Path | None = None, min_score: float = 0.0) -> dict[str, int]:
    """Write all_live.csv plus one CSV per opportunity kind."""
    out_dir = out_dir or settings.export_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    stmt = select(Opportunity).where(
        Opportunity.status.in_(LIVE_STATUSES), Opportunity.score >= min_score
    )
    # authority gate: only applied once metrics exist, so an un-enriched
    # database still exports everything instead of silently emptying out
    if settings.min_page_rank > 0:
        stmt = stmt.where(Opportunity.page_rank >= settings.min_page_rank)

    rows = list(
        session.execute(
            stmt.order_by(
                Opportunity.page_rank.desc().nullslast(),
                Opportunity.score.desc(),
                Opportunity.root_domain.asc(),
            )
        ).scalars()
    )

    written: dict[str, int] = {}
    buckets: dict[str, list[Opportunity]] = {"all_live": rows}
    for o in rows:
        buckets.setdefault(o.kind, []).append(o)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for name, items in buckets.items():
        path = out_dir / f"{name}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            writer.writeheader()
            for o in items:
                writer.writerow(_row(o))
        written[name] = len(items)
        log.info("exported %s rows -> %s", len(items), path.name)

    (out_dir / "LAST_EXPORT.txt").write_text(
        f"{stamp} :: " + ", ".join(f"{k}={v}" for k, v in sorted(written.items())),
        encoding="utf-8",
    )
    return written


def export_backlinks(session, out_dir: Path | None = None) -> int:
    """Every tracked link you placed, with its current state."""
    out_dir = out_dir or settings.export_dir
    rows = list(session.execute(select(Backlink).order_by(Backlink.status.asc())).scalars())
    path = out_dir / "tracked_backlinks.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=BACKLINK_COLUMNS)
        w.writeheader()
        for b in rows:
            w.writerow(
                {
                    "source_url": b.source_url,
                    "target_url": b.target_url,
                    "anchor_found": b.anchor_found or "",
                    "anchor_expected": b.anchor_expected or "",
                    "status": b.status,
                    "is_dofollow": "" if b.is_dofollow is None else int(b.is_dofollow),
                    "rel": b.rel or "",
                    "http_status": b.http_status or "",
                    "project": b.project or "",
                    "first_seen_live": b.first_seen_live.isoformat() if b.first_seen_live else "",
                    "lost_at": b.lost_at.isoformat() if b.lost_at else "",
                    "last_checked": b.last_checked.isoformat() if b.last_checked else "",
                }
            )
    return len(rows)


def export_prospects(session, out_dir: Path | None = None) -> int:
    """The outreach pipeline as a spreadsheet."""
    out_dir = out_dir or settings.export_dir
    rows = list(
        session.execute(
            select(Prospect).order_by(Prospect.page_rank.desc().nullslast())
        ).scalars()
    )
    path = out_dir / "prospects.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=PROSPECT_COLUMNS)
        w.writeheader()
        for p in rows:
            w.writerow(
                {
                    "root_domain": p.root_domain,
                    "url": p.url,
                    "kind": p.kind,
                    "email": p.email or "",
                    "contact_page": p.contact_page or "",
                    "status": p.status,
                    "page_rank": p.page_rank if p.page_rank is not None else "",
                    "score": p.score,
                    "project": p.project or "",
                    "template": p.template or "",
                    "contacted_at": p.contacted_at.isoformat() if p.contacted_at else "",
                    "follow_ups": p.follow_ups,
                }
            )
    return len(rows)
