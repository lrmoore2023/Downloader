"""Scraping/parsing logic for Discord channels (the official REST API).

Pure functions + a requests Session factory. No threads, no UI, no downloads —
the runner orchestrates those. Unlike the other platforms, Discord exposes a
full JSON REST API, so there is no HTML parsing: we page a channel's message
history and normalise each message's attachments/embeds/links.

Auth is a single **token** carried in the Authorization header — either a *bot*
token (`Authorization: Bot <token>`) or a *user* token (the raw token, no
prefix). Discord authenticates by this header, not by cookies. The token is
threaded in from settings (see api._default_state / creator_runner).

Endpoints (v10):
    Channel      : GET /channels/{channel_id}                 -> {id, name, ...}
    Messages     : GET /channels/{channel_id}/messages?limit=100[&before=<id>]
                       -> [ message, ... ]   (newest-first)
    Message obj  : {id, timestamp (ISO8601), content, attachments:[...],
                    embeds:[...], ...}
    Attachment   : {id, filename, url, content_type, size, width, height}
    Media URL    : cdn.discordapp.com/attachments/... — freshly signed when the
                   message is fetched (Accept-Ranges: bytes -> resume works).

A channel URL is what "Copy Link" produces:
    https://discord.com/channels/{guild_id|@me}/{channel_id}

Media file names preserve the original attachment name behind a
'date - Discord - ' prefix, e.g. "2026.05.05 - Discord - cool_render.png".
"""

import os
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

# Reuse the shared bits that are identical across platforms.
from backend.coomerfans_scraper import (
    IMAGE_EXTS, VIDEO_EXTS, target_path, DEFAULT_UA, ext_from_url,
)

BASE = "https://discord.com/api/v10"

# Snowflake epoch: 2015-01-01T00:00:00Z in ms. A Discord ID encodes its creation
# time as ((id >> 22) + DISCORD_EPOCH) ms — used both to date media (when a
# message has no usable timestamp) and to bound a year-scoped crawl.
DISCORD_EPOCH = 1420070400000

# Hosts whose media we download directly (Discord's own CDN / proxy). Anything
# else found in an embed/message is treated as an external link for the manifest.
_DISCORD_MEDIA_HOSTS = (
    "cdn.discordapp.com", "media.discordapp.net",
    "images-ext-1.discordapp.net", "images-ext-2.discordapp.net",
)

_URL_RE = re.compile(r"https?://[^\s<>\"'()]+", re.I)
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WS = re.compile(r"\s+")


# ── Session ─────────────────────────────────────────────────────────

def make_session(token=None, token_type="user", user_agent=None):
    """requests Session with a browser UA. When `token` is set, add the
    Authorization header: `Bot <token>` for a bot token, the raw token for a
    user token. Pass token=None to build an unauthenticated session for CDN file
    downloads (the token is never needed — and shouldn't be sent — to the CDN)."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent or DEFAULT_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    if token:
        s.headers["Authorization"] = (
            f"Bot {token}" if (token_type or "user").lower() == "bot" else token)
    return s


# ── URL / naming helpers ────────────────────────────────────────────

_CHANNEL_RE = re.compile(
    r"discord(?:app)?\.com/channels/(@me|\d+)/(\d+)", re.I)


def parse_creator_url(url):
    """A Discord channel URL -> {"guild_id", "channel_id"}, or None.

    Accepts https://discord.com/channels/{guild|@me}/{channel} (what the client's
    "Copy Link" produces). A trailing /{message_id} (a jump link) is ignored."""
    if not url:
        return None
    m = _CHANNEL_RE.search(url.strip())
    if not m:
        return None
    return {"guild_id": m.group(1), "channel_id": m.group(2)}


def channel_url(guild_id, channel_id):
    """Canonical channel URL (used to dedupe links)."""
    return f"https://discord.com/channels/{guild_id or '@me'}/{channel_id}"


def message_url(guild_id, channel_id, message_id):
    """A jump link to a single message (surfaced for manual external grabs)."""
    return f"https://discord.com/channels/{guild_id or '@me'}/{channel_id}/{message_id}"


def site_code():
    return "Discord"


# A Discord channel is often just a mirror of an artist's Patreon/Fanbox (or
# OnlyFans/Fansly) sets, so a link can opt to tag its files with that platform's
# code instead of "Discord" — making them blend with the rest of that creator's
# library. Values mirror the coomerfans/pawchive site codes.
LABEL_CODES = {"discord": "Discord", "patreon": "Patreon", "fanbox": "Fanbox",
               "onlyfans": "OF", "fansly": "Fansly"}


def label_code(label):
    """The filename site code for a link's chosen label ('' / unknown -> Discord)."""
    return LABEL_CODES.get((label or "").lower(), "Discord")


def sanitize_filename(name, max_len=180):
    """Make an arbitrary attachment name safe for Windows/NTFS while preserving it
    as much as possible. Keeps the extension; trims overlong middles."""
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


def build_filename(dt, original_name, label=None):
    """'2026.05.05 - Discord - cool_render.png'.

    The original attachment name (incl. its extension) is preserved behind a
    'YYYY.MM.DD - <CODE> - ' prefix so api._PREFIX_RE / the errors panel
    recognise it. `label` overrides the site code (e.g. 'patreon' -> 'Patreon')
    so a Patreon-mirror channel's files match the creator's other files; defaults
    to 'Discord'. Missing date -> '0000.00.00' (matches the other engines).
    Collision suffixes are added by the runner via add_index_suffix()."""
    date_str = f"{dt:%Y.%m.%d}" if dt else "0000.00.00"
    return f"{date_str} - {label_code(label)} - {sanitize_filename(original_name)}"


def add_index_suffix(filename, n):
    """Insert '_n' before the extension for collision avoidance."""
    root, dot, ext = filename.rpartition(".")
    if dot and 1 <= len(ext) <= 8:
        return f"{root}_{n}.{ext}"
    return f"{filename}_{n}"


# ── Dates / snowflakes ──────────────────────────────────────────────

def parse_dt(value):
    """ISO8601 timestamp -> aware UTC datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def snowflake_to_dt(snowflake):
    """Discord ID -> creation datetime (UTC), or None."""
    try:
        ms = (int(snowflake) >> 22) + DISCORD_EPOCH
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def dt_to_snowflake(dt):
    """Datetime -> the smallest snowflake at/after that instant (for range bounds)."""
    ms = int(dt.timestamp() * 1000)
    return (ms - DISCORD_EPOCH) << 22


def year_bounds(year):
    """(after_id, before_id) snowflakes bracketing a calendar year (UTC), so a
    year-scoped crawl only pages messages within that year."""
    year = int(year)
    after = dt_to_snowflake(datetime(year, 1, 1, tzinfo=timezone.utc))
    before = dt_to_snowflake(datetime(year + 1, 1, 1, tzinfo=timezone.utc))
    return after, before


# ── API fetching ────────────────────────────────────────────────────

def fetch_json(session, url, timeout=(15, 60)):
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_channel(session, channel_id):
    """Channel metadata (best-effort). Returns the dict, or None on any error —
    used only to resolve a display name, so failure is non-fatal."""
    try:
        return fetch_json(session, f"{BASE}/channels/{channel_id}")
    except Exception:
        return None


def channel_name(session, channel_id):
    """A human channel name ('#general', a DM recipient, or a fallback), or ''."""
    ch = fetch_channel(session, channel_id)
    if not isinstance(ch, dict):
        return ""
    name = ch.get("name")
    if name:
        return f"#{name}"
    # DM / group DM channels have no name — use the recipients.
    recips = ch.get("recipients") or []
    names = [r.get("global_name") or r.get("username") for r in recips if isinstance(r, dict)]
    names = [n for n in names if n]
    return ", ".join(names) if names else ""


def iter_messages(session, channel_id, on_page=None, should_cancel=None,
                  fetch=None, before=None, after_bound=None):
    """Yield raw message dicts for a channel, newest-first, paging backward via
    `before` until history is exhausted (or a page is short).

    fetch(session, url) -> parsed JSON overrides the default, letting the caller
    add retry/backoff/429 handling (returns None to abort). `before` seeds the
    starting cursor (a snowflake); `after_bound` stops paging once the page's
    oldest message is at/below that snowflake (year-scoped crawls)."""
    fetch = fetch or fetch_json
    cursor = before
    page = 0
    while True:
        if should_cancel and should_cancel():
            return
        url = f"{BASE}/channels/{channel_id}/messages?limit=100"
        if cursor:
            url += f"&before={cursor}"
        data = fetch(session, url)
        if data is None:                 # fetch declined (cancelled / fatal)
            return
        if not isinstance(data, list) or not data:
            return
        page += 1
        if on_page:
            on_page(page, len(data))
        for raw in data:
            yield raw
        oldest = data[-1].get("id")
        cursor = oldest
        if after_bound is not None and oldest is not None:
            try:
                if int(oldest) <= int(after_bound):
                    return
            except (TypeError, ValueError):
                pass
        if len(data) < 100:
            return


# ── Message normalisation ───────────────────────────────────────────

def _kind_for(ext, content_type=None):
    """image / video from an extension (and content_type hint). Unknown media is
    stored as an image (safer default; still downloaded and served)."""
    ext = (ext or "").lower().lstrip(".")
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    ct = (content_type or "").lower()
    if ct.startswith("video/"):
        return "video"
    if ct.startswith("image/"):
        return "image"
    return "image"


def _is_discord_media_host(url):
    host = (urlsplit(url or "").netloc or "").lower()
    return any(host == h or host.endswith("." + h.split(".", 1)[-1])
               for h in _DISCORD_MEDIA_HOSTS) or host in _DISCORD_MEDIA_HOSTS


def _host(url):
    return (urlsplit(url or "").netloc or "").lower().lstrip("www.")


def parse_message(raw):
    """Normalise a raw message dict.

    Returns {message_id, dt, content, media:[...], links:[...]} where
      media = [{entry, url, filename, ext, media_kind}]  (downloadable, on-CDN)
      links = [{url, label, host, kind}]                 (external, for manifest)
    """
    message_id = str(raw.get("id") or "")
    dt = parse_dt(raw.get("timestamp")) or snowflake_to_dt(message_id)
    content = raw.get("content") or ""

    media = []
    seen_urls = set()

    # 1) Attachments — always Discord-hosted, always downloadable.
    for att in raw.get("attachments") or []:
        url = att.get("url")
        if not url:
            continue
        att_id = str(att.get("id") or "")
        fname = att.get("filename") or (att_id + "." + ext_from_url(url, "bin"))
        ext = ext_from_url(fname, "") or ext_from_url(url, "bin")
        media.append({
            "entry": f"discord_{att_id}" if att_id else f"discord_{message_id}_a{len(media)}",
            "url": url,
            "filename": fname,
            "ext": ext,
            "media_kind": _kind_for(ext, att.get("content_type")),
        })
        seen_urls.add(url.split("?")[0])

    # 2) Embeds — download Discord-hosted/direct media; everything else is a link.
    links = []
    for i, emb in enumerate(raw.get("embeds") or []):
        got_media = False
        for field in ("image", "video", "thumbnail"):
            obj = emb.get(field) or {}
            url = obj.get("url") or obj.get("proxy_url")
            if not url:
                continue
            ext = ext_from_url(url, "")
            direct = _is_discord_media_host(url) or ext in IMAGE_EXTS or ext in VIDEO_EXTS
            if not direct:
                continue
            if url.split("?")[0] in seen_urls:
                got_media = True
                continue
            seen_urls.add(url.split("?")[0])
            base = url.split("?")[0].rsplit("/", 1)[-1] or f"embed_{i}"
            if "." not in base:
                base = f"{base}.{ext or 'bin'}"
            media.append({
                "entry": f"discord_{message_id}_e{i}",
                "url": url,
                "filename": base,
                "ext": ext or ext_from_url(base, "bin"),
                "media_kind": _kind_for(ext, None) if field != "video" else "video",
            })
            got_media = True
        # A rich/link embed we couldn't pull media from -> record its source URL.
        if not got_media and emb.get("url"):
            links.append({
                "url": emb["url"], "label": emb.get("title") or "",
                "host": _host(emb["url"]), "kind": "reference",
            })

    # 3) Bare URLs in the message text that we didn't already download -> links.
    for m in _URL_RE.finditer(content):
        url = m.group(0).rstrip(").,!?'\"")
        base = url.split("?")[0]
        if base in seen_urls or _is_discord_media_host(url):
            continue
        seen_urls.add(base)
        links.append({"url": url, "label": "", "host": _host(url), "kind": "reference"})

    return {
        "message_id": message_id,
        "dt": dt,
        "content": content,
        "media": media,
        "links": links,
    }
