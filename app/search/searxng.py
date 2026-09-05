"""SearXNG backend - self-hosted metasearch, no API key, shipped in compose."""
from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.search.base import SearchProvider, SearchResult

log = logging.getLogger(__name__)


class SearxngProvider(SearchProvider):
    name = "searxng"

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.searxng_url).rstrip("/")
        self.client = httpx.Client(
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            follow_redirects=True,
        )

    def search(self, query: str, pages: int = 1) -> list[SearchResult]:
        out: list[SearchResult] = []
        for page in range(1, max(1, pages) + 1):
            params = {
                "q": query,
                "format": "json",
                "pageno": page,
                "language": "en",
                "safesearch": 0,
            }
            try:
                r = self.client.get(f"{self.base_url}/search", params=params)
                r.raise_for_status()
                data = r.json()
            except Exception as exc:  # network, 429, HTML error page...
                log.warning("searxng query failed (%s): %s", query[:60], exc)
                break

            results = data.get("results") or []
            if not results:
                break
            for item in results:
                url = item.get("url")
                if url:
                    out.append(
                        SearchResult(
                            url=url,
                            title=item.get("title") or "",
                            snippet=item.get("content") or "",
                        )
                    )
        return out

    def close(self) -> None:
        self.client.close()
