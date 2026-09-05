"""OpenPageRank provider using the current Keywords Everywhere API."""
from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.metrics.base import DomainMetric, MetricsProvider

log = logging.getLogger(__name__)

ENDPOINT = "https://openpagerank.keywordseverywhere.com/v1/domains/bulk"


class OpenPageRankProvider(MetricsProvider):
    name = "openpagerank"
    batch_size = 100

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.openpagerank_api_key
        self.client = httpx.Client(
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent},
        )

    def fetch(self, domains: list[str]) -> list[DomainMetric]:
        if not self.api_key:
            log.warning("OPENPAGERANK_API_KEY missing; metrics job idle")
            return []
        if not domains:
            return []

        out: list[DomainMetric] = []
        for start in range(0, len(domains), self.batch_size):
            chunk = domains[start : start + self.batch_size]
            try:
                response = self.client.post(
                    ENDPOINT,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"domains": chunk, "include_history": False},
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                log.warning("openpagerank batch failed: %s", exc)
                out.extend(DomainMetric(d, error=str(exc)[:200]) for d in chunk)
                continue

            for item in payload.get("results", []):
                domain = (item.get("domain") or "").lower()
                if not domain:
                    continue
                if not item.get("found", False):
                    out.append(DomainMetric(domain, error="domain not found", raw=item))
                    continue
                out.append(
                    DomainMetric(
                        root_domain=domain,
                        page_rank=_as_float(item.get("open_page_rank")),
                        global_rank=_as_int(item.get("rank")),
                        referring_domains=_as_int(item.get("referring_domains")),
                        raw=item,
                    )
                )
        return out

    def close(self) -> None:
        self.client.close()


def _as_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v) -> int | None:
    try:
        return int(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None
