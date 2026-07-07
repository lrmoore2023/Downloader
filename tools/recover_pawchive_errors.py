"""One-time recovery helper: turn a pawchive run's 404 failures (which are logged
by *filename only*) back into direct file URLs + post-page URLs, then probe each to
say whether it's still recoverable or genuinely gone upstream.

Why this exists: `pawchive_diag.jsonl` records every give-up as a `dl_end_fail`
event with the *built* filename (e.g. "2024.11.21 - Patreon - Kabutsuri_teaser.gif")
but never the download URL — and the URL (a content-addressed sha256 path) lives
only in the live API response. So to hand the user the links for files that 404'd,
we re-crawl the creator's API, match each failed filename back to its media item,
and rebuild `https://file.pawchive.pw/data{path}?f={name}` + the post page.

The diag file is overwritten each run, so it reflects the *most recent* pawchive
run. Pass --diag / --service / --user to target a specific run/creator; by default
it reads pawchive_diag.jsonl and infers the creator from its api_req lines.

Usage:
  python tools/recover_pawchive_errors.py
  python tools/recover_pawchive_errors.py --service patreon --user 349605
  python tools/recover_pawchive_errors.py --diag path/to/pawchive_diag.jsonl --no-probe
"""

import argparse
import json
import os
import re
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from backend.pawchive_scraper import (  # noqa: E402
    make_session, iter_posts, parse_post, fetch_post, sanitize_filename,
)
from backend.pawchive_archive import entry_key  # noqa: E402
from backend.creator_runner import (  # noqa: E402
    pawchive_archive_path, errors_db_path,
)
from backend.download_errors import FailureStore, FAILED, GONE  # noqa: E402

DEFAULT_DIAG = os.path.join(APP_DIR, "pawchive_diag.jsonl")
STATE_FILE = os.path.join(APP_DIR, "app_state.json")

# "<date>[ HH.MM] - <SiteCode> - <rest>" — capture the date and everything after the
# "SITE - " prefix (the rest may itself contain " - ", e.g. "ExJ - The Reunion.mp4").
_BUILT_RE = re.compile(
    r"^(?P<date>\d{4}\.\d{2}\.\d{2})(?: \d{2}\.\d{2})? - [^-]+ - (?P<rest>.+)$"
)
# A leading page-order ordinal the runner inserts for multi-image posts: "01 - ".
_ORDINAL_RE = re.compile(r"^\d{2,} - (?P<name>.+)$")


def strip_collision(name):
    """Drop a trailing '_<n>' collision suffix (add_index_suffix) before the ext.
    Only unpadded, low integers are treated as collision markers, so a real name
    like 'Kabutsuri_Patreon_02.mp4' (zero-padded) is left intact."""
    root, dot, ext = name.rpartition(".")
    if not dot:
        return name
    m = re.match(r"^(?P<stem>.+)_(?P<n>[1-9]\d?)$", root)
    if m:
        return f"{m.group('stem')}.{ext}"
    return name


def parse_built_name(built):
    """'2024.11.21 - Patreon - 01 - Foo_1.gif' -> (date_str, [candidate core names]).

    Returns the date and a list of plausible original media names to match against
    the API (most specific first): the raw rest, the rest minus an ordinal, and each
    of those minus a collision suffix. All are compared case-insensitively later."""
    m = _BUILT_RE.match(built)
    if not m:
        return None, []
    date_str = m.group("date")
    rest = m.group("rest")
    cands = [rest]
    om = _ORDINAL_RE.match(rest)
    if om:
        cands.append(om.group("name"))
    for c in list(cands):
        sc = strip_collision(c)
        if sc != c:
            cands.append(sc)
    # de-dup, keep order
    seen, out = set(), []
    for c in cands:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            out.append(c)
    return date_str, out


def read_failures(diag_path):
    """Return (list of built filenames that 404'd, inferred (service, user_id)).

    Dedups the exact same logged filename; distinct source de-dup happens later,
    by resolved media path."""
    names, service, user_id = [], None, None
    seen = set()
    api_re = re.compile(r"/api/v1/([A-Za-z0-9_]+)/user/([^/?\s]+)")
    with open(diag_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if service is None and ev.get("event") == "api_req":
                mm = api_re.search(ev.get("what") or "")
                if mm:
                    service, user_id = mm.group(1).lower(), mm.group(2)
            if ev.get("event") == "dl_end_fail" and ev.get("status") == 404:
                f = ev.get("file")
                if f and f not in seen:
                    seen.add(f)
                    names.append(f)
    return names, service, user_id


def build_media_index(session, service, user_id):
    """date_str -> [ {name, url, path, post_id, post_url} ] for every media item."""
    index = {}
    count = 0

    def on_page(page, n):
        print(f"  page {page}: {n} post(s)", file=sys.stderr)

    for raw in iter_posts(session, service, user_id, on_page=on_page):
        post = parse_post(raw)
        # Un-imported listing rows can lack inline media; fetch detail if empty.
        if not post["media"] and not post["detail_fetched"]:
            try:
                post = parse_post(
                    fetch_post(session, service, user_id, post["post_id"]))
            except Exception:
                pass
        dt = post["dt"]
        date_str = f"{dt:%Y.%m.%d}" if dt else "0000.00.00"
        # 1-based position within the post's media list — matches the runner's
        # entry_key(post_id, idx) so a seeded failure aligns with the real archive.
        for idx, m in enumerate(post["media"], 1):
            index.setdefault(date_str, []).append({
                "name": m["name"],
                "url": m["url"],
                "path": m["path"],
                "post_id": post["post_id"],
                "post_url": post["url"],
                "index": idx,
                "kind": m["kind"],
            })
            count += 1
    print(f"  indexed {count} media item(s) across "
          f"{len(index)} date(s)", file=sys.stderr)
    return index


def match(built, index):
    """Best media matches for one built filename. Returns (list of media dicts,
    date_str, candidate-names-tried)."""
    date_str, cands = parse_built_name(built)
    if not date_str:
        return [], None, []
    pool = index.get(date_str, [])
    for cand in cands:
        target = sanitize_filename(cand).lower()
        exact = [m for m in pool if sanitize_filename(m["name"]).lower() == target]
        if exact:
            return exact, date_str, cands
    # fall back: fuzzy contains on the first (rawest) candidate stem
    if cands:
        stem = strip_collision(cands[0]).rsplit(".", 1)[0].lower()
        if len(stem) >= 4:
            fuzzy = [m for m in pool
                     if stem in sanitize_filename(m["name"]).lower()]
            if fuzzy:
                return fuzzy, date_str, cands
            # last resort: same name may have moved to a different post/date, so
            # search the whole listing (the logged date came from the *old* post).
            glob = [m for a in index.values() for m in a
                    if stem in sanitize_filename(m["name"]).lower()]
            if glob:
                return glob, date_str, cands
    return [], date_str, cands


def probe(session, url):
    """HEAD (fall back to a 1-byte GET) -> HTTP status int, or None on error."""
    try:
        r = session.head(url, timeout=(10, 20), allow_redirects=True)
        if r.status_code in (403, 405) or r.status_code >= 500:
            r = session.get(url, timeout=(10, 20), stream=True,
                            headers={"Range": "bytes=0-0"})
            r.close()
        return r.status_code
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--diag", default=DEFAULT_DIAG, help="pawchive_diag.jsonl path")
    ap.add_argument("--service", help="override service (e.g. patreon)")
    ap.add_argument("--user", help="override user_id (e.g. 349605)")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip the live HTTP status check per URL")
    ap.add_argument("--cookies", help="cookies.txt path (optional)")
    ap.add_argument("--seed", action="store_true",
                    help="write the discovered failures into the creator's real "
                         "failure store so the 'Redownload Errors' button lights up "
                         "(recoverable→failed, confirmed-404→gone)")
    ap.add_argument("--archive-dir",
                    help="archive dir for --seed (default: app_state.json's archive_dir)")
    ap.add_argument("--names-file",
                    help="read failed built filenames (one per line) from this file "
                         "instead of parsing the diag (the diag is overwritten each "
                         "run); requires --service and --user")
    args = ap.parse_args()

    if args.names_file:
        with open(args.names_file, encoding="utf-8") as fh:
            names = [ln.strip() for ln in fh if ln.strip()]
        dservice = duser = None
    else:
        if not os.path.isfile(args.diag):
            print(f"diag not found: {args.diag}", file=sys.stderr)
            return 2
        names, dservice, duser = read_failures(args.diag)
    service = (args.service or dservice or "").lower()
    user_id = args.user or duser
    if not names:
        print("No HTTP 404 (dl_end_fail) records found in the diag.")
        return 0
    if not service or not user_id:
        print("Could not infer creator (service/user) from the diag; "
              "pass --service and --user.", file=sys.stderr)
        return 2

    print(f"Creator: {service}/user/{user_id}")
    print(f"Failed (404) filenames logged: {len(names)}")
    print(f"Crawling live API to rebuild URLs...", file=sys.stderr)

    session = make_session(cookies_path=args.cookies)
    index = build_media_index(session, service, user_id)

    # Match, then de-dup distinct sources by resolved media path.
    resolved = {}     # path -> {media, builts:set}
    unmatched = []
    for built in names:
        matches, date_str, cands = match(built, index)
        if not matches:
            unmatched.append((built, date_str))
            continue
        for m in matches:
            slot = resolved.setdefault(m["path"], {"media": m, "builts": set()})
            slot["builts"].add(built)

    print(f"\nDistinct source files resolved: {len(resolved)}"
          + (f"   |   unmatched: {len(unmatched)}" if unmatched else ""))
    print("=" * 78)

    for i, (path, slot) in enumerate(sorted(resolved.items()), 1):
        m = slot["media"]
        status = None
        if not args.no_probe:
            status = probe(session, m["url"])
        slot["status"] = status
        if status is None and args.no_probe:
            verdict = "(not probed)"
        elif status is None:
            verdict = "PROBE FAILED"
        elif status in (200, 206):
            verdict = "RECOVERABLE (live)"
        elif status in (403, 404, 410):
            verdict = f"GONE (HTTP {status}) — use post page to grab manually"
        else:
            verdict = f"HTTP {status}"
        print(f"\n[{i}] {m['name']}   [{verdict}]")
        print(f"    logged as : {', '.join(sorted(slot['builts']))}")
        print(f"    file url  : {m['url']}")
        print(f"    post page : {m['post_url']}")

    if unmatched:
        print("\n" + "-" * 78)
        print("Unmatched (not found in the live listing — may be deleted from the "
              "post, or the name changed):")
        for built, date_str in unmatched:
            print(f"  - {built}")
            if date_str and date_str in index:
                avail = ", ".join(sorted(mm["name"] for mm in index[date_str]))
                print(f"      media on {date_str}: {avail or '(none)'}")
            else:
                print(f"      no posts indexed on {date_str}")

    if args.seed:
        seed_store(args, service, user_id, resolved)

    print("\nDone.")
    return 0


def seed_store(args, service, user_id, resolved):
    """Write the discovered failures into the creator's real FailureStore so the
    'Redownload Errors' button appears immediately. Live URLs seed as 'failed'
    (retryable); confirmed 403/404/410 seed as 'gone' (manual grab)."""
    archive_dir = args.archive_dir
    if archive_dir is None:
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                archive_dir = (json.load(fh).get("archive_dir") or "").strip()
        except Exception:
            archive_dir = ""
    link = {"platform": "pawchive", "service": service, "user_id": user_id}
    apath = pawchive_archive_path(archive_dir, link, "")
    edb = errors_db_path(apath)
    print(f"\nSeeding failure store: {edb}")
    store = FailureStore(edb)
    n_failed = n_gone = 0
    for slot in resolved.values():
        m = slot["media"]
        status = slot.get("status")
        state = GONE if status in (403, 404, 410) else FAILED
        entry = entry_key(m["post_id"], m["index"])
        filename = sorted(slot["builts"])[0] if slot["builts"] else m["name"]
        store.record_failure(
            entry, platform="pawchive", service=service, user_id=user_id,
            post_id=m["post_id"], filename=filename, url=m["url"],
            page_url=m["post_url"], media_kind=m.get("kind"), status=status,
            reason="http_gone")
        if state == GONE:
            store.mark_gone(entry, page_url=m["post_url"], status=status,
                            url=m["url"])
            n_gone += 1
        else:
            n_failed += 1
    store.close()
    print(f"  seeded {n_failed} retryable + {n_gone} gone "
          f"({n_failed + n_gone} total). Select the creator and use "
          f"'Redownload Errors'.")


if __name__ == "__main__":
    raise SystemExit(main())
