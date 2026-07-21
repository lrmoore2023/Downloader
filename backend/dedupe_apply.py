"""Execute (and reverse) the user's dedup decisions. This is the only module that
moves/deletes files or edits archives.

Everything is reversible: each action appends one JSON line to a journal, and
``revert`` replays the journal in reverse — restoring both disk and the per-creator
archive. Displaced/retired files are never hard-deleted; they go to a ``_deleted``
tree mirroring the source layout, collision-suffixed so nothing is ever clobbered.

Decision shapes (validated by api.py against the stored scan before calling):
  {group_id, action:"skip"}
  {group_id, action:"keep_one",       keep_path}
  {group_id, action:"rename_to_ref",  reference_name}
  {group_id, action:"adopt",          new_path, target_path, db_path, entry}
"""

import json
import os
import sqlite3
import stat
import time


# ── safe filesystem primitives ────────────────────────────────────────
def _replace_with_retry(src, dst, attempts=10, delay=0.1):
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            try:
                if os.path.exists(dst):
                    os.chmod(dst, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            time.sleep(delay)


def _unique(path):
    """A non-colliding variant of ``path`` — inserts ' (2)', ' (3)'… before the
    extension until the name is free."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 2
    while True:
        cand = f"{root} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def _mirror_deleted(base_root, path):
    """Destination under ``<base_root>/_deleted`` mirroring the file's location
    relative to base_root (falls back to basename if outside it). Collision-safe."""
    base_root = os.path.abspath(base_root)
    ap = os.path.abspath(path)
    try:
        rel = os.path.relpath(ap, base_root)
        if rel.startswith(".."):
            rel = os.path.basename(ap)
    except ValueError:
        rel = os.path.basename(ap)
    dst = os.path.join(base_root, "_deleted", rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    return _unique(dst)


def _move(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    dst = _unique(dst)
    _replace_with_retry(src, dst)
    return dst


def _update_archive_size(db_path, entry, filename, expected_size):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE archive SET filename=?, expected_size=? WHERE entry=?",
                     (filename, expected_size, entry))
        conn.commit()
    finally:
        conn.close()


def _read_archive_row(db_path, entry):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT filename, expected_size FROM archive WHERE entry=?",
                           (entry,))
        return cur.fetchone()
    finally:
        conn.close()


# ── apply ─────────────────────────────────────────────────────────────
def apply(decisions, groups_by_id, ctx, journal_path,
          on_progress=None, should_cancel=None):
    """Run decisions against the stored scan groups.

    ctx = {"mode": within|ref_check|new, "dest": <creator dest>, "check_dir": <dir>}
    Returns {moved, renamed, adopted, deleted, skipped, errors, journal}.
    """
    mode = ctx.get("mode")
    del_base = ctx.get("check_dir") if mode == "ref_check" else ctx.get("dest")
    counts = {"moved": 0, "renamed": 0, "adopted": 0, "deleted": 0,
              "skipped": 0, "errors": []}
    total = len(decisions)

    with open(journal_path, "w", encoding="utf-8") as jf:
        for n, dec in enumerate(decisions):
            if should_cancel and should_cancel():
                break
            gid = dec.get("group_id")
            action = dec.get("action")
            group = groups_by_id.get(gid)
            if not group or action == "skip":
                counts["skipped"] += 1
                continue
            try:
                if action == "keep_one":
                    _apply_keep_set(group, mode, del_base, jf, counts,
                                    {dec.get("keep_path")})
                elif action == "keep_many":
                    _apply_keep_set(group, mode, del_base, jf, counts,
                                    set(dec.get("keep_paths") or []))
                elif action == "rename_to_ref":
                    _apply_rename_ref(dec, group, jf, counts)
                elif action == "adopt":
                    _apply_adopt(dec, group, del_base, jf, counts)
                else:
                    counts["skipped"] += 1
            except Exception as e:
                counts["errors"].append({"group_id": gid, "error": str(e)})
            if on_progress:
                on_progress({"done": n + 1, "total": total,
                             "message": f"Applying {n + 1}/{total}"})

    counts["journal"] = journal_path
    return counts


def _bucket_deletable(item, mode):
    """Whether an item may be retired in this mode, ignoring keeper choice.
    Never lets a reference/existing library file be deleted."""
    if mode == "within":
        return True
    if mode == "ref_check":
        return item["bucket"] == "check"
    if mode == "new":
        return item["bucket"] == "new"
    return False


def _apply_keep_set(group, mode, del_base, jf, counts, keep_paths):
    """Retire every deletable item NOT in keep_paths. Covers keep-one (one path),
    keep-multiple (several), and delete-all (empty set)."""
    keep_paths = {p for p in (keep_paths or []) if p}
    for it in group["items"]:
        if it["path"] in keep_paths:
            continue
        if not _bucket_deletable(it, mode):
            continue
        src = it["path"]
        if not os.path.isfile(src):
            continue
        dst = _move(src, _mirror_deleted(del_base, src))
        jf.write(json.dumps({"action": "delete", "src": src, "dst": dst}) + "\n")
        jf.flush()
        counts["deleted"] += 1


def _apply_rename_ref(dec, group, jf, counts):
    ref_name = dec.get("reference_name") or group["suggested"].get("reference_name")
    if not ref_name:
        return
    for it in group["items"]:
        if it["bucket"] != "check":
            continue
        src = it["path"]
        if not os.path.isfile(src):
            continue
        dst = os.path.join(os.path.dirname(src), ref_name)
        # keep the check file's real extension if the reference ext differs
        src_ext = os.path.splitext(src)[1]
        if os.path.splitext(dst)[1].lower() != src_ext.lower():
            dst = os.path.splitext(dst)[0] + src_ext
        if os.path.abspath(dst) == os.path.abspath(src):
            continue
        dst = _move(src, dst)
        jf.write(json.dumps({"action": "rename", "src": src, "dst": dst}) + "\n")
        jf.flush()
        counts["renamed"] += 1


def _apply_adopt(dec, group, del_base, jf, counts):
    new_path = dec.get("new_path") or group["suggested"].get("new_path")
    target_path = dec.get("target_path") or group["suggested"].get("target_path")
    db_path = dec.get("db_path") or group["suggested"].get("db_path")
    entry = dec.get("entry") or group["suggested"].get("entry")
    if not (new_path and target_path and os.path.isfile(new_path)):
        counts["skipped"] += 1
        return

    target_dir = os.path.dirname(target_path)
    stem = os.path.splitext(os.path.basename(target_path))[0]
    new_ext = os.path.splitext(new_path)[1]
    adopted_name = stem + new_ext
    adopted_path = os.path.join(target_dir, adopted_name)

    old_row = _read_archive_row(db_path, entry) if (db_path and entry) else None
    old_filename = old_row[0] if old_row else os.path.basename(target_path)
    old_size = old_row[1] if old_row else None

    # 1. retire the existing original (if present) to _deleted
    old_target_deleted = None
    if os.path.isfile(target_path):
        old_target_deleted = _move(target_path, _mirror_deleted(del_base, target_path))

    # 2. move the higher-quality _new file into the vacated identity
    adopted_final = _move(new_path, adopted_path)
    new_size = os.path.getsize(adopted_final)

    # 3. keep the archive consistent (filename may change ext; size definitely does)
    if db_path and entry:
        _update_archive_size(db_path, entry, adopted_name, new_size)

    jf.write(json.dumps({
        "action": "adopt", "new_src": new_path, "adopted_dst": adopted_final,
        "old_target": target_path, "old_target_deleted": old_target_deleted,
        "db_path": db_path, "entry": entry,
        "old_filename": old_filename, "new_filename": adopted_name,
        "old_size": old_size, "new_size": new_size,
    }) + "\n")
    jf.flush()
    counts["adopted"] += 1


# ── revert ────────────────────────────────────────────────────────────
def revert(journal_path, on_progress=None):
    """Reverse every action recorded in the journal (disk + archive)."""
    with open(journal_path, "r", encoding="utf-8") as f:
        lines = [json.loads(ln) for ln in f if ln.strip()]
    counts = {"restored": 0, "errors": []}
    for rec in reversed(lines):
        try:
            _revert_one(rec)
            counts["restored"] += 1
        except Exception as e:
            counts["errors"].append({"rec": rec.get("action"), "error": str(e)})
    return counts


def _revert_one(rec):
    action = rec.get("action")
    if action in ("delete", "rename"):
        # move dst back to src
        if os.path.isfile(rec["dst"]):
            os.makedirs(os.path.dirname(rec["src"]), exist_ok=True)
            _replace_with_retry(rec["dst"], rec["src"])
    elif action == "adopt":
        # 1. adopted file back to its _new origin
        if os.path.isfile(rec["adopted_dst"]):
            os.makedirs(os.path.dirname(rec["new_src"]), exist_ok=True)
            _replace_with_retry(rec["adopted_dst"], rec["new_src"])
        # 2. old original back from _deleted
        if rec.get("old_target_deleted") and os.path.isfile(rec["old_target_deleted"]):
            os.makedirs(os.path.dirname(rec["old_target"]), exist_ok=True)
            _replace_with_retry(rec["old_target_deleted"], rec["old_target"])
        # 3. archive filename/size back
        if rec.get("db_path") and rec.get("entry"):
            _update_archive_size(rec["db_path"], rec["entry"],
                                 rec.get("old_filename"), rec.get("old_size"))
