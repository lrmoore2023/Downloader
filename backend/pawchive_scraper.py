"""Scraping/parsing logic for pawchive.st (a kemono-family archive site).

Pure functions + a requests Session factory. No threads, no UI, no downloads —
the runner orchestrates those.

Unlike coomerfans.com, pawchive exposes a **full JSON API**, so there is no HTML
parsing of listing/post pages — we hit the API and normalise the JSON. Only the
post *body* (`content`) is HTML, which we parse to pull out external links.

Site structure (confirmed against live API):
    Creator page : /{service}/user/{id}                (service ∈ patreon, fanbox, ...)
    Post page    : /{service}/user/{id}/post/{postId}
    Profile API  : /api/v1/{service}/user/{id}/profile -> {id, name, service, ...}
    Listing API  : /api/v1/{service}/user/{id}?o=N     -> [post, ...] (50/page, [] at end)
    Post API     : /api/v1/{service}/user/{id}/post/{postId} -> post
    Post object  : {id, user, service, title, content (HTML), published/added/edited,
                    file: {name, path}|{}, attachments: [{name, path}],
                    embed: {url, subject, description}|{}, tags, next, prev,
                    detail_fetched: bool, has_full: bool}
    Media URL    : file/attachment `path` is /xx/yy/<sha256>.ext (content-addressed);
                   full file = https://file.pawchive.st/data{path}?f=<name>.
                   No signed URLs / no expiry; Accept-Ranges: bytes (resume works).

Media file names are the file's OWN name (e.g. "Label stream night X teaser.mp4"),
not the post id — see build_filename().
"""

import os
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, quote

import requests
from bs4 import BeautifulSoup

# Reuse the shared bits that are identical to coomerfans.
from backend.coomerfans_scraper import (
    IMAGE_EXTS, VIDEO_EXTS, ext_from_url, target_path, DEFAULT_UA,
)

BASE = "https://pawchive.st"
FILE_BASE = "https://file.pawchive.st"
API = BASE + "/api/v1"

PAGE_SIZE = 50           # listing returns 50 posts per ?o= step
MAX_OFFSET = 5_000_000   # safety cap on pagination

# service slug -> display code used in filenames (Title case; default = svc.title())
SITE_CODES = {"patreon": "Patreon", "fanbox": "Fanbox"}

# External-link classification.
#   direct    -> a single file we can fetch and verify completely (auto-grab)
#   manual    -> a host that needs a special client / auth / has caps (list only)
#   reference -> a page, not a file host (list only)
_DIRECT_HOSTS = {"catbox.moe", "files.catbox.moe"}
_DIRECT_EXTS = (VIDEO_EXTS | {"gif", "zip", "rar", "7z", "gz", "tar", "tgz", "mp3", "wav"})
_MANUAL_HOSTS = {
    "mega.nz", "mega.io", "mega.co.nz",
    "drive.google.com", "docs.google.com",
    "gofile.io", "pixeldrain.com", "dropbox.com", "www.dropbox.com",
    "mediafire.com", "www.mediafire.com", "1fichier.com", "sendspace.com",
    "workupload.com", "bunkr.si", "bunkrr.su",
}

_CREATOR_RE = re.compile(r"/([A-Za-z0-9_]+)/user/([^/?#]+)")
_POST_RE = re.compile(r"/([A-Za-z0-9_]+)/user/([^/]+)/post/([^/?#]+)")
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WS = re.compile(r"\s+")


# ── Session ─────────────────────────────────────────────────────────

def make_session(user_agent=None, cookies_path=None):
    """requests Session with a browser UA (pawchive 403s non-browser UAs) that
    auto-carries the DDoS-Guard cookie set on the first hit."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent or DEFAULT_UA,
        "Referer": BASE + "/",
        "Accept": "application/json, text/plain, */*",
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
    """/{service}/user/{id}[/...] -> {service, user_id}, or None.

    Rejects post URLs (those go through parse_post_url)."""
    if not url or "/post/" in url:
        return None
    m = _CREATOR_RE.search(url)
    if not m:
        return None
    return {"service": m.group(1).lower(), "user_id": m.group(2)}


def parse_post_url(url):
    """/{service}/user/{id}/post/{postId} -> {service, user_id, post_id}, or None."""
    m = _POST_RE.search(url or "")
    if not m:
        return None
    return {"service": m.group(1).lower(), "user_id": m.group(2), "post_id": m.group(3)}


def site_code(service):
    s = (service or "").lower()
    return SITE_CODES.get(s, s.title() if s else "?")


def creator_url(service, user_id):
    return f"{BASE}/{service}/user/{user_id}"


def post_url(service, user_id, post_id):
    return f"{BASE}/{service}/user/{user_id}/post/{post_id}"


def media_url(path, name=None):
    """Content-addressed file URL. `path` is like '/09/fb/<sha>.gif'."""
    base = f"{FILE_BASE}/data{path}"
    return f"{base}?f={quote(name)}" if name else base


def sanitize_filename(name, max_len=180):
    """Make an arbitrary file name safe for Windows/NTFS while preserving it as
    much as possible. Keeps the extension; trims overlong middles."""
    name = _ILLEGAL_FS.sub("", name or "")
    name = _WS.sub(" ", name).strip().strip(".").strip()
    if not name:
        return "file"
    if len(name) <= max_len:
        return name
    root, dot, ext = name.rpartition(".")
    if dot and 1 <= len(ext) <= 8:
        keep = max_len - len(ext) - 1
        return f"{root[:keep].strip()}.{ext}"
    return name[:max_len].strip()


def build_filename(dt, service, original_name):
    """'2026.05.12 - Patreon - Label stream night X teaser.mp4'.

    The original file name (incl. its extension) is preserved; only the
    'YYYY.MM.DD - SITE - ' prefix is prepended. Collision suffixes are added by
    the runner via add_index_suffix()."""
    date_str = f"{dt:%Y.%m.%d}" if dt else "0000.00.00"
    return f"{date_str} - {site_code(service)} - {sanitize_filename(original_name)}"


def filename_prefix(dt, service):
    """The copyable 'YYYY.MM.DD - Patreon - ' prefix (trailing ' - ' + space)
    the user pastes in front of a manually-downloaded file's own name."""
    date_str = f"{dt:%Y.%m.%d}" if dt else "0000.00.00"
    return f"{date_str} - {site_code(service)} - "


def add_index_suffix(filename, n):
    """Insert '_n' before the extension for collision avoidance."""
    root, dot, ext = filename.rpartition(".")
    if dot and 1 <= len(ext) <= 8:
        return f"{root}_{n}.{ext}"
    return f"{filename}_{n}"


def _kind_and_ext(path_or_url):
    ext = ext_from_url(path_or_url, default="")
    if ext in VIDEO_EXTS:
        return "video", ext
    if ext in IMAGE_EXTS:
        return "image", ext
    # Unknown extension: treat as image (safer default for on-site attachments;
    # the runner still stores it and serves it).
    return "image", (ext or "bin")


def _host_of(url):
    return (urlsplit(url).netloc or "").lower().lstrip("www.")


def _classify_link(url):
    host = _host_of(url)
    if host in _DIRECT_HOSTS:
        return "direct"
    path = urlsplit(url).path.lower()
    if "." in path and path.rsplit(".", 1)[-1] in _DIRECT_EXTS:
        return "direct"
    # a manual host, unless the specific path is a direct file (handled above)
    for h in _MANUAL_HOSTS:
        if host == h or host.endswith("." + h):
            return "manual"
    return "reference"


# ── Date parsing ────────────────────────────────────────────────────

def parse_dt(value):
    """ISO timestamp (with or without microseconds, no tz) -> aware UTC datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── API fetching ────────────────────────────────────────────────────

def fetch_json(session, url, timeout=(15, 60)):
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_profile(session, service, user_id):
    """Return the profile dict ({name, ...}), or {} on failure."""
    try:
        return fetch_json(session, f"{API}/{service}/user/{user_id}/profile") or {}
    except Exception:
        return {}


def fetch_creator_name(session, service, user_id):
    return (fetch_profile(session, service, user_id) or {}).get("name") or ""


def iter_posts(session, service, user_id, on_page=None, should_cancel=None, fetch=None):
    """Yield raw post dicts for a creator, paginating ?o= by 50 until [] .

    fetch(session, url) -> parsed JSON overrides the default, letting the caller
    add retry/backoff for transient server errors (returns None to abort)."""
    fetch = fetch or fetch_json
    offset = 0
    page = 0
    while offset <= MAX_OFFSET:
        if should_cancel and should_cancel():
            return
        url = f"{API}/{service}/user/{user_id}" + (f"?o={offset}" if offset else "")
        data = fetch(session, url)
        if data is None:            # fetch declined (e.g. cancelled mid-retry)
            return
        if not isinstance(data, list) or not data:
            return
        page += 1
        if on_page:
            on_page(page, len(data))
        for raw in data:
            yield raw
        if len(data) < PAGE_SIZE:
            return
        offset += PAGE_SIZE


def fetch_post(session, service, user_id, post_id, fetch=None):
    """Fetch a single post's full detail (used when a listing entry lacks it)."""
    fetch = fetch or fetch_json
    return fetch(session, f"{API}/{service}/user/{user_id}/post/{post_id}")


# ── Post normalisation ──────────────────────────────────────────────

def parse_post(raw):
    """Normalise a raw API post dict.

    Returns: {post_id, user_id, service, title, dt (UTC|None), url,
              media:[{name, path, url, kind, ext}],
              content_html, embed, external_links:[{url, label, host, kind}],
              detail_fetched, has_full}
    Media = `file` + `attachments`, de-duplicated by storage path.
    """
    service = (raw.get("service") or "").lower()
    user_id = str(raw.get("user") or "")
    post_id = str(raw.get("id") or "")
    dt = parse_dt(raw.get("published")) or parse_dt(raw.get("added"))

    media = []
    seen = set()
    entries = []
    f = raw.get("file")
    if isinstance(f, dict) and f.get("path"):
        entries.append(f)
    for a in (raw.get("attachments") or []):
        if isinstance(a, dict) and a.get("path"):
            entries.append(a)
    for e in entries:
        path = e["path"]
        if path in seen:
            continue
        seen.add(path)
        name = e.get("name") or os.path.basename(path)
        kind, ext = _kind_and_ext(name if "." in name else path)
        media.append({
            "name": name,
            "path": path,
            "url": media_url(path, name),
            "kind": kind,
            "ext": ext,
        })

    content_html = raw.get("content") or ""
    embed = raw.get("embed") if isinstance(raw.get("embed"), dict) else {}
    external_links = extract_external_links(content_html, embed)

    return {
        "post_id": post_id,
        "user_id": user_id,
        "service": service,
        "title": raw.get("title") or "",
        "dt": dt,
        "url": post_url(service, user_id, post_id),
        "media": media,
        "content_html": content_html,
        "embed": embed,
        "external_links": external_links,
        "detail_fetched": bool(raw.get("detail_fetched", True)),
        "has_full": bool(raw.get("has_full", True)),
    }


def extract_external_links(content_html, embed=None):
    """Pull external links out of a post body + embed, de-duplicated by URL.

    Returns [{url, label, host, kind}] where kind ∈ {direct, manual, reference}.
    Internal pawchive links are ignored."""
    out = []
    seen = set()

    def add(url, label):
        url = (url or "").strip()
        if not url or not url.lower().startswith(("http://", "https://")):
            return
        host = _host_of(url)
        if not host or host.endswith("pawchive.st"):
            return
        if url in seen:
            return
        seen.add(url)
        out.append({"url": url, "label": (label or "").strip(),
                    "host": host, "kind": _classify_link(url)})

    if content_html:
        soup = BeautifulSoup(content_html, "html.parser")
        for a in soup.find_all("a", href=True):
            add(a["href"], a.get_text(strip=True))

    if embed and embed.get("url"):
        add(embed["url"], embed.get("subject") or embed.get("description") or "")

    return out
