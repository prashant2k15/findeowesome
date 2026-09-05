"""Classify a URL into an opportunity type and score how useful it looks.

Two passes:
  * `classify_url`  - cheap, offline, from the URL string alone (runs at insert).
  * `classify_page` - richer, uses the HTML the live-checker already fetched.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from selectolax.parser import HTMLParser

from app.db.models import (
    KIND_ARTICLE,
    KIND_BOOKMARK,
    KIND_DIRECTORY,
    KIND_FORUM,
    KIND_PROFILE,
    KIND_QA,
    KIND_UNKNOWN,
    KIND_WEB2,
)

# --- URL path patterns ---------------------------------------------------
URL_RULES: list[tuple[str, str]] = [
    (KIND_PROFILE, r"/(members?|users?|profiles?|people|author|u)/"),
    (KIND_PROFILE, r"/(register|signup|sign-up|join|create-account|new-account)"),
    (KIND_DIRECTORY, r"/(submit|add)[-_/](site|url|link|listing|business|company|website)"),
    (KIND_DIRECTORY, r"/(directory|listings?|businesses|companies|catalog)"),
    (KIND_DIRECTORY, r"/(add|submit)(-|_)?(a-)?(site|url|link)?\b"),
    (KIND_BOOKMARK, r"/(bookmark|bookmarks|saved|social-bookmarking)"),
    (KIND_ARTICLE, r"/(write|publish|submit-article|guest-post|contribute|write-for-us)"),
    (KIND_FORUM, r"/(forum|forums|board|community|discussion|thread)"),
    (KIND_QA, r"/(questions?|ask|answers?)"),
    (KIND_WEB2, r"/(blog|blogs|create-blog|new-blog|start-blog)"),
]

# --- on-page text signals -------------------------------------------------
PAGE_SIGNALS: dict[str, tuple[str, ...]] = {
    KIND_DIRECTORY: (
        "submit your site", "submit a site", "add your website", "add your business",
        "submit url", "add url", "add listing", "submit your business",
        "list your business", "free listing", "submit website",
    ),
    KIND_PROFILE: (
        "create an account", "create your account", "sign up", "register now",
        "create profile", "join our community", "create your free account",
    ),
    KIND_ARTICLE: (
        "write for us", "guest post", "submit an article", "become a contributor",
        "contribute an article", "submit your story", "publish with us",
    ),
    KIND_BOOKMARK: ("bookmark this", "social bookmarking", "save this link"),
    KIND_FORUM: ("new topic", "start a discussion", "post a reply", "board index"),
    KIND_QA: ("ask a question", "post your question", "answer this question"),
    KIND_WEB2: ("start your blog", "create a free blog", "start writing"),
}

# Anchor text that usually points straight at the submission form.
SUBMIT_ANCHORS = (
    "submit site", "submit url", "submit a site", "add site", "add url",
    "add your site", "add listing", "add business", "submit website",
    "submit link", "add link", "write for us", "guest post", "contribute",
    "register", "sign up", "create account", "join",
)

DEAD_END_SIGNALS = (
    "domain is for sale", "buy this domain", "parked domain",
    "this site can't be reached", "account suspended", "coming soon",
    "under construction", "database error",
)

PAID_SIGNALS = ("paid submission", "premium listing", "$", "pay to list")


def classify_url(url: str, hint: str | None = None) -> tuple[str, float]:
    """Cheap URL-only classification. Returns (kind, score 0-1)."""
    path = (urlsplit(url).path or "/").lower()
    host = (urlsplit(url).hostname or "").lower()
    haystack = f"{host}{path}"

    for kind, pattern in URL_RULES:
        if re.search(pattern, haystack):
            return kind, 0.45
    if hint and hint != KIND_UNKNOWN:
        return hint, 0.35
    return KIND_UNKNOWN, 0.1


def classify_page(url: str, html: str, url_kind: str = KIND_UNKNOWN) -> dict:
    """Inspect fetched HTML for submission signals.

    Returns a dict with kind, score, submission_url and the matched signals so
    every decision stays auditable in the database.
    """
    tree = HTMLParser(html or "")
    title_node = tree.css_first("title")
    title = (title_node.text(strip=True) if title_node else "")[:500]

    body_text = tree.body.text(separator=" ", strip=True).lower() if tree.body else ""
    body_text = re.sub(r"\s+", " ", body_text)[:200_000]

    matched: dict[str, list[str]] = {}
    scores: dict[str, float] = {}
    for kind, phrases in PAGE_SIGNALS.items():
        hits = [p for p in phrases if p in body_text]
        if hits:
            matched[kind] = hits
            scores[kind] = min(1.0, 0.3 + 0.15 * len(hits))

    # forms are the strongest signal that something can actually be submitted
    forms = tree.css("form")
    form_count = len(forms)
    has_signup_form = any(
        f.css_first('input[type="password"]') is not None for f in forms
    )
    has_url_field = any(
        f.css_first('input[name*="url" i], input[name*="website" i], input[name*="link" i]')
        is not None
        for f in forms
    )

    submission_url = _find_submission_link(url, tree)

    kind = url_kind
    score = 0.1
    if scores:
        kind = max(scores, key=scores.get)
        score = scores[kind]
    if url_kind != KIND_UNKNOWN and url_kind in scores:
        kind = url_kind
        score = max(score, scores[url_kind])
    elif url_kind != KIND_UNKNOWN and not scores:
        kind = url_kind
        score = 0.35

    if has_signup_form:
        score += 0.2
        if kind == KIND_UNKNOWN:
            kind = KIND_PROFILE
    if has_url_field:
        score += 0.2
        if kind == KIND_UNKNOWN:
            kind = KIND_DIRECTORY
    if submission_url:
        score += 0.1

    dead_end = [s for s in DEAD_END_SIGNALS if s in body_text]
    if dead_end:
        score = 0.0

    signals = {
        "title": title,
        "matched_phrases": matched,
        "forms": form_count,
        "signup_form": has_signup_form,
        "url_field": has_url_field,
        "paid_hint": any(s in body_text for s in PAID_SIGNALS[:3]),
        "dead_end": dead_end,
    }

    return {
        "kind": kind,
        "score": round(min(score, 1.0), 3),
        "title": title,
        "submission_url": submission_url,
        "signals": signals,
    }


def _find_submission_link(base_url: str, tree: HTMLParser) -> str | None:
    """Find the anchor most likely to lead to the submit/register form."""
    best: tuple[int, str] | None = None
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        text = a.text(strip=True).lower()
        blob = f"{text} {href.lower()}"
        for weight, anchor in enumerate(SUBMIT_ANCHORS):
            if anchor in blob:
                rank = len(SUBMIT_ANCHORS) - weight
                candidate = urljoin(base_url, href)
                if best is None or rank > best[0]:
                    best = (rank, candidate)
                break
    return best[1] if best else None
