"""URL extraction, normalisation and junk filtering.

Everything entering the database goes through `normalize_url` so the unique
index on `opportunities.url` does the de-duplication work for us.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import tldextract

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())  # offline snapshot, no net calls

URL_RE = re.compile(r"""https?://[^\s<>"'\)\]\},;|`]+""", re.IGNORECASE)
BARE_DOMAIN_RE = re.compile(
    r"(?<![\w.@/-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])",
    re.IGNORECASE,
)

TRACKING_PREFIXES = ("utm_", "ga_", "mc_", "pk_", "hsa_", "wt_")
TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "mkt_tok",
    "ref", "referrer", "source", "campaign", "_ga", "_gl", "spm",
}

# Hosts that are infrastructure/noise rather than link opportunities.
NOISE_DOMAINS = {
    "github.com", "githubusercontent.com", "gitlab.com", "bitbucket.org",
    "google.com", "google.co.in", "bing.com", "duckduckgo.com", "yahoo.com",
    "w3.org", "schema.org", "example.com", "example.org", "localhost",
    "gstatic.com", "googleapis.com", "gravatar.com", "shields.io",
    "img.shields.io", "travis-ci.org", "codecov.io", "npmjs.com",
    "python.org", "pypi.org", "stackoverflow.com", "wikipedia.org",
    "archive.org", "web.archive.org", "youtube.com", "youtu.be",
    "t.me", "wa.me", "whatsapp.com", "paypal.me", "buymeacoffee.com",
    "creativecommons.org", "opensource.org", "mozilla.org",
}

# URL shorteners: useless as backlink targets and they poison the DB.
SHORTENERS = {
    "bit.ly", "tinyurl.com", "goo.gl", "ow.ly", "t.co", "is.gd", "buff.ly",
    "cutt.ly", "rb.gy", "shorturl.at", "rebrand.ly", "lnkd.in",
}

BAD_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z", ".exe", ".dmg", ".apk",
    ".mp3", ".mp4", ".avi", ".mkv", ".mov", ".wav", ".css", ".js", ".json",
    ".xml", ".rss", ".woff", ".woff2", ".ttf", ".eot", ".txt", ".md",
)

IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# Real ccTLDs that are far more often file extensions in a README
# (readme.md, setup.py, build.sh). Only applied to bare, schemeless matches.
FILE_LIKE_SUFFIXES = {
    "md", "sh", "py", "rb", "go", "js", "ts", "css", "json", "html", "htm",
    "xml", "txt", "php", "aspx", "jsp", "yml", "yaml", "ini", "cfg", "log",
    "bak", "exe", "dll", "so", "cs", "pl", "vb", "im",
}

# ---------------------------------------------------------------------------
# Spam patterns. Public "backlink lists" are dominated by open-redirect URLs
# that other people's spam campaigns pinged for indexing. They carry no link
# value at all, so they must never enter the database.
# ---------------------------------------------------------------------------
SPAM_URL_PATTERNS = (
    # ANY parameter whose value is itself an absolute URL: ?d=http, ?url=http,
    # ?buylink=http%3A%2F%2F ... - the signature of an open redirect. The useful
    # page is the site itself, never someone else's embedded target.
    re.compile(r"[?&][a-z0-9_.\-]{1,24}=(https?(%3a|:)|%2f%2f|//)", re.I),
    # expired-domain / trademark parking scripts
    re.compile(r"/__media__/js/(netsoltrademark|pagead)", re.I),
    # search-engine redirectors
    re.compile(r"^https?://(www\.|images\.|maps\.|cse\.|clients\d*\.|toolbarqueries\.)?(google|yandex|baidu)\.[a-z.]{2,12}/(url|imgres|search)", re.I),
    # ad servers and hit counters
    re.compile(r"(adclick|adserver|/ads/|banner(id|_click)|pagecountimg|counter\.php|/goto\.php|/click\.php)", re.I),
    # session/tracking junk and raw scripts
    re.compile(r"[?&](phpsessid|sid|jsessionid)=", re.I),
    # translate / cache / proxy mirrors
    re.compile(r"(translate\.google|webcache\.googleusercontent|cachedview|/cdn-cgi/l/email-protection)", re.I),
)


def is_spam_url(url: str) -> bool:
    """True for open-redirect / ad-server / parking junk that has no link value."""
    return any(p.search(url) for p in SPAM_URL_PATTERNS)


def spam_ratio(urls: list[str]) -> float:
    """Share of a list that is junk - used to score a whole seed source."""
    if not urls:
        return 0.0
    return sum(1 for u in urls if is_spam_url(u)) / len(urls)


def root_domain(host: str) -> str:
    ext = _EXTRACT(host)
    if not ext.suffix:
        return host.lower()
    return f"{ext.domain}.{ext.suffix}".lower()


def normalize_url(raw: str) -> str | None:
    """Return a canonical URL, or None if it should never enter the database."""
    if not raw:
        return None
    raw = raw.strip().strip(".,;:'\"<>()[]")
    if not raw:
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    if not raw.lower().startswith(("http://", "https://")):
        if "." not in raw or " " in raw:
            return None
        raw = "https://" + raw

    if is_spam_url(raw):
        return None

    try:
        parts = urlsplit(raw)
    except ValueError:
        return None

    host = (parts.hostname or "").lower().strip(".")
    if not host or "." not in host or IP_RE.match(host):
        return None
    if host.endswith((".local", ".localhost", ".test", ".invalid", ".onion")):
        return None

    if host.startswith("www."):
        host = host[4:]

    ext = _EXTRACT(host)
    if not ext.suffix or not ext.domain:
        return None  # "node.js", "index.php" and friends are not domains

    rd = f"{ext.domain}.{ext.suffix}".lower()
    if rd in NOISE_DOMAINS or rd in SHORTENERS or host in NOISE_DOMAINS:
        return None

    path = parts.path or "/"
    if path.lower().endswith(BAD_EXTENSIONS):
        return None
    # collapse duplicate slashes, drop trailing slash (except root)
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")

    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith(TRACKING_PREFIXES) and k.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_pairs))

    scheme = "https" if parts.scheme.lower() in ("http", "https") else None
    if scheme is None:
        return None

    netloc = host
    if parts.port and parts.port not in (80, 443):
        netloc = f"{host}:{parts.port}"

    url = urlunsplit((scheme, netloc, path, query, ""))
    if len(url) > 2000:
        return None
    return url


def extract_urls(text: str, include_bare_domains: bool = True) -> list[str]:
    """Pull every plausible URL out of a blob of markdown/HTML/CSV text."""
    found: list[str] = []
    seen: set[str] = set()

    for m in URL_RE.finditer(text or ""):
        n = normalize_url(m.group(0))
        if n and n not in seen:
            seen.add(n)
            found.append(n)

    if include_bare_domains:
        stripped = URL_RE.sub(" ", text or "")
        for m in BARE_DOMAIN_RE.finditer(stripped):
            candidate = m.group(1)
            if candidate.lower().endswith(BAD_EXTENSIONS):
                continue
            if candidate.rsplit(".", 1)[-1].lower() in FILE_LIKE_SUFFIXES:
                continue  # readme.md, setup.py, main.go ... not domains here
            n = normalize_url(candidate)
            if n and n not in seen:
                seen.add(n)
                found.append(n)

    return found


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
