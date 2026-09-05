"""Pluggable domain-authority providers.

The engine only needs one number it can sort by (`page_rank`, 0-10). Everything
else a provider can fill in is a bonus, so a free backend and a paid one are
interchangeable.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class DomainMetric:
    root_domain: str
    page_rank: float | None = None        # 0-10, higher is stronger
    global_rank: int | None = None        # position, lower is stronger
    domain_rating: float | None = None
    backlinks: int | None = None
    referring_domains: int | None = None
    organic_traffic: int | None = None
    spam_score: float | None = None
    raw: dict = field(default_factory=dict)
    error: str | None = None


class MetricsProvider:
    name = "base"
    batch_size = 1
    daily_quota = 0  # 0 = unknown/unlimited

    def fetch(self, domains: list[str]) -> list[DomainMetric]:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        pass


class NullMetricsProvider(MetricsProvider):
    """No key configured: the pipeline keeps running, metrics stay empty."""

    name = "none"
    batch_size = 100

    def fetch(self, domains: list[str]) -> list[DomainMetric]:
        return []
