"""Command line interface - `blf <command>` (also: python -m app.cli).

Useful both locally and inside the container:
    docker compose exec worker python -m app.cli stats
"""
from __future__ import annotations

import logging

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from app import jobs
from app.collectors.list_importer import import_remote_list
from app.db.models import Opportunity
from app.db.repo import add_opportunities, recent_jobs, stats
from app.db.session import init_db, session_scope

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


if __name__ == "__main__":
    app()
