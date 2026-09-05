"""Pluggable search backends for the footprint hunter."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""


class SearchProvider:
    name = "base"

    def search(self, query: str, pages: int = 1) -> list[SearchResult]:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        pass


class NullProvider(SearchProvider):
    """Used when no backend is configured - keeps the worker running."""

    name = "none"

    def search(self, query: str, pages: int = 1) -> list[SearchResult]:
        return []
