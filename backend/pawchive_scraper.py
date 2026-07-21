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

from bs4 import BeautifulSoup

# HTTP backend: prefer curl_cffi (impersonates a real Chrome TLS/JA3 + HTTP2
# fingerprint) so DDoS-Guard stops rate-limiting us as a bot — this is the single
# biggest lever against the 429 wall on the file CDN. Fall back to stock `requests`
# if the wheel isn't available on this platform (the app still runs, just without
# impersonation). `_net` is the drop-in requests-compatible module either way, and
# the exception aliases below let callers catch the right classes regardless of
# which backend loaded (curl_cffi's exceptions do NOT subclass requests').
try:
    from curl_cffi import requests as _net
    from curl_cffi.requests import exceptions as _exc
    from curl_cffi import CurlHttpVersion as _HTTPV
    _IMPERSONATE = "chrome"       # latest Chrome profile curl_cffi ships
    # Force HTTP/1.1: DDoS-Guard resets multiplexed HTTP/2 streams under load, which
    # surfaces as "curl (92) http/2 stream not closed cleanly" errors mid-download.
    # HTTP/1.1 (one request per connection) sidesteps that entirely; the TLS/JA3
    # fingerprint stays Chrome, and the file CDN serves 206 over HTTP/1.1 all the same.
    _HTTP_VERSION = _HTTPV.V1_1
    USING_IMPERSONATION = True
except Exception:                 # not installed / import failure -> plain requests
    import requests as _net
    _exc = _net
    _IMPERSONATE = None
    _HTTP_VERSION = None
    USING_IMPERSONATION = False

# Backend-agnostic exception aliases (same names/hierarchy in both backends).
HTTPError = _exc.HTTPError
ConnectionError = _exc.ConnectionError
Timeout = _exc.Timeout
RequestException = _exc.RequestException

# Reuse the shared bits that are identical to coomerfans.
from backend.coomerfans_scraper import (
    IMAGE_EXTS, VIDEO_EXTS, ext_from_url, target_path, DEFAULT_UA,
)

# pawchive.st and pawchive.pw are equal mirrors of the same backend — .st now just
# 301-redirects to .pw. So we target .pw directly to avoid paying a redirect
# round-trip on every request (which itself counts against DDoS-Guard's rate limit).
# Links to either domain collapse to the same creator (parse_creator_url ignores host).
BASE = "https://pawchive.pw"
FILE_BASE = "https://file.pawchive.pw"
API = BASE + "/api/v1"

PRIMARY_HOST = "pawchive.pw"
# Kept only as a genuine second host for the icon/avatar fallback in api.py; the
# download runner no longer fails over between hosts (see PawchiveRunner).
MIRROR_HOST = "pawchive.st"
MIRROR_BASE = "https://" + MIRROR_HOST


def to_mirror(url):
    """Rewrite a primary-host URL to the other mirror. Retained for the api.py icon
    fallback; the download runner no longer calls this."""
    return (url or "").replace(PRIMARY_HOST, MIRROR_HOST)

PAGE_SIZE = 50           # listing returns 50 posts per ?o= step
MAX_OFFSET = 5_000_000   # safety cap on pagination

# Pack/archive attachments (posts sometimes ship a mix of mp4 + zip/rar). These
# aren't viewable media, so they're stored in the year folder (like videos)
# rather than images/, under the standard 'date - SITE - name' name.
ARCHIVE_EXTS = {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst"}

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
# Bare (non-hyperlinked) URLs pasted into a post body. Runs on extracted text, so
# it stops at whitespace — which keeps mega '#<key>' fragments intact.
_URL_RE = re.compile(r'https?://[^\s<>"\']+', re.I)


# ── Session ─────────────────────────────────────────────────────────

def _load_cookies(session, cookies_path):
    """Load a Netscape/Mozilla cookies.txt into `session`, copying each cookie in
    individually so it works for both the requests and curl_cffi cookie jars
    (curl_cffi's jar is its own type — assigning a MozillaCookieJar to .cookies
    would not behave)."""
    try:
        from http.cookiejar import MozillaCookieJar
        jar = MozillaCookieJar()
        jar.load(cookies_path, ignore_discard=True, ignore_expires=True)
        for c in jar:
            session.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
    except Exception:
        pass


def _impersonate_for_ua(user_agent):
    """Pick the curl_cffi impersonate target whose Chrome version best matches a
    real browser's User-Agent.

    Cloudflare binds a `cf_clearance` cookie to the exact UA (and TLS fingerprint)
    that solved the challenge. When we replay that cookie we want curl_cffi to send
    BOTH the same UA and a JA3 from the same Chrome generation, or Cloudflare simply
    re-challenges. This maps the UA's `Chrome/<major>` to the nearest available
    curl_cffi profile (the set differs by curl_cffi version, so we read it live);
    falls back to the generic "chrome" (latest) when we can't tell."""
    if not user_agent:
        return _IMPERSONATE
    m = re.search(r"Chrome/(\d+)", user_agent)
    if not m:
        return _IMPERSONATE
    major = int(m.group(1))
    try:
        from curl_cffi.requests.impersonate import BrowserTypeLiteral
        import typing
        avail = []
        for v in typing.get_args(BrowserTypeLiteral):
            mm = re.fullmatch(r"chrome(\d+)[a-z]?", v)
            if mm:
                avail.append((int(mm.group(1)), v))
        if not avail:
            return _IMPERSONATE
        avail.sort()
        # Highest profile that is <= the browser's major (so we never claim a newer
        # Chrome than the client actually is); else the lowest available.
        pick = None
        for ver, name in avail:
            if ver <= major:
                pick = name
        return pick or avail[0][1]
    except Exception:
        return _IMPERSONATE


def make_session(user_agent=None, cookies_path=None, proxies=None):
    """HTTP Session that gets past DDoS-Guard's bot rate-limiting (and replays a
    Cloudflare `cf_clearance` cookie when one is supplied).

    When curl_cffi is available it impersonates a real Chrome (matching TLS/JA3 +
    HTTP2 fingerprint AND the corresponding browser headers, incl. User-Agent) — so
    normally we do NOT set our own User-Agent (a UA that doesn't match the JA3 is
    itself a bot tell). The exception is when a caller passes `user_agent` (the UA the
    embedded browser used to earn a `cf_clearance` cookie): we then pin that exact UA
    AND align the impersonate profile to its Chrome major, so the replayed cookie
    validates. Without curl_cffi it falls back to a plain requests Session with a
    browser UA. Either way the Session auto-carries the DDoS-Guard cookie set on the
    first hit, and loads any `cookies_path` (Netscape) so `cf_clearance` rides along."""
    if USING_IMPERSONATION:
        impersonate = _impersonate_for_ua(user_agent) if user_agent else _IMPERSONATE
        s = _net.Session(impersonate=impersonate, http_version=_HTTP_VERSION)
        # impersonate already installs a matching UA + header order; only add the
        # request-context headers a browser XHR would send.
        s.headers.update({
            "Referer": BASE + "/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        # Pin the browser's exact UA so a replayed cf_clearance cookie validates
        # (curl_cffi lets an explicit header override the impersonate default).
        if user_agent:
            s.headers["User-Agent"] = user_agent
    else:
        s = _net.Session()
        s.headers.update({
            "User-Agent": user_agent or DEFAULT_UA,
            "Referer": BASE + "/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
    if proxies:
        # Both backends accept a {"http": ..., "https": ...} dict on .proxies.
        try:
            s.proxies.update(proxies)
        except Exception:
            pass
    if cookies_path and os.path.isfile(cookies_path):
        _load_cookies(s, cookies_path)
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


def _clean_title(title):
    """Strip filesystem-illegal chars + collapse whitespace from a post title,
    without length-truncating it (that's _truncate_title's job)."""
    t = _ILLEGAL_FS.sub("", title or "")
    return _WS.sub(" ", t).strip().strip(".").strip()


def _truncate_title(title, room):
    """Shorten a (cleaned) title to fit `room` chars, kept legible: prefer a word
    boundary and mark the cut with '…'. Returns '' when there's no useful room."""
    if room < 4:
        return ""
    if len(title) <= room:
        return title
    cut = title[:room - 1].rstrip()
    sp = cut.rfind(" ")
    if sp >= max(4, room // 2):     # break on a word if it stays readable
        cut = cut[:sp].rstrip()
    return cut + "…"


def build_filename(dt, service, original_name, ordinal=None, width=2,
                   include_time=False, post_title=None, max_len=None):
    """'2026.05.12 - Patreon - Label stream night X teaser.mp4'.

    The original file name (incl. its extension) is preserved; only a prefix is
    prepended. Collision suffixes are added by the runner via add_index_suffix().

    Optional page-order decoration (images only; see the runner):
      * include_time -> add ' HH.MM' to the date so same-day posts stay grouped
        ('2026.05.12 14.32 - Patreon - ...').
      * ordinal      -> insert a zero-padded 'NN - ' after the site code so a
        post's images sort in page order ('... - Patreon - 01 - name.png').
        `width` sets the zero-padding.

    Archives pass `post_title` to get '... - SITE - POST TITLE - ORIGINAL NAME'
    so you know which post a zip/rar belongs to. If `max_len` is given and the
    whole name would exceed it (Windows path limit), ONLY the post title is
    truncated — the original filename is never modified.
    """
    date_str = f"{dt:%Y.%m.%d}" if dt else "0000.00.00"
    if include_time and dt:
        date_str += f" {dt:%H.%M}"
    prefix = f"{date_str} - {site_code(service)} - "
    if ordinal is not None:
        prefix += f"{ordinal:0{width}d} - "
    if post_title:
        # 255 = NTFS component cap; the original name is otherwise preserved whole.
        name = sanitize_filename(original_name, max_len=255)
        title = _clean_title(post_title)
        if max_len:
            room = max_len - len(prefix) - len(name) - len(" - ")
            title = _truncate_title(title, room)
        return f"{prefix}{title} - {name}" if title else f"{prefix}{name}"
    return f"{prefix}{sanitize_filename(original_name)}"


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
    if ext in ARCHIVE_EXTS:
        # Packs (zip/rar/...) go to the year folder, not images/, and skip the
        # video-only ffprobe check and the image-only page-order numbering.
        return "archive", ext
    # Unknown extension: treat as image (safer default for on-site attachments;
    # the runner still stores it and serves it).
    return "image", (ext or "bin")


def _host_of(url):
    return (urlsplit(url).netloc or "").lower().lstrip("www.")


def _classify_link(url):
    host = _host_of(url)
    if host in _DIRECT_HOSTS:
        return "direct"
    # Known manual hosts (dropbox/mega/gdrive/gofile/…) serve a SHARE/LANDING page,
    # not the raw file — even when the URL path ends in '.mp4'/'.zip' (e.g.
    # dropbox.com/scl/fi/<id>/Anim.mp4?rlkey=…). So classify by HOST before looking at
    # the extension: auto-grabbing these just downloads HTML and fails every run, so
    # they must be listed for manual download instead. (Checked live: those dropbox
    # links return text/html, with and without ?dl=1.)
    for h in _MANUAL_HOSTS:
        if host == h or host.endswith("." + h):
            return "manual"
    # An UNKNOWN host whose path is itself a direct file (…/name.mp4) — auto-grab it.
    path = urlsplit(url).path.lower()
    if "." in path and path.rsplit(".", 1)[-1] in _DIRECT_EXTS:
        return "direct"
    return "reference"


# Default "junk" link filters — social/store/paywalled links that carry no
# downloadable content worth surfacing. The UI seeds these into app_state and the
# user can add/remove any of them, so nothing here is hard-coded into behavior.
DEFAULT_LINK_FILTERS = [
    "youtube.com", "youtu.be", "instagram.com", "twitch.tv",
    "twitter.com", "x.com", "newgrounds.com", "artstation.com",
    "picarto.tv", "shop.", "patreon.com/posts/",
]


def link_is_filtered(url, patterns):
    """True if `url` matches any user filter pattern. Matching rules (documented
    in the Settings UI so patterns are predictable):
      * a pattern with '/'      → substring match on the whole URL
        (e.g. 'patreon.com/posts/' hides paywalled post links, keeps profiles)
      * a pattern ending in '.' → host-prefix match ('shop.' hides shop.* hosts)
      * a plain domain          → exact host or subdomain match
        ('youtube.com' hides youtube.com and m.youtube.com, not myyoutube.com)
    """
    if not url or not patterns:
        return False
    u = url.lower()
    host = _host_of(url)
    for p in patterns:
        p = (p or "").strip().lower()
        if not p:
            continue
        if "/" in p:
            if p in u:
                return True
        elif p.endswith("."):
            if host.startswith(p):
                return True
        elif host == p or host.endswith("." + p):
            return True
    return False


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

def is_cloudflare_challenge(resp):
    """True if `resp` is a Cloudflare bot-challenge interstitial ("Just a moment…"),
    NOT a genuine permission error.

    pawchive.pw moved its API behind Cloudflare's managed JS challenge, which returns
    403 (sometimes 503/429) with `server: cloudflare` and a challenge HTML body. This
    is only cleared by a real browser solving it (yielding a `cf_clearance` cookie) —
    so callers must treat it as "needs a Cloudflare reconnect", never as a permanent
    'gone' or a plain non-retriable error."""
    if resp is None:
        return False
    try:
        server = (resp.headers.get("server") or "").lower()
        if server != "cloudflare":
            return False
        if resp.status_code not in (403, 503, 429):
            return False
        # Confirm it's the challenge page, not a normal Cloudflare-fronted API reply.
        if resp.headers.get("cf-mitigated", "").lower() == "challenge":
            return True
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/html" not in ctype:
            return False
        body = ""
        try:
            body = (resp.text or "")[:4000].lower()
        except Exception:
            body = ""
        return ("just a moment" in body
                or "challenge-platform" in body
                or "cf_chl" in body
                or "_cf_chl_opt" in body)
    except Exception:
        return False


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
    deferred_media = []
    seen = set()
    seen_deferred = set()
    # Walk file + attachments in source order (a deferred attachment keeps its slot
    # when pawchive later fills in its path, so trailing deferred slots map cleanly
    # onto the media indices they'll occupy once imported — see runner _note_deferred).
    raw_entries = []
    f = raw.get("file")
    if isinstance(f, dict):
        raw_entries.append(f)
    for a in (raw.get("attachments") or []):
        if isinstance(a, dict):
            raw_entries.append(a)
    for e in raw_entries:
        path = e.get("path")
        if path:
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
        else:
            # No path yet: a 'deferred' attachment (pawchive has catalogued the file
            # but not fetched its bytes, so it serves no URL) or an empty file
            # placeholder. A 'scraped' post can still carry these — has_full is False
            # while individual files finish importing. We keep it (named) so the runner
            # can surface it as an error and auto-grab it once pawchive imports it,
            # instead of silently dropping it as the old path-only filter did.
            name = e.get("name")
            if not name:
                continue
            key = name.lower()
            if key in seen_deferred:
                continue
            seen_deferred.add(key)
            kind, ext = _kind_and_ext(name)
            deferred_media.append({
                "name": name,
                "kind": kind,
                "ext": ext,
                "deferred": bool(e.get("deferred")),
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
        # Named files pawchive knows about but hasn't imported the bytes for yet
        # ({name, kind, ext, deferred}). Empty for a fully-imported post. The runner
        # logs these so they're visible and grabbed once they land — see _note_deferred.
        "deferred_media": deferred_media,
        "content_html": content_html,
        "embed": embed,
        "external_links": external_links,
        "detail_fetched": bool(raw.get("detail_fetched", True)),
        "has_full": bool(raw.get("has_full", True)),
        # preview_state is the RELIABLE availability signal (has_full is not): pawchive
        # serves a post's files iff it has imported them. "scraped" → files present
        # (serve 200/206 even when has_full is False, e.g. a post whose extra .zip
        # attachments aren't imported yet but whose videos are). "pending" → pawchive
        # holds only kemono metadata, no files, so every URL 404s. Default "" (unknown
        # → treat as available/attempt) for older responses that omit it.
        "preview_state": (raw.get("preview_state") or "").lower(),
        "origin": (raw.get("origin") or "").lower(),
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
        # Many creators paste links as plain text (mega/gdrive/etc.), not <a> tags,
        # so also scan the visible text. add() dedups, so URLs already linked above
        # aren't double-counted.
        for m in _URL_RE.finditer(soup.get_text(" ")):
            add(m.group(0).rstrip(".,;:!?)]}'\""), "")

    if embed and embed.get("url"):
        add(embed["url"], embed.get("subject") or embed.get("description") or "")

    return out
