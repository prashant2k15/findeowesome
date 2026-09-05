from __future__ import annotations

from app.config import settings
from app.search.searxng import SearxngProvider


def test_searxng_defaults_to_bing_selector(monkeypatch):
    monkeypatch.setattr(settings, "searxng_engines", "bing")
    provider = SearxngProvider("http://example.invalid")
    try:
        assert provider._query('"write for us"') == '!bing "write for us"'
    finally:
        provider.close()


def test_searxng_supports_multiple_engine_selectors(monkeypatch):
    monkeypatch.setattr(settings, "searxng_engines", "bing,brave")
    provider = SearxngProvider("http://example.invalid")
    try:
        assert provider._query("backlink sites") == "!bing !brave backlink sites"
    finally:
        provider.close()


def test_empty_engine_setting_preserves_query(monkeypatch):
    monkeypatch.setattr(settings, "searxng_engines", "")
    provider = SearxngProvider("http://example.invalid")
    try:
        assert provider._query("backlink sites") == "backlink sites"
    finally:
        provider.close()
