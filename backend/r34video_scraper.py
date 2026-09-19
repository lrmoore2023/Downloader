"""rule34video.com — metadata-only listing scraper for the PMV tracker.

The site runs the KVS (Kernel Video Sharing) engine. A member page renders the
first 8 uploads inline; every page — including the first — is also served as an
async block at

    /members/<id>/?mode=async&function=get_block
        &block_id=list_videos_uploaded_videos&sort_by=&from_videos=<NN>

with NN zero-padded (01, 02, …), newest first, and **HTTP 404 past the last
page**, which is the clean end-of-listing signal. Listing items carry only the id,
title, thumbnail, duration and a generic "HD" badge; the exact upload date and the
best available quality (`'4k'` → 2160p) live on the video page, so the runner
fetches that once per *new* video. Verified live 2026-09-18: oldest-first listing
order reproduces the user's own catalogue numbers.

Plain HTTP works (no Cloudflare / bot-guard on these endpoints), so this uses a
stock `requests` session.
"""

import html as _html
import re
import time

import requests
from bs4 import BeautifulSoup

from backend.coomerfans_scraper import DEFAULT_UA

BASE = "https://rule34video.com"
PAGE_SIZE = 8
MAX_PAGES = 5000

_MEMBER_RE = re.compile(r"rule34video\.com/members/(\d+)", re.I)
_VIDEO_ID_RE = re.compile(r"/video/(\d+)/")
_ALL_VIDEOS_RE = re.compile(r"All Videos\s*\((\d[\d,]*)\)")
_UPLOAD_DATE_RE = re.compile(r'"uploadDate"\s*:\s*"([^"]+)"')
_ISO_DUR_RE = re.compile(r'"duration"\s*:\s*"(PT[^"]+)"')
_QUALITY_TEXT_RE = re.compile(r"video(?:_alt)?_url\d*_text\s*:\s*'([^']*)'")
_DL_ROW_RE = re.compile(r">\s*MP4\s+(\d{3,4})p\s*<", re.I)
_ISO8601_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?")


class SiteError(Exception):
    """The site answered in a way we can't use (after retries)."""


class NotFound(SiteError):
    """HTTP 404 — end of listing, or a member that no longer exists."""


# ── URLs ────────────────────────────────────────────────────────────

def parse_member_url(url):
    m = _MEMBER_RE.search(url or "")
    return {"user_id": m.group(1)} if m else None


def member_url(user_id):
    return f"{BASE}/members/{user_id}/"


def listing_url(user_id, page):
    return (f"{BASE}/members/{user_id}/?mode=async&function=get_block"
            f"&block_id=list_videos_uploaded_videos&sort_by=&from_videos={int(page):02d}")


def video_id_from_url(url):
    m = _VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else None


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": BASE + "/",
    })
    return s


# ── parsing ─────────────────────────────────────────────────────────

def parse_member_page(html):
    """{name, total} from a member page. Name comes from '<title>X's Page</title>'."""
    name = ""
    m = re.search(r"<title>\s*(.*?)\s*</title>", html or "", re.S | re.I)
    if m:
        t = _html.unescape(m.group(1)).strip()
        t = re.sub(r"['’]s Page$", "", t).strip()
        name = t
    total = None
    m = _ALL_VIDEOS_RE.search(html or "")
    if m:
        total = int(m.group(1).replace(",", ""))
    return {"name": name, "total": total}


def parse_duration_text(text):
    """'3:38' → 218, '1:02:03' → 3723, else None."""
    parts = [p for p in (text or "").strip().split(":") if p != ""]
    if not parts or not all(p.isdigit() for p in parts):
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + int(p)
    return secs


def parse_iso8601_duration(text):
    """'PT0H3M40S' → 220."""
    m = _ISO8601_RE.fullmatch((text or "").strip())
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + int(float(s or 0))


def parse_listing(html):
    """Video cards from a member page or an async listing block, in page order
    (newest first). Only the uploaded-videos container is read when present, so a
    full member page's favourites / related blocks never leak in."""
    soup = BeautifulSoup(html or "", "html.parser")
    container = soup.find(id="list_videos_uploaded_videos_items") or soup
    out = []
    for card in container.select("div.item[data-video-card-id]"):
        vid = (card.get("data-video-card-id") or "").strip()
        a = card.select_one("a.th[href]") or card.select_one("a[href*='/video/']")
        if not a:
            continue
        url = a.get("href") or ""
        if not vid:
            vid = video_id_from_url(url) or ""
        if not vid:
            continue
        title = (a.get("title") or "").strip()
        if not title:
            t = card.select_one(".thumb_title")
            title = t.get_text(" ", strip=True) if t else ""
        tm = card.select_one(".time")
        out.append({
            "id": vid,
            "title": _html.unescape(title),
            "url": url,
            "duration_text": tm.get_text(strip=True) if tm else "",
            "duration": parse_duration_text(tm.get_text(strip=True)) if tm else None,
        })
    return out


def _quality_from_text(text):
    t = (text or "").strip().lower()
    if t in ("4k", "2160p", "uhd"):
        return 2160
    m = re.fullmatch(r"(\d{3,4})p?", t)
    return int(m.group(1)) if m else None


def parse_video_page(html):
    """{title, date, duration, quality, uploader_id} from a video page.

    quality = the best rendition offered (2160 when a '4k' source exists)."""
    html = html or ""
    title = ""
    m = re.search(r"<title>\s*(.*?)\s*</title>", html, re.S | re.I)
    if m:
        title = _html.unescape(m.group(1)).strip()
    date = None
    m = _UPLOAD_DATE_RE.search(html)
    if m:
        date = m.group(1).strip()[:10]
    duration = None
    m = _ISO_DUR_RE.search(html)
    if m:
        duration = parse_iso8601_duration(m.group(1))
    qualities = [q for q in (_quality_from_text(t) for t in _QUALITY_TEXT_RE.findall(html)) if q]
    if not qualities:
        qualities = [int(x) for x in _DL_ROW_RE.findall(html)]
    quality = max(qualities) if qualities else None
    uploader = None
    m = _MEMBER_RE.search(html)
    if m:
        uploader = m.group(1)
    return {"title": title, "date": date, "duration": duration,
            "quality": quality, "uploader_id": uploader}


# ── fetching ────────────────────────────────────────────────────────

def _retry_after(resp, default):
    try:
        return float(resp.headers.get("Retry-After") or default)
    except (TypeError, ValueError):
        return default


def fetch_text(session, url, throttle=None, should_cancel=None, retries=3, timeout=(15, 60)):
    """GET with pacing + bounded retries. 404 → NotFound (never retried);
    429/5xx and connection errors back off and retry; anything else → SiteError."""
    cancelled = should_cancel or (lambda: False)
    last = "no attempt"
    for attempt in range(retries + 1):
        if cancelled():
            raise SiteError("cancelled")
        if throttle:
            throttle.wait(cancelled)
        try:
            r = session.get(url, timeout=timeout)
        except requests.RequestException as e:
            last = f"{e.__class__.__name__}"
            if throttle:
                throttle.on_throttled(1.5 * (attempt + 1))
            else:
                time.sleep(1.0)
            continue
        if r.status_code == 404:
            raise NotFound(url)
        if r.status_code == 429 or 500 <= r.status_code < 600:
            last = f"HTTP {r.status_code}"
            wait = _retry_after(r, 2.0 * (attempt + 1))
            if throttle:
                throttle.on_throttled(wait)
            else:
                time.sleep(min(wait, 10))
            continue
        if r.status_code != 200:
            raise SiteError(f"HTTP {r.status_code} for {url}")
        if throttle:
            throttle.on_success()
        return r.text
    raise SiteError(f"{last} after {retries + 1} attempts: {url}")


def fetch_member(session, user_id, throttle=None, should_cancel=None):
    return parse_member_page(fetch_text(session, member_url(user_id), throttle, should_cancel))


def iter_listing_pages(session, user_id, throttle=None, should_cancel=None):
    """Yield (page_no, [items]) newest-first until the site 404s or a page is empty."""
    page = 1
    while page <= MAX_PAGES:
        if should_cancel and should_cancel():
            return
        try:
            html = fetch_text(session, listing_url(user_id, page), throttle, should_cancel)
        except NotFound:
            return
        items = parse_listing(html)
        if not items:
            return
        yield page, items
        page += 1


def fetch_video_detail(session, url, throttle=None, should_cancel=None):
    return parse_video_page(fetch_text(session, url, throttle, should_cancel))
