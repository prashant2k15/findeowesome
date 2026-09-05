"""Brave Search API backend - cheap, independent index, 2k free queries/month."""
from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.search.base import SearchProvider, SearchResult

log = logging.getLogger(__name__)

PAGE_SIZE = 20


class BraveProvider(SearchProvider):
    name = "brave"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.brave_api_key
        self.client = httpx.Client(
            timeout=settings.request_timeout,
            headers={
                "X-Subscription-Token": self.api_key,
                "Accept": "application/json",
                "User-Agent": settings.user_agent,
            },
        )

    def search(self, query: str, pages: int = 1) -> list[SearchResult]:
        if not self.api_key:
            log.warning("BRAVE_API_KEY missing; skipping query")
            return []
        out: list[SearchResult] = []
        for page in range(max(1, pages)):
            try:
                r = self.client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": PAGE_SIZE, "offset": page},
                )
                r.raise_for_status()
                data = r.json()
            except Exception as exc:
                log.warning("brave query failed (%s): %s", query[:60], exc)
                break
            results = (data.get("web") or {}).get("results") or []
            if not results:
                break
            for item in results:
                if item.get("url"):
                    out.append(
                        SearchResult(
                            url=item["url"],
                            title=item.get("title") or "",
                            snippet=item.get("description") or "",
                        )
                    )
        return out

    def close(self) -> None:
        self.client.close()
