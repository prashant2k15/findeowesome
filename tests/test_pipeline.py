"""Unit tests for the discovery pipeline: cleaning, classifying, storing."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Opportunity
from app.db.repo import add_opportunities, stats
from app.processors.classifier import classify_page, classify_url
from app.processors.url_cleaner import extract_urls, normalize_url


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


# --- normalisation -------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://WWW.Sample-Site.com/Submit/", "https://sample-site.com/Submit"),
        ("https://sample-site.com/a?utm_source=x&b=1", "https://sample-site.com/a?b=1"),
        ("https://sample-site.com/a#frag", "https://sample-site.com/a"),
        ("sample-site.com/add-url", "https://sample-site.com/add-url"),
        ("https://sample-site.com//double//slash/", "https://sample-site.com/double/slash"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://github.com/user/repo",   # noise domain
        "https://bit.ly/xyz",             # shortener
        "https://sample-site.com/logo.png",   # asset
        "http://127.0.0.1/admin",         # ip
        "not a url",
        "",
    ],
)
def test_normalize_rejects_junk(raw):
    assert normalize_url(raw) is None


def test_query_params_are_order_stable():
    a = normalize_url("https://sample-site.com/x?b=2&a=1")
    b = normalize_url("https://sample-site.com/x?a=1&b=2")
    assert a == b


def test_extract_urls_from_markdown():
    text = """
    | Site | DA |
    | [Alpha Directory](https://alpha-directory.com/submit-site) | 45 |
    plain-listing.net and https://beta.io/add-url
    ![img](https://cdn.sample-site.com/x.png)
    """
    urls = extract_urls(text)
    assert "https://alpha-directory.com/submit-site" in urls
    assert "https://beta.io/add-url" in urls
    assert "https://plain-listing.net/" in urls
    assert not any(u.endswith(".png") for u in urls)


# --- classification ------------------------------------------------------


@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://site.com/submit-site", "directory"),
        ("https://site.com/members/john", "profile"),
        ("https://site.com/register", "profile"),
        ("https://site.com/write-for-us", "article"),
        ("https://site.com/forum/general", "forum"),
        ("https://site.com/random/page", "unknown"),
    ],
)
def test_classify_url(url, kind):
    assert classify_url(url)[0] == kind


def test_classify_page_finds_submission_form():
    html = """
    <html><head><title>Free Web Directory</title></head>
    <body>
      <h1>Submit your site</h1>
      <p>Add your website to our free listing in seconds.</p>
      <a href="/submit.php">Submit Site</a>
      <form action="/submit.php"><input name="website_url"><input type="password"></form>
    </body></html>
    """
    result = classify_page("https://dir.example.com/", html)
    assert result["kind"] == "directory"
    assert result["score"] > 0.6
    assert result["submission_url"] == "https://dir.example.com/submit.php"
    assert result["signals"]["url_field"] is True
    assert result["title"] == "Free Web Directory"


def test_classify_page_zeroes_parked_domains():
    html = "<html><body><h1>This domain is for sale</h1><form></form></body></html>"
    result = classify_page("https://parked.example.com/", html)
    assert result["score"] == 0.0
    assert result["signals"]["dead_end"]


# --- storage / de-duplication -------------------------------------------


def test_add_opportunities_deduplicates(session):
    urls = [
        "https://alpha.com/submit-site",
        "http://www.alpha.com/submit-site/",      # same after normalisation
        "https://beta.com/add-url?utm_source=x",
    ]
    seen, created = add_opportunities(session, urls, source="test")
    assert (seen, created) == (2, 2)

    seen, created = add_opportunities(session, urls, source="test")
    assert created == 0  # second pass adds nothing

    stored = list(session.execute(select(Opportunity.url)).scalars())
    assert sorted(stored) == ["https://alpha.com/submit-site", "https://beta.com/add-url"]


def test_per_domain_cap(session):
    urls = [f"https://flood.com/page-{i}" for i in range(30)]
    _, created = add_opportunities(session, urls, source="test", max_per_domain=5)
    assert created == 5


def test_kind_hint_used_when_url_is_ambiguous(session):
    add_opportunities(session, ["https://vague.com/x"], source="test", kind_hint="web2")
    row = session.execute(select(Opportunity)).scalar_one()
    assert row.kind == "web2"


def test_stats(session):
    add_opportunities(session, ["https://a.com/submit-site", "https://b.com/register"], source="test")
    session.commit()
    data = stats(session)
    assert data["total"] == 2
    assert data["pending"] == 2
    assert data["domains"] == 2
