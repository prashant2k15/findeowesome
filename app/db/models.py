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
