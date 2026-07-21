"""Grouping + ranking for duplicate/similar media. Pure analysis: it reads files
(through the signature cache) and returns groups + suggested actions. It never
moves or deletes anything — dedupe_apply.py owns all mutation.

Three entry points, one per workflow:
  * scan_within(dest)          — a creator's own tree, internal dup/near-dupes
  * scan_ref_check(ref, check) — arbitrary reference vs check folders
  * scan_new_ingest(dest, ...) — the <dest>/_new ingest pipeline

Matching mirrors czkawka: images via a BK-tree over the Mean@16 hash (Hamming),
videos via the similario 3D-DCT window match. Exact duplicates (same size + same
content hash) are surfaced as the "identical" tier; perceptual matches as "similar".
"""

import os

import pybktree

from backend.coomerfans_scraper import IMAGE_EXTS, VIDEO_EXTS
from backend.media_library import scan_creator_media
from backend import media_hash as mh

# Folders we create/manage; never scanned as content.
MANAGED_DIRS = {"_new", "_deleted", "_latest"}


# ── file enumeration ──────────────────────────────────────────────────
def _ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _kind(name):
    e = _ext(name)
    if e in VIDEO_EXTS:
        return "video"
    if e in IMAGE_EXTS:
        return "image"
    return None


def _walk_folder(root, bucket):
    """Flat list of media entries under an arbitrary folder (recursive), skipping
    managed (_new/_deleted/...) subfolders. Used for ref/check and _new."""
    out = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in MANAGED_DIRS]
        for fn in filenames:
            kind = _kind(fn)
            if not kind:
                continue
            fp = os.path.join(dirpath, fn)
            out.append(_base_entry(fp, fn, kind, bucket))
    return [e for e in out if e]


def _entries_from_library(dest, bucket="existing"):
    """Creator media via the existing scanner (year-structured, managed dirs
    already excluded). Returns (reachable, entries)."""
    res = scan_creator_media(dest)
    if not res.get("reachable"):
        return (False, [])
    out = []
    for it in res["items"]:
        e = _base_entry(it["path"], it["filename"], it["kind"], bucket)
        if e:
            e["year"] = it.get("year")
            e["source"] = it.get("source", "")
        out.append(e)
    return (True, [e for e in out if e])


def _base_entry(path, filename, kind, bucket):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {
        "path": path.replace("\\", "/"),
        "filename": filename,
        "kind": kind,
        "bucket": bucket,
        "size": st.st_size,
        "mtime": st.st_mtime_ns,
        "year": None,
        "source": "",
        "sig": None,
    }


# ── signatures (cache-backed) ─────────────────────────────────────────
def _ensure_sig(entry, cache, ffmpeg, orientations):
    if entry["sig"] is not None:
        return entry["sig"]
    path, mtime, size, kind = entry["path"], entry["mtime"], entry["size"], entry["kind"]
    if cache:
        hit = cache.get(path, mtime, size)
        if hit is not None:
            entry["sig"] = hit
            return hit
    content = mh.file_blake2b(path)
    reuse = cache.find_by_content(content, size) if cache else None
    if reuse is not None and (reuse.get("ahash") or reuse.get("windows")):
        sig = dict(reuse)
        sig["sha256"] = content
    else:
        sig = mh.compute_signature(path, kind, ffmpeg, orientations=orientations) or {}
        sig["sha256"] = content
    if cache:
        cache.put(path, mtime, size, kind, sig, sha256=content)
    entry["sig"] = sig
    return sig


def _hydrate(entries, cache, ffmpeg, orientations, on_progress, should_cancel, phase):
    total = len(entries)
    for i, e in enumerate(entries):
        if should_cancel and should_cancel():
            break
        _ensure_sig(e, cache, ffmpeg, orientations)
        # copy dimension/dur fields up for the UI
        sig = e["sig"] or {}
        e["width"], e["height"] = sig.get("width"), sig.get("height")
        e["duration"] = sig.get("duration")
        if on_progress and (i % 10 == 0 or i == total - 1):
            on_progress({"phase": "hash", "stage": phase, "done": i + 1, "total": total,
                         "message": f"Hashing {phase}: {i + 1}/{total}"})


# ── union-find ────────────────────────────────────────────────────────
class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


# ── grouping within one set ───────────────────────────────────────────
def _int_hash(hex_hash):
    return int(hex_hash, 16) if hex_hash else None


def _image_components(entries, idxs, threshold):
    """Connected components of image entries within Hamming ``threshold`` (Mean@16),
    matched via a BK-tree. Considers stored dihedral orientations if present."""
    uf = _UF(len(entries))
    hash_to_idxs = {}
    items = set()
    for i in idxs:
        sig = entries[i]["sig"] or {}
        hashes = []
        if sig.get("ahash"):
            hashes.append(sig["ahash"])
        hashes.extend(sig.get("orient") or [])
        for h in hashes:
            v = _int_hash(h)
            if v is None:
                continue
            hash_to_idxs.setdefault(v, set()).add(i)
            items.add(v)
    if not items:
        return []
    tree = pybktree.BKTree(lambda a, b: (a ^ b).bit_count(), list(items))
    for i in idxs:
        sig = entries[i]["sig"] or {}
        v = _int_hash(sig.get("ahash"))
        if v is None:
            continue
        for _dist, matched in tree.find(v, threshold):
            for j in hash_to_idxs.get(matched, ()):
                if j != i:
                    uf.union(i, j)
    return _components_from_uf(uf, idxs)


def _video_components(entries, idxs):
    uf = _UF(len(entries))
    vids = [i for i in idxs if (entries[i]["sig"] or {}).get("windows")]
    for a in range(len(vids)):
        for b in range(a + 1, len(vids)):
            ia, ib = vids[a], vids[b]
            match, _frac = mh.video_match(entries[ia]["sig"], entries[ib]["sig"])
            if match:
                uf.union(ia, ib)
    return _components_from_uf(uf, idxs)


def _components_from_uf(uf, idxs):
    groups = {}
    for i in idxs:
        groups.setdefault(uf.find(i), []).append(i)
    return [g for g in groups.values() if len(g) >= 2]


# ── quality ranking ───────────────────────────────────────────────────
def _pixels(entry):
    w, h = entry.get("width"), entry.get("height")
    return (w or 0) * (h or 0)


def _rank_key(entry):
    """Higher = better: resolution first, byte size as tiebreaker."""
    return (_pixels(entry), entry["size"])


def _best(entries, idxs):
    return max(idxs, key=lambda i: _rank_key(entries[i]))


def _is_upgrade(new_e, existing_e):
    """True when the new file is a genuine higher-quality version of the existing
    one: strictly higher resolution, near-identical content, and not absurdly
    smaller in bytes (guards against bloated re-encodes / upscales)."""
    if _pixels(new_e) <= _pixels(existing_e):
        return False
    if new_e["kind"] == "image":
        d = mh.image_distance(new_e["sig"], existing_e["sig"])
        if d is None or d > mh.IMAGE_UPGRADE_DISTANCE:
            return False
    else:
        match, _f = mh.video_match(new_e["sig"], existing_e["sig"])
        if not match:
            return False
    if new_e["size"] < existing_e["size"] * 0.5:
        return False
    return True


# ── public group shape ────────────────────────────────────────────────
def _tier(entries, idxs):
    sigs = [(entries[i]["size"], (entries[i]["sig"] or {}).get("sha256")) for i in idxs]
    first = sigs[0]
    return "identical" if all(s == first and s[1] for s in sigs) else "similar"


def _public_item(entry, is_best):
    return {
        "path": entry["path"],
        "filename": entry["filename"],
        "kind": entry["kind"],
        "bucket": entry["bucket"],
        "year": entry.get("year"),
        "source": entry.get("source", ""),
        "size": entry["size"],
        "width": entry.get("width"),
        "height": entry.get("height"),
        "duration": entry.get("duration"),
        "is_best": is_best,
    }


def _make_group(gid, entries, idxs, kind, tier, suggested):
    best = _best(entries, idxs)
    items = [_public_item(entries[i], i == best) for i in idxs]
    # keeper's public path for convenience
    return {
        "id": gid,
        "tier": tier,
        "kind": kind,
        "items": items,
        "best_path": entries[best]["path"],
        "suggested": suggested,
    }


# ── workflow 1: within one creator ────────────────────────────────────
def scan_within(dest, cache, ffmpeg, opts=None, on_progress=None, should_cancel=None):
    opts = opts or {}
    threshold = int(opts.get("image_distance", mh.IMAGE_DEFAULT_DISTANCE))
    orientations = bool(opts.get("orientations", False))

    reachable, entries = _entries_from_library(dest, "existing")
    if not reachable:
        return {"reachable": False, "groups": [], "unmatched_new": []}
    _hydrate(entries, cache, ffmpeg, orientations, on_progress, should_cancel, "media")

    groups = _group_one_set(entries, list(range(len(entries))), threshold,
                            on_progress, action="keep_one")
    return {"reachable": True, "mode": "within", "groups": groups, "unmatched_new": []}


def _group_one_set(entries, idxs, threshold, on_progress, action):
    img_idxs = [i for i in idxs if entries[i]["kind"] == "image"]
    vid_idxs = [i for i in idxs if entries[i]["kind"] == "video"]
    if on_progress:
        on_progress({"phase": "compare", "message": "Grouping matches…"})
    groups = []
    n = 0
    for comp in _image_components(entries, img_idxs, threshold):
        tier = _tier(entries, comp)
        best = _best(entries, comp)
        groups.append(_make_group(
            f"g{n}", entries, comp, "image", tier,
            {"action": action, "keep_path": entries[best]["path"],
             "reason": "highest resolution kept; others retired to _deleted"}))
        n += 1
    for comp in _video_components(entries, vid_idxs):
        tier = _tier(entries, comp)
        best = _best(entries, comp)
        groups.append(_make_group(
            f"g{n}", entries, comp, "video", tier,
            {"action": action, "keep_path": entries[best]["path"],
             "reason": "highest resolution kept; others retired to _deleted"}))
        n += 1
    return groups


# ── workflow 2: reference vs check ────────────────────────────────────
def scan_ref_check(ref_dir, check_dir, cache, ffmpeg, opts=None,
                   on_progress=None, should_cancel=None):
    opts = opts or {}
    threshold = int(opts.get("image_distance", mh.IMAGE_DEFAULT_DISTANCE))
    orientations = bool(opts.get("orientations", False))
    if not os.path.isdir(ref_dir) or not os.path.isdir(check_dir):
        return {"reachable": False, "groups": [], "unmatched_new": []}

    ref = _walk_folder(ref_dir, "ref")
    check = _walk_folder(check_dir, "check")
    entries = ref + check
    _hydrate(entries, cache, ffmpeg, orientations, on_progress, should_cancel, "folders")

    ref_idx = list(range(len(ref)))
    check_idx = list(range(len(ref), len(entries)))
    groups = _cross_groups(entries, check_idx, ref_idx, threshold, on_progress,
                           primary_bucket="check", other_bucket="ref",
                           suggest=_suggest_ref_check)
    return {"reachable": True, "mode": "ref_check", "groups": groups, "unmatched_new": []}


def _suggest_ref_check(entries, primary_idxs, other_idx):
    ref_name = entries[other_idx]["filename"]
    return {"action": "rename_to_ref", "reference_path": entries[other_idx]["path"],
            "reference_name": ref_name,
            "reason": f"rename check file(s) to reference name '{ref_name}' "
                      f"(or delete the check copy)"}


# ── workflow 3: _new ingest ───────────────────────────────────────────
def scan_new_ingest(dest, cache, ffmpeg, resolve_entry=None, opts=None,
                    on_progress=None, should_cancel=None):
    """dest is the creator destination; its <dest>/_new is the ingest folder.
    ``resolve_entry(filename, kind, year) -> (db_path, entry)|None`` maps a matched
    library file back to its archive row so apply can keep it consistent."""
    opts = opts or {}
    threshold = int(opts.get("image_distance", mh.IMAGE_DEFAULT_DISTANCE))
    orientations = bool(opts.get("orientations", True))   # default ON for messy dumps
    new_dir = os.path.join(dest, "_new")
    if not os.path.isdir(new_dir):
        return {"reachable": True, "mode": "new", "groups": [], "unmatched_new": [],
                "note": "no _new folder"}

    reachable, existing = _entries_from_library(dest, "existing")
    if not reachable:
        return {"reachable": False, "groups": [], "unmatched_new": []}
    new_entries = _walk_folder(new_dir, "new")
    if not new_entries:
        return {"reachable": True, "mode": "new", "groups": [], "unmatched_new": [],
                "note": "_new is empty"}

    # Orientation (8x) hashing only needs to run on the small _new set — a rotated
    # dump file still matches an existing library file because the query side (new)
    # carries the extra orientations. So hash the (potentially large) library
    # without them and the _new dump with them.
    _hydrate(existing, cache, ffmpeg, False, on_progress, should_cancel, "library")
    _hydrate(new_entries, cache, ffmpeg, orientations, on_progress, should_cancel, "_new")
    entries = existing + new_entries
    ex_idx = list(range(len(existing)))
    new_idx = list(range(len(existing), len(entries)))

    groups = []
    n = 0

    # Stage A — dedup WITHIN _new first.
    internal = _image_components(entries, [i for i in new_idx if entries[i]["kind"] == "image"], threshold)
    internal += _video_components(entries, [i for i in new_idx if entries[i]["kind"] == "video"])
    internal_members = set()
    reps = []                       # best of each internal component (representatives)
    for comp in internal:
        internal_members.update(comp)
        best = _best(entries, comp)
        reps.append(best)
        groups.append(_make_group(
            f"g{n}", entries, comp, entries[best]["kind"], _tier(entries, comp),
            {"action": "keep_one", "keep_path": entries[best]["path"], "scope": "new_internal",
             "reason": "duplicate inside _new — keep the best, delete the rest"}))
        n += 1

    # Candidates for cross-matching = internal reps + new singletons.
    singletons = [i for i in new_idx if i not in internal_members]
    candidates = reps + singletons

    # Stage B — candidates vs existing library.
    matched_candidates = set()
    for group, cand_idx, existing_i in _pairwise_cross(entries, candidates, ex_idx,
                                                        threshold, on_progress):
        matched_candidates.add(cand_idx)
        new_e, ex_e = entries[cand_idx], entries[existing_i]
        year = ex_e.get("year")
        arch = resolve_entry(ex_e["filename"], ex_e["kind"], year) if resolve_entry else None
        can_adopt = _is_upgrade(new_e, ex_e) and arch is not None
        if _is_upgrade(new_e, ex_e) and arch is None:
            reason = ("higher quality, but its match is a Twitter/derpibooru/discord "
                      "file — resolve manually")
            suggested = {"action": "skip", "reason": reason}
        elif can_adopt:
            suggested = {
                "action": "adopt", "new_path": new_e["path"],
                "target_path": ex_e["path"], "target_name": ex_e["filename"],
                "db_path": arch[0], "entry": arch[1],
                "reason": f"higher quality — adopt name '{ex_e['filename']}', "
                          f"retire the old copy to _deleted"}
        else:
            suggested = {
                "action": "keep_one", "keep_path": ex_e["path"], "scope": "new_vs_existing",
                "reason": "existing copy is equal/better — retire the _new copy to _deleted"}
        g = _make_group(f"g{n}", entries, [cand_idx, existing_i], new_e["kind"],
                        _tier(entries, [cand_idx, existing_i]), suggested)
        groups.append(g)
        n += 1

    # Unmatched new files (no internal group, no library match) — stay in _new.
    unmatched = [_public_item(entries[i], False)
                 for i in singletons if i not in matched_candidates]
    return {"reachable": True, "mode": "new", "groups": groups,
            "unmatched_new": unmatched}


def _cross_groups(entries, primary_idxs, other_idxs, threshold, on_progress,
                  primary_bucket, other_bucket, suggest):
    """Group each primary-bucket file with its best match in the other bucket."""
    groups = []
    n = 0
    # gather (primary, other) matched pairs, then collapse by matched-other.
    by_other = {}
    for _g, p_idx, o_idx in _pairwise_cross(entries, primary_idxs, other_idxs,
                                            threshold, on_progress):
        by_other.setdefault(o_idx, []).append(p_idx)
    for o_idx, p_list in by_other.items():
        member = [o_idx] + p_list
        groups.append(_make_group(
            f"g{n}", entries, member, entries[o_idx]["kind"],
            _tier(entries, member), suggest(entries, p_list, o_idx)))
        n += 1
    return groups


def _pairwise_cross(entries, primary_idxs, other_idxs, threshold, on_progress):
    """Yield (None, primary_idx, best_other_idx) for each primary that matches some
    file in the other set. Images via BK-tree, videos via similario match."""
    if on_progress:
        on_progress({"phase": "compare", "message": "Matching against library…"})
    # image BK-tree over the other set (primary + orientations).
    hash_to_idx = {}
    items = set()
    for i in other_idxs:
        sig = entries[i]["sig"] or {}
        for h in ([sig["ahash"]] if sig.get("ahash") else []) + list(sig.get("orient") or []):
            v = _int_hash(h)
            if v is not None:
                hash_to_idx.setdefault(v, i)
                items.add(v)
    tree = pybktree.BKTree(lambda a, b: (a ^ b).bit_count(), list(items)) if items else None

    other_vids = [i for i in other_idxs if (entries[i]["sig"] or {}).get("windows")]

    for p in primary_idxs:
        e = entries[p]
        sig = e["sig"] or {}
        best_other, best_score = None, None
        if e["kind"] == "image" and tree is not None:
            cand_hashes = ([sig["ahash"]] if sig.get("ahash") else []) + list(sig.get("orient") or [])
            for h in cand_hashes:
                v = _int_hash(h)
                if v is None:
                    continue
                for dist, matched in tree.find(v, threshold):
                    if best_score is None or dist < best_score:
                        best_score, best_other = dist, hash_to_idx.get(matched)
        elif e["kind"] == "video":
            for o in other_vids:
                match, frac = mh.video_match(sig, entries[o]["sig"])
                if match and (best_score is None or frac > best_score):
                    best_score, best_other = frac, o
        if best_other is not None:
            yield (None, p, best_other)
