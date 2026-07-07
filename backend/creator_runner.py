"""Creator-centric download orchestrator.

A creator owns one destination folder and several *links* across platforms
(coomerfans OnlyFans/Fansly accounts, an X/Twitter account, ...). This module
runs a download across a chosen *scope* of those links by driving the existing
per-platform engines one link at a time:

    coomerfans -> CoomerfansRunner   (writes <dest>/<year>/ + <dest>/Images/<year>/)
    pawchive   -> PawchiveRunner     (shares the coomerfans root layout; also writes
                                      a stateful <dest>/_pawchive_links manifest)
    twitter    -> GalleryDlRunner    (writes <dest>/Twitter/<year>/ + .../Images/<year>/)

Links run sequentially (not concurrently) so the single output log reads
cleanly and we don't hammer a site from two angles at once; within one
coomerfans link the runner still uses its own thread pool. Every progress event
is prefixed with the link's label (e.g. "[OF queen_egirl27]") so the unified log
shows provenance, and per-link counts are summed into one aggregate total.

On-disk layout and per-link archive DBs are unchanged from the old per-tab
flow — this only coordinates the existing engines.
"""

import os
import threading

from backend.coomerfans_runner import CoomerfansRunner
from backend.coomerfans_scraper import site_code
from backend.pawchive_runner import PawchiveRunner
from backend.pawchive_scraper import site_code as pw_site_code
from backend.gallery_dl_runner import GalleryDlRunner
from backend.derpibooru_runner import DerpibooruRunner
from backend.derpibooru_scraper import (
    query_label as db_query_label, query_slug as db_query_slug,
)
from backend.discord_runner import DiscordRunner
from backend.config_builder import (
    build_config, build_latest_config, build_redownload_config, cleanup_config,
)
from backend.file_scanner import scan_destination
from backend.archive_sync import (
    sync_archive, sync_archive_for_year, reorganize_twitter_media,
)


# ── scope → link filtering ──────────────────────────────────────────

def filter_links(links, scope):
    """Return the creator links matching a fetch scope.

    scope ∈ {everything, twitter, coomerfans, onlyfans, fansly, pawchive,
    patreon, fanbox, derpibooru} or a single-link selector "link:<url>" that
    targets exactly one link.
    """
    links = links or []
    if scope == "everything":
        return list(links)
    if scope == "twitter":
        return [l for l in links if l.get("platform") == "twitter"]
    if scope == "coomerfans":
        return [l for l in links if l.get("platform") == "coomerfans"]
    if scope in ("onlyfans", "fansly"):
        return [l for l in links
                if l.get("platform") == "coomerfans" and l.get("service") == scope]
    if scope == "pawchive":
        return [l for l in links if l.get("platform") == "pawchive"]
    if scope in ("patreon", "fanbox"):
        return [l for l in links
                if l.get("platform") == "pawchive" and l.get("service") == scope]
    if scope == "derpibooru":
        return [l for l in links if l.get("platform") == "derpibooru"]
    if scope == "discord":
        return [l for l in links if l.get("platform") == "discord"]
    if scope and scope.startswith("link:"):
        target = scope[len("link:"):]
        return [l for l in links if (l.get("url") or "") == target]
    return []


def link_label(link):
    """Short human label for a link, used to prefix its log lines."""
    if link.get("platform") == "twitter":
        return f"Twitter @{link.get('username') or '?'}"
    if link.get("platform") == "derpibooru":
        return f"Derpibooru {link.get('name') or db_query_label(link.get('query'))}"
    if link.get("platform") == "discord":
        return f"Discord {link.get('name') or link.get('channel_id') or '?'}"
    name = link.get("name") or link.get("user_id") or "?"
    if link.get("platform") == "pawchive":
        return f"{pw_site_code(link.get('service'))} {name}"
    return f"{site_code(link.get('service'))} {name}"


def cf_archive_path(archive_dir, link, destination):
    """Per-link coomerfans archive DB (filename mirrors api._cf_resolve_archive_path)."""
    archive_dir = (archive_dir or "").strip()
    if archive_dir and os.path.isdir(archive_dir):
        return os.path.join(
            archive_dir, f"coomerfans_{link['service']}_{link['user_id']}.db")
    return os.path.join(destination, ".coomerfans-archive.db")


def pawchive_archive_path(archive_dir, link, destination):
    """Per-link pawchive archive DB (mirrors cf_archive_path's scheme)."""
    archive_dir = (archive_dir or "").strip()
    if archive_dir and os.path.isdir(archive_dir):
        return os.path.join(
            archive_dir, f"pawchive_{link['service']}_{link['user_id']}.db")
    return os.path.join(destination, ".pawchive-archive.db")


def derpibooru_archive_path(archive_dir, link, destination):
    """Per-link derpibooru archive DB, keyed by the search query slug (derpibooru
    is query-based, not account-based). Mirrors pawchive_archive_path's scheme."""
    archive_dir = (archive_dir or "").strip()
    slug = db_query_slug(link.get("query"))
    if archive_dir and os.path.isdir(archive_dir):
        return os.path.join(archive_dir, f"derpibooru_{slug}.db")
    return os.path.join(destination, f".derpibooru-{slug}-archive.db")


def discord_archive_path(archive_dir, link, destination):
    """Per-link discord archive DB, keyed by channel id (discord is channel-based).
    Mirrors cf_archive_path's scheme."""
    archive_dir = (archive_dir or "").strip()
    cid = link.get("channel_id") or "unknown"
    if archive_dir and os.path.isdir(archive_dir):
        return os.path.join(archive_dir, f"discord_{cid}.db")
    return os.path.join(destination, f".discord-{cid}-archive.db")


def twitter_archive_path(archive_dir, username, twitter_dest):
    """Per-link twitter archive DB (filename mirrors api._resolve_archive_path)."""
    archive_dir = (archive_dir or "").strip()
    if archive_dir and os.path.isdir(archive_dir):
        return os.path.join(archive_dir, f"twitter_{username or 'unknown'}.db")
    return os.path.join(twitter_dest, ".gallery-dl-archive.db")


def errors_db_path(archive_path):
    """Per-link failure store, a sibling of that link's archive DB (so it lives
    wherever the archive lives — the archive_dir when configured, else the
    destination). '<stem>.db' -> '<stem>.errors.db'."""
    root, ext = os.path.splitext(archive_path)
    return f"{root}.errors{ext or '.db'}"


class CreatorRunner:
    """Runs a batch of links for one creator. Mirrors the per-engine public
    contract (run/cancel/is_running + downloaded/skipped/error counters)."""

    def __init__(self, workers=5, pawchive_workers=None, pawchive_extract=True):
        self.workers = workers
        # pawchive has its own (lower) concurrency knob because its CDN is behind
        # DDoS-Guard, which rate-limits on connection count; coomerfans tolerates the
        # full `workers`. Falls back to `workers` when unset.
        self.pawchive_workers = pawchive_workers
        # Auto-extract downloaded archives (zip/rar) into the library.
        self.pawchive_extract = pawchive_extract
        self.downloaded_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self._running = False
        self._cancel = threading.Event()
        self._current = None   # the active sub-runner, so cancel() can reach it
        self._derpibooru_api_key = None
        self._derpibooru_filter_id = None
        self._discord_token = None
        self._discord_token_type = "user"

    @property
    def is_running(self):
        return self._running

    def cancel(self):
        self._cancel.set()
        cur = self._current
        if cur is not None:
            try:
                cur.cancel()
            except Exception:
                pass

    def request_skip(self, entry):
        """Forward a per-file skip request to the active sub-runner (pawchive)."""
        cur = self._current
        if cur is not None and hasattr(cur, "request_skip"):
            try:
                cur.request_skip(entry)
            except Exception:
                pass

    # ── batch entry point ────────────────────────────────────────
    def run(self, creator, scope, mode, year, archive_dir,
            cookies_path, cookies_browser, on_progress, on_complete, on_error,
            derpibooru_api_key=None, derpibooru_filter_id=None,
            discord_token=None, discord_token_type=None):
        self._derpibooru_api_key = derpibooru_api_key or None
        self._derpibooru_filter_id = derpibooru_filter_id or None
        self._discord_token = discord_token or None
        self._discord_token_type = discord_token_type or "user"
        self._running = True
        self._cancel.clear()
        self.downloaded_count = self.skipped_count = self.error_count = 0
        try:
            links = filter_links(creator.get("links", []), scope)
            if not links:
                on_progress({"type": "info",
                             "message": f"No links match scope '{scope}' for this creator."})
                on_complete(self._stats())
                return

            verb = {"full": "Download Everything",
                    "latest": "Fetch Latest",
                    "redownload_year": f"Download {year}",
                    "errors": "Redownload Errors"}.get(mode, mode)
            on_progress({"type": "info",
                         "message": f"{verb} — scope '{scope}', {len(links)} link(s)."})

            for link in links:
                if self._cancel.is_set():
                    break
                on_progress({"type": "info", "message": f"──── {link_label(link)} ────"})
                try:
                    if link.get("platform") == "coomerfans":
                        self._run_coomerfans(link, creator, mode, year, archive_dir,
                                             on_progress, on_error)
                    elif link.get("platform") == "pawchive":
                        self._run_pawchive(link, creator, mode, year, archive_dir,
                                           on_progress, on_error)
                    elif link.get("platform") == "twitter":
                        self._run_twitter(link, creator, mode, year, archive_dir,
                                          cookies_path, cookies_browser, on_progress, on_error)
                    elif link.get("platform") == "derpibooru":
                        self._run_derpibooru(link, creator, mode, year, archive_dir,
                                             on_progress, on_error)
                    elif link.get("platform") == "discord":
                        self._run_discord(link, creator, mode, year, archive_dir,
                                          on_progress, on_error)
                    else:
                        on_progress({"type": "info",
                                     "message": f"Unknown platform '{link.get('platform')}', skipped."})
                except Exception as e:
                    self.error_count += 1
                    on_error({"type": "error",
                              "message": f"[{link_label(link)}] failed: {e}"})

            on_complete(self._stats())
        except Exception as e:
            on_error({"type": "error", "message": f"Fatal: {e}"})
            on_complete(self._stats())
        finally:
            self._current = None
            self._running = False

    # ── per-platform runs ────────────────────────────────────────
    def _run_coomerfans(self, link, creator, mode, year, archive_dir, on_progress, on_error):
        # Coomerfans has no targeted error-recovery path (its signed URLs must be
        # re-parsed from the post anyway), so "Redownload Errors" is a full run: it
        # re-attempts every missing/errored file through the normal download path,
        # which records still-gone files and clears any that come back.
        if mode == "errors":
            mode = "full"
        destination = creator["destination"]
        archive_path = cf_archive_path(archive_dir, link, destination)
        errors_path = errors_db_path(archive_path)
        prog = self._prefix(link, on_progress)
        err = self._prefix(link, on_error)

        # Respect the curation workflow: 'latest' is a top-up over a prior
        # download. With no archive yet there's nothing to top up, and treating
        # it as a full grab would defeat the point — skip and tell the user.
        if mode == "latest" and not os.path.isfile(archive_path):
            prog({"type": "info",
                  "message": "no prior download — skipping latest (run Download Everything first)"})
            return

        runner = CoomerfansRunner(workers=self.workers)
        self._current = runner
        stats = {}
        runner.run(
            creator_url=link["url"],
            destination=destination,
            mode=mode,
            archive_path=archive_path,
            on_progress=prog,
            on_complete=lambda d: stats.update(d),
            on_error=err,
            year=year,
            errors_path=errors_path,
        )
        self._accumulate(stats)

    def _run_pawchive(self, link, creator, mode, year, archive_dir, on_progress, on_error):
        destination = creator["destination"]
        archive_path = pawchive_archive_path(archive_dir, link, destination)
        errors_path = errors_db_path(archive_path)
        # The stateful external-links manifest lives at the creator root (no
        # pawchive subfolder — media shares the coomerfans root layout).
        links_path = os.path.join(destination, "_pawchive_links.json")
        prog = self._prefix(link, on_progress)
        err = self._prefix(link, on_error)

        # Same curation rule as coomerfans: 'latest' is a top-up over a prior
        # download; with no archive there's nothing to top up.
        if mode == "latest" and not os.path.isfile(archive_path):
            prog({"type": "info",
                  "message": "no prior download — skipping latest (run Download Everything first)"})
            return

        # The crawl (API) is sequential and self-paced by the runner's adaptive
        # API throttle, so `workers` only sets file-download concurrency against the
        # DDoS-Guarded CDN — use the pawchive-specific (lower) knob when set.
        runner = PawchiveRunner(workers=self.pawchive_workers or self.workers,
                                extract=self.pawchive_extract)
        self._current = runner
        stats = {}
        runner.run(
            creator_url=link["url"],
            destination=destination,
            mode=mode,
            archive_path=archive_path,
            links_path=links_path,
            on_progress=prog,
            on_complete=lambda d: stats.update(d),
            on_error=err,
            year=year,
            errors_path=errors_path,
        )
        self._accumulate(stats)

    def _run_derpibooru(self, link, creator, mode, year, archive_dir, on_progress, on_error):
        # derpibooru shares the coomerfans root layout (<dest>/<year>/ +
        # <dest>/Images/<year>/); no site subfolder. It has no failure store, so
        # "Redownload Errors" is just a full re-scan that re-fetches anything missing.
        if mode == "errors":
            mode = "full"
        destination = creator["destination"]
        archive_path = derpibooru_archive_path(archive_dir, link, destination)
        prog = self._prefix(link, on_progress)
        err = self._prefix(link, on_error)

        # Same curation rule as the others: 'latest' is a top-up over a prior
        # download; with no archive there's nothing to top up.
        if mode == "latest" and not os.path.isfile(archive_path):
            prog({"type": "info",
                  "message": "no prior download — skipping latest (run Download Everything first)"})
            return

        runner = DerpibooruRunner(workers=self.workers)
        self._current = runner
        stats = {}
        runner.run(
            creator_url=link["url"],
            destination=destination,
            mode=mode,
            archive_path=archive_path,
            on_progress=prog,
            on_complete=lambda d: stats.update(d),
            on_error=err,
            api_key=self._derpibooru_api_key,
            filter_id=self._derpibooru_filter_id,
            year=year,
        )
        self._accumulate(stats)

    def _run_discord(self, link, creator, mode, year, archive_dir, on_progress, on_error):
        # Discord shares the coomerfans root layout (<dest>/<year>/ +
        # <dest>/Images/<year>/); no site subfolder. It has no targeted
        # error-recovery path (a failed signed CDN URL must be re-fetched from the
        # message anyway), so "Redownload Errors" is a full re-crawl: it re-fetches
        # fresh URLs and retries anything missing, re-recording what still fails.
        if mode == "errors":
            mode = "full"
        destination = creator["destination"]
        archive_path = discord_archive_path(archive_dir, link, destination)
        errors_path = errors_db_path(archive_path)
        # The stateful external-links manifest lives at the creator root (shared
        # layout, no subfolder) — a separate file from pawchive's.
        links_path = os.path.join(destination, "_discord_links.json")
        prog = self._prefix(link, on_progress)
        err = self._prefix(link, on_error)

        # Same curation rule as the others: 'latest' is a top-up over a prior
        # download; with no archive there's nothing to top up.
        if mode == "latest" and not os.path.isfile(archive_path):
            prog({"type": "info",
                  "message": "no prior download — skipping latest (run Download Everything first)"})
            return

        runner = DiscordRunner(workers=self.workers)
        self._current = runner
        stats = {}
        runner.run(
            creator_url=link["url"],
            destination=destination,
            mode=mode,
            archive_path=archive_path,
            on_progress=prog,
            on_complete=lambda d: stats.update(d),
            on_error=err,
            token=self._discord_token,
            token_type=self._discord_token_type,
            year=year,
            errors_path=errors_path,
            links_path=links_path,
            # Per-link filename tag: name files "<date> - Patreon - …" etc. so a
            # channel that mirrors a Patreon/Fanbox blends with the creator's files.
            site_label=link.get("tag"),
        )
        self._accumulate(stats)

    def _run_twitter(self, link, creator, mode, year, archive_dir,
                     cookies_path, cookies_browser, on_progress, on_error):
        # Twitter content nests under <creator>/Twitter/ (images in Twitter/Images/).
        twitter_dest = os.path.join(creator["destination"], "Twitter")
        username = link.get("username")
        archive_path = twitter_archive_path(archive_dir, username, twitter_dest)
        errors_path = errors_db_path(archive_path)
        cp = cookies_path if not cookies_browser else None
        cb = cookies_browser or None
        prog = self._prefix(link, on_progress)
        err = self._prefix(link, on_error)

        if mode == "latest":
            scan = scan_destination(twitter_dest)
            if scan["latest_year"] is None:
                prog({"type": "info",
                      "message": "no existing media — skipping latest (run Download Everything first)"})
                return
            config_path = build_latest_config(twitter_dest, cp, cb, scan["latest_year"])
            use_archive, sync_kind = True, "full"
        elif mode == "redownload_year":
            os.makedirs(twitter_dest, exist_ok=True)
            config_path = build_redownload_config(twitter_dest, year, cp, cb)
            # Use the archive so 'Download Year' skips tweets already downloaded by
            # tweet id (folder-agnostic) — the file-existence skip would miss images
            # that now live in Twitter/Images/<year>/ and re-download duplicates.
            use_archive, sync_kind = True, "redownload_year"
        else:  # full
            os.makedirs(twitter_dest, exist_ok=True)
            config_path = build_config(twitter_dest, cp, cb)
            use_archive, sync_kind = True, "full"

        runner = GalleryDlRunner()
        self._current = runner
        stats = {}
        try:
            runner.run(
                url=link["url"],
                config_path=config_path,
                archive_path=archive_path if use_archive else None,
                on_progress=prog,
                on_complete=lambda d: stats.update(d),
                on_error=err,
                errors_path=errors_path,
                # full/errors re-attempt everything, so wipe stale failures first and
                # let the run re-record only what still breaks.
                reset_errors=mode in ("full", "errors"),
            )
        finally:
            cleanup_config(config_path)

        if not stats.get("cancelled"):
            # gallery-dl dumps everything flat into <year>; split images out into
            # Images/<year> to match the coomerfans/pawchive layout. Do this before
            # the archive sync, which scans both locations.
            try:
                moved = reorganize_twitter_media(twitter_dest)
                if moved:
                    prog({"type": "info",
                          "message": f"organized {moved} image(s) into Images/<year>"})
            except Exception as e:
                prog({"type": "info", "message": f"image organize note: {e}"})
            try:
                if sync_kind == "redownload_year":
                    sync_archive_for_year(archive_path, twitter_dest, year)
                else:
                    sync_archive(archive_path, twitter_dest)
            except Exception as e:
                prog({"type": "info", "message": f"archive sync note: {e}"})
        self._accumulate(stats)

    # ── helpers ──────────────────────────────────────────────────
    def _prefix(self, link, cb):
        """Wrap a callback so each message is tagged with the link label."""
        label = link_label(link)
        def wrapped(data):
            d = dict(data)
            if d.get("message"):
                d["message"] = f"[{label}] {d['message']}"
            cb(d)
        return wrapped

    def _accumulate(self, stats):
        self.downloaded_count += stats.get("downloaded", 0) or 0
        self.skipped_count += stats.get("skipped", 0) or 0
        self.error_count += stats.get("errors", 0) or 0

    def _stats(self):
        return {
            "downloaded": self.downloaded_count,
            "skipped": self.skipped_count,
            "errors": self.error_count,
            "cancelled": self._cancel.is_set(),
        }
