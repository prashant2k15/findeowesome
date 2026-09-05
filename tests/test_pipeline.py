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


# --- spam filtering ------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://framesi-usa.net/__media__/js/netsoltrademark.php?d=https%3A%2F%2Fblog.x.id",
        "https://images.google.com.sg/url?q=http%3A%2F%2Fwww.site.my.id%2F",
        "https://maps.google.la/url?q=http%3A%2F%2Fsite.my.id%2F",
        "https://proaudioguide.com/ads/adclick.php?bannerid=179&dest=https%3A%2F%2Fx.io",
        "https://bk.sanw.net/link.php?url=https%3A%2F%2Fblog.x.id%2F",
        "https://motoring.vn/PageCountImg.aspx?id=Banner1&url=https%3A%2F%2Fx.io",
        "https://site.com/out.php?goto=https://target.com",
    ],
)
def test_open_redirect_spam_is_rejected(url):
    from app.processors.url_cleaner import is_spam_url

    assert is_spam_url(url) is True
    assert normalize_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://alpha-directory.com/submit-site?category=seo",
        "https://forum.example-site.net/register?ref_id=12",
        "https://dir.site.com/add-url",
    ],
)
def test_legit_urls_survive_the_spam_filter(url):
    from app.processors.url_cleaner import is_spam_url

    assert is_spam_url(url) is False
    assert normalize_url(url) is not None


def test_spam_ratio_scores_a_whole_list():
    from app.processors.url_cleaner import spam_ratio

    junk = [f"https://s{i}.com/__media__/js/netsoltrademark.php?d=https%3A%2F%2Fx.io" for i in range(9)]
    assert spam_ratio(junk + ["https://real-directory.com/submit"]) == 0.9
    assert spam_ratio(["https://real-directory.com/submit"]) == 0.0
    assert spam_ratio([]) == 0.0


@pytest.mark.parametrize("token", ["readme.md", "setup.py", "main.go", "styles.css", "index.php"])
def test_file_names_are_not_treated_as_domains(token):
    assert extract_urls(f"see {token} for details") == []


def test_scheme_qualified_md_domain_still_works():
    assert normalize_url("https://keep.md/page") == "https://keep.md/page"


def test_purge_removes_rows_that_current_filters_reject(session):
    from app.db.models import Opportunity as O

    session.add_all(
        [
            O(url="https://good.com/submit-site", domain="good.com", root_domain="good.com", source="test"),
            O(
                url="https://bad.com/__media__/js/netsoltrademark.php?d=https%3A%2F%2Fx.io",
                domain="bad.com",
                root_domain="bad.com",
                source="test",
            ),
        ]
    )
    session.commit()

    from app.jobs import job_purge_junk

    scanned, removed = job_purge_junk(session)
    session.commit()

    assert (scanned, removed) == (2, 1)
    assert session.execute(select(Opportunity.url)).scalars().all() == ["https://good.com/submit-site"]
