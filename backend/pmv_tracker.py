"""PMV tracker: per-site video manifests + catalogue numbering.

The PMV tab tracks creators across rule34video / iwara / pawchive WITHOUT ever
downloading anything. Each (creator, site link) pair owns one JSON manifest —
`<archive_dir>/pmv/<pc_id>/<link_key>.json` — holding every video the site lists
for that creator, its catalogue number, and the user's ✓ / ✗ review status. The
number is what the user types into filenames ("SadBernard - R34 - 02 - …"), so the
two numbering rules below are the contract that keeps existing filenames valid:

* **locked** (rule34video, iwara) — the first complete walk numbers oldest → newest
  1..N and after that numbers are never changed. New uploads are appended
  (`next_number`, oldest-first among the new batch). A video that vanishes keeps
  its slot and is flagged `gone`; a video that reappears keeps its number too.
  These sites can only add at the top, so a stable append-only sequence is exact.

* **chronological** (pawchive) — an archive that back-fills older posts, so the
  number is recomputed on every walk as the post's chronological rank across ALL
  posts (by published date, ties by numeric id). Posts that vanish stay in the
  ordering (otherwise every later ✓ post would appear to shift). When a ✓ post's
  current number differs from the number it had when checked off, the UI shows a
  "shifted" warning (`number_at_check` vs `number`).

Manifests are metadata only and are rewritten atomically (temp file + replace).
"""

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone

from backend.pawchive_links import _replace_with_retry

VERSION = 1

LOCKED = "locked"
CHRONOLOGICAL = "chronological"
PLATFORMS = ("rule34video", "iwara", "pawchive", "hmvmania", "pmvhaven")
NUMBERING_FOR_PLATFORM = {
    "rule34video": LOCKED,
    "iwara": LOCKED,
    "pawchive": CHRONOLOGICAL,
    "hmvmania": LOCKED,
    "pmvhaven": LOCKED,
}
DEFAULT_SITE_CODES = {"rule34video": "R34", "iwara": "Iwara", "pawchive": "Pawchive",
                      "hmvmania": "HMVMania", "pmvhaven": "PMVHaven"}
STATUSES = ("unreviewed", "downloaded", "skipped")

# Attachment kinds that make a pawchive post worth showing by default (the user
# hides image/text-only posts; a PMV arrives as a video, an archive, or a link).
MEDIA_POST_KINDS = ("video", "archive")

_locks = {}
_locks_guard = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lock_key(path):
    p = os.path.abspath(path or "")
    return p.lower() if os.name == "nt" else p


def manifest_lock(path):
    """One re-entrant lock per manifest path: the fetch thread's merge and the
    UI's status writes both do load → mutate → save and must not interleave."""
    key = _lock_key(path)
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.RLock()
        return lk


# ── identity / paths ────────────────────────────────────────────────

def link_key(link):
    """Stable file-name-safe id for a site link. pawchive needs the service too:
    patreon and fanbox user ids are both plain integers and can collide."""
    platform = (link or {}).get("platform") or ""
    uid = str((link or {}).get("user_id") or "")
    if platform == "pawchive":
        key = f"pawchive_{(link.get('service') or '').lower()}_{uid}"
    else:
        key = f"{platform}_{uid}"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key)


def manifest_path(root, key):
    return os.path.join(root, f"{key}.json")


def numbering_for(platform):
    return NUMBERING_FOR_PLATFORM.get(platform, LOCKED)


def default_site_code(platform, service=None):
    """The code that goes in the filename. pawchive is only an archive, so its
    code is the *origin* service (Patreon / Fanbox), matching the downloader."""
    if platform == "pawchive" and service:
        return (service or "").strip().title() or "Pawchive"
    return DEFAULT_SITE_CODES.get(platform, (platform or "?").title())


def link_site_code(link):
    """A link's effective site code. A pawchive link saved with the old generic
    default ('Pawchive') is read as its service so existing creators follow the
    'Name - Patreon - …' convention without re-saving."""
    link = link or {}
    code = (link.get("site_code") or "").strip()
    if link.get("platform") == "pawchive" and code in ("", "Pawchive"):
        return default_site_code("pawchive", link.get("service"))
    return code or default_site_code(link.get("platform"))


# ── manifest IO ─────────────────────────────────────────────────────

def new_manifest(platform, user_id):
    return {
        "version": VERSION,
        "platform": platform,
        "user_id": str(user_id or ""),
        "numbering": numbering_for(platform),
        "next_number": 1,
        "initial_complete": False,
        "initial_at": "",
        "last_fetch": "",
        "last_full_scan": "",
        "last_fetch_new": 0,
        "last_error": "",
        "items": {},
    }


def _normalize(data, platform, user_id):
    base = new_manifest(platform, user_id)
    for k, v in base.items():
        data.setdefault(k, v)
    if not isinstance(data.get("items"), dict):
        data["items"] = {}
    for vid, it in list(data["items"].items()):
        if not isinstance(it, dict):
            data["items"].pop(vid, None)
            continue
        it.setdefault("id", vid)
        it.setdefault("status", "unreviewed")
        if it.get("status") not in STATUSES:
            it["status"] = "unreviewed"
        for k in ("gone", "backfilled", "detail_pending", "initial", "excluded"):
            it[k] = bool(it.get(k))
    return data


def load_manifest(path, platform, user_id):
    """Read a manifest, or start a fresh one. A genuinely unparsable file is
    renamed aside (never deleted) so nothing the user checked off is lost."""
    if not os.path.isfile(path):
        return new_manifest(platform, user_id)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("manifest is not an object")
        return _normalize(data, platform, user_id)
    except (OSError, ValueError) as e:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            os.replace(path, f"{path}.corrupt-{stamp}")
        except OSError:
            pass
        m = new_manifest(platform, user_id)
        m["last_error"] = f"Manifest unreadable ({e.__class__.__name__}); started fresh"
        return m


def save_manifest(path, data):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        _replace_with_retry(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ── prefixes ────────────────────────────────────────────────────────

def number_width(items):
    """Always 2: the user's filenames pad to two digits (03) and a three-digit
    number simply prints as itself (103) — never 003 or a 3-wide pad."""
    return 2


def prefix_date(iso):
    """'2026-05-04T12:00:00+00:00' → '2026.05.04' (the date-first filename style)."""
    if not iso:
        return ""
    d = str(iso)[:10]
    return d.replace("-", ".") if len(d) == 10 else ""


def format_prefix(name, code, number, width=2, date=None):
    """The filename prefix: 'Name - R34 - 2026.05.04 - ' (site first, then the
    upload date — user's convention, so a folder sorted by name groups by site
    and reads chronologically within it). Dates never shift, so the prefix
    stays valid however the site's listing changes. A video whose date is not
    known yet (r34 detail fetch pending) falls back to its catalogue number."""
    d = prefix_date(date) if date else ""
    if d:
        return f"{name} - {code} - {d} - "
    if not isinstance(number, int) or number <= 0:
        return f"{name} - {code} - "
    return f"{name} - {code} - {number:0{width}d} - "


# ── item helpers ────────────────────────────────────────────────────

_ITEM_FIELDS = ("title", "url", "date", "duration", "quality",
                "media_kinds", "link_hosts", "preview_state", "private", "unlisted")


def _new_item(f, now, initial=False):
    it = {
        "id": str(f["id"]),
        "title": f.get("title") or "",
        "url": f.get("url") or "",
        "date": f.get("date"),
        "duration": f.get("duration"),
        "quality": f.get("quality"),
        "number": None,
        "status": "unreviewed",
        "status_at": None,
        "number_at_check": None,
        "first_seen": now,
        "initial": bool(initial),
        "gone": False,
        "gone_since": None,
        "backfilled": False,
        "detail_pending": bool(f.get("detail_pending")),
        # chronological sites only: a post the user says is not a release (a
        # preview, a poll, a text update…) — listed, but holds no number.
        "excluded": False,
    }
    for k in ("media_kinds", "link_hosts", "preview_state", "private", "unlisted"):
        if k in f:
            it[k] = f[k]
    return it


def _refresh_item(it, f):
    """Bring a known item's metadata up to date. Only non-empty fetched values win,
    so a listing-only pass never blanks a date/quality learned from a detail page."""
    for k in _ITEM_FIELDS:
        if k not in f:
            continue
        v = f[k]
        if v is None or v == "" or v == []:
            continue
        it[k] = v
    if "detail_pending" in f:
        it["detail_pending"] = bool(f["detail_pending"])
    if it.get("gone"):
        it["gone"] = False
        it["gone_since"] = None


def _sort_oldest_first(fetched):
    # `pos` is the site's newest-first listing index (0 = newest), so descending
    # pos is oldest-first. Dates only break ties (they are absent on r34 listings).
    return sorted(fetched, key=lambda f: (-(f.get("pos") if f.get("pos") is not None else -1),
                                          f.get("date") or ""))


def _mark_gone(manifest, fetched_ids, now):
    n = 0
    for vid, it in manifest["items"].items():
        if vid not in fetched_ids and not it.get("gone"):
            it["gone"] = True
            it["gone_since"] = now
            n += 1
    return n


def _max_date(items):
    dates = [it.get("date") for it in items.values() if it.get("date")]
    return max(dates) if dates else None


# ── merge: locked numbering (rule34video, iwara) ────────────────────

def merge_locked(manifest, fetched, *, full, complete, now=None):
    """Fold a newest-first listing walk into a locked-numbering manifest.

    fetched  — [{id, title, url, pos, date?, duration?, quality?, …}] newest-first
    full     — the walk covered the whole listing, so anything missing is gone
    complete — the walk reached the end without error/cancel (required to assign
               the initial numbers: numbering a partial list would be wrong forever)
    Returns {"new", "gone", "numbered"}.
    """
    now = now or now_iso()
    items = manifest["items"]
    fetched_ids = {str(f["id"]) for f in fetched}
    result = {"new": 0, "gone": 0, "numbered": False}

    numbered = any(isinstance(it.get("number"), int) for it in items.values())
    if not numbered:
        if not complete:
            manifest["initial_complete"] = False
            return result
        ordered = _sort_oldest_first(fetched)
        for n, f in enumerate(ordered, 1):
            it = _new_item(f, now, initial=True)
            it["number"] = n
            items[it["id"]] = it
        manifest["next_number"] = len(ordered) + 1
        manifest["initial_complete"] = True
        manifest["initial_at"] = now
        result.update(new=len(ordered), numbered=True)
        return result

    prev_max_date = _max_date(items)
    new = _sort_oldest_first([f for f in fetched if str(f["id"]) not in items])
    nxt = manifest.get("next_number") or 1
    top = max((it["number"] for it in items.values() if isinstance(it.get("number"), int)), default=0)
    nxt = max(nxt, top + 1)
    for f in new:
        it = _new_item(f, now)
        it["number"] = nxt
        nxt += 1
        if prev_max_date and f.get("date") and f["date"] < prev_max_date:
            it["backfilled"] = True
        items[it["id"]] = it
    manifest["next_number"] = nxt
    for f in fetched:
        it = items.get(str(f["id"]))
        if it is not None and str(f["id"]) not in {n["id"] for n in new}:
            _refresh_item(it, f)
    result["new"] = len(new)
    if full:
        result["gone"] = _mark_gone(manifest, fetched_ids, now)
    # An incomplete first scan (e.g. iwara before logging in) surfaces its
    # missing videos on the next full walk as back-fills. While nothing is ✓ no
    # filename depends on the numbers yet, so slot them in properly instead.
    if (full and complete and any(items[n["id"]].get("backfilled") for n in new)
            and not any(it.get("status") == "downloaded" for it in items.values())):
        renumber_locked(manifest)
        result["renumbered"] = True
    return result


def renumber_locked(manifest, now=None):
    """Rebuild a locked catalogue chronologically: by date, ties by old number;
    an undated video inherits its predecessor's date so it keeps its place.
    Existing ✓ items keep number_at_check, so any that move show as shifted.
    Returns {"changed", "shifted"}."""
    items = manifest["items"]
    ordered = sorted(items.values(), key=lambda it: (it.get("number") or 10 ** 9, str(it.get("id"))))
    keyed, last = [], ""
    for it in ordered:
        d = it.get("date") or last
        last = d
        keyed.append((d, it.get("number") or 10 ** 9, str(it.get("id")), it))
    keyed.sort(key=lambda t: (t[0], t[1], t[2]))
    changed = shifted = 0
    for n, (_, _, _, it) in enumerate(keyed, 1):
        if it.get("number") != n:
            changed += 1
            if it.get("status") == "downloaded":
                shifted += 1
        it["number"] = n
        it["backfilled"] = False
    manifest["next_number"] = len(keyed) + 1
    return {"changed": changed, "shifted": shifted}


# ── merge: chronological numbering (pawchive) ───────────────────────

def _chrono_key(it):
    vid = str(it.get("id") or "")
    return (it.get("date") or "", (0, int(vid)) if vid.isdigit() else (1, vid))


def merge_chronological(manifest, fetched, *, now=None):
    """Fold a full listing walk into a chronological manifest and renumber every
    post by (published date, id). Missing posts are kept in the ordering as gone.
    Returns {"new", "gone", "numbered": True}."""
    now = now or now_iso()
    items = manifest["items"]
    first_walk = not items
    prev_max_date = _max_date(items)
    fetched_ids = set()
    new = 0
    for f in fetched:
        vid = str(f["id"])
        fetched_ids.add(vid)
        it = items.get(vid)
        if it is None:
            it = _new_item(f, now, initial=first_walk)
            if prev_max_date and f.get("date") and f["date"] < prev_max_date:
                it["backfilled"] = True
            items[vid] = it
            new += 1
        else:
            _refresh_item(it, f)
    gone = _mark_gone(manifest, fetched_ids, now)
    renumber_chronological(manifest)
    if first_walk:
        manifest["initial_at"] = now
    manifest["initial_complete"] = True
    return {"new": new, "gone": gone, "numbered": True}


def renumber_chronological(manifest):
    """Number every counted post 1..N by (date, id); excluded posts get None.
    Returns the number of posts whose number changed."""
    items = manifest["items"]
    changed = 0
    counted = sorted((it for it in items.values() if not it.get("excluded")), key=_chrono_key)
    for n, it in enumerate(counted, 1):
        if it.get("number") != n:
            changed += 1
        it["number"] = n
    for it in items.values():
        if it.get("excluded") and it.get("number") is not None:
            it["number"] = None
            changed += 1
    manifest["next_number"] = len(counted) + 1
    return changed


def set_excluded(item, excluded):
    """Flip a post in or out of the catalogue count. Only meaningful on a
    chronological site; the caller renumbers afterwards."""
    item["excluded"] = bool(excluded)
    if item["excluded"]:
        item["number"] = None
        item["number_at_check"] = None
    return item


def merge(manifest, fetched, *, full, complete, now=None):
    if manifest.get("numbering") == CHRONOLOGICAL:
        return merge_chronological(manifest, fetched, now=now)
    return merge_locked(manifest, fetched, full=full, complete=complete, now=now)


# ── review status ───────────────────────────────────────────────────

def set_status(item, status, now=None):
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}")
    now = now or now_iso()
    item["status"] = status
    item["status_at"] = now if status != "unreviewed" else None
    item["number_at_check"] = item.get("number") if status == "downloaded" else None
    return item


def is_shifted(item, numbering=LOCKED):
    # Chronological sites name files by date, so a moved rank is harmless there.
    if numbering == CHRONOLOGICAL:
        return False
    return (item.get("status") == "downloaded"
            and not item.get("excluded")
            and isinstance(item.get("number_at_check"), int)
            and item.get("number_at_check") != item.get("number"))


def is_media_post(item):
    """pawchive: does this post plausibly carry a PMV (video/archive attachment or
    an external link such as MEGA)? Other platforms: always."""
    if "media_kinds" not in item and "link_hosts" not in item:
        return True
    kinds = set(item.get("media_kinds") or [])
    return bool(kinds & set(MEDIA_POST_KINDS)) or bool(item.get("link_hosts"))


def counts(manifest):
    c = {"total": 0, "unreviewed": 0, "new": 0, "downloaded": 0,
         "skipped": 0, "shifted": 0, "gone": 0, "excluded": 0}
    for it in manifest.get("items", {}).values():
        c["total"] += 1
        if it.get("excluded"):
            c["excluded"] += 1
            if it.get("gone"):
                c["gone"] += 1
            continue                       # not a release: never a to-do
        st = it.get("status")
        if it.get("gone"):
            c["gone"] += 1
        if st == "downloaded":
            c["downloaded"] += 1
        elif st == "skipped":
            c["skipped"] += 1
        elif not it.get("gone"):
            c["unreviewed"] += 1
            if not it.get("initial"):
                c["new"] += 1
        if is_shifted(it, manifest.get("numbering")):
            c["shifted"] += 1
    return c
