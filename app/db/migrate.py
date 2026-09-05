"""Tiny additive migrations.

`Base.metadata.create_all` creates missing tables but never adds a column to an
existing one, so an upgrade would silently break a running install. Every column
added after v1 is listed here and applied with ALTER TABLE ... ADD COLUMN, which
both SQLite and PostgreSQL accept. Additive only - nothing is ever dropped.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

# table -> column -> SQL type (portable subset)
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "opportunities": {
        "page_rank": "FLOAT",
        "metrics_at": "TIMESTAMP",
    },
}


def apply(engine: Engine) -> list[str]:
    """Add any missing column. Returns what it changed, for the logs."""
    applied: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table, columns in ADDED_COLUMNS.items():
        if table not in existing_tables:
            continue  # create_all just made it with every column
        have = {c["name"] for c in inspector.get_columns(table)}
        for column, sql_type in columns.items():
            if column in have:
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
            applied.append(f"{table}.{column}")
            log.info("migration: added %s.%s", table, column)

    return applied
