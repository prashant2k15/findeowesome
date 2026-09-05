from __future__ import annotations

from app.config import settings
from app.search.base import NullProvider, SearchProvider, SearchResult


def get_provider(name: str | None = None) -> SearchProvider:
    name = (name or settings.search_provider or "none").lower()
    if name == "searxng":
        from app.search.searxng import SearxngProvider

        return SearxngProvider()
    if name == "serper":
        from app.search.serper import SerperProvider

        return SerperProvider()
    if name == "brave":
        from app.search.brave import BraveProvider

        return BraveProvider()
    return NullProvider()


__all__ = ["get_provider", "SearchProvider", "SearchResult", "NullProvider"]
