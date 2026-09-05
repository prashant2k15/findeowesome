"""Test settings must not depend on the developer's .env file.

Environment variables take priority over .env in pydantic-settings, so pinning
them here (before app.config is imported) makes every run deterministic - the
CI box and a laptop mid-campaign behave identically.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["SEARCH_PROVIDER"] = "searxng"
os.environ["SEARXNG_URL"] = "http://searxng:8080"
os.environ["METRICS_PROVIDER"] = "none"
os.environ["PER_DOMAIN_DELAY"] = "0"
os.environ["OUTREACH_ENABLED"] = "false"
os.environ["OUTREACH_REQUIRE_APPROVAL"] = "true"
os.environ["OUTREACH_MIN_PAGE_RANK"] = "0"
os.environ["OUTREACH_DAILY_LIMIT"] = "25"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["SMTP_HOST"] = ""
