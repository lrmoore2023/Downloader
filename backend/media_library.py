"""Scan a creator's downloaded media for the in-app browser.

Walks the on-disk layout the downloaders produce and returns a flat, dated list
the frontend can filter (year, image/video) and sort. Reachability is reported
as a flag rather than raised, so an offline NAS shows an empty "not reachable"
state instead of crashing the UI.

Layout (see coomerfans_scraper / config_builder):
  coomerfans  videos <dest>/<year>/            images <dest>/Images/<year>/
  twitter     videos <dest>/Twitter/<year>/    images <dest>/Twitter/Images/<year>/
Filenames start "YYYY.MM.DD"; coomerfans ones carry " - OF - " / " - Fansly - ".
"""

import os
import re

from backend.coomerfans_scraper import IMAGE_EXTS, VIDEO_EXTS

_YEAR_RE = re.compile(r"^\d{4}$")
_DATE_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})")
_SOURCE_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}\s*-\s*([^-]+?)\s*-")


def _date_of(filename, year):
    m = _DATE_RE.match(filename)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else f"{year}-00-00"


def _source_of(filename):
    m = _SOURCE_RE.match(filename)
    return m.group(1).strip() if m else ""


def _collect(items, base, twitter):
    """Append media from <base>/<year>/* (year = 4-digit dir)."""
    if not os.path.isdir(base):
        return
    try:
        entries = os.listdir(base)
    except OSError:
        return
    for y in entries:
        if not _YEAR_RE.match(y):
            continue
        ydir = os.path.join(base, y)
        if not os.path.isdir(ydir):
            continue
        try:
            files = os.listdir(ydir)
        except OSError:
            continue
        for fn in files:
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            if ext in VIDEO_EXTS:
                kind = "video"
            elif ext in IMAGE_EXTS:
                kind = "image"
            else:
                continue
            fp = os.path.join(ydir, fn)
            if not os.path.isfile(fp):
                continue
            items.append({
                "path": fp.replace("\\", "/"),
                "kind": kind,
                "year": int(y),
                "date": _date_of(fn, y),
                "filename": fn,
                "source": "Twitter" if twitter else _source_of(fn),
            })


def scan_creator_media(destination):
    """Return {"reachable": bool, "items": [...]}, newest first."""
    dest = (destination or "").strip()
    if not dest:
        return {"reachable": False, "items": []}
    try:
        os.listdir(dest)              # reachability probe (NAS may be offline)
    except OSError:
        return {"reachable": False, "items": []}

    items = []
    _collect(items, dest, twitter=False)                              # coomerfans videos
    _collect(items, os.path.join(dest, "Images"), twitter=False)      # coomerfans images
    tw = os.path.join(dest, "Twitter")
    _collect(items, tw, twitter=True)                                 # twitter videos
    _collect(items, os.path.join(tw, "Images"), twitter=True)         # twitter images

    items.sort(key=lambda it: (it["date"], it["filename"]), reverse=True)
    return {"reachable": True, "items": items}
