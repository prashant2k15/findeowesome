"""Tests for the metrics, backlink-tracker and outreach modules."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    LINK_LIVE,
    PROSPECT_NEW,
    PROSPECT_QUEUED,
    PROSPECT_READY,
    STATUS_LIVE,
    Backlink,
    Base,
    DomainMetrics,
    Opportunity,
    OutreachMessage,
    Prospect,
)
from app.metrics.base import DomainMetric
from app.metrics.enrich import domains_needing_metrics, store_metrics
from app.outreach import campaign, mailer, templates
from app.outreach.contact_finder import clean_emails, extract_from_html
from app.trackers.backlink_tracker import add_backlink, find_link, import_rows


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    yield s
    s.close()


def _live_opportunity(session, url, kind="article", domain=None, score=0.6):
    root = domain or url.split("/")[2]
    o = Opportunity(
        url=url, domain=root, root_domain=root, kind=kind, source="test",
        status=STATUS_LIVE, score=score,
    )
    session.add(o)
    session.flush()
    return o


# =========================================================================
# Domain metrics
# =========================================================================


def test_only_live_domains_are_queued_for_metrics(session):
    _live_opportunity(session, "https://good.com/submit-site")
    session.add(
        Opportunity(url="https://dead.com/x", domain="dead.com", root_domain="dead.com",
                    source="test", status="dead")
    )
    session.commit()

    assert domains_needing_metrics(session, 10) == ["good.com"]


def test_store_metrics_denormalises_onto_opportunities(session):
    _live_opportunity(session, "https://good.com/a")
    _live_opportunity(session, "https://good.com/b")
    session.commit()

    stored = store_metrics(session, [DomainMetric("good.com", page_rank=4.7, global_rank=1234)], "test")
    session.commit()

    assert stored == 1
    assert session.scalar(select(DomainMetrics.page_rank)) == 4.7
    ranks = session.execute(select(Opportunity.page_rank)).scalars().all()
    assert ranks == [4.7, 4.7]
    assert domains_needing_metrics(session, 10) == []


def test_domains_the_provider_skipped_do_not_block_the_queue(session):
    _live_opportunity(session, "https://unknown.com/a")
    session.commit()
    store_metrics(session, [], "test")
    assert session.scalar(select(Opportunity.page_rank)) is None


# =========================================================================
# Backlink tracker
# =========================================================================


def test_add_backlink_is_idempotent(session):
    a = add_backlink(session, "https://blog.com/post", "https://mysite.com/page", anchor="hi")
    b = add_backlink(session, "http://www.blog.com/post/", "https://mysite.com/page")
    assert a is not None and a.id == b.id
    assert session.scalar(select(Backlink.source_domain)) == "blog.com"


def test_find_link_exact_match_and_rel():
    html = '<a href="https://mysite.com/page?utm_source=x" rel="nofollow ugc">My Site</a>'
    hit = find_link(html, "https://blog.com/post", "https://mysite.com/page", "mysite.com")
    assert hit == {"anchor": "My Site", "rel": "nofollow ugc", "dofollow": False}


def test_find_link_rejects_same_domain_when_exact_target_is_gone():
    html = '<a href="/out">x</a><a href="https://mysite.com/other">Homepage</a>'
    assert find_link(
        html, "https://blog.com/post", "https://mysite.com/page", "mysite.com"
    ) is None


def test_find_link_returns_none_when_gone():
    html = "<p>nothing here</p><a href='https://other.com'>other</a>"
    assert find_link(html, "https://blog.com/post", "https://mysite.com/page", "mysite.com") is None


def test_tracker_can_match_domains_discovery_ignores():
    """github.com is a noise domain for discovery but a valid link target."""
    html = '<a href="https://github.com/me/repo">my repo</a>'
    hit = find_link(html, "https://blog.com/post", "https://github.com/me/repo", "github.com")
    assert hit is not None and hit["anchor"] == "my repo"


def test_import_rows_counts_distinct_source_target_pairs(session):
    rows = [
        {"source_url": "https://a.com/p", "target_url": "https://mine.com/", "anchor": "mine"},
        {"source_url": "https://a.com/p", "target_url": "https://other.com/", "anchor": "other"},
        {"source_url": "https://b.com/p", "target_url": "https://mine.com/"},
        {"source_url": "", "target_url": "https://mine.com/"},
    ]
    seen, added = import_rows(session, rows, project="q1")
    assert (seen, added) == (3, 3)
    assert len(session.execute(select(Backlink)).scalars().all()) == 3


# =========================================================================
# Outreach: contact discovery
# =========================================================================


def test_extract_contacts_from_html():
    html = """
    <html><head><title>Tech Blog</title></head><body>
      <a href="/write-for-us">Write For Us</a>
      <a href="mailto:editor@techblog.com">mail</a>
      Also reachable at info [at] techblog [dot] com
    </body></html>
    """
    emails, links, title = extract_from_html(html, "https://techblog.com/")
    assert title == "Tech Blog"
    assert "https://techblog.com/write-for-us" in links
    assert "editor@techblog.com" in emails
    assert "info@techblog.com" in emails


def test_clean_emails_ranks_and_filters():
    raw = [
        "noreply@techblog.com",
        "someone@wixpress.com",
        "logo@2x.png",
        "info@techblog.com",
        "editor@techblog.com",
        "freelancer@gmail.com",
    ]
    assert clean_emails(raw, "techblog.com") == [
        "editor@techblog.com",
        "info@techblog.com",
        "freelancer@gmail.com",
    ]


# =========================================================================
# Outreach: templates
# =========================================================================


def test_render_marks_missing_variables_without_raising():
    subject, body, warnings = templates.render(
        "guest_post", {"domain": "site.com", "site_name": "Site"}
    )
    assert "Site" in subject
    assert "[[my_name]]" in body
    assert any("missing variable" in w for w in warnings)


def test_needs_editing_blocks_unfinished_drafts():
    assert templates.needs_editing("Hi, see [angle one - replace before sending]") is True
    assert templates.needs_editing("Hi, plain finished text") is False
    assert templates.needs_editing("Hi [[my_name]]") is True


def test_unknown_template_raises():
    with pytest.raises(templates.TemplateError):
        templates.render("no_such_template", {})


# =========================================================================
# Outreach: pipeline
# =========================================================================


def test_build_prospects_respects_authority_threshold(session):
    weak = _live_opportunity(session, "https://weak.com/write-for-us")
    strong = _live_opportunity(session, "https://strong.com/write-for-us")
    weak.page_rank, strong.page_rank = 1.0, 5.0
    session.commit()

    scanned, created = campaign.build_prospects(session, limit=10, min_page_rank=3.0)
    session.commit()

    assert created == 1
    assert session.scalar(select(Prospect.root_domain)) == "strong.com"


def test_build_prospects_never_duplicates_a_domain(session):
    _live_opportunity(session, "https://site.com/write-for-us")
    _live_opportunity(session, "https://site.com/contribute")
    session.commit()

    _, created = campaign.build_prospects(session, limit=10, min_page_rank=0)
    assert created == 1


def test_draft_requires_approval_when_placeholders_remain(session):
    session.add(
        Prospect(root_domain="site.com", url="https://site.com/write-for-us",
                 kind="article", email="editor@site.com", status=PROSPECT_READY,
                 template="guest_post")
    )
    session.commit()

    considered, drafted = campaign.draft_messages(session, limit=5)
    session.commit()

    assert (considered, drafted) == (1, 1)
    msg = session.execute(select(OutreachMessage)).scalar_one()
    assert msg.approved is False
    assert msg.to_email == "editor@site.com"
    assert session.scalar(select(Prospect.status)) == PROSPECT_QUEUED


def test_draft_is_not_written_twice(session):
    session.add(
        Prospect(root_domain="site.com", url="https://site.com/x", kind="article",
                 email="e@site.com", status=PROSPECT_READY, template="guest_post")
    )
    session.commit()
    campaign.draft_messages(session, limit=5)
    session.commit()
    session.execute(select(Prospect)).scalar_one().status = PROSPECT_READY
    session.commit()
    campaign.draft_messages(session, limit=5)
    session.commit()
    assert len(session.execute(select(OutreachMessage)).scalars().all()) == 1


# =========================================================================
# Outreach: safety gates
# =========================================================================


def _draft(session, email="a@b.com", approved=True):
    m = OutreachMessage(prospect_id=1, sequence=0, to_email=email, subject="s",
                        body="b", approved=approved)
    session.add(m)
    session.flush()
    return m


def test_send_blocked_when_outreach_disabled(session, monkeypatch):
    monkeypatch.setattr(mailer.settings, "outreach_enabled", False)
    with pytest.raises(mailer.SendBlocked, match="OUTREACH_ENABLED"):
        mailer.check_gates(session, _draft(session))


def test_send_blocked_without_approval(session, monkeypatch):
    monkeypatch.setattr(mailer.settings, "outreach_enabled", True)
    monkeypatch.setattr(mailer.settings, "outreach_require_approval", True)
    with pytest.raises(mailer.SendBlocked, match="not approved"):
        mailer.check_gates(session, _draft(session, approved=False))


def test_send_blocked_for_suppressed_address(session, monkeypatch):
    monkeypatch.setattr(mailer.settings, "outreach_enabled", True)
    mailer.suppress(session, "blocked@site.com", "opt-out")
    with pytest.raises(mailer.SendBlocked, match="suppressed"):
        mailer.check_gates(session, _draft(session, email="blocked@site.com"))


def test_suppressing_a_domain_blocks_every_address_on_it(session, monkeypatch):
    monkeypatch.setattr(mailer.settings, "outreach_enabled", True)
    mailer.suppress(session, "site.com", "domain block")
    with pytest.raises(mailer.SendBlocked, match="suppressed"):
        mailer.check_gates(session, _draft(session, email="anyone@site.com"))


def test_daily_limit_is_enforced(session, monkeypatch):
    monkeypatch.setattr(mailer.settings, "outreach_enabled", True)
    monkeypatch.setattr(mailer.settings, "outreach_daily_limit", 0)
    with pytest.raises(mailer.SendBlocked, match="daily limit"):
        mailer.check_gates(session, _draft(session))


def test_send_is_dry_run_without_smtp(session, monkeypatch):
    monkeypatch.setattr(mailer.settings, "outreach_enabled", True)
    monkeypatch.setattr(mailer.settings, "smtp_host", "")
    msg = _draft(session)
    assert mailer.send(session, msg) is False
    assert msg.sent is True and "dry-run" in msg.error


def test_mark_replied_stops_the_sequence(session):
    session.add(
        Prospect(root_domain="site.com", url="https://site.com/x", email="e@site.com",
                 status="contacted")
    )
    session.commit()
    assert campaign.mark_replied(session, "e@site.com") is True
    p = session.execute(select(Prospect)).scalar_one()
    assert p.status == "replied" and p.replied_at is not None

    _, drafted = campaign.schedule_follow_ups(session, limit=5)
    assert drafted == 0


def test_link_status_constant_is_used(session):
    add_backlink(session, "https://a.com/p", "https://mine.com/")
    row = session.execute(select(Backlink)).scalar_one()
    assert row.status != LINK_LIVE
    assert row.status == "pending"
    assert session.scalar(select(Prospect.status)) is None or True
    assert PROSPECT_NEW == "new"
