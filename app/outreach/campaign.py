"""The outreach pipeline: prospect -> contact -> draft -> approve -> send -> follow up.

Nothing here sends on its own initiative. Drafting is automatic, sending is
gated (see mailer), and the default configuration writes drafts that a human
approves. That ordering is the point: the bottleneck should be your judgement,
not your typing.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import (
    KIND_ARTICLE,
    KIND_DIRECTORY,
    KIND_FORUM,
    KIND_QA,
    KIND_WEB2,
    PROSPECT_CONTACTED,
    PROSPECT_NEW,
    PROSPECT_NO_CONTACT,
    PROSPECT_QUEUED,
    PROSPECT_READY,
    PROSPECT_REPLIED,
    STATUS_LIVE,
    STATUS_REDIRECT,
    Opportunity,
    OutreachMessage,
    Prospect,
    utcnow,
)
from app.outreach import contact_finder, mailer, templates
from app.processors.url_cleaner import host_of, root_domain

log = logging.getLogger(__name__)

# Which opportunity kinds deserve a human email rather than a form
OUTREACH_KINDS = (KIND_ARTICLE, KIND_WEB2, KIND_FORUM, KIND_QA, KIND_DIRECTORY)

DEFAULT_TEMPLATE_BY_KIND = {
    KIND_ARTICLE: "guest_post",
    KIND_WEB2: "guest_post",
    KIND_FORUM: "resource_page",
    KIND_QA: "resource_page",
    KIND_DIRECTORY: "listing",
}


def build_prospects(
    session: Session,
    limit: int = 100,
    project: str | None = None,
    min_page_rank: float | None = None,
    kinds: tuple[str, ...] = OUTREACH_KINDS,
) -> tuple[int, int]:
    """Promote the best verified opportunities into the outreach pipeline."""
    threshold = settings.outreach_min_page_rank if min_page_rank is None else min_page_rank

    # An authority floor is meaningless before any metrics have been fetched,
    # and applying it anyway would silently build zero prospects. Metrics are an
    # optional enrichment, so they must never be able to stall the pipeline.
    if threshold > 0:
        have_metrics = session.execute(
            select(Opportunity.id).where(Opportunity.page_rank.isnot(None)).limit(1)
        ).first()
        if not have_metrics:
            log.warning(
                "outreach_min_page_rank=%s ignored: no domain metrics fetched yet "
                "(set METRICS_PROVIDER + key, or leave the floor at 0)",
                threshold,
            )
            threshold = 0.0

    existing = {d for (d,) in session.execute(select(Prospect.root_domain)).all()}

    stmt = (
        select(Opportunity)
        .where(
            Opportunity.status.in_([STATUS_LIVE, STATUS_REDIRECT]),
            Opportunity.kind.in_(kinds),
        )
        .order_by(Opportunity.page_rank.desc().nullslast(), Opportunity.score.desc())
        .limit(limit * 4)
    )

    created = 0
    scanned = 0
    for opp in session.execute(stmt).scalars():
        scanned += 1
        if created >= limit:
            break
        if opp.root_domain in existing:
            continue
        # unknown authority is allowed through only when no threshold is set
        if threshold > 0 and (opp.page_rank is None or opp.page_rank < threshold):
            continue

        session.add(
            Prospect(
                root_domain=opp.root_domain,
                url=opp.final_url or opp.url,
                kind=opp.kind,
                project=project,
                template=DEFAULT_TEMPLATE_BY_KIND.get(opp.kind, "guest_post"),
                page_rank=opp.page_rank,
                score=opp.score,
                site_title=opp.title,
                opportunity_id=opp.id,
                status=PROSPECT_NEW,
            )
        )
        existing.add(opp.root_domain)
        created += 1

    session.flush()
    return scanned, created


def find_contacts(session: Session, limit: int = 50, delay: float | None = None) -> tuple[int, int]:
    """Look up an email for prospects that do not have one yet."""
    delay = settings.per_domain_delay if delay is None else delay
    rows = list(
        session.execute(
            select(Prospect)
            .where(Prospect.status == PROSPECT_NEW)
            .order_by(Prospect.page_rank.desc().nullslast(), Prospect.score.desc())
            .limit(limit)
        ).scalars()
    )
    if not rows:
        return 0, 0

    found = 0
    with contact_finder.make_client() as client:
        for p in rows:
            result = contact_finder.find_contact(client, p.url)
            p.emails_all = result["emails"]
            p.contact_page = result["contact_page"]
            if result["title"]:
                p.site_title = result["title"][:255]

            if result["email"]:
                p.email = result["email"][:255]
                p.status = PROSPECT_READY
                found += 1
            else:
                p.status = PROSPECT_NO_CONTACT
                p.notes = result["error"] or "no contact address found"
            p.last_touch_at = utcnow()
            session.flush()
            time.sleep(delay)

    return len(rows), found


def draft_messages(
    session: Session, limit: int = 50, project: str | None = None, template: str | None = None
) -> tuple[int, int]:
    """Write the first email for every ready prospect."""
    stmt = select(Prospect).where(Prospect.status == PROSPECT_READY, Prospect.email.isnot(None))
    if project:
        stmt = stmt.where(Prospect.project == project)
    rows = list(
        session.execute(
            stmt.order_by(Prospect.page_rank.desc().nullslast()).limit(limit)
        ).scalars()
    )

    drafted = 0
    for p in rows:
        already = session.execute(
            select(OutreachMessage.id).where(
                OutreachMessage.prospect_id == p.id, OutreachMessage.sequence == 0
            )
        ).first()
        if already:
            continue

        name = template or p.template or "guest_post"
        ctx = templates.context_for(
            project or p.project,
            site_name=p.site_title or p.root_domain,
            domain=p.root_domain,
            url=p.url,
            contact_page=p.contact_page,
            page_rank=p.page_rank,
            name_suffix=f" {p.contact_name}" if p.contact_name else "",
        )
        subject, body, warnings = templates.render(name, ctx)

        session.add(
            OutreachMessage(
                prospect_id=p.id,
                sequence=0,
                to_email=p.email,
                subject=subject,
                body=body,
                template=name,
                # a draft still holding [instructions] can never auto-approve
                approved=not settings.outreach_require_approval
                and not templates.needs_editing(body),
            )
        )
        p.status = PROSPECT_QUEUED
        p.template = name
        if warnings:
            p.notes = "; ".join(warnings)[:1000]
        drafted += 1

    session.flush()
    return len(rows), drafted


def send_queue(session: Session, limit: int | None = None) -> dict:
    """Send approved, unsent drafts within the daily limit."""
    budget = mailer.remaining_today(session)
    limit = min(limit or budget, budget)
    summary = {"eligible": 0, "sent": 0, "dry_run": 0, "blocked": 0, "failed": 0}
    if limit <= 0:
        summary["blocked"] = 1
        return summary

    msgs = list(
        session.execute(
            select(OutreachMessage)
            .where(OutreachMessage.sent.is_(False), OutreachMessage.approved.is_(True))
            .order_by(OutreachMessage.created_at.asc())
            .limit(limit)
        ).scalars()
    )
    summary["eligible"] = len(msgs)

    for msg in msgs:
        prospect = session.get(Prospect, msg.prospect_id)
        try:
            really_sent = mailer.send(session, msg)
        except mailer.SendBlocked as exc:
            log.info("send blocked: %s", exc)
            summary["blocked"] += 1
            continue
        except Exception:
            summary["failed"] += 1
            continue

        summary["sent" if really_sent else "dry_run"] += 1
        if prospect:
            prospect.status = PROSPECT_CONTACTED
            prospect.contacted_at = prospect.contacted_at or utcnow()
            prospect.last_touch_at = utcnow()
            if msg.sequence > 0:
                prospect.follow_ups = msg.sequence
        session.flush()

    return summary


def schedule_follow_ups(session: Session, limit: int = 50) -> tuple[int, int]:
    """Draft the next follow-up for silent prospects."""
    cutoff = utcnow() - timedelta(days=settings.outreach_follow_up_days)
    rows = list(
        session.execute(
            select(Prospect)
            .where(
                Prospect.status == PROSPECT_CONTACTED,
                Prospect.replied_at.is_(None),
                Prospect.follow_ups < settings.outreach_max_follow_ups,
                or_(Prospect.last_touch_at.is_(None), Prospect.last_touch_at < cutoff),
            )
            .limit(limit)
        ).scalars()
    )

    drafted = 0
    for p in rows:
        next_seq = p.follow_ups + 1
        exists = session.execute(
            select(OutreachMessage.id).where(
                OutreachMessage.prospect_id == p.id, OutreachMessage.sequence == next_seq
            )
        ).first()
        if exists or not p.email:
            continue

        first = session.execute(
            select(OutreachMessage)
            .where(OutreachMessage.prospect_id == p.id, OutreachMessage.sequence == 0)
            .limit(1)
        ).scalar_one_or_none()

        ctx = templates.context_for(
            p.project,
            site_name=p.site_title or p.root_domain,
            domain=p.root_domain,
            url=p.url,
            original_subject=first.subject if first else "my last email",
            name_suffix=f" {p.contact_name}" if p.contact_name else "",
        )
        try:
            subject, body, _ = templates.render_follow_up(next_seq - 1, ctx)
        except templates.TemplateError as exc:
            log.warning("follow-up skipped for %s: %s", p.root_domain, exc)
            continue

        session.add(
            OutreachMessage(
                prospect_id=p.id,
                sequence=next_seq,
                to_email=p.email,
                subject=subject,
                body=body,
                template=f"follow_up_{next_seq}",
                approved=not settings.outreach_require_approval
                and not templates.needs_editing(body),
            )
        )
        drafted += 1

    session.flush()
    return len(rows), drafted


def mark_replied(session: Session, domain_or_email: str) -> bool:
    """Stop the sequence for someone who answered."""
    value = domain_or_email.strip().lower()
    p = session.execute(
        select(Prospect).where(
            or_(Prospect.email == value, Prospect.root_domain == root_domain(host_of(value) or value))
        )
    ).scalar_one_or_none()
    if not p:
        return False
    p.status = PROSPECT_REPLIED
    p.replied_at = utcnow()
    session.flush()
    return True
