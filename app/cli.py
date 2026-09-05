"""Command line interface - `blf <command>` (also: python -m app.cli).

Useful both locally and inside the container:
    docker compose exec worker python -m app.cli stats
"""
from __future__ import annotations

import logging

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from app import jobs
from app.collectors.list_importer import import_remote_list
from app.db.models import Backlink, Opportunity, OutreachMessage, Prospect
from app.db.repo import add_opportunities, recent_jobs, stats
from app.db.session import init_db, session_scope
from app.metrics.enrich import enrich as metrics_enrich
from app.outreach.campaign import (
    build_prospects,
    draft_messages,
    find_contacts,
    mark_replied,
    schedule_follow_ups,
    send_queue,
)
from app.outreach.mailer import dry_run, remaining_today, suppress
from app.outreach.templates import needs_editing, template_names
from app.trackers.backlink_tracker import add_backlink, import_rows, verify_batch

app = typer.Typer(add_completion=False, help="BingLinkFinder - backlink opportunity engine")
console = Console()

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")


@app.command("init")
def cmd_init() -> None:
    """Create tables and load config/ into the database."""
    init_db()
    jobs.run_job("sync_config", jobs.job_sync_config)
    console.print("[green]database initialised[/green]")


@app.command("sync-config")
def cmd_sync_config() -> None:
    """Re-read config/footprints.json and config/seed_sources.yaml."""
    jobs.run_job("sync_config", jobs.job_sync_config)


@app.command("discover")
def cmd_discover() -> None:
    """Search GitHub for new public backlink lists."""
    jobs.run_job("github_discover", jobs.job_github_discover)


@app.command("harvest")
def cmd_harvest() -> None:
    """Fetch registered seed lists and extract URLs."""
    jobs.run_job("github_harvest", jobs.job_github_harvest)


@app.command("footprints")
def cmd_footprints() -> None:
    """Run the next batch of search footprints."""
    jobs.run_job("footprints", jobs.job_footprints)


@app.command("check")
def cmd_check(limit: int = typer.Option(None, help="URLs to check in this batch")) -> None:
    """Validate + classify a batch of URLs."""
    if limit:
        from app.checkers.live_checker import check_batch

        with session_scope() as s:
            console.print(check_batch(s, limit=limit))
        return
    jobs.run_job("check", jobs.job_check)


@app.command("export")
def cmd_export() -> None:
    """Write CSV exports to exports/."""
    jobs.run_job("export", jobs.job_export)


@app.command("report")
def cmd_report() -> None:
    """Send the Telegram digest now."""
    jobs.run_job("report", jobs.job_report)


@app.command("import-url")
def cmd_import_url(url: str, kind: str = typer.Option(None, help="kind hint")) -> None:
    """Import a remote list of URLs (raw text/markdown/CSV)."""
    with session_scope() as s:
        seen, new = import_remote_list(s, url, kind)
    console.print(f"[green]{seen} URLs seen, {new} new[/green]")


@app.command("add")
def cmd_add(urls: list[str]) -> None:
    """Add one or more URLs manually."""
    with session_scope() as s:
        seen, new = add_opportunities(s, list(urls), source="manual")
    console.print(f"[green]{seen} seen, {new} added[/green]")


@app.command("purge-junk")
def cmd_purge_junk() -> None:
    """Re-apply URL filters to stored rows and delete anything that now fails."""
    jobs.run_job("purge", jobs.job_purge)


@app.command("doctor")
def cmd_doctor(
    offline: bool = typer.Option(False, "--offline", help="skip live network probes")
) -> None:
    """Check every dependency that can fail silently. Run this after deploying."""
    from app import doctor

    checks = doctor.run_all(probe_network=not offline)
    t = Table(title="BingLinkFinder preflight")
    t.add_column("check")
    t.add_column("status")
    t.add_column("detail")
    colour = {doctor.OK: "green", doctor.WARN: "yellow", doctor.FAIL: "red"}
    for c in checks:
        t.add_row(c.name, f"[{colour[c.status]}]{c.status.upper()}[/{colour[c.status]}]", c.detail)
    console.print(t)

    failures = [c for c in checks if c.status == doctor.FAIL]
    if failures:
        console.print(
            f"\n[red]{len(failures)} check(s) failed[/red] - discovery may not be working"
        )
        raise typer.Exit(1)
    console.print("\n[green]all checks passed[/green]")


@app.command("stats")
def cmd_stats() -> None:
    """Show database and worker health."""
    with session_scope() as s:
        data = stats(s)
        runs = recent_jobs(s, 8)

        t = Table(title="Master database", show_header=False)
        t.add_row("Total URLs", f"{data['total']:,}")
        t.add_row("Distinct domains", f"{data['domains']:,}")
        t.add_row("New (24h)", f"{data['new_today']:,}")
        t.add_row("Live", f"{data['live']:,}")
        t.add_row("Dead", f"{data['dead']:,}")
        t.add_row("Blocked", f"{data['blocked']:,}")
        t.add_row("Pending check", f"{data['pending']:,}")
        console.print(t)

        k = Table(title="Live by kind")
        k.add_column("kind")
        k.add_column("count", justify="right")
        for kind, count in sorted(data["by_kind"].items(), key=lambda x: -x[1]):
            k.add_row(kind, f"{count:,}")
        console.print(k)

        j = Table(title="Recent jobs")
        for col in ("job", "started", "ok", "processed", "new", "message"):
            j.add_column(col)
        for r in runs:
            j.add_row(
                r.job,
                r.started_at.strftime("%m-%d %H:%M"),
                "yes" if r.ok else ("running" if r.ok is None else "NO"),
                str(r.processed),
                str(r.created),
                (r.message or "").splitlines()[-1][:60] if r.message else "",
            )
        console.print(j)


@app.command("list")
def cmd_list(
    kind: str = typer.Option(None, help="filter by kind"),
    status: str = typer.Option("live", help="filter by status"),
    limit: int = typer.Option(25),
) -> None:
    """Print top opportunities from the database."""
    with session_scope() as s:
        stmt = select(Opportunity).order_by(Opportunity.score.desc()).limit(limit)
        if kind:
            stmt = stmt.where(Opportunity.kind == kind)
        if status:
            stmt = stmt.where(Opportunity.status == status)
        rows = list(s.execute(stmt).scalars())

    t = Table(show_lines=False)
    for col in ("score", "kind", "url", "submission"):
        t.add_column(col)
    for o in rows:
        t.add_row(f"{o.score:.2f}", o.kind, o.url[:70], (o.submission_url or "")[:50])
    console.print(t)


@app.command("run-all")
def cmd_run_all() -> None:
    """One full cycle: harvest -> footprints -> check -> export (handy for cron)."""
    for name, fn in (
        ("github_harvest", jobs.job_github_harvest),
        ("footprints", jobs.job_footprints),
        ("check", jobs.job_check),
        ("export", jobs.job_export),
    ):
        jobs.run_job(name, fn)


# =========================================================================
# Domain authority metrics
# =========================================================================


@app.command("metrics")
def cmd_metrics(limit: int = typer.Option(None, help="domains to enrich")) -> None:
    """Fetch domain authority for stored opportunities."""
    if limit:
        with session_scope() as s:
            requested, stored = metrics_enrich(s, limit=limit)
        console.print(f"[green]{stored} domains enriched of {requested} requested[/green]")
        return
    jobs.run_job("metrics", jobs.job_metrics)


# =========================================================================
# Backlink tracker: links YOU placed
# =========================================================================

link_app = typer.Typer(help="Track the links you have placed")
app.add_typer(link_app, name="link")


@link_app.command("add")
def cmd_link_add(
    source_url: str = typer.Argument(..., help="page that holds your link"),
    target_url: str = typer.Argument(..., help="your page it points to"),
    anchor: str = typer.Option(None, help="expected anchor text"),
    project: str = typer.Option(None, help="group links under a project"),
) -> None:
    """Register a placed link so it gets monitored."""
    with session_scope() as s:
        row = add_backlink(s, source_url, target_url, anchor=anchor, project=project)
    if row is None:
        console.print("[red]could not parse those URLs[/red]")
        raise typer.Exit(1)
    console.print(f"[green]tracking #{row.id}[/green] {row.source_url} -> {row.target_url}")


@link_app.command("import")
def cmd_link_import(
    path: str = typer.Argument(..., help="CSV with source_url,target_url[,anchor,project]"),
    project: str = typer.Option(None),
) -> None:
    """Bulk-register links from a CSV file."""
    import csv as _csv

    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(_csv.DictReader(fh))
    with session_scope() as s:
        seen, added = import_rows(s, rows, project=project)
    console.print(f"[green]{seen} rows read, {added} newly tracked[/green]")


@link_app.command("check")
def cmd_link_check(limit: int = typer.Option(None)) -> None:
    """Verify tracked links now."""
    if limit:
        with session_scope() as s:
            console.print(verify_batch(s, limit=limit))
        return
    jobs.run_job("verify_backlinks", jobs.job_verify_backlinks)


@link_app.command("list")
def cmd_link_list(
    status: str = typer.Option(None, help="pending|live|missing|unreachable"),
    project: str = typer.Option(None),
    limit: int = typer.Option(40),
) -> None:
    """Show tracked links and their current state."""
    with session_scope() as s:
        stmt = select(Backlink).order_by(Backlink.last_checked.desc().nullsfirst()).limit(limit)
        if status:
            stmt = stmt.where(Backlink.status == status)
        if project:
            stmt = stmt.where(Backlink.project == project)
        rows = list(s.execute(stmt).scalars())

    t = Table(title="Tracked backlinks")
    for col in ("id", "status", "follow", "source", "target", "anchor", "checked"):
        t.add_column(col)
    for b in rows:
        follow = "-" if b.is_dofollow is None else ("dofollow" if b.is_dofollow else "nofollow")
        colour = {"live": "green", "missing": "red", "unreachable": "yellow"}.get(b.status, "white")
        t.add_row(
            str(b.id),
            f"[{colour}]{b.status}[/{colour}]",
            follow,
            b.source_url[:44],
            b.target_url[:34],
            (b.anchor_found or b.anchor_expected or "")[:24],
            b.last_checked.strftime("%m-%d %H:%M") if b.last_checked else "never",
        )
    console.print(t)


@link_app.command("lost")
def cmd_link_lost() -> None:
    """Links that were live and have since disappeared."""
    with session_scope() as s:
        rows = list(
            s.execute(
                select(Backlink).where(Backlink.lost_at.isnot(None)).order_by(Backlink.lost_at.desc())
            ).scalars()
        )
    if not rows:
        console.print("[green]no lost links[/green]")
        return
    t = Table(title="Lost links")
    for col in ("source", "target", "lost at", "project"):
        t.add_column(col)
    for b in rows:
        t.add_row(b.source_url[:56], b.target_url[:40], b.lost_at.strftime("%Y-%m-%d"), b.project or "")
    console.print(t)


# =========================================================================
# Outreach
# =========================================================================

out_app = typer.Typer(help="Prospecting and email outreach")
app.add_typer(out_app, name="outreach")


@out_app.command("prospects")
def cmd_out_prospects(
    limit: int = typer.Option(100),
    project: str = typer.Option(None),
    min_pr: float = typer.Option(None, help="minimum Open PageRank"),
) -> None:
    """Promote verified opportunities into the outreach pipeline."""
    with session_scope() as s:
        scanned, created = build_prospects(s, limit=limit, project=project, min_page_rank=min_pr)
    console.print(f"[green]{created} new prospects[/green] from {scanned} opportunities")


@out_app.command("contacts")
def cmd_out_contacts(limit: int = typer.Option(40)) -> None:
    """Find an email address for pending prospects."""
    with session_scope() as s:
        checked, found = find_contacts(s, limit=limit)
    console.print(f"[green]{found} contacts found[/green] of {checked} prospects")


@out_app.command("draft")
def cmd_out_draft(
    limit: int = typer.Option(50),
    project: str = typer.Option(None),
    template: str = typer.Option(None, help=f"one of the configured templates"),
) -> None:
    """Write first emails plus any due follow-ups."""
    with session_scope() as s:
        considered, drafted = draft_messages(s, limit=limit, project=project, template=template)
        fc, fd = schedule_follow_ups(s, limit=limit)
    console.print(f"[green]{drafted} drafts[/green] ({considered} ready), {fd} follow-ups ({fc} due)")


@out_app.command("review")
def cmd_out_review(limit: int = typer.Option(10), show_body: bool = typer.Option(False)) -> None:
    """Read the drafts waiting for your approval."""
    with session_scope() as s:
        msgs = list(
            s.execute(
                select(OutreachMessage)
                .where(OutreachMessage.sent.is_(False))
                .order_by(OutreachMessage.approved.asc(), OutreachMessage.created_at.asc())
                .limit(limit)
            ).scalars()
        )
        if not msgs:
            console.print("[yellow]no pending drafts[/yellow]")
            return
        for m in msgs:
            p = s.get(Prospect, m.prospect_id)
            flag = "[green]approved[/green]" if m.approved else "[yellow]needs approval[/yellow]"
            edit = " [red](still has placeholders)[/red]" if needs_editing(m.body) else ""
            console.print(
                f"\n[bold]#{m.id}[/bold] {flag}{edit}  seq={m.sequence}  "
                f"to {m.to_email}  ({p.root_domain if p else '?'}, PR "
                f"{p.page_rank if p and p.page_rank is not None else '-'})"
            )
            # markup=False: drafts contain [bracketed instructions] that rich
            # would otherwise parse as markup tags and silently swallow
            console.print(f"  subject: {m.subject}", markup=False)
            if show_body:
                console.print("  " + m.body.replace("\n", "\n  "), markup=False)


@out_app.command("approve")
def cmd_out_approve(
    ids: list[int] = typer.Argument(None, help="message ids"),
    all_clean: bool = typer.Option(False, "--all-clean", help="approve every draft with no placeholders left"),
) -> None:
    """Approve drafts for sending."""
    with session_scope() as s:
        if all_clean:
            msgs = list(
                s.execute(
                    select(OutreachMessage).where(
                        OutreachMessage.sent.is_(False), OutreachMessage.approved.is_(False)
                    )
                ).scalars()
            )
            targets = [m for m in msgs if not needs_editing(m.body)]
        else:
            targets = [s.get(OutreachMessage, i) for i in (ids or [])]
            targets = [m for m in targets if m]

        blocked = [m.id for m in targets if needs_editing(m.body)]
        approved = 0
        for m in targets:
            if needs_editing(m.body):
                continue
            m.approved = True
            approved += 1

    console.print(f"[green]{approved} approved[/green]")
    if blocked:
        console.print(f"[red]skipped (edit the placeholders first): {blocked}[/red]")


@out_app.command("send")
def cmd_out_send(limit: int = typer.Option(None)) -> None:
    """Send approved drafts (respects every safety gate)."""
    from app.config import settings as _s

    if not _s.outreach_enabled:
        console.print("[red]OUTREACH_ENABLED is false - nothing will be sent[/red]")
    with session_scope() as s:
        console.print(send_queue(s, limit=limit))


@out_app.command("status")
def cmd_out_status() -> None:
    """Pipeline overview."""
    with session_scope() as s:
        counts = dict(
            s.execute(select(Prospect.status, func.count()).group_by(Prospect.status)).all()
        )
        drafts = s.scalar(select(func.count(OutreachMessage.id)).where(OutreachMessage.sent.is_(False))) or 0
        approved = (
            s.scalar(
                select(func.count(OutreachMessage.id)).where(
                    OutreachMessage.sent.is_(False), OutreachMessage.approved.is_(True)
                )
            )
            or 0
        )
        sent = s.scalar(select(func.count(OutreachMessage.id)).where(OutreachMessage.sent.is_(True))) or 0
        left = remaining_today(s)

    t = Table(title="Outreach pipeline", show_header=False)
    for status, count in sorted(counts.items(), key=lambda x: -x[1]):
        t.add_row(status, str(count))
    t.add_row("---", "---")
    t.add_row("drafts waiting", str(drafts))
    t.add_row("  of which approved", str(approved))
    t.add_row("messages sent", str(sent))
    t.add_row("send budget left today", str(left))
    console.print(t)
    if dry_run():
        console.print("[yellow]dry-run mode: no SMTP configured, nothing actually leaves[/yellow]")


@out_app.command("suppress")
def cmd_out_suppress(value: str, reason: str = typer.Option("manual")) -> None:
    """Never contact this email or domain again."""
    with session_scope() as s:
        added = suppress(s, value, reason)
    console.print("[green]suppressed[/green]" if added else "[yellow]already suppressed[/yellow]")


@out_app.command("replied")
def cmd_out_replied(value: str) -> None:
    """Mark a prospect as replied - stops all follow-ups."""
    with session_scope() as s:
        ok = mark_replied(s, value)
    console.print("[green]marked replied[/green]" if ok else "[red]prospect not found[/red]")


@out_app.command("templates")
def cmd_out_templates() -> None:
    """List configured templates."""
    console.print("\n".join(f"  {n}" for n in template_names()))


if __name__ == "__main__":
    app()
