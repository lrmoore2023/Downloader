"""Orchestrator for the Albums tab.

Takes a destination directory and a list of pasted links, downloads each whole
album into its own titled subfolder, and reports unified progress. Each link is
routed to its best engine (see `album_sites`):

  * bunkr / cyberdrop -> CyberdropDlRunner, with GalleryDlRunner as a fallback if
    cyberdrop-dl produces nothing.
  * filester          -> GalleryDlRunner.

Mirrors CreatorRunner: links run sequentially so the unified log stays readable,
the active sub-runner is held in `self._current` so `cancel()` reaches its
subprocess, and per-file failures land in one FailureStore keyed off the
destination (surfaced in the tab's Retry-failed panel).

All engine bookkeeping (cyberdrop-dl's isolated state, gallery-dl's download
archive, the shared errors DB) lives under an app-managed state dir keyed by the
destination path, so the user's album folders stay clean (media only).
"""

import hashlib
import os
import threading

from backend import album_sites
from backend.config_builder import build_album_config, cleanup_config
from backend.cyberdrop_dl_runner import CyberdropDlRunner
from backend.download_errors import FailureStore
from backend.filester_downloader import FilesterRunner
from backend.gallery_dl_runner import GalleryDlRunner


def album_state_dir(app_root, destination):
    """Per-destination state dir (engine dbs, logs) under the app, not the album."""
    key = hashlib.sha1(os.path.abspath(destination).encode("utf-8")).hexdigest()[:12]
    return os.path.join(app_root, ".album_state", key)


def album_errors_db(state_dir):
    return os.path.join(state_dir, "album_errors.db")


def album_archive_db(state_dir):
    return os.path.join(state_dir, "gallery_archive.db")


class AlbumRunner:
    def __init__(self, app_root, cookies_path=None, cookies_browser=None):
        self._app_root = app_root
        self._cookies_path = cookies_path
        self._cookies_browser = cookies_browser
        self._cancel_event = threading.Event()
        self._current = None
        self._running = False
        self.downloaded_count = 0
        self.skipped_count = 0
        self.error_count = 0

    @property
    def is_running(self):
        return self._running

    def cancel(self):
        self._cancel_event.set()
        cur = self._current
        if cur is not None:
            try:
                cur.cancel()
            except Exception:
                pass

    def run(self, destination, links, on_progress, on_complete, on_error,
            retry=False, force_urls=None):
        """Download each link's album. `force_urls` is the subset to re-download
        whole (ignore download history); everything else runs incrementally
        (skip what the engine's persistent history already has = "scan for new").
        The final summary carries `results` — one per link, for the ledger."""
        self._running = True
        self._cancel_event.clear()
        self.downloaded_count = self.skipped_count = self.error_count = 0
        force_set = {self._norm(u) for u in (force_urls or [])}

        state_dir = album_state_dir(self._app_root, destination)
        os.makedirs(state_dir, exist_ok=True)
        errors_path = album_errors_db(state_dir)

        # Normalise the pasted links: (url, password), dropping blanks.
        entries = []
        for raw in links or []:
            url, pwd = album_sites.parse_entry(raw)
            if url:
                entries.append((url, pwd))

        total = len(entries)
        if not total:
            self._running = False
            on_complete(self._summary([], []))
            return

        unsupported, results = [], []
        try:
            for idx, (url, password) in enumerate(entries, start=1):
                if self._cancel_event.is_set():
                    break
                site = album_sites.detect_site(url)
                label = f"[{idx}/{total}] {(site or {}).get('key', 'link')}"
                prog = self._prefixed(on_progress, label)
                err = self._prefixed(on_error, label)

                if site is None:
                    unsupported.append(url)
                    err({"type": "error", "message": f"Unsupported link (no known site): {url}"})
                    continue

                prog({"type": "info", "message": f"Starting {url}"})
                force = self._norm(url) in force_set
                results.append(self._run_link(
                    url, password, site, destination, state_dir, errors_path,
                    prog, err, retry, force))
        finally:
            self._current = None
            self._running = False

        summary = self._summary(unsupported, results)
        summary["cancelled"] = self._cancel_event.is_set()
        on_complete(summary)

    def _run_link(self, url, password, site, destination, state_dir, errors_path,
                  on_progress, on_error, retry, force):
        # Clear THIS album's prior failures so a re-run re-records only what still
        # fails (bunkr's transient 429s otherwise compound across runs).
        try:
            store = FailureStore(errors_path)
            store.clear_by_page_url(url)
            store.close()
        except Exception:
            pass

        # Sniff downloaded file paths so we can report the album's subfolder/title.
        paths = []

        def prog(data):
            if data.get("type") == "download" and data.get("path"):
                paths.append(data["path"])
            on_progress(data)

        agg = {"downloaded": 0, "skipped": 0, "errors": 0}
        subfolder_hint = None
        if site["engine"] == "filester":
            stats = self._run_filester(url, destination, state_dir, errors_path,
                                       prog, on_error, password)
            self._merge(agg, stats)
            subfolder_hint = stats.get("subfolder")
        elif site["engine"] == "cyberdrop-dl":
            stats = self._run_cyberdrop(url, destination, state_dir, errors_path,
                                        prog, on_error, password, force,
                                        site.get("tuning"))
            self._merge(agg, stats)
            subfolder_hint = stats.get("subfolder")
            # Fallback: cyberdrop-dl resolved nothing at all (not merely "all
            # skipped") and we weren't cancelled -> let gallery-dl try. But NOT on a
            # scrape error (bunkr 502/etc.) — that's a bunkr-side outage gallery-dl
            # would just hit too, so surface it instead of doubling the noise.
            if (not self._cancel_event.is_set()
                    and not stats.get("scrape_error")
                    and stats.get("downloaded", 0) == 0
                    and stats.get("skipped", 0) == 0):
                prog({"type": "info", "message": "cyberdrop-dl found nothing — trying gallery-dl"})
                self._merge(agg, self._run_gallery(
                    url, destination, state_dir, errors_path, prog, on_error, retry, password, force))
        else:
            self._merge(agg, self._run_gallery(
                url, destination, state_dir, errors_path, prog, on_error, retry, password, force))

        # cyberdrop-dl reports its subfolder directly; gallery-dl download events
        # carry a local path, so sniff it from there.
        subfolder = subfolder_hint or self._subfolder_of(paths, destination)
        return {
            "url": url, "site": site["key"],
            "downloaded": agg["downloaded"], "skipped": agg["skipped"],
            "errors": agg["errors"], "subfolder": subfolder,
            "title": self._title_of(subfolder),
        }

    # ── engine dispatch ─────────────────────────────────────────────────
    def _run_filester(self, url, destination, state_dir, errors_path,
                      on_progress, on_error, password):
        runner = FilesterRunner(platform="album")
        self._current = runner
        stats = {}
        runner.run(
            url, destination, on_progress,
            on_complete=lambda s: stats.update(s),
            on_error=on_error,
            password=password,
            errors_path=errors_path,
        )
        self._accumulate(stats)
        return stats

    def _run_cyberdrop(self, url, destination, state_dir, errors_path,
                       on_progress, on_error, password, force, tuning=None):
        runner = CyberdropDlRunner(platform="album")
        self._current = runner
        stats = {}
        runner.run(
            url, destination, on_progress,
            on_complete=lambda s: stats.update(s),
            on_error=on_error,
            errors_path=errors_path,
            state_dir=os.path.join(state_dir, "cyberdrop"),
            ignore_history=force,
            tuning=tuning,
            # NB: do NOT pass cookies to bunkr/cyberdrop — measured a bunkr session
            # cookie (bnState) throttling downloads to ~0.4x speed vs anonymous.
        )
        self._accumulate(stats)
        return stats

    def _run_gallery(self, url, destination, state_dir, errors_path,
                     on_progress, on_error, retry, password, force):
        # A password only applies to a locked filester folder. Unlock it here and
        # hand gallery-dl the resulting session cookie (unlock_to_cookies returns
        # None for an open folder / wrong password, so gallery-dl just runs plain).
        filester_cookies = None
        if password:
            on_progress({"type": "info", "message": "Unlocking password-protected folder…"})
            filester_cookies = unlock_to_cookies(url, password)
            if filester_cookies is None:
                on_progress({"type": "info", "message": "Unlock failed or not needed — trying without a password"})

        config_path = build_album_config(
            destination,
            cookies_path=self._cookies_path,
            cookies_browser=self._cookies_browser,
            filester_cookies=filester_cookies,
        )
        try:
            runner = GalleryDlRunner(platform="album")
            self._current = runner
            stats = {}
            # Force = whole re-download: drop the archive so nothing is skipped by
            # prior-run history (the user deleted the files, so file-existence
            # skip won't fire either). Otherwise keep the archive for scan-for-new.
            archive = None if force else album_archive_db(state_dir)
            runner.run(
                url, config_path, archive,
                on_progress,
                on_complete=lambda s: stats.update(s),
                on_error=on_error,
                errors_path=errors_path,
                reset_errors=retry,
            )
            self._accumulate(stats)
            return stats
        finally:
            cleanup_config(config_path)
            if filester_cookies:
                try:
                    os.remove(filester_cookies)
                except OSError:
                    pass
            if filester_cookies:
                try:
                    os.remove(filester_cookies)
                except OSError:
                    pass

    # ── helpers ─────────────────────────────────────────────────────────
    def _accumulate(self, stats):
        self.downloaded_count += stats.get("downloaded", 0) or 0
        self.skipped_count += stats.get("skipped", 0) or 0
        self.error_count += stats.get("errors", 0) or 0

    @staticmethod
    def _merge(agg, stats):
        agg["downloaded"] += stats.get("downloaded", 0) or 0
        agg["skipped"] += stats.get("skipped", 0) or 0
        agg["errors"] += stats.get("errors", 0) or 0

    @staticmethod
    def _norm(url):
        """Normalised album URL for matching (host+path, lowercased, no trailing /)."""
        from urllib.parse import urlparse
        p = urlparse((url or "").strip())
        host = (p.hostname or "").lower()
        path = (p.path or "").rstrip("/")
        return f"{host}{path}" if host else (url or "").strip().lower()

    @staticmethod
    def _subfolder_of(paths, destination):
        """The album's own subfolder name (first path segment under destination)."""
        dest = os.path.abspath(destination)
        for p in paths:
            try:
                rel = os.path.relpath(os.path.abspath(p), dest)
            except (ValueError, OSError):
                continue
            parts = rel.replace("\\", "/").split("/")
            if len(parts) >= 2 and parts[0] not in ("", ".", ".."):
                return parts[0]
        return None

    @staticmethod
    def _title_of(subfolder):
        """Album title = subfolder minus a trailing " (Bunkr)" / " (id)" suffix."""
        if not subfolder:
            return None
        import re
        return re.sub(r"\s*\([^)]*\)\s*$", "", subfolder).strip() or subfolder

    @staticmethod
    def _prefixed(cb, label):
        def wrapped(data):
            msg = data.get("message")
            if msg:
                data = dict(data)
                data["message"] = f"{label}: {msg}"
            cb(data)
        return wrapped

    def _summary(self, unsupported, results):
        return {
            "downloaded": self.downloaded_count,
            "skipped": self.skipped_count,
            "errors": self.error_count,
            "unsupported": unsupported,
            "results": results,
        }
