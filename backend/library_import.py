"""Reconstruct creators from what's actually on disk.

The app's creator index (folder ↔ account URL) lived only in app_state.json. If
that file was truncated/corrupted, the index shrinks even though every download
is still on the NAS. This module rebuilds the index from ground truth:

  * the per-account archive DBs in the central archive dir
    (coomerfans_<service>_<id>.db, twitter_<username>.db), and
  * the creator folders under the library roots.

Correlation (which account lives in which folder):
  * coomerfans — the DB records each item's `filename`; a folder owns the
    account if it physically contains one of those files.
  * twitter   — the gallery-dl DB records `twitter_<tweetid>_0_<num>` entries;
    a folder owns the account if its <folder>/twitter/ subtree contains files
    for those tweet ids.

For coomerfans accounts whose URL name-slug isn't already known, the canonical
name is fetched online (the page at /u/<svc>/<id>/<anything> returns 200 and its
<title> is "<name> posts"). Twitter URLs are exact from the DB filename.

Output is shaped like the legacy artist_map / cf_artist_map so api.Api can reuse
its existing folder-grouping + dedup merge.
"""

import os
import re
import sqlite3
import time

from backend.coomerfans_scraper import BASE

_YEAR_RE = re.compile(r"^\d{4}$")
# Twitter files on disk (original + newer "- Twitter -" form) -> tweet id
_TW_FILE_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2} - (?:Twitter - )?(\d+)_\d+\.\w+$")
# gallery-dl archive entry: "twitter_1234567890_0_1" -> tweet id
_TW_ENTRY_RE = re.compile(r"twitter_(\d+)_\d+_\d+")


# ── online name resolution ──────────────────────────────────────────

def resolve_cf_name(session, service, user_id):
    """Canonical coomerfans name-slug for a service+id, or None.

    /u/<svc>/<id>/<placeholder> returns 200 with <title>"<name> posts".
    """
    try:
        r = session.get(f"{BASE}/u/{service}/{user_id}/_", timeout=(15, 30))
        if r.status_code != 200:
            return None
        m = re.search(r"<title>\s*(.*?)\s+posts\s*</title>", r.text, re.I | re.S)
        if m and m.group(1).strip():
            return m.group(1).strip()
        m = re.search(rf"/u/{re.escape(service)}/{re.escape(user_id)}/([^/?#\"'<>\s]+)", r.text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


# ── disk indexing ───────────────────────────────────────────────────

def _list_creator_folders(roots):
    folders = []
    for root in roots or []:
        if not root or not os.path.isdir(root):
            continue
        try:
            for e in os.listdir(root):
                p = os.path.join(root, e)
                if os.path.isdir(p):
                    folders.append(p)
        except OSError:
            pass
    return folders


def _year_dirs(base):
    if not base or not os.path.isdir(base):
        return []
    out = []
    try:
        for e in os.listdir(base):
            p = os.path.join(base, e)
            if _YEAR_RE.match(e) and os.path.isdir(p):
                out.append(p)
    except OSError:
        pass
    return out


def _files_in(d):
    try:
        return {e.name for e in os.scandir(d) if e.is_file()}
    except OSError:
        return set()


def _folder_cf_basenames(folder):
    """All coomerfans file basenames in a folder: <year>/ (videos) + images/<year>/."""
    names = set()
    for d in _year_dirs(folder):
        names |= _files_in(d)
    for d in _year_dirs(os.path.join(folder, "images")):
        names |= _files_in(d)
    return names


def _folder_tw_tweetids(folder):
    """Tweet ids present under <folder>/twitter/ (<year>/ + Images/<year>/)."""
    ids = set()
    tw = os.path.join(folder, "twitter")
    if not os.path.isdir(tw):
        return ids
    dirs = _year_dirs(tw) + _year_dirs(os.path.join(tw, "Images"))
    for d in dirs:
        for name in _files_in(d):
            m = _TW_FILE_RE.match(name)
            if m:
                ids.add(m.group(1))
    return ids


# ── archive DB readers ──────────────────────────────────────────────

def _list_dbs(archive_dir):
    try:
        return [os.path.join(archive_dir, f) for f in os.listdir(archive_dir)
                if f.lower().endswith(".db")]
    except OSError:
        return []


def _parse_cf_db(name):
    """coomerfans_<service>_<id>.db -> (service, user_id) or (None, None)."""
    core = name[len("coomerfans_"):-3]   # strip prefix + ".db"
    if "_" not in core:
        return (None, None)
    svc, uid = core.rsplit("_", 1)
    return (svc, uid)


def _parse_tw_db(name):
    return name[len("twitter_"):-3] or None


def _cf_db_filenames(db_path, limit=40):
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT filename FROM archive WHERE filename IS NOT NULL LIMIT ?",
                (limit,)).fetchall()
        finally:
            conn.close()
        return {r[0] for r in rows if r[0]}
    except Exception:
        return set()


def _tw_db_tweetids(db_path, limit=400):
    ids = set()
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT entry FROM archive LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
        for (entry,) in rows:
            m = _TW_ENTRY_RE.search(entry or "")
            if m:
                ids.add(m.group(1))
    except Exception:
        pass
    return ids


# ── scan ─────────────────────────────────────────────────────────────

def scan_library(roots, archive_dir, session, known_cf=None,
                 log=None, should_cancel=None, resolve_online=True):
    """Return (cf_map, tw_map, report).

    cf_map / tw_map are shaped like the legacy cf_artist_map / artist_map so the
    caller can reuse its folder-grouping merge.
    """
    known_cf = known_cf or {}
    log = log or (lambda m: None)
    cancelled = (lambda: bool(should_cancel and should_cancel()))

    folders = _list_creator_folders(roots)
    log(f"Scanning {len(folders)} folder(s) across {len(roots)} root(s)…")

    dbs = _list_dbs(archive_dir)
    cf_dbs = sorted(d for d in dbs if os.path.basename(d).startswith("coomerfans_"))
    tw_dbs = sorted(d for d in dbs if os.path.basename(d).startswith("twitter_"))
    log(f"Found {len(cf_dbs)} coomerfans + {len(tw_dbs)} twitter archive DB(s).")

    cf_basename_cache = {}   # folder -> set(basenames)
    tw_id_cache = {}         # folder -> set(tweetids)

    def folder_cf(folder):
        if folder not in cf_basename_cache:
            cf_basename_cache[folder] = _folder_cf_basenames(folder)
        return cf_basename_cache[folder]

    def folder_tw(folder):
        if folder not in tw_id_cache:
            tw_id_cache[folder] = _folder_tw_tweetids(folder)
        return tw_id_cache[folder]

    cf_map, tw_map = {}, {}
    matched_cf = matched_tw = resolved = 0
    unmatched = []

    # coomerfans
    for db in cf_dbs:
        if cancelled():
            break
        base = os.path.basename(db)
        svc, uid = _parse_cf_db(base)
        if not svc:
            continue
        wanted = _cf_db_filenames(db)
        folder = next((f for f in folders if wanted & folder_cf(f)), None) if wanted else None
        if not folder:
            unmatched.append(base)
            log(f"  ⚠ no folder match for {base}")
            continue
        key = (svc, uid)
        info = known_cf.get(key)
        if info and info.get("url"):
            name, url = info.get("name") or uid, info["url"]
        else:
            name = (resolve_cf_name(session, svc, uid) if resolve_online else None) or uid
            url = f"{BASE}/u/{svc}/{uid}/{name}"
            resolved += 1
            log(f"  resolved {svc}/{uid} → {name}")
            time.sleep(0.4)   # be gentle on the site
        cf_map[f"{svc}_{uid}"] = {
            "name": name, "service": svc, "user_id": uid, "url": url,
            "destination": folder, "last_used": "",
        }
        matched_cf += 1
        log(f"  {base} → {os.path.basename(folder)}")

    # twitter
    for db in tw_dbs:
        if cancelled():
            break
        base = os.path.basename(db)
        user = _parse_tw_db(base)
        if not user:
            continue
        wanted = _tw_db_tweetids(db)
        folder = next((f for f in folders if wanted & folder_tw(f)), None) if wanted else None
        if not folder:
            unmatched.append(base)
            log(f"  ⚠ no folder match for {base}")
            continue
        tw_map[user] = {
            "destination": os.path.join(folder, "twitter"),
            "url": f"https://x.com/{user}", "last_used": "",
        }
        matched_tw += 1
        log(f"  {base} → {os.path.basename(folder)}/twitter")

    report = {
        "folders": len(folders),
        "cf_dbs": len(cf_dbs), "tw_dbs": len(tw_dbs),
        "matched_cf": matched_cf, "matched_tw": matched_tw,
        "resolved_online": resolved, "unmatched": unmatched,
    }
    return cf_map, tw_map, report
