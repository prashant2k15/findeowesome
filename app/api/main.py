"""FastAPI dashboard + JSON API."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select

from app import jobs
from app.config import settings
from app.db.models import Opportunity
from app.db.repo import recent_jobs, stats
from app.db.session import init_db, session_scope

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="BingLinkFinder", version="1.0.0", docs_url="/api/docs")

TRIGGERABLE = {
    "sync_config": jobs.job_sync_config,
    "github_discover": jobs.job_github_discover,
    "github_harvest": jobs.job_github_harvest,
    "footprints": jobs.job_footprints,
    "import": jobs.job_import,
    "check": jobs.job_check,
    "export": jobs.job_export,
    "cleanup": jobs.job_cleanup,
    "purge": jobs.job_purge,
    "report": jobs.job_report,
}


@app.on_event("startup")
def _startup() -> None:
    init_db()


def require_key(request: Request) -> None:
    """Optional shared-secret gate: set DASHBOARD_KEY to enable."""
    if not settings.dashboard_key:
        return
    supplied = request.query_params.get("key") or request.headers.get("X-API-Key")
    if supplied != settings.dashboard_key:
        raise HTTPException(status_code=401, detail="invalid or missing key")


@app.get("/healthz")
def healthz() -> dict:
    with session_scope() as s:
        s.execute(select(func.count(Opportunity.id)))
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _=Depends(require_key)) -> HTMLResponse:
    with session_scope() as s:
        data = stats(s)
        runs = recent_jobs(s, 10)
        top = list(
            s.execute(
                select(Opportunity)
                .where(Opportunity.status.in_(["live", "redirect"]))
                .order_by(Opportunity.score.desc(), Opportunity.first_seen.desc())
                .limit(25)
            ).scalars()
        )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": data,
            "runs": runs,
            "top": top,
            "jobs": sorted(TRIGGERABLE),
            "key": request.query_params.get("key", ""),
        },
    )


@app.get("/api/stats")
def api_stats(_=Depends(require_key)) -> dict:
    with session_scope() as s:
        data = stats(s)
        data["jobs"] = [
            {
                "job": r.job,
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "ok": r.ok,
                "processed": r.processed,
                "created": r.created,
            }
            for r in recent_jobs(s, 10)
        ]
    return data


@app.get("/api/opportunities")
def api_opportunities(
    _=Depends(require_key),
    kind: str | None = None,
    status: str | None = None,
    q: str | None = None,
    min_score: float = 0.0,
    limit: int = Query(100, le=1000),
    offset: int = 0,
) -> dict:
    with session_scope() as s:
        stmt = select(Opportunity).where(Opportunity.score >= min_score)
        if kind:
            stmt = stmt.where(Opportunity.kind == kind)
        if status:
            stmt = stmt.where(Opportunity.status == status)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Opportunity.url.ilike(like), Opportunity.title.ilike(like)))

        total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = list(
            s.execute(
                stmt.order_by(Opportunity.score.desc(), Opportunity.id.desc())
                .limit(limit)
                .offset(offset)
            ).scalars()
        )
        items = [
            {
                "id": o.id,
                "url": o.url,
                "domain": o.domain,
                "kind": o.kind,
                "status": o.status,
                "score": o.score,
                "submission_url": o.submission_url,
                "title": o.title,
                "source": o.source,
                "first_seen": o.first_seen.isoformat() if o.first_seen else None,
                "last_checked": o.last_checked.isoformat() if o.last_checked else None,
                "used": o.used,
            }
            for o in rows
        ]
    return {"total": total, "count": len(items), "items": items}


@app.post("/api/opportunities/{opp_id}/used")
def api_mark_used(opp_id: int, used: bool = True, _=Depends(require_key)) -> dict:
    with session_scope() as s:
        o = s.get(Opportunity, opp_id)
        if not o:
            raise HTTPException(404, "not found")
        o.used = used
    return {"id": opp_id, "used": used}


@app.post("/api/run/{job_name}")
def api_run_job(job_name: str, background: BackgroundTasks, _=Depends(require_key)) -> dict:
    fn = TRIGGERABLE.get(job_name)
    if not fn:
        raise HTTPException(404, f"unknown job: {job_name}")
    background.add_task(jobs.run_job, job_name, fn)
    return {"queued": job_name}


@app.get("/api/export/{name}")
def api_export_file(name: str, _=Depends(require_key)):
    if "/" in name or "\\" in name or not name.endswith(".csv"):
        raise HTTPException(400, "bad filename")
    path = settings.export_dir / name
    if not path.exists():
        return JSONResponse({"error": "not generated yet - run the export job"}, status_code=404)
    return FileResponse(path, media_type="text/csv", filename=name)
