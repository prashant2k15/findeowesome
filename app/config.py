"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # database
    database_url: str = "sqlite:///./data/blf.db"

    # search
    search_provider: str = "searxng"
    searxng_url: str = "http://searxng:8080"
    serper_api_key: str = ""
    brave_api_key: str = ""

    # github
    github_token: str = ""

    # crawler
    user_agent: str = "BingLinkFinder/1.0"
    request_timeout: int = 20
    checker_concurrency: int = 16
    checker_batch_size: int = 500
    per_domain_delay: float = 2.0
    respect_robots_txt: bool = True

    # scheduler
    job_github_seeds_hours: int = 24
    job_footprint_hours: int = 6
    job_classify_minutes: int = 30
    job_checker_hours: int = 1
    job_recheck_days: int = 14
    job_report_hour: int = 9

    # telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # api
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    dashboard_key: str = ""

    @property
    def export_dir(self) -> Path:
        d = ROOT / "exports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def config_dir(self) -> Path:
        return ROOT / "config"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.database_url.startswith("sqlite"):
        (ROOT / "data").mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()
