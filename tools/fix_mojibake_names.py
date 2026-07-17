"""One-time maintenance: repair Japanese filenames that were garbled on extraction.

Some Fanbox packs are zips whose member names are Shift-JIS/CP932 with the zip's
UTF-8 flag unset. Python's zipfile then decoded those names as CP437, so the files
landed on disk as mojibake, e.g.

  2026.03.05 - Fanbox - ÅHì╪é┐éßé±üyû{ò╥üz_20260306 - ÅHì╪é┐éßé±üyâïü[âvö┼01üz.mp4
  ->
  2026.03.05 - Fanbox - 秋菜ちゃん【本編】_20260306 - 秋菜ちゃん【ループ版01】.mp4

The extractor is now fixed (backend/pawchive_extract._zip_member_name), so newly
downloaded packs are named correctly. This tool repairs files ALREADY on disk in
place, so you don't have to re-download. The ASCII parts of a name (date, ' - Fanbox
- ', the trailing '_YYYYMMDD', the extension) are unchanged by the reversal.

Safe by construction — see backend.pawchive_extract.repair_mojibake_name: a name is
only renamed when it cleanly re-encodes to CP437 AND decodes to text that actually
contains Japanese, so correctly-named files (ASCII, proper UTF-8 Japanese, or Latin
accents) are never touched. Idempotent (a repaired name no longer matches). Files and
folders are both handled, deepest-first so nested names stay valid. Name collisions
are skipped and reported. A reversible TSV log (old<TAB>new) is written.

These files are extracted-archive media (the zip entry is marked 'extracted' in the
per-creator archive DB; the individual media names are NOT stored there), so renaming
them cannot desync the archive or trigger a re-download.

IMPORTANT: close the Downloader app first, and make sure the NAS is mounted.

Usage:
  python tools/fix_mojibake_names.py --root "P:\\Hentai\\2D\\<creator>"          # dry run
  python tools/fix_mojibake_names.py --root "P:\\Hentai\\2D\\<creator>" --apply
  python tools/fix_mojibake_names.py --revert "<mojibake_rename_log.tsv>"
"""

import argparse
import os
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

# The repaired names are Japanese; a default Windows console is cp1252 and would
# crash on print(). Force UTF-8 output (replacing anything unmappable) so the
# dry-run/summary is always readable regardless of the console code page.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.pawchive_extract import repair_mojibake_name


def plan(root):
    """Collect rename ops under `root`, deepest-first. Each op is
    (old_path, new_path); a file/folder whose target already exists is a collision."""
    renames, collisions, scanned = [], [], 0
    # topdown=False → children before parents, so applying in this order renames a
    # folder's contents before the folder itself.
    for cur, dirs, files in os.walk(root, topdown=False):
        for name in files + dirs:
            scanned += 1
            fixed = repair_mojibake_name(name)
            if not fixed or fixed == name:
                continue
            old_path = os.path.join(cur, name)
            new_path = os.path.join(cur, fixed)
            if os.path.exists(new_path):
                collisions.append((old_path, fixed))
                continue
            renames.append((old_path, new_path))
    return renames, collisions, scanned


def run(root, apply):
    if not os.path.isdir(root):
        sys.exit(f"root not found (is the NAS mounted?): {root}")
    renames, collisions, scanned = plan(root)
    print(f"root            : {root}")
    print(f"entries scanned : {scanned}")
    print(f"to repair       : {len(renames)}")
    print(f"collisions (skip): {len(collisions)}")
    print("\nsample mappings:")
    for old_path, new_path in renames[:12]:
        print(f"  {os.path.basename(old_path)}\n   -> {os.path.basename(new_path)}")
    if collisions:
        print(f"\n!! COLLISIONS ({len(collisions)}) — left as-is:")
        for op, nf in collisions[:12]:
            print("   ", op, "->", nf)

    if not apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply to rename.")
        return

    log_path = os.path.join(APP_DIR, "mojibake_rename_log.tsv")
    done = 0
    with open(log_path, "w", encoding="utf-8") as log:
        for old_path, new_path in renames:
            try:
                os.rename(old_path, new_path)
            except OSError as e:
                print(f"  FAILED {old_path}: {e}")
                continue
            log.write(f"{old_path}\t{new_path}\n")
            log.flush()
            done += 1
    print(f"\nDONE — repaired {done} name(s). Reversible log: {log_path}")


def revert(log_path):
    with open(log_path, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f if "\t" in ln]
    n = 0
    # Reverse order so a parent folder is restored before its (deeper) contents.
    for ln in reversed(lines):
        old_path, new_path = ln.split("\t", 1)
        if os.path.exists(new_path) and not os.path.exists(old_path):
            try:
                os.rename(new_path, old_path)
                n += 1
            except OSError as e:
                print(f"  FAILED {new_path}: {e}")
    print(f"reverted {n} name(s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", help="folder tree to repair (recurses into all subfolders)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert")
    a = ap.parse_args()
    if a.revert:
        revert(a.revert)
        return
    if not a.root:
        sys.exit("--root is required (or use --revert <log.tsv>)")
    run(a.root, a.apply)


if __name__ == "__main__":
    main()
