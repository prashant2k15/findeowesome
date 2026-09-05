"""Open PageRank (domcop) - free domain authority, 100 domains per request.

Free tier is generous (thousands of domains/day) and needs only an email to
register at https://www.domcop.com/openpagerank/. This is the default because
it costs nothing and covers the one decision that matters: is this domain worth
my time at all?
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.metrics.base import DomainMetric, MetricsProvider

log = logging.getLogger(__name__)

ENDPOINT = "https://openpagerank.com/api/v1.0/getPageRank"


class OpenPageRankProvider(MetricsProvider):
    name = "openpagerank"
    batch_size = 100
    daily_quota = 1000  # requests, i.e. 100k domains

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.openpagerank_api_key
        self.client = httpx.Client(
            timeout=settings.request_timeout,
            headers={"API-OPR": self.api_key, "User-Agent": settings.user_agent},
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
                r = self.client.get(ENDPOINT, params=[("domains[]", d) for d in chunk])
                r.raise_for_status()
                payload = r.json()
            except Exception as exc:
                log.warning("openpagerank batch failed: %s", exc)
                out.extend(DomainMetric(d, error=str(exc)[:200]) for d in chunk)
                continue

            for item in payload.get("response", []):
                domain = (item.get("domain") or "").lower()
                if not domain:
                    continue
                if item.get("status_code") != 200:
                    out.append(DomainMetric(domain, error=f"status {item.get('status_code')}", raw=item))
                    continue
                out.append(
                    DomainMetric(
                        root_domain=domain,
                        page_rank=_as_float(item.get("page_rank_decimal")),
                        global_rank=_as_int(item.get("rank")),
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
