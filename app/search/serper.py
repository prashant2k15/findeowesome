"""serper.dev backend - paid Google SERP API, highest quality footprint hits."""
from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.search.base import SearchProvider, SearchResult

log = logging.getLogger(__name__)


class SerperProvider(SearchProvider):
    name = "serper"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.serper_api_key
        self.client = httpx.Client(
            timeout=settings.request_timeout,
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
        )

    def search(self, query: str, pages: int = 1) -> list[SearchResult]:
        if not self.api_key:
            log.warning("SERPER_API_KEY missing; skipping query")
            return []
        out: list[SearchResult] = []
        for page in range(1, max(1, pages) + 1):
            try:
                r = self.client.post(
                    "https://google.serper.dev/search",
                    json={"q": query, "page": page, "num": 100},
                )
                r.raise_for_status()
                data = r.json()
            except Exception as exc:
                log.warning("serper query failed (%s): %s", query[:60], exc)
                break
            organic = data.get("organic") or []
            if not organic:
                break
            for item in organic:
                if item.get("link"):
                    out.append(
                        SearchResult(
                            url=item["link"],
                            title=item.get("title") or "",
                            snippet=item.get("snippet") or "",
                        )
                    )
        return out

    def close(self) -> None:
        self.client.close()
