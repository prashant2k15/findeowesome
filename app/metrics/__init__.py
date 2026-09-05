from __future__ import annotations

from app.config import settings
from app.metrics.base import DomainMetric, MetricsProvider, NullMetricsProvider


def get_metrics_provider(name: str | None = None) -> MetricsProvider:
    name = (name or settings.metrics_provider or "none").lower()
    if name == "openpagerank":
        from app.metrics.openpagerank import OpenPageRankProvider

        return OpenPageRankProvider()
    if name == "dataforseo":
        from app.metrics.dataforseo import DataForSeoProvider

        return DataForSeoProvider()
    return NullMetricsProvider()


__all__ = ["get_metrics_provider", "MetricsProvider", "DomainMetric", "NullMetricsProvider"]
