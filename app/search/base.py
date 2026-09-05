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

    def healthcheck(self) -> tuple[bool, str]:
        """One cheap live query. Returns (ok, human-readable detail).

        A search backend that quietly stops working is the worst failure this
        system can have: every job still reports success while the database
        stops growing. `blf doctor` and the footprint job both call this.
        """
        try:
            results = self.search("web directory submit site", pages=1)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"[:200]
        if not results:
            return False, "provider reachable but returned 0 results"
        return True, f"{len(results)} results for a probe query"

    def close(self) -> None:  # pragma: no cover
        pass


class NullProvider(SearchProvider):
    """Used when no backend is configured - keeps the worker running."""

    name = "none"

    def search(self, query: str, pages: int = 1) -> list[SearchResult]:
        return []

    def healthcheck(self) -> tuple[bool, str]:
        return False, "SEARCH_PROVIDER is not configured"
