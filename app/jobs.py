"""Every unit of scheduled work, wrapped with audit logging + alerting.

Each job takes a Session and returns (processed, created, message). The
`run_job` wrapper records a JobRun row so the dashboard and the Telegram
report can show worker health without any extra plumbing.
"""
from __future__ import annotations

import json
import logging
import traceback
from typing import Callable

import yaml
from sqlalchemy import delete, select

from app.checkers.live_checker import check_batch
from app.collectors.footprint_collector import run_footprints
from app.collectors.github_collector import discover_lists, harvest_sources
from app.collectors.list_importer import import_local_files
from app.config import settings
from app.db.models import STATUS_DEAD, Opportunity
from app.db.repo import (
    finish_job,
    recent_jobs,
    start_job,
    stats,
    sync_footprints,
    sync_seed_sources,
)
from app.db.session import session_scope
from app.exporters.csv_exporter import export_all, export_backlinks, export_prospects
from app.metrics.enrich import enrich as metrics_enrich
from app.outreach.campaign import (
    build_prospects,
    draft_messages,
    find_contacts,
    schedule_follow_ups,
    send_queue,
)
from app.trackers.backlink_tracker import verify_batch
from app.notify import telegram
from app.processors.url_cleaner import is_spam_url, normalize_url

log = logging.getLogger(__name__)

JobFn = Callable[..., tuple[int, int, str]]


def run_job(name: str, fn: JobFn) -> dict:
    """Execute a job in its own transaction, recording success or failure."""
    log.info("job %s starting", name)
    result = {"job": name, "ok": True, "processed": 0, "created": 0, "message": ""}
    with session_scope() as session:
        run = start_job(session, name)
        session.commit()
        try:
            processed, created, message = fn(session)
            finish_job(session, run, True, processed, created, message)
            result.update(processed=processed, created=created, message=message)
            log.info("job %s done: %s processed, %s new (%s)", name, processed, created, message)
        except Exception:
            tb = traceback.format_exc()
            session.rollback()
            run = session.merge(run)
            finish_job(session, run, False, 0, 0, tb)
            result.update(ok=False, message=tb)
            log.error("job %s failed:\n%s", name, tb)
            telegram.alert(name, tb)
    return result


# --- individual jobs -----------------------------------------------------


def job_sync_config(session) -> tuple[int, int, str]:
    """Load config/seed_sources.yaml + config/footprints.json into the DB."""
    seeds_path = settings.config_dir / "seed_sources.yaml"
    fp_path = settings.config_dir / "footprints.json"

    seeds_added = fps_added = 0
    if seeds_path.exists():
        data = yaml.safe_load(seeds_path.read_text(encoding="utf-8")) or {}
        seeds_added = sync_seed_sources(session, data.get("sources", []))
    if fp_path.exists():
        data = json.loads(fp_path.read_text(encoding="utf-8"))
        entries = [
            {"query": q, "kind": group.get("kind", "unknown")}
            for group in data.get("groups", [])
            for q in _expand(group)
        ]
        fps_added = sync_footprints(session, entries)

    return seeds_added + fps_added, seeds_added + fps_added, (
        f"{seeds_added} seed sources, {fps_added} footprints added"
    )


def _expand(group: dict) -> list[str]:
    """Expand a footprint template across its modifier list."""
    templates = group.get("templates", [])
    modifiers = group.get("modifiers", [""])
    out = []
    for t in templates:
        if "{mod}" in t:
            out.extend(t.replace("{mod}", m).strip() for m in modifiers)
        else:
            out.append(t)
    return [q for q in dict.fromkeys(out) if q]


def job_github_discover(session) -> tuple[int, int, str]:
    added = discover_lists(session)
    return added, added, f"{added} new GitHub list repos registered"


def job_github_harvest(session) -> tuple[int, int, str]:
    seen, new = harvest_sources(session, limit=40)
    return seen, new, f"{seen} URLs seen, {new} new"


def job_footprints(session) -> tuple[int, int, str]:
    seen, new = run_footprints(session, batch=25, pages=2)
    return seen, new, f"{seen} search results, {new} new"


def job_import(session) -> tuple[int, int, str]:
    seen, new = import_local_files(session)
    return seen, new, f"{seen} URLs imported, {new} new"


def job_check(session) -> tuple[int, int, str]:
    summary = check_batch(session)
    return (
        summary["checked"],
        summary["live"],
        f"{summary['checked']} checked / {summary['live']} live / "
        f"{summary['dead']} dead / {summary['blocked']} blocked",
    )


def job_export(session) -> tuple[int, int, str]:
    written = export_all(session)
    written["tracked_backlinks"] = export_backlinks(session)
    written["prospects"] = export_prospects(session)
    total = written.get("all_live", 0)
    return total, 0, ", ".join(f"{k}={v}" for k, v in sorted(written.items()))


def job_cleanup(session) -> tuple[int, int, str]:
    """Drop URLs that failed repeatedly and are worthless to keep."""
    doomed = session.execute(
        select(Opportunity.id).where(
            Opportunity.status == STATUS_DEAD,
            Opportunity.fail_count >= 5,
            Opportunity.check_count >= 3,
        )
    ).scalars().all()
    if doomed:
        session.execute(delete(Opportunity).where(Opportunity.id.in_(doomed)))
    return len(doomed), 0, f"{len(doomed)} dead URLs purged"


def job_report(session) -> tuple[int, int, str]:
    s = stats(session)
    jobs = recent_jobs(session, 10)
    sent = telegram.send(telegram.daily_report(s, jobs))
    return s["total"], s["new_today"], f"report {'sent' if sent else 'skipped (no token)'}"


def job_purge_junk(session) -> tuple[int, int]:
    """Re-apply the current URL filters to everything already stored.

    Filters get stricter over time (new spam patterns, new noise domains); this
    retro-cleans rows that were imported before a rule existed.
    """
    rows = session.execute(select(Opportunity.id, Opportunity.url)).all()
    doomed = [i for i, u in rows if is_spam_url(u) or normalize_url(u) is None]
    for chunk_start in range(0, len(doomed), 500):
        chunk = doomed[chunk_start : chunk_start + 500]
        session.execute(delete(Opportunity).where(Opportunity.id.in_(chunk)))
    return len(rows), len(doomed)


def job_purge(session) -> tuple[int, int, str]:
    scanned, removed = job_purge_junk(session)
    return scanned, removed, f"{removed} junk URLs removed of {scanned} scanned"


# =========================================================================
# Module: domain quality metrics
# =========================================================================


def job_metrics(session) -> tuple[int, int, str]:
    requested, stored = metrics_enrich(session)
    return requested, stored, f"{stored} domains enriched of {requested} requested"


# =========================================================================
# Module: backlink tracker
# =========================================================================


def job_verify_backlinks(session) -> tuple[int, int, str]:
    summary = verify_batch(session)
    lost = summary.get("lost_links") or []
    if lost:
        lines = "\n".join(f"  {l['source']} -> {l['target']}" for l in lost[:15])
        telegram.send(
            f"<b>Backlinks lost: {len(lost)}</b>\n<pre>{lines}</pre>"
            "\nThese pages still load but your link is gone."
        )
    return (
        summary["checked"],
        summary["live"],
        f"{summary['checked']} checked / {summary['live']} live / "
        f"{summary['missing']} missing / {summary['unreachable']} unreachable"
        + (f" / {summary['lost']} NEWLY LOST" if summary.get("lost") else ""),
    )


# =========================================================================
# Module: outreach
# =========================================================================


def job_build_prospects(session) -> tuple[int, int, str]:
    scanned, created = build_prospects(session, limit=100)
    return scanned, created, f"{created} new prospects from {scanned} opportunities"


def job_find_contacts(session) -> tuple[int, int, str]:
    checked, found = find_contacts(session, limit=40)
    return checked, found, f"{found} contacts found of {checked} prospects"


def job_draft_outreach(session) -> tuple[int, int, str]:
    considered, drafted = draft_messages(session, limit=50)
    follow_considered, follow_drafted = schedule_follow_ups(session, limit=50)
    return (
        considered + follow_considered,
        drafted + follow_drafted,
        f"{drafted} first drafts, {follow_drafted} follow-ups",
    )


def job_send_outreach(session) -> tuple[int, int, str]:
    if not settings.outreach_enabled:
        return 0, 0, "outreach disabled (OUTREACH_ENABLED=false)"
    summary = send_queue(session)
    return (
        summary["eligible"],
        summary["sent"],
        f"{summary['sent']} sent, {summary['dry_run']} dry-run, "
        f"{summary['blocked']} blocked, {summary['failed']} failed",
    )
