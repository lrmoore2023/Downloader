"""pmvhaven.com — metadata-only uploader listing for the PMV tracker.

Nuxt site with a public JSON API (verified live 2026-09-19):

    GET https://pmvhaven.com/api/users/<24-hex id>
        → {"success": true, "data": {"_id", "username", "createdAt", …}}
    GET https://pmvhaven.com/api/videos?uploader=<id>&limit=100&page=N   (1-based)
        → {"success": true, "videos": [...], "pagination": {"page", "limit",
           "total", "totalPages", "hasNext", "hasPrev"}}

A video record has `_id`, `title`, `uploadDate`/`releaseDate`, `durationSeconds`,
`width`/`height` (missing while still processing), `isReleased`, and an `oldId`
for videos migrated from the previous site. Its page is
`/video/<slugified title>_<oldId or _id>`.

Profile URLs come as `/profile/<id>` or `/profile/<username>`; the users API
only takes the id, so a username is resolved from the profile page's embedded
Nuxt payload (a `user-profile-…` entry holding `userId`).
"""

import json
import re
import unicodedata

import requests

from backend.coomerfans_scraper import DEFAULT_UA

SITE = "https://pmvhaven.com"
API = SITE + "/api"
PAGE_LIMIT = 100
MAX_PAGES = 2000

_PROFILE_RE = re.compile(r"pmvhaven\.com/profile/([^/?#]+)", re.I)
_HEX24_RE = re.compile(r"^[0-9a-fA-F]{24}$")


class SiteError(Exception):
    pass


class NotFound(SiteError):
    pass


# ── URLs ────────────────────────────────────────────────────────────

def parse_profile_url(url):
    m = _PROFILE_RE.search(url or "")
    if not m:
        return None
    ident = m.group(1)
    if _HEX24_RE.match(ident):
        return {"user_id": ident.lower(), "username": ""}
    return {"user_id": "", "username": ident}


def profile_url(user_id):
    return f"{SITE}/profile/{user_id}"


def slugify(title):
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
    return t


def video_url(raw):
    vid = raw.get("oldId") or raw.get("_id") or ""
    slug = slugify(raw.get("title") or "")
    return f"{SITE}/video/{slug}_{vid}" if slug else f"{SITE}/video/{vid}"


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": SITE + "/",
    })
    return s


# ── parsing ─────────────────────────────────────────────────────────

def _norm_date(s):
    if not s:
        return None
    s = str(s).strip()
    s = re.sub(r"\.\d+Z$", "Z", s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return s


def parse_video(raw):
    w, h = raw.get("width"), raw.get("height")
    quality = min(w, h) if isinstance(w, int) and isinstance(h, int) and w and h else None
    dur = raw.get("durationSeconds")
    return {
        "id": str(raw.get("_id") or ""),
        "title": raw.get("title") or "",
        "url": video_url(raw),
        "date": _norm_date(raw.get("uploadDate") or raw.get("releaseDate")),
        "duration": int(dur) if isinstance(dur, (int, float)) else None,
        "quality": quality,
        "released": bool(raw.get("isReleased", True)),
    }


def parse_profile(data):
    d = (data or {}).get("data") or {}
    return {"id": d.get("_id") or "", "name": d.get("username") or "", "username": d.get("username") or ""}


def user_id_from_profile_html(html):
    """Pull the 24-hex user id out of a /profile/<username> page's Nuxt payload."""
    html = html or ""
    m = re.search(r'id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            # devalue format: a flat list; dicts reference other entries by index.
            for entry in data:
                if isinstance(entry, dict) and "userId" in entry and "username" in entry:
                    ref = entry["userId"]
                    val = data[ref] if isinstance(ref, int) and 0 <= ref < len(data) else ref
                    if isinstance(val, str) and _HEX24_RE.match(val):
                        return val.lower()
        except (ValueError, TypeError, IndexError):
            pass
    m = re.search(r'user-profile-([0-9a-fA-F]{24})', html)
    if m:
        return m.group(1).lower()
    m = re.search(r'"uploaderId"\s*:\s*"([0-9a-fA-F]{24})"', html)
    return m.group(1).lower() if m else ""


# ── fetching ────────────────────────────────────────────────────────

def _retry_after(resp, default):
    try:
        return float(resp.headers.get("Retry-After") or default)
    except (TypeError, ValueError):
        return default


def _get(session, url, params=None, throttle=None, should_cancel=None, retries=3, timeout=(15, 60)):
    cancelled = should_cancel or (lambda: False)
    last = "no attempt"
    for attempt in range(retries + 1):
        if cancelled():
            raise SiteError("cancelled")
        if throttle:
            throttle.wait(cancelled)
        try:
            r = session.get(url, params=params, timeout=timeout)
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
        if r.status_code != 200:
            raise SiteError(f"HTTP {r.status_code} for {url}")
        if throttle:
            throttle.on_success()
        return r
    raise SiteError(f"{last} after {retries + 1} attempts: {url}")


def fetch_json(session, url, params=None, throttle=None, should_cancel=None):
    r = _get(session, url, params, throttle, should_cancel)
    try:
        return r.json()
    except ValueError:
        raise SiteError(f"non-JSON response for {url}")


def fetch_profile(session, user_id, throttle=None, should_cancel=None):
    p = parse_profile(fetch_json(session, f"{API}/users/{user_id}", throttle=throttle, should_cancel=should_cancel))
    if not p["id"]:
        raise SiteError("users response had no id")
    return p


def resolve_username(session, username, throttle=None, should_cancel=None):
    """username → 24-hex user id via the profile page, or '' when not found."""
    r = _get(session, f"{SITE}/profile/{username}", throttle=throttle, should_cancel=should_cancel)
    return user_id_from_profile_html(r.text)


def iter_videos(session, user_id, throttle=None, should_cancel=None, limit=PAGE_LIMIT):
    """Yield (page_no, [raw videos], pagination) newest-first until hasNext is false
    or a page comes back empty."""
    page = 1
    while page <= MAX_PAGES:
        if should_cancel and should_cancel():
            return
        data = fetch_json(session, f"{API}/videos",
                          params={"uploader": user_id, "limit": limit, "page": page},
                          throttle=throttle, should_cancel=should_cancel)
        videos = (data or {}).get("videos") or []
        pagination = (data or {}).get("pagination") or {}
        if not videos:
            return
        yield page, videos, pagination
        if not pagination.get("hasNext", len(videos) >= limit):
            return
        page += 1
