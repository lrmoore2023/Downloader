"""One-time maintenance: add the "- Twitter -" source tag to existing Twitter
media so it matches the coomerfans naming convention.

  Old: 2025.02.04 - 1886660049600184733_1.jpg
  New: 2025.02.04 - Twitter - 1886660049600184733_1.jpg

Scans every creator's Twitter subtree (<dest>/twitter/…, case-insensitive) listed
in app_state.json and renames files that are still in the old form. Unlike the
coomerfans rename, NOTHING in the archive needs updating: the Twitter (gallery-dl)
archive keys on entries like `twitter_<tweetid>_0_<num>`, not on filenames, so the
rename can't desync it. The app's parsers already accept both the old and new
names, so this is purely cosmetic alignment.

Idempotent: files already in the new form don't match the old pattern and are left
alone. Collisions and missing files are skipped. A reversible TSV log
(old_path<TAB>new_path) is written.

IMPORTANT: close the Downloader app first (so nothing holds the files open), and
make sure the NAS is mounted.

Usage:
  python tools/rename_twitter_source.py                 # dry run, all creators
  python tools/rename_twitter_source.py --apply         # do it
  python tools/rename_twitter_source.py --dest "<folder>"          # one creator (dry run)
  python tools/rename_twitter_source.py --dest "<folder>" --apply
  python tools/rename_twitter_source.py --revert "<rename_log.tsv>"
"""

import argparse
import json
import os
import re
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(APP_DIR, "app_state.json")

# Old Twitter filename: "<date> - <tweetid>_<num>.<ext>" with NO source word.
# (Coomerfans files carry a source segment, so they never match this; and a file
# already renamed to "<date> - Twitter - …" won't match either → idempotent.)
_OLD_RE = re.compile(r"^(\d{4}\.\d{2}\.\d{2}) - (\d+_\d+)\.([^.]+)$")


def _twitter_roots(dest):
    """Top-level 'twitter' subfolder(s) of a creator dest (case-insensitive)."""
    roots = []
    try:
        for e in os.listdir(dest):
            p = os.path.join(dest, e)
            if e.lower() == "twitter" and os.path.isdir(p):
                roots.append(p)
    except OSError:
        pass
    return roots


def plan_for_dest(dest):
    renames, skipped, collisions = [], 0, []
    for root in _twitter_roots(dest):
        for cur, _dirs, files in os.walk(root):
            for fn in files:
                m = _OLD_RE.match(fn)
                if not m:
                    skipped += 1
                    continue
                date, ids, ext = m.groups()
                new_fn = f"{date} - Twitter - {ids}.{ext}"
                old_path = os.path.join(cur, fn)
                new_path = os.path.join(cur, new_fn)
                if os.path.exists(new_path):
                    collisions.append((old_path, new_fn))
                    continue
                renames.append((old_path, new_path))
    return renames, skipped, collisions


def creator_dests():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        sys.exit(f"Could not read {STATE_FILE}: {e}")
    dests = []
    for c in (state.get("creators") or {}).values():
        d = c.get("destination")
        if d and os.path.isdir(d):
            dests.append(d)
        elif d:
            print(f"  (skip, not reachable) {d}")
    return dests


def run(dests, apply):
    all_renames, total_skipped, all_collisions = [], 0, []
    for d in dests:
        r, s, c = plan_for_dest(d)
        all_renames += r
        total_skipped += s
        all_collisions += c

    print(f"creators scanned    : {len(dests)}")
    print(f"files to rename     : {len(all_renames)}")
    print(f"already tagged/other : {total_skipped}")
    print(f"name collisions (skipped) : {len(all_collisions)}")
    print("\nsample mappings:")
    for old_path, new_path in all_renames[:8]:
        print(f"  {os.path.basename(old_path)}\n   -> {os.path.basename(new_path)}")
    if all_collisions:
        print(f"\n!! COLLISIONS ({len(all_collisions)}):")
        for op, nf in all_collisions[:10]:
            print("   ", op, "->", nf)

    if not apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply to rename.")
        return

    log_path = os.path.join(APP_DIR, "rename_twitter_log.tsv")
    done = 0
    with open(log_path, "w", encoding="utf-8") as log:
        for old_path, new_path in all_renames:
            try:
                os.rename(old_path, new_path)
            except OSError as e:
                print(f"  FAILED {old_path}: {e}")
                continue
            log.write(f"{old_path}\t{new_path}\n")
            log.flush()
            done += 1
    print(f"\nDONE — renamed {done} files. Reversible log: {log_path}")


def revert(log_path):
    n = 0
    with open(log_path, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f if "\t" in ln]
    for ln in reversed(lines):
        old_path, new_path = ln.split("\t", 1)
        if os.path.isfile(new_path) and not os.path.exists(old_path):
            os.rename(new_path, old_path)
            n += 1
    print(f"reverted {n} files")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", help="rename just this one creator folder")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert")
    a = ap.parse_args()
    if a.revert:
        revert(a.revert)
        return
    if a.dest:
        if not os.path.isdir(a.dest):
            sys.exit(f"dest not found: {a.dest}")
        dests = [a.dest]
    else:
        dests = creator_dests()
    if not dests:
        sys.exit("No reachable creator folders found (is the NAS mounted?).")
    run(dests, a.apply)


if __name__ == "__main__":
    main()
