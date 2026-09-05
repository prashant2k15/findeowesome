"""Find a real human contact for a prospect site.

Homepage first, then the most promising contact-ish page. Only two requests per
domain, throttled like everything else - this is prospecting, not scraping.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.parser import HTMLParser

from app.config import settings
from app.processors.url_cleaner import host_of, root_domain

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
# "name [at] domain [dot] com" and friends
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*(?:\[at\]|\(at\)|\s+at\s+|&#64;)\s*([A-Za-z0-9.\-]+)\s*(?:\[dot\]|\(dot\)|\s+dot\s+)\s*([A-Za-z]{2,24})",
    re.I,
)

CONTACT_HINTS = (
    "write-for-us", "write for us", "contribute", "guest-post", "guest post",
    "contact", "contact-us", "about", "about-us", "advertise", "submit",
    "editorial", "team", "impressum",
)

# Addresses that are never a human editor
JUNK_EMAIL_PARTS = (
    "example.com", "yourdomain", "domain.com", "email.com", "sentry.io",
    "wixpress.com", "godaddy.com", "squarespace.com", "shopify.com",
    "@2x.png", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    "noreply", "no-reply", "donotreply", "abuse@", "postmaster@",
    "privacy@", "dmca@", "legal@", "security@", "spam@",
)
# Preferred local parts, best first
PREFERRED_LOCALS = (
    "editor", "editorial", "content", "hello", "hi", "team", "press",
    "contact", "info", "admin", "support", "office", "mail",
)


def clean_emails(candidates: list[str], site_domain: str) -> list[str]:
    """Drop junk, prefer addresses on the site's own domain, best-first."""
    seen: set[str] = set()
    kept: list[str] = []
    for raw in candidates:
        email = raw.strip().strip(".,;:'\"<>()").lower()
        if not email or len(email) > 120 or email in seen:
            continue
        if any(part in email for part in JUNK_EMAIL_PARTS):
            continue
        if not EMAIL_RE.fullmatch(email):
            continue
        seen.add(email)
        kept.append(email)

    def rank(email: str) -> tuple[int, int]:
        local, _, domain = email.partition("@")
        same_site = 0 if root_domain(domain) == site_domain else 1
        try:
            local_rank = PREFERRED_LOCALS.index(local)
        except ValueError:
            local_rank = len(PREFERRED_LOCALS)
        return (same_site, local_rank)

    return sorted(kept, key=rank)


def extract_from_html(html: str, base_url: str) -> tuple[list[str], list[str], str]:
    """Return (emails, candidate contact links, page title)."""
    tree = HTMLParser(html or "")
    title_node = tree.css_first("title")
    title = title_node.text(strip=True)[:255] if title_node else ""

    emails: list[str] = []
    links: list[str] = []

    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if href.lower().startswith("mailto:"):
            emails.append(href[7:].split("?")[0])
            continue
        text = a.text(strip=True).lower()
        blob = f"{text} {href.lower()}"
        if any(h in blob for h in CONTACT_HINTS):
            links.append(urljoin(base_url, href))

    body = tree.body.text(separator=" ", strip=True) if tree.body else ""
    emails.extend(EMAIL_RE.findall(body))
    emails.extend(f"{m[0]}@{m[1]}.{m[2]}" for m in OBFUSCATED_RE.findall(body))

    # de-duplicate links, keep order, cap the follow-up crawl
    seen: set[str] = set()
    ordered = [u for u in links if not (u in seen or seen.add(u))]
    return emails, ordered[:4], title


def find_contact(client: httpx.Client, url: str) -> dict:
    """Fetch a site (plus one contact page) and pull out the best email."""
    site_domain = root_domain(host_of(url))
    result: dict = {
        "email": None,
        "emails": [],
        "contact_page": None,
        "title": None,
        "error": None,
    }

    try:
        r = client.get(url)
        if r.status_code >= 400:
            result["error"] = f"HTTP {r.status_code}"
            return result
        html = r.text
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return result

    emails, links, title = extract_from_html(html, str(r.url))
    result["title"] = title

    # Nothing on the homepage: follow the two most promising contact pages.
    # Directory sites usually hide the address one click deep, behind
    # "contact" or "write for us", and stopping at one page misses most of them.
    if not clean_emails(emails, site_domain) and links:
        for candidate in _ranked_contact_links(links)[:2]:
            result["contact_page"] = result["contact_page"] or candidate
            try:
                r2 = client.get(candidate)
            except Exception as exc:
                log.debug("contact page fetch failed for %s: %s", candidate, exc)
                continue
            if r2.status_code >= 400:
                continue
            more, _, _ = extract_from_html(r2.text, str(r2.url))
            emails.extend(more)
            result["contact_page"] = str(r2.url)
            if clean_emails(emails, site_domain):
                break
    elif links:
        result["contact_page"] = links[0]

    cleaned = clean_emails(emails, site_domain)
    result["emails"] = cleaned[:5]
    result["email"] = cleaned[0] if cleaned else None
    return result


def _ranked_contact_links(links: list[str]) -> list[str]:
    """Prefer a write-for-us/contribute page over a generic contact form."""
    def rank(u: str) -> int:
        low = u.lower()
        for i, hint in enumerate(CONTACT_HINTS):
            if hint.replace(" ", "-") in low or hint in low:
                return i
        return len(CONTACT_HINTS)

    return sorted(links, key=rank)


def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=settings.request_timeout,
        headers={"User-Agent": settings.user_agent, "Accept": "text/html"},
        follow_redirects=True,
        max_redirects=5,
        verify=False,
    )


def domain_of(url: str) -> str:
    return root_domain(urlsplit(url).hostname or "")
