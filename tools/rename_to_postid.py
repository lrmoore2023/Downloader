"""One-time maintenance: rename a coomerfans creator's files from the old
title-slug naming to the post-ID naming, driven by the creator's archive DB.

Old: 2025.09.12 - OF - enjoy-a-short-little-test-ride-on-my-clear-chair_1.mp4
New: 2025.09.12 - OF - 89904542_1.mp4        (89904542 = post_id)

The archive (entry = 'coomerfans_{post_id}_{index}', plus the stored filename,
kind, year) is the exact slug->post_id map, so no re-scraping is needed. The
archive's filename column is updated in lockstep with each disk rename, or the
app would think the file is missing and re-download it.

Files already named with the post_id are skipped (new == old). A reversible
TSV log (old_path<TAB>new_path) is written so the whole pass can be undone.

IMPORTANT: close the Downloader app before running --apply (it must not hold
the archive DB or the files open).

Usage:
  python tools/rename_to_postid.py --archive "<db>" --dest "<folder>"            # dry run
  python tools/rename_to_postid.py --archive "<db>" --dest "<folder>" --apply    # do it
  python tools/rename_to_postid.py --revert "<rename_log.tsv>"                    # undo
"""

import argparse
import os
import re
import sqlite3
import sys

_FN_RE = re.compile(r"^(\d{4}\.\d{2}\.\d{2}) - (.+?) - (.+)\.([^.]+)$")


def _disk_path(dest, kind, year, filename):
    """Mirror backend.coomerfans_scraper.target_path layout."""
    if kind == "image":
        return os.path.join(dest, "images", year, filename)
    return os.path.join(dest, year, filename)


def plan_renames(archive_db, dest):
    conn = sqlite3.connect(archive_db)
    rows = conn.execute(
        "SELECT entry, post_id, filename, kind, year FROM archive"
    ).fetchall()
    conn.close()

    renames, skipped, missing, collisions, unparsed = [], 0, [], [], []
    for entry, post_id, filename, kind, year in rows:
        m = _FN_RE.match(filename)
        if not m:
            unparsed.append((entry, filename))
            continue
        date, site, _oldname, ext = m.groups()
        index = entry.rsplit("_", 1)[-1]
        new_fn = f"{date} - {site} - {post_id}_{index}.{ext}"
        if new_fn == filename:
            skipped += 1
            continue
        old_path = _disk_path(dest, kind, year, filename)
        new_path = _disk_path(dest, kind, year, new_fn)
        if not os.path.isfile(old_path):
            missing.append((entry, filename))
            continue
        if os.path.exists(new_path):
            collisions.append((filename, new_fn))
            continue
        renames.append((entry, old_path, new_path, filename, new_fn))
    return {
        "renames": renames, "skipped": skipped, "missing": missing,
        "collisions": collisions, "unparsed": unparsed, "total": len(rows),
    }


def run(archive_db, dest, apply):
    p = plan_renames(archive_db, dest)
    print(f"archive rows : {p['total']}")
    print(f"to rename    : {len(p['renames'])}")
    print(f"skipped (already post_id) : {p['skipped']}")
    print(f"missing on disk (skipped) : {len(p['missing'])}")
    print(f"name collisions (skipped) : {len(p['collisions'])}")
    print(f"unparsable filenames      : {len(p['unparsed'])}")
    print("\nsample mappings:")
    for _e, _op, _np, old, new in p["renames"][:6]:
        print(f"  {old}\n   -> {new}")
    for label, items in (("COLLISIONS", p["collisions"]),
                         ("MISSING", p["missing"]),
                         ("UNPARSED", p["unparsed"])):
        if items:
            print(f"\n!! {label} ({len(items)}):")
            for it in items[:10]:
                print("   ", it)

    if not apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply to rename.")
        return

    log_path = os.path.join(os.path.dirname(os.path.abspath(archive_db)),
                            f"rename_log_{os.path.basename(dest)}.tsv")
    conn = sqlite3.connect(archive_db)
    done = 0
    with open(log_path, "w", encoding="utf-8") as log:
        for entry, old_path, new_path, _old, new_fn in p["renames"]:
            os.rename(old_path, new_path)
            # Commit the archive update per-file so a mid-run failure leaves disk
            # and DB in sync (already-renamed rows become no-ops on a re-run).
            conn.execute("UPDATE archive SET filename=? WHERE entry=?", (new_fn, entry))
            conn.commit()
            log.write(f"{old_path}\t{new_path}\n")
            log.flush()
            done += 1
    conn.close()
    print(f"\nDONE — renamed {done} files; archive updated. Reversible log: {log_path}")


def revert(log_path):
    n = 0
    with open(log_path, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f if "\t" in ln]
    # NOTE: reverts disk names only. Re-run --apply afterward to resync the
    # archive, or keep the archive as-is if you are reverting immediately.
    for ln in reversed(lines):
        old_path, new_path = ln.split("\t", 1)
        if os.path.isfile(new_path) and not os.path.exists(old_path):
            os.rename(new_path, old_path)
            n += 1
    print(f"reverted {n} files (disk only — resync archive if needed)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive")
    ap.add_argument("--dest")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert")
    a = ap.parse_args()
    if a.revert:
        revert(a.revert)
        return
    if not a.archive or not a.dest:
        ap.error("--archive and --dest are required (or use --revert)")
    if not os.path.isfile(a.archive):
        sys.exit(f"archive not found: {a.archive}")
    if not os.path.isdir(a.dest):
        sys.exit(f"dest not found: {a.dest}")
    run(a.archive, a.dest, a.apply)


if __name__ == "__main__":
    main()
