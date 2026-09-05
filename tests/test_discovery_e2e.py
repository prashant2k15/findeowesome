"""End-to-end proof that discovery works: query -> SERP -> database.

The remote search engine is the only thing stubbed. Everything else is the real
code path the worker runs: footprint rotation, HTTP call, result parsing, URL
normalisation, spam rejection, per-domain caps, de-duplication, insertion, and
the rotation state that lets a restarted worker carry on where it stopped.
"""
from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.collectors.footprint_collector import SearchProviderDown, run_footprints
from app.db.models import Base, Footprint, Opportunity
from app.db.repo import next_footprints, sync_footprints
from app.search.searxng import SearxngProvider

SEARX = "http://searxng:8080"


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


@pytest.fixture()
def footprints(session):
    sync_footprints(
        session,
        [
            {"query": '"submit your site" saas directory', "kind": "directory"},
            {"query": "seo blog \"write for us\"", "kind": "article"},
            {"query": "inurl:add-url business", "kind": "directory"},
        ],
    )
    session.commit()
    return session


def _serp(results: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"results": results})


def _provider() -> SearxngProvider:
    return SearxngProvider(base_url=SEARX)


# =========================================================================
# The full path
# =========================================================================


@respx.mock
def test_search_results_become_database_rows(footprints):
    session = footprints
    respx.get(url__startswith=f"{SEARX}/search").mock(
        return_value=_serp(
            [
                {"url": "https://alpha-directory.com/submit-site", "title": "Submit", "content": ""},
                {"url": "https://beta-blog.net/write-for-us", "title": "Write for us", "content": ""},
                # junk that must never reach the database:
                {"url": "https://spam.com/__media__/js/netsoltrademark.php?d=https%3A%2F%2Fx.io"},
                {"url": "https://images.google.com/url?q=http%3A%2F%2Fx.io"},
                {"url": "https://github.com/some/repo"},
                {"url": "https://cdn.site.com/logo.png"},
            ]
        )
    )

    seen, created = run_footprints(session, batch=1, pages=1, delay=0)
    session.commit()

    assert created == 2, "only the two real opportunities should be stored"
    stored = sorted(session.execute(select(Opportunity.url)).scalars())
    assert stored == [
        "https://alpha-directory.com/submit-site",
        "https://beta-blog.net/write-for-us",
    ]

    # classification and provenance survive the trip
    row = session.execute(
        select(Opportunity).where(Opportunity.url.like("%submit-site"))
    ).scalar_one()
    assert row.kind == "directory"
    assert row.source == "footprint"
    assert row.source_detail == '"submit your site" saas directory'
    assert row.status == "new"          # discovered, not yet verified


@respx.mock
def test_second_run_adds_nothing_new(footprints):
    session = footprints
    respx.get(url__startswith=f"{SEARX}/search").mock(
        return_value=_serp([{"url": "https://alpha-directory.com/submit-site"}])
    )

    run_footprints(session, batch=1, pages=1, delay=0)
    session.commit()
    _, created_again = run_footprints(session, batch=1, pages=1, delay=0)
    session.commit()

    assert created_again == 0
    assert len(session.execute(select(Opportunity)).scalars().all()) == 1


@respx.mock
def test_footprints_rotate_so_a_restart_resumes(footprints):
    """Least-recently-run first: a restarted worker never re-runs the same query."""
    session = footprints
    respx.get(url__startswith=f"{SEARX}/search").mock(return_value=_serp([]))

    first = next_footprints(session, 1)[0].query
    run_footprints(session, batch=1, pages=1, delay=0)
    session.commit()

    second = next_footprints(session, 1)[0].query
    assert second != first, "the just-run query must go to the back of the queue"

    ran = session.execute(
        select(Footprint).where(Footprint.query == first)
    ).scalar_one()
    assert ran.last_run is not None and ran.run_count == 1

    # simulate a restart: state lives in the database, not in memory
    run_footprints(session, batch=1, pages=1, delay=0)
    session.commit()
    third = next_footprints(session, 1)[0].query
    assert third not in (first, second)


@respx.mock
def test_per_domain_cap_survives_a_flooding_serp(footprints):
    session = footprints
    respx.get(url__startswith=f"{SEARX}/search").mock(
        return_value=_serp([{"url": f"https://flood.com/page-{i}"} for i in range(200)])
    )

    _, created = run_footprints(session, batch=1, pages=1, delay=0)
    session.commit()

    assert created == 60, "one domain must not be able to flood the database"


@respx.mock
def test_pagination_is_followed(footprints):
    session = footprints
    page = {"count": 0}

    def handler(request):
        page["count"] += 1
        n = page["count"]
        if n > 3:
            return _serp([])
        return _serp([{"url": f"https://site-{n}.com/submit-site"}])

    respx.get(url__startswith=f"{SEARX}/search").mock(side_effect=handler)

    _, created = run_footprints(session, batch=1, pages=3, delay=0)
    session.commit()
    assert created == 3
    assert page["count"] == 3


# =========================================================================
# Failure must be loud
# =========================================================================


@respx.mock
def test_dead_backend_raises_instead_of_reporting_success(footprints):
    """A search backend that answers nothing must fail the job, not go green."""
    session = footprints
    respx.get(url__startswith=f"{SEARX}/search").mock(return_value=_serp([]))

    with pytest.raises(SearchProviderDown, match="returned 0 results"):
        run_footprints(session, batch=3, pages=1, delay=0)


@respx.mock
def test_html_response_is_survived_not_crashed(footprints):
    """Public SearXNG instances answer HTML instead of JSON - handle it."""
    session = footprints
    respx.get(url__startswith=f"{SEARX}/search").mock(
        return_value=httpx.Response(200, text="<!doctype html><html>nope</html>")
    )

    with pytest.raises(SearchProviderDown):
        run_footprints(session, batch=3, pages=1, delay=0)

    # every footprint still got its rotation stamp, so the queue keeps moving
    assert all(f.last_run is not None for f in session.execute(select(Footprint)).scalars())


@respx.mock
def test_rate_limited_backend_does_not_lose_the_batch(footprints):
    session = footprints
    respx.get(url__startswith=f"{SEARX}/search").mock(return_value=httpx.Response(429))

    with pytest.raises(SearchProviderDown):
        run_footprints(session, batch=3, pages=1, delay=0)


@respx.mock
def test_healthcheck_reports_a_working_backend():
    respx.get(url__startswith=f"{SEARX}/search").mock(
        return_value=_serp([{"url": "https://a.com/submit-site"}])
    )
    ok, detail = _provider().healthcheck()
    assert ok is True and "1 results" in detail


@respx.mock
def test_healthcheck_reports_a_broken_backend():
    respx.get(url__startswith=f"{SEARX}/search").mock(
        return_value=httpx.Response(200, text="<html>bot check</html>")
    )
    ok, detail = _provider().healthcheck()
    assert ok is False and "0 results" in detail
