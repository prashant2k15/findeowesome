"""Database schema for the master backlink-opportunity database."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --- status values -------------------------------------------------------
STATUS_NEW = "new"           # discovered, never checked
STATUS_LIVE = "live"         # responded 2xx
STATUS_REDIRECT = "redirect"  # resolved elsewhere, final_url stored
STATUS_DEAD = "dead"         # 4xx/5xx/DNS failure past retry budget
STATUS_BLOCKED = "blocked"   # 403/429/robots-disallowed -> keep, don't hammer

# --- opportunity kinds ---------------------------------------------------
KIND_PROFILE = "profile"
KIND_DIRECTORY = "directory"
KIND_WEB2 = "web2"
KIND_BOOKMARK = "bookmark"
KIND_ARTICLE = "article"
KIND_FORUM = "forum"
KIND_QA = "qa"
KIND_UNKNOWN = "unknown"


class Opportunity(Base):
    """One discovered backlink opportunity URL."""

    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    url: Mapped[str] = mapped_column(String(2048), unique=True, index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    root_domain: Mapped[str] = mapped_column(String(255), index=True)

    kind: Mapped[str] = mapped_column(String(32), default=KIND_UNKNOWN, index=True)
    submission_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    source: Mapped[str] = mapped_column(String(64), index=True)       # github | footprint | import
    source_detail: Mapped[str | None] = mapped_column(String(512), nullable=True)

    status: Mapped[str] = mapped_column(String(16), default=STATUS_NEW, index=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)

    signals: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)

    # denormalised from domain_metrics so every export and filter stays a
    # single-table query; refreshed by the metrics job
    page_rank: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    metrics_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    check_count: Mapped[int] = mapped_column(Integer, default=0)

    used: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_opportunities_status_kind", "status", "kind"),
        Index("ix_opportunities_root_kind", "root_domain", "kind"),
    )


class SeedSource(Base):
    """A public list (GitHub repo file, gist, raw URL) we harvest URLs from."""

    __tablename__ = "seed_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(1024), unique=True)
    kind_hint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    last_fetched: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    urls_found: Mapped[int] = mapped_column(Integer, default=0)
    urls_new: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Footprint(Base):
    """A search query template used to discover new opportunities."""

    __tablename__ = "footprints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    query: Mapped[str] = mapped_column(String(512))
    kind_hint: Mapped[str] = mapped_column(String(32), default=KIND_UNKNOWN)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    last_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    results_total: Mapped[int] = mapped_column(Integer, default=0)
    results_new: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (UniqueConstraint("query", name="uq_footprint_query"),)


class JobRun(Base):
    """Health/audit trail for every scheduled job execution."""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


# =========================================================================
# Module: domain quality metrics
# =========================================================================

class DomainMetrics(Base):
    """Authority metrics for one root domain, cached and refreshed on a cycle."""

    __tablename__ = "domain_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    root_domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    # 0-10 logarithmic authority (Open PageRank) - the free signal
    page_rank: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    # global position, lower is stronger
    global_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # optional paid enrichment
    domain_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    backlinks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    referring_domains: Mapped[int | None] = mapped_column(Integer, nullable=True)
    organic_traffic: Mapped[int | None] = mapped_column(Integer, nullable=True)
    spam_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    provider: Mapped[str] = mapped_column(String(32), default="openpagerank")
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)


# =========================================================================
# Module: backlink tracker (links YOU placed)
# =========================================================================

LINK_PENDING = "pending"          # submitted, not seen live yet
LINK_LIVE = "live"                # link found on the page
LINK_MISSING = "missing"          # page loads, link is gone
LINK_UNREACHABLE = "unreachable"  # page itself failed to load


class Backlink(Base):
    """A link you placed: where it lives, where it points, is it still there."""

    __tablename__ = "backlinks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    source_url: Mapped[str] = mapped_column(String(2048), index=True)   # page holding the link
    source_domain: Mapped[str] = mapped_column(String(255), index=True)
    target_url: Mapped[str] = mapped_column(String(2048), index=True)   # your page
    target_domain: Mapped[str] = mapped_column(String(255), index=True)

    anchor_expected: Mapped[str | None] = mapped_column(String(255), nullable=True)
    anchor_found: Mapped[str | None] = mapped_column(String(255), nullable=True)
    project: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    status: Mapped[str] = mapped_column(String(16), default=LINK_PENDING, index=True)
    is_dofollow: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rel: Mapped[str | None] = mapped_column(String(128), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)

    first_seen_live: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lost_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    check_count: Mapped[int] = mapped_column(Integer, default=0)

    opportunity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("source_url", "target_url", name="uq_backlink_pair"),
        Index("ix_backlinks_project_status", "project", "status"),
    )


# =========================================================================
# Module: outreach
# =========================================================================

PROSPECT_NEW = "new"
PROSPECT_READY = "ready"          # contact found, message drafted
PROSPECT_QUEUED = "queued"        # approved, waiting to send
PROSPECT_CONTACTED = "contacted"
PROSPECT_REPLIED = "replied"
PROSPECT_WON = "won"              # link placed
PROSPECT_LOST = "lost"
PROSPECT_NO_CONTACT = "no_contact"
PROSPECT_OPTED_OUT = "opted_out"


class Prospect(Base):
    """A site worth a personal email rather than a form submission."""

    __tablename__ = "prospects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    root_domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    url: Mapped[str] = mapped_column(String(2048))

    kind: Mapped[str] = mapped_column(String(32), default=KIND_UNKNOWN)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    emails_all: Mapped[list] = mapped_column(JSON, default=list)
    contact_page: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    site_title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    project: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    template: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=PROSPECT_NEW, index=True)

    page_rank: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)

    contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_touch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    follow_ups: Mapped[int] = mapped_column(Integer, default=0)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    opportunity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class OutreachMessage(Base):
    """Every drafted/sent email - the audit trail and the follow-up chain."""

    __tablename__ = "outreach_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prospect_id: Mapped[int] = mapped_column(Integer, index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)  # 0 = first mail

    to_email: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    template: Mapped[str | None] = mapped_column(String(64), nullable=True)

    approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Suppression(Base):
    """Never contact these again: opt-outs, bounces, manual blocks."""

    __tablename__ = "suppressions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # email or domain
    reason: Mapped[str] = mapped_column(String(128), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
