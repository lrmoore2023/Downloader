"""Verify & Repair for the coomerfans tab.

Diagnoses files that exist on disk but are corrupt/short, and (after the user
accepts) re-downloads only those — reusing the hardened download path.

Core rule (see the curation-deletion memory): a file the archive knows about
but that is **missing** from disk is treated as an intentional deletion — it is
ignored, never flagged, never re-downloaded. Repair only ever touches files that
are present-but-broken.
"""

import os
import shutil

from backend.coomerfans_scraper import BASE, parse_post
from backend.coomerfans_runner import ffprobe_ok, expected_total
from backend.coomerfans_archive import Archive, entry_key


def _path_for(destination, kind, year, filename):
    if kind == "image":
        return os.path.join(destination, "Images", year, filename)
    return os.path.join(destination, year, filename)


def _post_url(post_id, user_id, service):
    return f"{BASE}/p/{post_id}/{user_id}/{service}"


def _index_of(entry):
    try:
        return int(entry.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        return 1


def _network_size(session, post_url, index, cache):
    """Authoritative full size of a post's media[index] from the server."""
    info = cache.get(post_url)
    if info is None:
        info = parse_post(session, post_url)
        cache[post_url] = info
    media = info["media"]
    if 0 <= index - 1 < len(media):
        r = session.get(media[index - 1]["url"], stream=True,
                        headers={"Range": "bytes=0-0"}, timeout=(15, 60))
        try:
            return expected_total(r, 0)
        finally:
            r.close()
    return None


def find_broken(archive_path, destination, service, user_id, session,
                on_progress=None, should_cancel=None):
    """Return {checked, present, broken:[{...row, path, reason}]}.

    Detection priority per present file:
      - stored expected_size differs from disk size  -> broken (offline, exact)
      - video w/o stored size + ffprobe available     -> ffprobe invalid -> broken
      - video w/o stored size + no ffprobe            -> network size < disk -> broken
      - image w/o stored size                         -> skipped (low risk, no cheap check)
    """
    def log(m):
        if on_progress:
            on_progress({"type": "info", "message": m})

    arch = Archive(archive_path)
    rows = arch.rows()
    arch.close()

    have_ffprobe = bool(shutil.which("ffprobe"))
    cache = {}
    checked = present = 0
    broken = []

    for row in rows:
        if should_cancel and should_cancel():
            break
        checked += 1
        kind = row["kind"]
        path = _path_for(destination, kind, row["year"], row["filename"])
        if not os.path.isfile(path):
            continue                      # missing = intentional deletion -> ignore
        present += 1

        size = os.path.getsize(path)
        exp = row["expected_size"]
        reason = None

        if exp is not None:
            if size != exp:
                reason = f"size {size} != expected {exp}"
        elif kind == "video":
            if have_ffprobe:
                if not ffprobe_ok(path):
                    reason = "ffprobe: invalid/short video"
            else:
                try:
                    netsize = _network_size(
                        session, _post_url(row["post_id"], user_id, service),
                        _index_of(row["entry"]), cache)
                    if netsize and size < netsize:
                        reason = f"size {size} < server {netsize}"
                except Exception:
                    pass
        # images without a stored size are not deep-checked (small/fast, rarely truncate)

        if reason:
            broken.append({**row, "path": path, "reason": reason})
            log(f"BROKEN: {row['filename']}  ({reason})")

    log(f"Verify done — checked {checked}, present {present}, broken {len(broken)}.")
    return {"checked": checked, "present": present, "broken": broken}


def repair_broken(broken, destination, service, user_id, runner, session,
                  on_progress=None, should_cancel=None):
    """Re-download each present-but-broken item via the hardened download path,
    reusing the same filename. Returns {repaired, still_bad}."""
    def log(m):
        if on_progress:
            on_progress({"type": "info", "message": m})

    repaired = still_bad = 0
    by_post = {}
    for it in broken:
        by_post.setdefault(it["post_id"], []).append(it)

    for post_id, items in by_post.items():
        if should_cancel and should_cancel():
            break
        try:
            info = parse_post(session, _post_url(post_id, user_id, service))
        except Exception as e:
            log(f"Could not re-read post {post_id}: {e}")
            still_bad += len(items)
            continue
        media = info["media"]
        for it in items:
            if should_cancel and should_cancel():
                break
            idx = _index_of(it["entry"])
            if not (0 <= idx - 1 < len(media)):
                still_bad += 1
                log(f"Media gone for {it['filename']}")
                continue
            m = media[idx - 1]
            job = {
                "post_url": info["url"], "post_id": post_id, "index": idx,
                "kind": it["kind"], "ext": m["ext"], "url": m["url"],
                "path_key": m["path_key"], "dt": info["dt"], "name": "",
            }
            ok = runner._download_stream(job, it["path"], it["entry"], it["filename"])
            if ok:
                repaired += 1
            else:
                still_bad += 1

    log(f"Repair done — repaired {repaired}, still bad {still_bad}.")
    return {"repaired": repaired, "still_bad": still_bad}
