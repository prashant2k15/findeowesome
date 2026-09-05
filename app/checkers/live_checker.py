"""Live checker: verify every stored URL, then classify what it actually is.

Politeness rules baked in:
  * robots.txt is honoured (configurable) and cached per host
  * at most one request per host at a time, with a configurable delay
  * responses are streamed and truncated - we never download whole media files
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.db.models import (
    STATUS_BLOCKED,
    STATUS_DEAD,
    STATUS_LIVE,
    STATUS_NEW,
    STATUS_REDIRECT,
    Opportunity,
    utcnow,
)
from app.db.repo import due_for_check
from app.processors.classifier import classify_page
from app.processors.url_cleaner import normalize_url, root_domain

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 400_000
MAX_FAILS_BEFORE_DEAD = 3


@dataclass
class CheckOutcome:
    opportunity_id: int
    status: str
    http_status: int | None = None
    final_url: str | None = None
    title: str | None = None
    kind: str | None = None
    score: float | None = None
    submission_url: str | None = None
    signals: dict = field(default_factory=dict)
    failed: bool = False


class DomainThrottle:
    """One in-flight request per host, plus a minimum gap between them."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._guard = threading.Lock()

    def _lock_for(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    def acquire(self, host: str) -> threading.Lock:
        lock = self._lock_for(host)
        lock.acquire()
        gap = time.monotonic() - self._last.get(host, 0.0)
        if gap < self.delay:
            time.sleep(self.delay - gap)
        return lock

    def release(self, host: str, lock: threading.Lock) -> None:
        self._last[host] = time.monotonic()
        lock.release()


class RobotsCache:
    """Fetch-once, reuse-forever robots.txt rules for the life of a batch."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._guard = threading.Lock()

    def allowed(self, url: str) -> bool:
        if not settings.respect_robots_txt:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._guard:
            hit = origin in self._cache
            parser = self._cache.get(origin)
        if not hit:
            parser = self._fetch(origin)
            with self._guard:
                self._cache[origin] = parser
        if parser is None:
            return True  # no robots.txt / unreachable -> treat as allowed
        try:
            return parser.can_fetch(settings.user_agent, url)
        except Exception:
            return True

    def _fetch(self, origin: str):
        try:
            r = self.client.get(f"{origin}/robots.txt", timeout=10)
            if r.status_code != 200 or not r.text.strip():
                return None
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(r.text.splitlines())
            return parser
        except Exception:
            return None


def check_batch(session, limit: int | None = None, recheck_days: int | None = None) -> dict:
    """Check a batch of due URLs and write the results back to the database."""
    limit = limit or settings.checker_batch_size
    recheck_days = recheck_days or settings.job_recheck_days

    rows = due_for_check(session, limit, recheck_days)
    if not rows:
        return {"checked": 0, "live": 0, "dead": 0, "blocked": 0}

    # plain tuples so worker threads never touch ORM objects
    jobs = [(o.id, o.url, o.kind, o.fail_count) for o in rows]
    # interleave hosts so concurrent workers rarely land on the same domain
    jobs.sort(key=lambda j: (j[0] % 97, j[1]))

    throttle = DomainThrottle(settings.per_domain_delay)
    outcomes: list[CheckOutcome] = []

    with httpx.Client(
        timeout=settings.request_timeout,
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
        follow_redirects=True,
        max_redirects=5,
        verify=False,  # many small directory sites have broken certs
    ) as client:
        robots = RobotsCache(client)
        with ThreadPoolExecutor(max_workers=settings.checker_concurrency) as pool:
            futures = [pool.submit(_check_one, client, robots, throttle, *job) for job in jobs]
            for fut in futures:
                try:
                    outcomes.append(fut.result())
                except Exception as exc:  # one bad URL must not kill the batch
                    log.warning("checker task crashed: %s", exc)

    summary = {"checked": len(outcomes), "live": 0, "dead": 0, "blocked": 0}
    by_id: dict[int, Opportunity] = {o.id: o for o in rows}
    for out in outcomes:
        row = by_id.get(out.opportunity_id)
        if row is None:
            continue
        row.status = out.status
        row.http_status = out.http_status
        row.final_url = out.final_url
        row.last_checked = utcnow()
        row.check_count += 1
        row.fail_count = row.fail_count + 1 if out.failed else 0
        if out.title:
            row.title = out.title[:500]
        if out.kind:
            row.kind = out.kind
        if out.score is not None:
            row.score = out.score
        if out.submission_url:
            row.submission_url = out.submission_url[:2000]
        if out.signals:
            row.signals = out.signals
            row.classified_at = utcnow()
        if out.status in (STATUS_LIVE, STATUS_REDIRECT):
            summary["live"] += 1
        elif out.status == STATUS_DEAD:
            summary["dead"] += 1
        elif out.status == STATUS_BLOCKED:
            summary["blocked"] += 1

    session.flush()
    return summary


def _check_one(
    client: httpx.Client,
    robots: RobotsCache,
    throttle: DomainThrottle,
    opp_id: int,
    url: str,
    kind: str,
    fail_count: int,
) -> CheckOutcome:
    host = root_domain(urlsplit(url).hostname or "")

    if not robots.allowed(url):
        return CheckOutcome(opp_id, STATUS_BLOCKED, signals={"robots": "disallowed"})

    lock = throttle.acquire(host)
    try:
        try:
            with client.stream("GET", url) as resp:
                code = resp.status_code
                ctype = resp.headers.get("content-type", "")
                final_url = str(resp.url)
                body = b""
                if code < 400 and "html" in ctype.lower():
                    for chunk in resp.iter_bytes(32_768):
                        body += chunk
                        if len(body) >= MAX_BODY_BYTES:
                            break
        except Exception as exc:
            # transient failures stay "new" until the retry budget runs out
            status = STATUS_DEAD if fail_count + 1 >= MAX_FAILS_BEFORE_DEAD else STATUS_NEW
            return CheckOutcome(
                opp_id,
                status,
                failed=True,
                signals={"error": f"{type(exc).__name__}: {exc}"[:300]},
            )
    finally:
        throttle.release(host, lock)

    if code in (401, 403, 429, 503):
        # alive but shielded - keep it, stop hammering it
        return CheckOutcome(opp_id, STATUS_BLOCKED, http_status=code, final_url=final_url)

    if code >= 400:
        hard_gone = code in (404, 410)
        status = (
            STATUS_DEAD
            if hard_gone or fail_count + 1 >= MAX_FAILS_BEFORE_DEAD
            else STATUS_NEW
        )
        return CheckOutcome(opp_id, status, http_status=code, final_url=final_url, failed=True)

    normalized_final = normalize_url(final_url) or final_url
    moved = normalized_final != url
    status = STATUS_REDIRECT if moved else STATUS_LIVE

    html = body.decode("utf-8", "ignore") if body else ""
    result = classify_page(url, html, url_kind=kind) if html else {}

    return CheckOutcome(
        opp_id,
        status,
        http_status=code,
        final_url=normalized_final if moved else None,
        title=result.get("title"),
        kind=result.get("kind"),
        score=result.get("score"),
        submission_url=result.get("submission_url"),
        signals=result.get("signals", {}),
    )
