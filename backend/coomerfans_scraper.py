"""Scraping/parsing logic for coomerfans.com.

Pure functions + a requests Session factory. No threads, no UI, no downloads —
the runner orchestrates those. coomerfans.com has no JSON API; pages are
server-rendered HTML, so everything here is HTML parsing.

Site structure (confirmed against live pages):
    Creator page : /u/{service}/{id}/{name}   (pagination: ?page=N, "Next" link)
    Post page    : /p/{postId}/{userId}/{service}
    Post media   : <div class="post-wrap"><div class="post-body">
                       <img src="https://img1.coomerfans.com/storage/.../x.jpg">
                       <video><source src="https://img1.coomerfans.com/storage/.../x.mp4?e=..&hash=..">
    Post date    : <span class="post-date">Added 2025-08-27 15:49:03 +0000 UTC</span>

Avatar/recommended-creator thumbnails live at coomerfans.com/istorage/{id}.jpg
and are deliberately excluded — only img1.../storage/ URLs inside post-body are
real post media.
"""

import os
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

BASE = "https://coomerfans.com"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# service slug -> short code used in filenames
SERVICE_CODES = {"onlyfans": "OF", "fansly": "Fansly"}

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}
VIDEO_EXTS = {"mp4", "webm", "m4v", "mov", "avi", "mpg", "mpeg", "wmv", "mkv"}

_CREATOR_RE = re.compile(r"/u/([^/]+)/([^/]+)/([^/?#]+)")
_POST_PATH_RE = re.compile(r"^/p/[^/]+/[^/]+/[^/?#]+$")
_POST_PARTS_RE = re.compile(r"/p/([^/]+)/([^/]+)/([^/?#]+)")
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


# ── Session ─────────────────────────────────────────────────────────

def make_session(user_agent=None, cookies_path=None):
    """Create a requests Session with browser-like headers.

    coomerfans content is public, so cookies are optional; a Netscape
    cookies.txt can be supplied if a creator is ever gated.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent or DEFAULT_UA,
        "Referer": BASE + "/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    if cookies_path and os.path.isfile(cookies_path):
        try:
            from http.cookiejar import MozillaCookieJar
            jar = MozillaCookieJar()
            jar.load(cookies_path, ignore_discard=True, ignore_expires=True)
            s.cookies = jar
        except Exception:
            pass
    return s


# ── URL / naming helpers ────────────────────────────────────────────

def parse_creator_url(url):
    """/u/{service}/{id}/{name} -> dict, or None if not a creator URL."""
    m = _CREATOR_RE.search(url or "")
    if not m:
        return None
    return {
        "service": m.group(1).lower(),
        "user_id": m.group(2),
        "name": m.group(3),
    }


def parse_post_url(url):
    """/p/{postId}/{userId}/{service} -> dict, or None."""
    m = _POST_PARTS_RE.search(url or "")
    if not m:
        return None
    return {
        "post_id": m.group(1),
        "user_id": m.group(2),
        "service": m.group(3).lower(),
    }


def site_code(service):
    return SERVICE_CODES.get((service or "").lower(), (service or "").upper())


def slugify(title, fallback):
    """Lowercase hyphenated slug from a post title, truncated to ~60 chars.

    Falls back to `fallback` (the post id) when there is no usable title.
    """
    if title:
        t = title.strip().strip(".").replace("…", "")
        slug = re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-").lower()
        slug = slug[:60].strip("-")
        if slug:
            return slug
    return str(fallback)


def ext_from_url(url, default="bin"):
    path = urlsplit(url).path
    if "." in path:
        ext = path.rsplit(".", 1)[-1].lower()
        if 1 <= len(ext) <= 5 and ext.isalnum():
            return ext
    return default


def build_filename(dt, service, name, index, ext):
    """e.g. '2026.01.07 - OF - 89344156_1.mp4' (name is the post_id)."""
    date_str = f"{dt:%Y.%m.%d}" if dt else "0000.00.00"
    return f"{date_str} - {site_code(service)} - {name}_{index}.{ext}"


def target_path(destination, kind, dt, filename):
    """Videos -> <dest>/<year>/, images -> <dest>/Images/<year>/.

    'Images' is capitalized to match the Twitter layout so every creator's image
    folder has one consistent name. (Windows/NAS shares are case-insensitive, so
    this also resolves to any pre-existing lowercase 'images' folder.)"""
    year = f"{dt:%Y}" if dt else "unknown"
    if kind == "image":
        return os.path.join(destination, "Images", year, filename)
    return os.path.join(destination, year, filename)


# ── Year-range scoping (shared by pawchive + coomerfans crawls) ─────

def norm_year_range(year=None, year_range=None):
    """Normalise a year filter to inclusive `(lo, hi)` bounds — ints or None — or
    return None meaning "all years".

    `year_range` wins when given: a {"start", "end"} dict or a (lo, hi) sequence,
    each bound optional (None/""/"null" → open-ended). Otherwise a single scalar
    `year` collapses to `(y, y)` — so the existing single-year "Download Year" path
    is just the degenerate range and behaves identically. Bounds are ordered so
    lo <= hi. Blank on both sides → None (no filtering)."""
    def _i(v):
        if v in (None, "", "null"):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    lo = hi = None
    if year_range is not None:
        if isinstance(year_range, dict):
            lo, hi = _i(year_range.get("start")), _i(year_range.get("end"))
        elif isinstance(year_range, (list, tuple)) and len(year_range) == 2:
            lo, hi = _i(year_range[0]), _i(year_range[1])
    if lo is None and hi is None:
        y = _i(year)
        if y is not None:
            lo = hi = y
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return (lo, hi)


def year_in_range(y, rng):
    """True if year `y` (int or None) falls within an inclusive `(lo, hi)` range.
    A None range means "all years" (always True). An undated post (`y is None`) is
    NOT in any bounded range — it can't be placed, so year-scoped runs skip it (the
    same choice the old single-year filter made)."""
    if rng is None:
        return True
    if y is None:
        return False
    lo, hi = rng
    if lo is not None and y < lo:
        return False
    if hi is not None and y > hi:
        return False
    return True


def year_scan_decision(y, rng):
    """Crawl gate for a listing walked strictly NEWEST->OLDEST by date.

    Returns one of:
      'stop' — year `y` is below the range's lower bound, so every remaining (older)
               post is out of range too → stop paginating (the early-stop optimisation).
      'skip' — out of range but not past it (e.g. newer than the upper bound, or
               undated) → skip this post but keep scanning older ones.
      'take' — in range → download it.
    A None range means no scoping, so always 'take'. Undated posts (`y is None`) never
    trigger 'stop' (we can't prove older posts are out of range from an undated one)."""
    if rng is None:
        return "take"
    lo, _ = rng
    if lo is not None and y is not None and y < lo:
        return "stop"
    return "take" if year_in_range(y, rng) else "skip"


# ── Page fetching / parsing ─────────────────────────────────────────

def fetch_html(session, url, timeout=(15, 60)):
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def iter_post_urls(session, creator_url, on_page=None, should_cancel=None,
                   max_pages=100000, fetch=None):
    """Yield every post URL for a creator, walking ?page=N until exhausted.

    Stops when a page yields no new post links or there is no "Next" link.
    on_page(page_num, new_count) is called after each page is parsed.
    should_cancel() -> bool lets the caller abort the crawl.
    fetch(session, url) -> html overrides the default fetch_html, letting the
    caller add retry/backoff for transient server errors.
    """
    fetch = fetch or fetch_html
    seen = set()
    base = creator_url.split("?")[0].split("#")[0]
    page = 1
    while page <= max_pages:
        if should_cancel and should_cancel():
            return
        page_url = base if page == 1 else f"{base}?page={page}"
        html = fetch(session, page_url)
        if html is None:        # fetch declined (e.g. cancelled mid-retry)
            return
        soup = BeautifulSoup(html, "html.parser")

        new_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if _POST_PATH_RE.match(href):
                full = urljoin(BASE, href)
                if full not in seen:
                    seen.add(full)
                    new_links.append(full)

        if on_page:
            on_page(page, len(new_links))

        if not new_links:
            return
        for u in new_links:
            yield u

        has_next = any(
            "page=" in a["href"] and a.get_text(strip=True).lower() == "next"
            for a in soup.find_all("a", href=True)
        )
        if not has_next:
            return
        page += 1


def parse_post(session, post_url):
    """Parse a single post page into structured media metadata.

    Returns dict: {post_id, user_id, service, title, dt (UTC datetime|None),
                   url, media: [{url, path_key, kind, ext}]}
    Media are collected in document order and de-duplicated by storage path.
    """
    html = fetch_html(session, post_url)
    return parse_post_html(html, post_url)


def parse_post_html(html, post_url):
    soup = BeautifulSoup(html, "html.parser")
    parts = parse_post_url(post_url) or {}

    wrap = soup.select_one("div.post-wrap") or soup

    h1 = wrap.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    dt = None
    date_el = wrap.select_one("span.post-date")
    date_text = date_el.get_text(" ", strip=True) if date_el else ""
    dm = _DATE_RE.search(date_text) or _DATE_RE.search(html)
    if dm:
        y, mo, d, h, mi, s = map(int, dm.groups())
        try:
            dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
        except ValueError:
            dt = None

    body = wrap.select_one("div.post-body") or wrap
    media = []
    seen_paths = set()
    for el in body.find_all(["img", "source", "video"]):
        src = el.get("src") or el.get("data-src")
        if not src:
            continue
        full = urljoin(BASE, src)
        # only real post media: img1.../storage/, never /istorage/ avatars
        if "/istorage/" in full or "/storage/" not in full:
            continue
        path_key = urlsplit(full).path
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)

        ext = ext_from_url(full, default="")
        if el.name in ("source", "video") or ext in VIDEO_EXTS:
            kind = "video"
            ext = ext if ext in VIDEO_EXTS else "mp4"
        else:
            kind = "image"
            ext = ext if ext in IMAGE_EXTS else "jpg"

        media.append({"url": full, "path_key": path_key, "kind": kind, "ext": ext})

    return {
        "post_id": parts.get("post_id"),
        "user_id": parts.get("user_id"),
        "service": parts.get("service"),
        "title": title,
        "dt": dt,
        "url": post_url,
        "media": media,
    }


def refresh_media_url(session, post_url, path_key):
    """Re-parse a post and return a fresh (possibly re-signed) URL for the
    media item identified by its storage path. Used when a signed video URL
    has expired mid-queue. Returns the new URL or None.
    """
    try:
        info = parse_post(session, post_url)
    except Exception:
        return None
    for m in info["media"]:
        if m["path_key"] == path_key:
            return m["url"]
    return None
