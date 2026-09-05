"""DataForSEO backlinks summary - paid, but far richer than PageRank alone.

Gives referring domains, backlink counts and a rank per domain, which is what
you actually want when deciding whether a directory is worth a submission.
Costs roughly $0.02 per 100 domains at the time of writing.
"""
from __future__ import annotations

import base64
import logging

import httpx

from app.config import settings
from app.metrics.base import DomainMetric, MetricsProvider

log = logging.getLogger(__name__)

ENDPOINT = "https://api.dataforseo.com/v3/backlinks/bulk_backlinks/live"
RANKS_ENDPOINT = "https://api.dataforseo.com/v3/backlinks/bulk_ranks/live"


class DataForSeoProvider(MetricsProvider):
    name = "dataforseo"
    batch_size = 100

    def __init__(self, login: str | None = None, password: str | None = None) -> None:
        self.login = login or settings.dataforseo_login
        self.password = password or settings.dataforseo_password
        token = base64.b64encode(f"{self.login}:{self.password}".encode()).decode()
        self.client = httpx.Client(
            timeout=max(60, settings.request_timeout),
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
        )

    def fetch(self, domains: list[str]) -> list[DomainMetric]:
        if not (self.login and self.password):
            log.warning("DATAFORSEO_LOGIN/PASSWORD missing; metrics job idle")
            return []

        out: list[DomainMetric] = []
        for start in range(0, len(domains), self.batch_size):
            chunk = domains[start : start + self.batch_size]
            try:
                links = self._post(ENDPOINT, chunk)
                ranks = self._post(RANKS_ENDPOINT, chunk)
            except Exception as exc:
                log.warning("dataforseo batch failed: %s", exc)
                out.extend(DomainMetric(d, error=str(exc)[:200]) for d in chunk)
                continue

            rank_by_target = {r.get("target"): r.get("rank") for r in ranks}
            for item in links:
                target = (item.get("target") or "").lower()
                if not target:
                    continue
                rank = rank_by_target.get(item.get("target"))
                out.append(
                    DomainMetric(
                        root_domain=target,
                        # DataForSEO rank is 0-1000; fold it onto the same 0-10 scale
                        page_rank=round(rank / 100, 2) if isinstance(rank, (int, float)) else None,
                        domain_rating=float(rank) if isinstance(rank, (int, float)) else None,
                        backlinks=item.get("backlinks"),
                        referring_domains=item.get("referring_domains"),
                        spam_score=item.get("backlinks_spam_score"),
                        raw={"backlinks": item, "rank": rank},
                    )
                )
        return out

    def _post(self, endpoint: str, targets: list[str]) -> list[dict]:
        r = self.client.post(endpoint, json=[{"targets": targets}])
        r.raise_for_status()
        payload = r.json()
        tasks = payload.get("tasks") or []
        if not tasks or not tasks[0].get("result"):
            raise RuntimeError(payload.get("status_message", "empty response"))
        return tasks[0]["result"][0].get("items", []) or []

    def close(self) -> None:
        self.client.close()
