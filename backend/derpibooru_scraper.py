"""Scraping/parsing logic for derpibooru.org (a Philomena tag/booru site).

Pure functions + a requests Session factory. No threads, no UI, no downloads —
the runner orchestrates those.

Unlike coomerfans/pawchive (which are per-account), derpibooru is a *booru*:
content is fetched by a **search query** (e.g. `artist:gunya`), not a user id.
The site exposes a full JSON API, so there is no HTML parsing — we hit the search
endpoint and normalise the JSON.

Login matters here: logged-out users can't see many images/videos. Passing an
account **API key** (`&key=…`) authenticates the request, and the **Everything
filter** (`filter_id=56027`) lifts the content filter, so the two together expose
everything the account is allowed to see.

Site structure (confirmed against the live API / gallery-dl's philomena extractor):
    Search page  : /search?q=<query>                     (what the user pastes)
    Search API   : /api/v1/json/search/images?q=<query>&page=N&per_page=50
                       &sf=created_at&sd=desc&filter_id=<id>&key=<apikey>
                   -> {"images": [ image, ... ], "total": N}
    Image object : {id, created_at (ISO8601), name (orig filename), format (ext:
                    png/jpg/gif/webm/mp4/svg...), mime_type, view_url (direct
                    original), representations: {full, mp4, webm, ...}, ...}
    Media URL    : view_url is the direct, unsigned, non-expiring original file
                   (Accept-Ranges: bytes -> resume works). Videos are usually
                   .webm with an .mp4 alternate under representations.

Media file names are the image ID (stable, unique), e.g.
"2026.05.05 - Derpibooru - 3456789.png" — see build_filename().
"""

import os
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs, quote

import requests

# Reuse the shared bits that are identical to coomerfans/pawchive.
from backend.coomerfans_scraper import (
    IMAGE_EXTS, VIDEO_EXTS, target_path, DEFAULT_UA,
)

BASE = "https://derpibooru.org"
API = BASE + "/api/v1/json"

PAGE_SIZE = 50               # search endpoint hard-caps per_page at 50
MAX_PAGE = 100_000           # safety cap on pagination
# "Everything" system filter — shows all content the account can access. The
# default site filter (filter_id=2) hides a lot even when logged in.
EVERYTHING_FILTER = "56027"

# platform slug -> display code used in filenames (Title case; default = .title()).
SITE_CODES = {"derpibooru": "Derpibooru"}


# ── Session ─────────────────────────────────────────────────────────

def make_session(user_agent=None, api_key=None):
    """requests Session with a browser UA. The API key is NOT stored on the
    session — it's a per-request query param (`key=`), so the runner appends it
    when building each URL. `api_key` is accepted here only for symmetry with the
    other scrapers' make_session()."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent or DEFAULT_UA,
        "Referer": BASE + "/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


# ── URL / naming helpers ────────────────────────────────────────────

def parse_creator_url(url):
    """A derpibooru search URL -> {"query": <decoded q>}, or None.

    Accepts anything on the derpibooru.org host that carries a `q=` param
    (the /search?q=… URL the site produces), and also a bare tag pasted after
    /search (e.g. /search?q=artist:gunya). Non-derpibooru URLs return None."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    host = (parts.netloc or "").lower().lstrip("www.")
    if "derpibooru.org" not in host:
        return None
    q = parse_qs(parts.query).get("q", [""])[0].strip()
    if not q:
        return None
    return {"query": q}


def query_label(query):
    """Human display name for a query. `artist:gunya` -> `gunya`; a bare tag or
    multi-term query is shown as-is."""
    q = (query or "").strip()
    m = re.match(r"^artist:(.+)$", q, re.I)
    if m:
        return m.group(1).strip()
    return q or "?"


def query_slug(query):
    """Filesystem/DB-safe slug for a query, used in the archive DB filename.
    'artist:gunya' -> 'artist-gunya'; 'safe, cute' -> 'safe-cute'."""
    q = (query or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", q).strip("-")
    return slug or "query"


def search_url(query):
    """Canonical search URL for a query (used to dedupe links)."""
    return f"{BASE}/search?q={quote(query or '', safe='')}"


def site_code(platform="derpibooru"):
    s = (platform or "").lower()
    return SITE_CODES.get(s, s.title() if s else "?")


def build_filename(dt, image_id, ext):
    """'2026.05.05 - Derpibooru - 3456789.png'.

    The image ID is stable and unique, so no collision suffix is normally needed
    (the runner still guards with add_index_suffix). Missing date -> '0000.00.00'
    (matches the other engines). To switch to the original uploaded name or an
    ID+name scheme later, this is the only function to change.
    """
    date_str = f"{dt:%Y.%m.%d}" if dt else "0000.00.00"
    ext = (ext or "bin").lower()
    return f"{date_str} - {site_code()} - {image_id}.{ext}"


def add_index_suffix(filename, n):
    """Insert '_n' before the extension for collision avoidance."""
    root, dot, ext = filename.rpartition(".")
    if dot and 1 <= len(ext) <= 8:
        return f"{root}_{n}.{ext}"
    return f"{filename}_{n}"


# ── Date parsing ────────────────────────────────────────────────────

def parse_dt(value):
    """ISO8601 timestamp (e.g. '2024-05-05T12:00:00Z' or with an offset) ->
    aware UTC datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── API fetching ────────────────────────────────────────────────────

def fetch_json(session, url, timeout=(15, 60)):
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _search_page_url(query, page, api_key, filter_id):
    """Build one search API URL. Order is stable so cached/retried URLs match."""
    params = [
        ("q", query or ""),
        ("page", str(page)),
        ("per_page", str(PAGE_SIZE)),
        ("sf", "created_at"),
        ("sd", "desc"),
        ("filter_id", str(filter_id or EVERYTHING_FILTER)),
    ]
    if api_key:
        params.append(("key", api_key))
    qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params)
    return f"{API}/search/images?{qs}"


def iter_images(session, query, api_key=None, filter_id=None,
                on_page=None, should_cancel=None, fetch=None):
    """Yield raw image dicts for a search query, paginating page=1.. until the
    returned page is short/empty or we've covered `total`.

    fetch(session, url) -> parsed JSON overrides the default, letting the caller
    add retry/backoff for transient server errors (returns None to abort)."""
    fetch = fetch or fetch_json
    page = 1
    seen = 0
    total = None
    while page <= MAX_PAGE:
        if should_cancel and should_cancel():
            return
        url = _search_page_url(query, page, api_key, filter_id)
        data = fetch(session, url)
        if data is None:                # fetch declined (e.g. cancelled mid-retry)
            return
        if not isinstance(data, dict):
            return
        if total is None:
            try:
                total = int(data.get("total"))
            except (TypeError, ValueError):
                total = None
        images = data.get("images") or []
        if not images:
            return
        if on_page:
            on_page(page, len(images))
        for raw in images:
            yield raw
        seen += len(images)
        if len(images) < PAGE_SIZE:
            return
        if total is not None and seen >= total:
            return
        page += 1


# ── Image normalisation ─────────────────────────────────────────────

def _ext_and_kind(raw):
    """Extension + kind (image/video) from the image's format field, falling
    back to the view_url extension. Unknown -> treated as image."""
    fmt = (raw.get("format") or "").lower().lstrip(".")
    if not fmt:
        path = urlsplit(raw.get("view_url") or "").path
        if "." in path:
            fmt = path.rsplit(".", 1)[-1].lower()
    fmt = fmt or "bin"
    if fmt in VIDEO_EXTS:
        return fmt, "video"
    if fmt in IMAGE_EXTS:
        return fmt, "image"
    # svg and any other on-site type: store it, serve it, treat as image.
    return fmt, "image"


def parse_image(raw):
    """Normalise a raw API image dict.

    Returns: {image_id, dt (UTC|None), ext, kind (image/video), url, name}
    `url` is the direct original (view_url). One media file per image.
    """
    image_id = str(raw.get("id") or "")
    dt = parse_dt(raw.get("created_at"))
    ext, kind = _ext_and_kind(raw)
    url = raw.get("view_url") or ""
    if not url:
        # Fallback to the full representation if view_url is ever absent.
        reps = raw.get("representations") or {}
        url = reps.get("full") or ""
    name = raw.get("name") or image_id
    return {
        "image_id": image_id,
        "dt": dt,
        "ext": ext,
        "kind": kind,
        "url": url,
        "name": name,
    }
