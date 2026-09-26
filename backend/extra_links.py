"""Per-creator tracked links — extra download sources (Terabox, gofile, bunkr…,
MEGA/Drive) a creator posts outside the sites the Creator tab crawls.

They live in `creator["extra_links"]`, apart from `creator["links"]`, precisely so
Fetch Latest / Download Everything never touch them: each one is fetched on its
own, when the user asks, through the Albums tab's engines. Each record:
    {url, site, password, folder, track, added, last_run, last_result}
`folder` is where it downloads to — relative to the creator's destination, or an
absolute path (the only option for a folder-less track-only creator).
"""

import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from backend import album_sites

# Hosts that can be stored and opened but have no download engine yet.
_UNSUPPORTED = (
    ("mega", re.compile(r"(?:^|\.)mega\.(?:nz|co\.nz|io)$", re.I)),
    ("gdrive", re.compile(r"^(?:drive|docs)\.google\.com$", re.I)),
    ("dropbox", re.compile(r"(?:^|\.)dropbox\.com$", re.I)),
    ("mediafire", re.compile(r"(?:^|\.)mediafire\.com$", re.I)),
    ("pixeldrain", re.compile(r"(?:^|\.)pixeldrain\.com$", re.I)),
)
_LABELS = {"mega": "MEGA", "gdrive": "Google Drive", "dropbox": "Dropbox",
           "mediafire": "MediaFire", "pixeldrain": "Pixeldrain"}


def classify(url):
    """(site_key, downloadable) for a link; ('other', False) for unknown hosts."""
    site = album_sites.detect_site(url)
    if site:
        return site["key"], True
    host = (urlparse(url).hostname or "").lower()
    for key, rx in _UNSUPPORTED:
        if rx.search(host):
            return key, False
    return "other", False


def site_label(key):
    return _LABELS.get(key) or key.capitalize()


def clean_url(raw):
    """(url, password) from a pasted line (optional ' | password'), or (None, None)."""
    url, pwd = album_sites.parse_entry(raw or "")
    if not url or not re.match(r"^https?://", url, re.I):
        return None, None
    return url, (pwd or None)


def new_record(url, password=None, folder=None, track=True):
    key, _ = classify(url)
    return {"url": url, "site": key, "password": password or "",
            "folder": (folder or "").strip() or key, "track": bool(track),
            "added": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_run": "", "last_result": None}


def norm(url):
    u = urlparse((url or "").strip())
    return f"{(u.hostname or '').lower()}{u.path.rstrip('/')}?{u.query}"


def find(records, url):
    n = norm(url)
    return next((r for r in records or [] if norm(r.get("url")) == n), None)


def resolve_dest(creator, record):
    """Absolute download folder for a record, or (None, reason)."""
    folder = (record.get("folder") or "").strip()
    if folder and os.path.isabs(folder):
        return folder, None
    base = (creator.get("destination") or "").strip()
    if not base:
        return None, ("this creator has no folder — give the link an absolute "
                      "download folder (e.g. P:\\…\\terabox)")
    parts = [p for p in re.split(r"[\\/]+", folder) if p and p not in (".", "..")]
    return os.path.join(base, *parts) if parts else base, None


def view(creator, record):
    """A record as the UI shows it: plus label, downloadable flag, resolved folder."""
    key, ok = classify(record.get("url", ""))
    dest, why = resolve_dest(creator, record)
    shown = {k: v for k, v in record.items() if k != "password"}
    return {**shown, "site": key, "site_label": site_label(key), "downloadable": ok,
            "dest": dest or "", "dest_problem": why or "",
            "has_password": bool(record.get("password"))}
