"""CSV exports of the master database."""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.db.models import STATUS_LIVE, STATUS_REDIRECT, Opportunity

log = logging.getLogger(__name__)

COLUMNS = [
    "url", "domain", "root_domain", "kind", "submission_url", "status",
    "http_status", "score", "title", "source", "source_detail",
    "first_seen", "last_checked", "used",
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

    rows = list(
        session.execute(
            select(Opportunity)
            .where(Opportunity.status.in_(LIVE_STATUSES), Opportunity.score >= min_score)
            .order_by(Opportunity.score.desc(), Opportunity.root_domain.asc())
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
