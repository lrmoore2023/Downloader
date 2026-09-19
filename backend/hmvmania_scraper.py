"""hmvmania.com — metadata-only author listing for the PMV tracker.

WordPress (viewtube theme) with videos as the `video` post type. The author
archive page renders its grid through the theme's admin-ajax loader, and both
the REST API (`/wp-json/…`, `?rest_route=`) and the ajax endpoint sit behind a
Cloudflare WAF rule that 403s non-browser clients. The author **RSS feed** is
not covered by that rule and pages cleanly (verified live 2026-09-19):

    https://hmvmania.com/author/<slug>/feed/?post_type=video            (page 1)
    https://hmvmania.com/author/<slug>/feed/?post_type=video&paged=N    (N ≥ 2)

10 items per page, newest first; a page past the end answers 404 with a
"Page not found" channel. Each <item> carries the title (prefixed
"[Author] "), the /video/<slug>/ link, an RFC-822 pubDate, the numeric post id
in the guid (?p=NNNN) and dc:creator = the author's display name.
"""

import html as _html
import re
from email.utils import parsedate_to_datetime

import requests

from backend.coomerfans_scraper import DEFAULT_UA

BASE = "https://hmvmania.com"
PAGE_SIZE = 10
MAX_PAGES = 2000

_AUTHOR_RE = re.compile(r"hmvmania\.com/author/([^/?#]+)", re.I)
_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_GUID_ID_RE = re.compile(r"[?&](?:amp;|#038;)?p=(\d+)")
_CDATA_RE = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.S)
_LEADING_TAG_RE = re.compile(r"^\s*\[[^\]]{1,60}\]\s*[-–:]?\s*")


class SiteError(Exception):
    pass


class NotFound(SiteError):
    pass


# ── URLs ────────────────────────────────────────────────────────────

def parse_author_url(url):
    m = _AUTHOR_RE.search(url or "")
    return {"slug": m.group(1).lower()} if m else None


def author_url(slug):
    return f"{BASE}/author/{slug}/"


def feed_url(slug, page=1):
    base = f"{BASE}/author/{slug}/feed/?post_type=video"
    return base if int(page) <= 1 else f"{base}&paged={int(page)}"


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": BASE + "/",
    })
    return s


# ── parsing ─────────────────────────────────────────────────────────

def _tag(block, name):
    m = re.search(rf"<{name}(?:\s[^>]*)?>(.*?)</{name}>", block, re.S | re.I)
    if not m:
        return ""
    v = m.group(1)
    c = _CDATA_RE.match(v)
    if c:
        v = c.group(1)
    return _html.unescape(v).strip()


def _date_iso(rfc822):
    if not rfc822:
        return None
    try:
        return parsedate_to_datetime(rfc822).isoformat(timespec="seconds")
    except (TypeError, ValueError, IndexError):
        return None


def clean_title(title):
    """'[Minahakuba] Yumiko Kimura x Senorita' → 'Yumiko Kimura x Senorita'."""
    return _LEADING_TAG_RE.sub("", title or "", count=1).strip() or (title or "").strip()


def parse_feed(xml):
    """{"items": [{id, title, url, date, author}], "not_found": bool,
        "page": n|None, "pages": n|None, "author": display name}"""
    xml = xml or ""
    ch_title = ""
    m = re.search(r"<channel>\s*<title>(.*?)</title>", xml, re.S | re.I)
    if m:
        ch_title = _html.unescape(m.group(1)).strip()
    not_found = "page not found" in ch_title.lower()
    page = pages = None
    m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", ch_title)
    if m:
        page, pages = int(m.group(1)), int(m.group(2))
    items, author = [], ""
    for block in _ITEM_RE.findall(xml):
        link = _tag(block, "link")
        guid = _tag(block, "guid")
        gm = _GUID_ID_RE.search(guid) or _GUID_ID_RE.search(_html.unescape(guid))
        vid = gm.group(1) if gm else (link.rstrip("/").rsplit("/", 1)[-1] if link else "")
        if not vid:
            continue
        creator = _tag(block, "dc:creator")
        author = author or creator
        items.append({
            "id": vid,
            "title": clean_title(_tag(block, "title")),
            "url": link,
            "date": _date_iso(_tag(block, "pubDate")),
            "author": creator,
        })
    return {"items": items, "not_found": not_found, "page": page, "pages": pages, "author": author}


# ── fetching ────────────────────────────────────────────────────────

def _retry_after(resp, default):
    try:
        return float(resp.headers.get("Retry-After") or default)
    except (TypeError, ValueError):
        return default


def fetch_text(session, url, throttle=None, should_cancel=None, retries=3, timeout=(15, 60)):
    cancelled = should_cancel or (lambda: False)
    last = "no attempt"
    for attempt in range(retries + 1):
        if cancelled():
            raise SiteError("cancelled")
        if throttle:
            throttle.wait(cancelled)
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
        except requests.RequestException as e:
            last = e.__class__.__name__
            if throttle:
                throttle.on_throttled(1.5 * (attempt + 1))
            continue
        if r.status_code == 404:
            raise NotFound(url)
        if r.status_code == 429 or 500 <= r.status_code < 600:
            last = f"HTTP {r.status_code}"
            if throttle:
                throttle.on_throttled(_retry_after(r, 2.0 * (attempt + 1)))
            continue
        if r.status_code == 403:
            raise SiteError("HTTP 403 (Cloudflare) — the feed endpoint was blocked")
        if r.status_code != 200:
            raise SiteError(f"HTTP {r.status_code} for {url}")
        if throttle:
            throttle.on_success()
        return r.text
    raise SiteError(f"{last} after {retries + 1} attempts: {url}")


def fetch_author(session, slug, throttle=None, should_cancel=None):
    """{name, total_pages} from the first feed page (name = dc:creator)."""
    feed = parse_feed(fetch_text(session, feed_url(slug, 1), throttle, should_cancel))
    if feed["not_found"]:
        raise NotFound(slug)
    name = feed["author"] or slug.replace("-", " ").title()
    return {"name": name, "pages": feed["pages"]}


def iter_feed_pages(session, slug, throttle=None, should_cancel=None):
    """Yield (page_no, [items]) newest-first until a 404 / not-found / empty page."""
    page = 1
    while page <= MAX_PAGES:
        if should_cancel and should_cancel():
            return
        try:
            xml = fetch_text(session, feed_url(slug, page), throttle, should_cancel)
        except NotFound:
            return
        feed = parse_feed(xml)
        if feed["not_found"] or not feed["items"]:
            return
        yield page, feed["items"]
        if feed["pages"] and page >= feed["pages"]:
            return
        page += 1
