"""Download engine for the coomerfans tab.

Mirrors GalleryDlRunner's public contract (run/cancel/is_running + the
downloaded/skipped/error counters) so backend.api can treat both tabs the
same way, but instead of shelling out to gallery-dl it crawls and downloads
coomerfans.com directly with requests.

Resilience is the headline requirement (see plan): videos may sit in cold
storage and take *minutes* to send their first byte, and nothing may ever be
dropped. So downloads use a long read timeout (tolerating slow time-to-first-
byte), resume via HTTP Range on a .part file, refresh expired signed video
URLs, and retry effectively forever — only user cancellation stops a download.
A final reconciliation pass re-queues anything still missing.
"""

import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urlsplit

import requests

from backend.coomerfans_scraper import (
    make_session, parse_creator_url, iter_post_urls, parse_post, fetch_html,
    refresh_media_url, build_filename, target_path, site_code,
    norm_year_range, year_in_range, media_shard, swap_media_host, CDN_SHARD_ORDER,
    BotChallenge, bg_score, check_challenge,
    BG_WARN_SCORE, BG_TRIP_SCORE, BG_COOLOFF,
)
from backend.rate_limit import AdaptiveThrottle
from backend.coomerfans_archive import Archive, entry_key
from backend.download_errors import FailureStore

# HTTP statuses that mean "this (signed) URL is dead — re-parse the post for a
# fresh one" rather than "retry the same URL".
_EXPIRED_STATUSES = (401, 403, 404, 410)
_GONE_REFRESH_LIMIT = 4   # give up (flag error) after this many refresh-resistant gone responses
_VIDEO_RESTART_LIMIT = 10  # give up on a video whose connection keeps breaking (never resumes)

# Transient HTTP statuses for page/post *reads* (server hiccups, rate limits,
# Cloudflare 52x). coomerfans intermittently 500s on a valid post; a retry a
# few seconds later succeeds. These are retried (with backoff) rather than
# dropping the post. 404/410 are NOT here — a missing post page is permanent.
_TRANSIENT_READ_STATUSES = (408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524)

# coomerfans splits into two hosts with very different tolerances:
#   coomerfans.com        -> HTML pages, behind a scoring bot-guard (see
#                            coomerfans_scraper.check_challenge). Needs pacing.
#   img*.coomerfans.com   -> media bytes, no bot-guard headers at all.
# So page reads run through _read_pace on a small pool while file downloads keep
# the full `workers` fan-out; throttling the CDN would cost throughput for nothing.
_READ_WORKERS = 2          # concurrent HTML readers (the guard scores concurrency)
_READ_INTERVAL = 0.8       # starting seconds between HTML requests, pool-wide
_READ_FLOOR = 0.45         # fastest sustained pace (~2.2 req/s; 1 worker at full
                           # tilt measured ~1.3 req/s at a safe score of ~0.17)
_READ_CEIL = 20.0
_READ_EST_SECS = 1.8       # measured wall-clock per post read (160 posts / 284s)

# A shard can be entirely unreachable (img7 refused every TCP connect on
# 2026-09-12) while its siblings serve the same bytes. Without failover such a
# file retried forever, pinning a worker and never reaching the errors panel.
_CONNECT_ROTATE_AFTER = 2   # consecutive connect failures before trying a mirror


def expected_total(response, resume_pos):
    """Authoritative full file size from a response, or None if unknown.

    206 -> total in 'Content-Range: bytes a-b/total' (also '*/total' on 416);
    200 -> Content-Length is the full size;
    206 without Content-Range -> resume_pos + Content-Length (remaining).
    """
    cr = response.headers.get("Content-Range", "")
    m = re.search(r"/\s*(\d+)\s*$", cr)
    if m:
        return int(m.group(1))
    cl = response.headers.get("Content-Length")
    if cl and cl.strip().isdigit():
        cl = int(cl)
        return cl if response.status_code == 200 else resume_pos + cl
    return None


def content_range_start(response):
    """Start byte of a 206 'Content-Range: bytes START-END/TOTAL' header, or None
    if absent/unparseable. Used to confirm a resume actually continues from where
    our .part ends before we append to it."""
    m = re.search(r"bytes\s+(\d+)\s*-", response.headers.get("Content-Range", ""))
    return int(m.group(1)) if m else None


def url_is_expired(url, margin=120):
    """True if a signed URL's `e=<unix>` expiry is past (or within `margin` s)."""
    try:
        e = parse_qs(urlsplit(url).query).get("e", [None])[0]
        if e and e.isdigit():
            return time.time() > (int(e) - margin)
    except Exception:
        pass
    return False


def ffprobe_ok(path):
    """Validate a media file with ffprobe if available. Returns True when the
    file has a positive duration, or when ffprobe is unavailable/errors (so a
    missing ffprobe never blocks a download — the size check is the primary guard)."""
    exe = shutil.which("ffprobe")
    if not exe:
        return True
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=60,
        )
        dur = out.stdout.strip()
        return out.returncode == 0 and dur not in ("", "N/A") and float(dur) > 0
    except Exception:
        return True


class CoomerfansRunner:
    def __init__(self, workers=3, connect_timeout=30, read_timeout=600,
                 chunk_size=1 << 20):
        self.workers = max(1, int(workers))
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout          # max gap between bytes (cold-storage aware)
        self.chunk_size = chunk_size

        self.downloaded_count = 0
        self.skipped_count = 0
        self.error_count = 0

        self._running = False
        self._cancel_event = threading.Event()
        self._count_lock = threading.Lock()
        self._fname_lock = threading.Lock()
        self._claimed = set()                     # absolute paths claimed this run

        self._on_progress = None
        self._archive = None
        self._errors = None       # FailureStore (persistent per-link failure record)
        self._session = None
        self._read_workers = max(1, min(_READ_WORKERS, self.workers))
        self._read_pace = None      # AdaptiveThrottle over coomerfans.com HTML
        self._challenge_hits = 0
        self._dead_shards = set()   # CDN shards that refused to connect this run
        self._shard_lock = threading.Lock()
        self._destination = None
        self._service = None
        self._user_id = None
        self._mode = "full"
        # Per-creator fetch prefs (overwritten by run(); all-on by default).
        self._include_images = True
        self._include_videos = True
        self._tracked_posts = []
        # Per-file skip requests from the UI's active-downloads panel.
        self._skip_ids = set()
        self._skip_lock = threading.Lock()

    def request_skip(self, entry):
        """Abandon a single in-flight download without cancelling the run.

        Called via CreatorRunner.request_skip when the user clicks Skip on a row
        in the active-downloads panel. The download loops poll this wherever they
        poll cancel, so the worker aborts promptly and moves to the next file.
        """
        if not entry:
            return
        with self._skip_lock:
            self._skip_ids.add(entry)

    def _skip_requested(self, entry):
        with self._skip_lock:
            return entry in self._skip_ids

    def _dl_event(self, kind, entry, filename=None):
        """Push an active-download start/stop event, keyed by entry id, so the
        shared UI panel can list in-flight files with a per-file Skip button."""
        if not self._on_progress:
            return
        msg = {"type": kind, "id": entry}
        if filename is not None:
            msg["message"] = filename
        self._on_progress(msg)

    def _mark_skipped(self, job, entry, filename):
        """Record a user-skipped download so future runs never re-fetch it."""
        if self._archive:
            year = f"{job['dt']:%Y}" if job.get("dt") else "unknown"
            try:
                self._archive.set_skipped(entry, job["post_id"], filename,
                                          job["kind"], year)
            except Exception:
                pass
        self._bump("skip")
        self._info(f"Skipped (won't re-download): {filename}")

    def _kind_included(self, kind):
        """Whether the creator's fetch prefs include this media kind ('videos'
        covers every non-image kind)."""
        return self._include_images if kind == "image" else self._include_videos

    # ── public contract ──────────────────────────────────────
    @property
    def is_running(self):
        return self._running

    def cancel(self):
        self._cancel_event.set()

    def _cancelled(self):
        return self._cancel_event.is_set()

    def run(self, creator_url, destination, mode, archive_path,
            on_progress, on_complete, on_error, cookies_path=None, year=None,
            errors_path=None, year_range=None, include_images=True,
            include_videos=True):
        self.downloaded_count = self.skipped_count = self.error_count = 0
        self._cancel_event.clear()
        self._skip_ids = set()
        self._claimed.clear()
        # Per-creator what-to-fetch switches ('videos' = every non-image kind).
        self._include_images = bool(include_images)
        self._include_videos = bool(include_videos)
        self._tracked_posts = []   # (post_id, dt) whose media was fully excluded
        self._running = True
        self._on_progress = on_progress
        self._destination = destination
        self._mode = mode
        # Inclusive (lo, hi) year bounds, or None for "all years". Single-year
        # 'Download Year' collapses to (y, y); 'Fetch Latest' may carry a saved/one-off
        # range. See handle_post in _crawl_and_parse.
        self._year_range = norm_year_range(year, year_range)

        try:
            creator = parse_creator_url(creator_url)
            if not creator:
                on_error({"type": "error", "message": "Invalid creator URL"})
                on_complete(self._stats(cancelled=False))
                return
            self._service = creator["service"]
            self._user_id = creator.get("user_id")
            self._session = make_session(cookies_path=cookies_path)
            self._read_pace = AdaptiveThrottle(
                _READ_INTERVAL, floor=_READ_FLOOR, ceil=_READ_CEIL)
            self._challenge_hits = 0
            self._dead_shards = set()
            self._archive = Archive(archive_path) if archive_path else None
            self._errors = FailureStore(errors_path) if errors_path else None

            jobs = self._crawl_and_parse(creator_url)
            if self._cancelled():
                self._info("Cancelled during crawl.")
                self._finish(on_complete)
                return

            self._info(f"{len(jobs)} media item(s) to download "
                       f"({self.skipped_count} already present).")
            self._download_all(jobs)
            if not self._cancelled():
                self._reconcile(jobs)

            # Mark fully-excluded posts 'seen' so the next 'latest' skips them.
            # Only after an UNcancelled run — an interrupted one must stay
            # revisitable. Marker entry keys never collide with real media
            # entries, so re-enabling a kind + 'Download Everything' still works.
            if self._tracked_posts and self._archive and not self._cancelled():
                for pid, pdt in self._tracked_posts:
                    yr = f"{pdt:%Y}" if pdt else "unknown"
                    self._archive.record(f"coomerfans_{pid}_tracked", pid, None,
                                         "tracked", yr)
                self._info(f"{len(self._tracked_posts)} post(s) tracked without "
                           "downloading (excluded by this creator's fetch settings).")

            self._finish(on_complete)
        except Exception as e:
            on_error({"type": "error", "message": f"Fatal: {e}"})
            self._finish(on_complete)
        finally:
            if self._archive:
                self._archive.close()
            if self._errors:
                self._errors.close()
            self._running = False

    # ── crawl + parse ────────────────────────────────────────
    def _crawl_and_parse(self, creator_url):
        self._info("Crawling creator pages...")

        def on_page(page, n):
            self._info(f"Page {page}: {n} post(s)")

        post_urls = list(iter_post_urls(
            self._session, creator_url, on_page=on_page,
            should_cancel=self._cancelled, fetch=self._fetch_page,
        ))
        if self._cancelled():
            return []
        # Reads are deliberately paced (coomerfans' bot-guard scores sustained
        # activity), so this phase runs for minutes with no downloads yet. Say so
        # and report progress throughout — silence here reads as a hang.
        est = max(1, round(len(post_urls) * _READ_EST_SECS / 60))
        self._info(f"Found {len(post_urls)} post(s). Reading posts "
                   f"(paced to stay under coomerfans' bot-check, ~{est} min)...")

        jobs = []
        jobs_lock = threading.Lock()
        failed = []
        failed_lock = threading.Lock()

        read_done = [0]
        read_total = [len(post_urls)]
        last_report = [time.monotonic()]
        report_lock = threading.Lock()

        def note_read():
            """Emit a heartbeat every 10 posts or 15s, whichever comes first."""
            with report_lock:
                read_done[0] += 1
                n, tot = read_done[0], read_total[0]
                now = time.monotonic()
                if not (n % 10 == 0 or n == tot or now - last_report[0] >= 15):
                    return
                last_report[0] = now
            pace = self._read_pace.interval if self._read_pace else 0.0
            self._info(f"Read {n}/{tot} post(s) - {len(jobs)} media found "
                       f"({pace:.1f}s/read)")

        def read_one(post_url, attempts):
            if self._cancelled():
                return
            parts = post_url.rstrip("/").split("/")
            post_id = parts[-3] if len(parts) >= 3 else post_url
            # 'latest' mode: skip posts already recorded, don't even fetch them
            if (self._mode == "latest" and self._archive
                    and self._archive.post_seen(post_id)):
                return

            # Resilient read: transient server errors (e.g. coomerfans' sporadic
            # 500s) are retried with backoff instead of dropping the post.
            info = self._read_post(post_url, attempts)
            if info is None:
                if not self._cancelled():
                    with failed_lock:
                        failed.append(post_url)
                return

            # Year-scoped run ('Download Year' or a 'Fetch Latest' range): skip the
            # whole post when its date falls outside the bounds (undated posts can't be
            # placed, so they're skipped too). Hoisted out of the media loop — it's a
            # per-post decision.
            if self._year_range is not None:
                py = info["dt"].year if info["dt"] else None
                if not year_in_range(py, self._year_range):
                    return

            # Filenames use the post_id (the /p/{postId}/ number), not a title slug.
            # Fetch prefs may exclude kinds; entry keys keep the ALL-media index
            # so they stay stable across pref changes.
            name = str(info["post_id"])
            added = 0
            for idx, m in enumerate(info["media"], 1):
                if not self._kind_included(m["kind"]):
                    continue
                job = {
                    "post_url": info["url"],
                    "post_id": info["post_id"],
                    "index": idx,
                    "kind": m["kind"],
                    "ext": m["ext"],
                    "url": m["url"],
                    "path_key": m["path_key"],
                    "dt": info["dt"],
                    "name": name,
                }
                with jobs_lock:
                    jobs.append(job)
                added += 1
            # A post whose every media item was excluded is fully handled under
            # the current prefs — remember it so a marker row (written after the
            # run) makes the next 'latest' skip it instead of re-reading it.
            if info["media"] and not added:
                with jobs_lock:
                    self._tracked_posts.append((info["post_id"], info["dt"]))

        def handle_post(post_url, attempts):
            try:
                read_one(post_url, attempts)
            finally:
                note_read()

        with ThreadPoolExecutor(max_workers=self._read_workers) as ex:
            futures = [ex.submit(handle_post, u, 4) for u in post_urls]
            for _ in as_completed(futures):
                if self._cancelled():
                    break

        # Second window: posts that exhausted their inline retries get another,
        # longer pass. A transient 5xx/timeout has usually cleared by now, so
        # nothing is dropped just because the server hiccuped during the burst.
        if failed and not self._cancelled():
            retry_urls = list(failed)
            failed.clear()
            self._info(f"Re-reading {len(retry_urls)} post(s) that errored on the first pass...")
            read_done[0] = 0
            read_total[0] = len(retry_urls)
            with ThreadPoolExecutor(max_workers=self._read_workers) as ex:
                futures = [ex.submit(handle_post, u, 10) for u in retry_urls]
                for _ in as_completed(futures):
                    if self._cancelled():
                        break

        # Only posts still failing after both windows are counted as real errors.
        for u in failed:
            self._bump("error")
            self._error(f"Failed to read post after repeated retries: {u}")

        return jobs

    # ── resilient reads ──────────────────────────────────────
    def _request_with_retry(self, func, what, attempts, backoff=3):
        """Call func() with cancellable retry on transient HTTP/network errors.

        Returns func()'s result, or None if it failed after `attempts` tries,
        hit a non-transient HTTP status, or the run was cancelled. Transient
        statuses (5xx/429/52x) and connection/timeout errors back off and retry;
        404/410 and other client errors give up immediately (the page is gone).
        """
        b = backoff
        for attempt in range(1, attempts + 1):
            if self._cancelled():
                return None
            wait = None
            try:
                return func()
            except BotChallenge as e:
                # Not a server fault: we paced too hard and the guard wants a
                # pause. Its Retry-After is accurate, so honor it instead of our
                # generic backoff — the pacer has already widened the interval.
                err = str(e)
                wait = e.retry_after
            except requests.HTTPError as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status not in _TRANSIENT_READ_STATUSES:
                    self._error(f"{what}: HTTP {status} (not retriable)")
                    return None
                err = f"HTTP {status}"
            except requests.RequestException as e:
                err = e.__class__.__name__
            if attempt < attempts:
                delay = max(b, wait) if wait else b
                self._info(f"{what}: {err}; retry {attempt}/{attempts} in {delay}s")
                self._sleep_cancellable(delay)
                b = min(b * 2, 120)
            else:
                self._info(f"{what}: {err}; gave up after {attempts} attempt(s)")
        return None

    def _fetch_once(self, session, url):
        """One bot-guard-aware HTML fetch, paced with every other reader.

        The guard reports X-Bg-Score on *every* response, so a clean fetch still
        carries the signal for how close the pool is to tripping — feeding it to
        the pacer keeps us out of the challenge rather than recovering from it."""
        self._read_pace.wait(self._cancelled)
        r = session.get(url, timeout=(15, 60))
        try:
            check_challenge(r)
        except BotChallenge as e:
            self._challenge_hits += 1
            self._read_pace.on_throttled(e.retry_after)
            raise
        r.raise_for_status()
        # on_success first, then on_score, so an elevated score wins the round.
        self._read_pace.on_success()
        self._read_pace.on_score(bg_score(r), BG_WARN_SCORE, BG_TRIP_SCORE,
                                 cooloff=BG_COOLOFF)
        return r.text

    def _paced_fetch(self, session, url, attempts):
        return self._request_with_retry(
            lambda: self._fetch_once(session, url), f"page {url}", attempts)

    def _read_post(self, post_url, attempts):
        return self._request_with_retry(
            lambda: parse_post(self._session, post_url,
                               fetch=lambda s, u: self._fetch_once(s, u)),
            f"post {post_url}", attempts)

    def _fetch_page(self, session, url):
        """Resilient page fetch for the crawl. Raises only after retries are
        exhausted on a non-transient error, so a transient blip no longer
        truncates pagination (which would silently lose later pages)."""
        html = self._paced_fetch(session, url, attempts=10)
        if html is None and not self._cancelled():
            raise RuntimeError(f"Could not fetch creator page after retries: {url}")
        return html

    # ── download ─────────────────────────────────────────────
    def _download_all(self, jobs):
        # Media bytes come from img*.coomerfans.com, which carries no bot-guard
        # headers, so downloads keep the full `workers` fan-out; only the HTML
        # host is paced (see _READ_WORKERS).
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futures = {ex.submit(self._process_job, job): job for job in jobs}
            for fut in as_completed(futures):
                # Collect every result: without this an unexpected exception
                # (permission error, full disk, bad field) was swallowed by the
                # pool and the item vanished with no error count and no log,
                # while the run still reported success. Nothing may be dropped
                # silently, so surface it, count it, and persist it for redownload.
                try:
                    fut.result()
                except Exception as e:
                    if self._cancelled():
                        continue
                    job = futures[fut]
                    self._bump("error")
                    self._error(f"Failed to download post {job.get('post_id')} "
                                f"item {job.get('index')}: {e.__class__.__name__}: {e}")
                    self._record_failure(
                        entry_key(job.get("post_id"), job.get("index")), job,
                        filename=None, url=job.get("url"), status=None)

    def _process_job(self, job):
        if self._cancelled():
            return
        entry = entry_key(job["post_id"], job["index"])

        # Previously skipped by the user -> never fetch it again.
        if self._archive and self._archive.is_skipped(entry):
            self._bump("skip")
            return

        # If we have downloaded this exact item before, reuse its recorded name.
        recorded = self._archive.get_filename(entry) if self._archive else None
        if recorded:
            prev_path = target_path(self._destination, job["kind"], job["dt"], recorded)
            if os.path.isfile(prev_path):
                self._bump("skip")          # already downloaded -> never re-fetch
                return
            # In the archive but missing on disk -> re-fetch to the same name.
            self._download_stream(job, prev_path, entry, recorded)
            return

        # New item (no archive record). If the correctly-named file is already on
        # disk (archive lost, files copied in), adopt it and skip — the name is
        # unique per post_id+index, so it's the same item, not a collision.
        natural = build_filename(job["dt"], self._service, job["name"],
                                 job["index"], job["ext"])
        natural_path = target_path(self._destination, job["kind"], job["dt"], natural)
        with self._fname_lock:
            if natural_path not in self._claimed and os.path.isfile(natural_path):
                self._claimed.add(natural_path)
                self._bump("skip")
                self._record(entry, job, natural)
                return
        dest_path, filename = self._assign_path(job, entry)
        self._download_stream(job, dest_path, entry, filename)

    def _assign_path(self, job, entry):
        """Pick a collision-free destination path. (post_id+index is unique per
        creator, so collisions shouldn't occur, but the ' (n)' suffix guards
        against any edge case so nothing is ever overwritten.)"""
        base = build_filename(job["dt"], self._service, job["name"],
                              job["index"], job["ext"])
        with self._fname_lock:
            path = target_path(self._destination, job["kind"], job["dt"], base)
            if path not in self._claimed and not os.path.isfile(path):
                self._claimed.add(path)
                return path, base
            stem, ext = os.path.splitext(base)
            n = 2
            while True:
                cand = f"{stem} ({n}){ext}"
                cand_path = target_path(self._destination, job["kind"], job["dt"], cand)
                if cand_path not in self._claimed and not os.path.isfile(cand_path):
                    self._claimed.add(cand_path)
                    return cand_path, cand
                n += 1

    def _download_stream(self, job, dest_path, entry, filename):
        """Show this file in the UI's active-downloads panel while it runs.

        A thin wrapper so the row appears once per file and is always removed —
        success, failure, cancel or skip — and so the skip bookkeeping lives in
        one place rather than at each of the loops' several exit points. The
        video path delegates to _download_segmented inside _run_download, which
        is why this wraps rather than emitting in both.
        """
        self._dl_event("dl_start", entry, filename)
        try:
            ok = self._run_download(job, dest_path, entry, filename)
            if not ok and self._skip_requested(entry):
                self._discard(dest_path + ".part")
                self._mark_skipped(job, entry, filename)
            return ok
        finally:
            self._dl_event("dl_stop", entry)

    def _run_download(self, job, dest_path, entry, filename):
        """Stream to <dest>.part with resume + integrity-verified finalize.

        A file is only renamed to its final name once its byte count matches the
        size the server reported (and, for videos, ffprobe validates it). Signed
        URLs that expire (401/403/404/410) are refreshed by re-parsing the post.
        Retries network errors indefinitely; gives up only on cancel or on media
        that stays gone after several URL refreshes. Returns True on success.
        """
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        part = dest_path + ".part"
        # Skip a shard already known to be unreachable rather than paying its
        # connect timeout again; job["url"] is updated so the segmented video
        # path below starts on the healthy mirror too.
        url = self._healthy_url(job["url"])
        job["url"] = url
        native_shard = media_shard(url)
        tried_shards = {native_shard} if native_shard is not None else set()
        connect_fails = 0
        attempt = 0
        backoff = 5
        gone_refreshes = 0
        video_restarts = 0

        # Videos: prefer a segmented download — small byte-range chunks with per-chunk
        # retry. Coomer serves byte-accurate ranges, so this reliably completes even
        # flaky/large files a single stream can't (a dropped chunk is retried cheaply),
        # and it never stitches bytes across a signed-URL refresh. Returns None if the
        # server doesn't advertise usable range support -> fall through to streaming.
        if job["kind"] == "video":
            seg = self._download_segmented(job, dest_path, entry, filename)
            if seg is not None:
                return seg

        while not self._cancelled():
            if self._skip_requested(entry):
                return False
            attempt += 1
            # Another worker may have discovered this shard is dead while we were
            # mid-retry. Re-consult the shared set each pass so we move off it at
            # once, instead of every in-flight file paying its own two connect
            # timeouts to rediscover the same thing.
            healthy = self._healthy_url(url)
            if healthy != url:
                tried_shards.add(media_shard(url))
                url = healthy
                connect_fails = 0

            # Proactively refresh a signed video URL that's already expired,
            # so we skip the guaranteed 410 round-trip.
            if job["kind"] == "video" and url_is_expired(url):
                fresh = refresh_media_url(self._session, job["post_url"], job["path_key"],
                                         fetch=self._fetch_once)
                if fresh:
                    url = fresh

            try:
                resume_pos = os.path.getsize(part) if os.path.exists(part) else 0
                # Coomer serves byte-MISALIGNED data on video Range-resumes: the 206's
                # Content-Range start matches what we asked for, but the bytes don't line
                # up, so a resumed video finalizes at the correct size yet is internally
                # corrupt (garbage mid-stream). A fresh single-shot download is always
                # clean. So never resume a video — discard any partial and restart from
                # zero, giving up only after too many broken connections.
                if job["kind"] == "video" and resume_pos:
                    self._discard(part)
                    resume_pos = 0
                    video_restarts += 1
                    if video_restarts > _VIDEO_RESTART_LIMIT:
                        self._bump("error")
                        self._error(
                            f"Connection kept breaking after {video_restarts} clean restarts, "
                            f"skipping: {filename}  [{job['post_url']}]")
                        self._record_failure(entry, job, filename, url, "repeated connection breaks")
                        return False
                headers = {}
                open_mode = "wb"
                if resume_pos:
                    headers["Range"] = f"bytes={resume_pos}-"
                    open_mode = "ab"

                with self._session.get(
                    url, stream=True, headers=headers,
                    timeout=(self.connect_timeout, self.read_timeout),
                ) as r:
                    total = expected_total(r, resume_pos)

                    # Expired/forbidden/gone signed URL -> re-parse post for a fresh one.
                    if r.status_code in _EXPIRED_STATUSES:
                        # Videos are 404 on most mirrors even when the signature is
                        # fine, so while we're off the native shard a 4xx means
                        # "wrong mirror" — try the next one before blaming the URL.
                        if media_shard(url) != native_shard:
                            alt = self._next_shard_url(url, tried_shards)
                            if alt:
                                tried_shards.add(media_shard(alt))
                                url = alt
                                continue
                            url = swap_media_host(url, native_shard)
                        fresh = refresh_media_url(self._session, job["post_url"], job["path_key"],
                                         fetch=self._fetch_once)
                        if fresh and fresh != url:
                            url = fresh
                            gone_refreshes = 0
                            continue
                        gone_refreshes += 1
                        if gone_refreshes >= _GONE_REFRESH_LIMIT:
                            self._bump("error")
                            self._error(
                                f"Gone (HTTP {r.status_code}) after {gone_refreshes} refresh attempts, "
                                f"skipping: {filename}  [{job['post_url']}]"
                            )
                            self._record_failure(entry, job, filename, url,
                                                 r.status_code)
                            return False
                        self._sleep_cancellable(backoff)
                        backoff = min(backoff * 2, 120)
                        continue

                    # 416: only complete if the .part is already the full size.
                    if r.status_code == 416:
                        if (resume_pos and total is not None and resume_pos == total
                                and os.path.exists(part)):
                            if self._finalize(part, dest_path, entry, job, filename, total):
                                return True
                            continue
                        # stale/short .part the server won't extend -> discard, restart
                        self._discard(part)
                        continue

                    # Server ignored our Range -> restart from byte 0.
                    if resume_pos and r.status_code == 200:
                        resume_pos = 0
                        open_mode = "wb"

                    # A 206 MUST continue from exactly where our .part ends. If the
                    # server resumes from a different offset (or omits Content-Range),
                    # appending would stitch misaligned bytes into a right-length but
                    # internally-corrupt file (garbage mid-stream, passes the size
                    # check). Discard the .part and restart clean instead.
                    if resume_pos and r.status_code == 206:
                        start = content_range_start(r)
                        if start != resume_pos:
                            self._info(
                                f"Resume misaligned for {filename} "
                                f"(server start={start}, expected {resume_pos}); restarting clean")
                            self._discard(part)
                            continue

                    # Error-page guard: HTML/JSON where media is expected (e.g. an
                    # expired URL that 200s with an error page) -> refresh, don't write it.
                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if r.status_code == 200 and ("text/html" in ctype or "application/json" in ctype):
                        fresh = refresh_media_url(self._session, job["post_url"], job["path_key"],
                                         fetch=self._fetch_once)
                        if fresh and fresh != url:
                            url = fresh
                            continue
                        self._sleep_cancellable(backoff)
                        backoff = min(backoff * 2, 120)
                        continue

                    r.raise_for_status()

                    with open(part, open_mode) as f:
                        for chunk in r.iter_content(chunk_size=self.chunk_size):
                            if self._cancelled():
                                return False
                            if self._skip_requested(entry):
                                return False   # .part cleaned up by the wrapper
                            if chunk:
                                f.write(chunk)

                # Stream ended without error -> verify size before finalizing.
                size = os.path.getsize(part) if os.path.exists(part) else 0
                if total is not None and size < total:
                    self._info(f"Incomplete ({size}/{total} bytes) for {filename}, resuming...")
                    self._sleep_cancellable(2)
                    continue
                if total is not None and size > total:
                    # overshoot (shouldn't happen) -> restart clean
                    self._discard(part)
                    continue

                if self._finalize(part, dest_path, entry, job, filename,
                                  total if total is not None else size):
                    return True
                # finalize rejected it (ffprobe failed); .part removed -> redownload
                continue

            except Exception as e:
                if self._cancelled():
                    return False
                # Couldn't establish a connection at all -> suspect the shard, not
                # the file. ReadTimeout is deliberately excluded: a cold-storage
                # video legitimately stalls for minutes and must keep waiting.
                if isinstance(e, requests.ConnectionError) and not isinstance(
                        e, requests.ReadTimeout):
                    connect_fails += 1
                    if isinstance(e, requests.ConnectTimeout):
                        self._mark_shard_down(media_shard(url))
                    if connect_fails >= _CONNECT_ROTATE_AFTER:
                        alt = self._next_shard_url(url, tried_shards)
                        if alt:
                            tried_shards.add(media_shard(alt))
                            self._info(f"img{media_shard(url)} unreachable, retrying "
                                       f"{filename} on img{media_shard(alt)}")
                            url = alt
                            connect_fails = 0
                            continue
                        # Every mirror refused us. Stop pinning a worker on it and
                        # put it where the user can actually see it.
                        self._bump("error")
                        self._error(
                            f"CDN unreachable on every mirror, skipping: {filename}  "
                            f"[{job['post_url']}]")
                        self._record_failure(entry, job, filename, url, None,
                                             reason="cdn_unreachable")
                        return False
                self._info(f"Retry {attempt} for {filename}: {e}")
                self._sleep_cancellable(backoff)
                backoff = min(backoff * 2, 120)
        return False

    def _download_segmented(self, job, dest_path, entry, filename, chunk=8 << 20,
                            max_refreshes=5):
        """Download a video in verified byte-range chunks. Returns True (done),
        False (failed after retries), or None (no usable range support -> caller
        falls back to the streaming path)."""
        part = dest_path + ".part"
        url = job["url"]
        if url_is_expired(url):
            fresh = refresh_media_url(self._session, job["post_url"], job["path_key"],
                                         fetch=self._fetch_once)
            if fresh:
                url = fresh

        # Probe for total size + range support.
        try:
            pr = self._session.get(
                url, headers={"Range": "bytes=0-0"}, stream=True,
                timeout=(self.connect_timeout, self.read_timeout))
            status = pr.status_code
            total = None
            m = re.search(r"/\s*(\d+)\s*$", pr.headers.get("Content-Range", ""))
            if m:
                total = int(m.group(1))
            pr.close()
        except Exception:
            return None
        if status != 206 or not total:
            return None    # server won't do ranges here -> fall back to streaming

        refreshes = 0
        while not self._cancelled():
            stale = False
            try:
                with open(part, "wb") as f:
                    pos = 0
                    while pos < total:
                        if self._cancelled():
                            return False
                        if self._skip_requested(entry):
                            return False
                        end = min(pos + chunk, total) - 1
                        data = self._fetch_chunk(url, pos, end)
                        if data == "refresh":
                            fresh = refresh_media_url(
                                self._session, job["post_url"], job["path_key"],
                                fetch=self._fetch_once)
                            if fresh:
                                url = fresh
                            stale = True
                            break
                        if data is None:
                            self._bump("error")
                            self._error(f"Chunk {pos}-{end} failed for {filename}")
                            self._record_failure(entry, job, filename, url, "chunk failed")
                            self._discard(part)
                            return False
                        f.write(data)
                        pos = end + 1
            except Exception as e:
                self._info(f"Segmented retry for {filename}: {e}")
                self._sleep_cancellable(3)
                continue
            if stale:
                refreshes += 1
                if refreshes > max_refreshes:
                    self._bump("error")
                    self._error(f"URL kept expiring mid-file, skipping: {filename}")
                    self._record_failure(entry, job, filename, url, "url refresh loop")
                    self._discard(part)
                    return False
                continue                       # restart chunks with the fresh URL
            break                              # all chunks written

        if self._cancelled():
            return False
        size = os.path.getsize(part) if os.path.exists(part) else 0
        if size != total:
            self._discard(part)
            return False
        return self._finalize(part, dest_path, entry, job, filename, total)

    def _fetch_chunk(self, url, pos, end, attempts=6):
        """Fetch one verified byte-range chunk. Returns bytes, None (failed after
        retries), or 'refresh' (signed URL expired)."""
        backoff = 3
        want = end - pos + 1
        for att in range(attempts):
            if self._cancelled():
                return None
            try:
                r = self._session.get(
                    url, headers={"Range": f"bytes={pos}-{end}"},
                    timeout=(self.connect_timeout, self.read_timeout))
                if r.status_code in _EXPIRED_STATUSES:
                    return "refresh"
                if r.status_code != 206:
                    raise RuntimeError(f"HTTP {r.status_code} (expected 206)")
                start = content_range_start(r)
                if start is not None and start != pos:
                    raise RuntimeError(f"misaligned chunk {start}!={pos}")
                data = r.content
                if len(data) != want:
                    raise RuntimeError(f"short chunk {len(data)}!={want}")
                return data
            except Exception:
                self._sleep_cancellable(backoff)
                backoff = min(backoff * 2, 30)
        return None

    def _finalize(self, part, dest_path, entry, job, filename, expected_size):
        """Validate (videos via ffprobe) then atomically rename .part -> final and
        record the archive entry. Returns True if finalized, False if rejected
        (the .part is removed so the next loop re-downloads from scratch)."""
        if job["kind"] == "video" and not ffprobe_ok(part):
            self._discard(part)
            self._info(f"ffprobe validation failed for {filename}, re-downloading")
            return False
        os.replace(part, dest_path)
        self._bump("download")
        self._record(entry, job, filename, expected_size)
        self._clear_failure(entry)   # a prior run's failure is now on disk
        self._progress_download(filename)
        return True

    # ── CDN shard failover ───────────────────────────────────
    def _mark_shard_down(self, shard):
        """Remember an unreachable shard so every other job skips it instead of
        paying the full connect timeout again."""
        if shard is None:
            return
        with self._shard_lock:
            first = shard not in self._dead_shards
            self._dead_shards.add(shard)
        if first:
            self._info(f"img{shard}.coomerfans.com is unreachable - "
                       f"using mirrors for the rest of this run.")

    def _healthy_url(self, url):
        """Re-point a URL off a known-dead shard before the first request."""
        shard = media_shard(url)
        if shard is None:
            return url
        with self._shard_lock:
            if shard not in self._dead_shards:
                return url
            dead = set(self._dead_shards)
        for alt in CDN_SHARD_ORDER:
            if alt not in dead:
                return swap_media_host(url, alt)
        return url

    def _next_shard_url(self, url, tried):
        """Same media on the next untried, not-known-dead shard, or None."""
        shard = media_shard(url)
        if shard is None:
            return None
        with self._shard_lock:
            dead = set(self._dead_shards)
        for alt in CDN_SHARD_ORDER:
            if alt != shard and alt not in tried and alt not in dead:
                return swap_media_host(url, alt)
        return None

    def _record_failure(self, entry, job, filename, url, status, reason="http_gone"):
        """Persist a durable 'gone' failure (survives the run; the diag/log don't)
        so the UI can offer a redownload + the post page for a manual grab."""
        if not self._errors:
            return
        try:
            self._errors.record_failure(
                entry, platform="coomerfans", service=self._service,
                user_id=self._user_id, post_id=job.get("post_id"),
                filename=filename, url=url, page_url=job.get("post_url"),
                media_kind=job.get("kind"), status=status, reason=reason)
        except Exception:
            pass

    def _clear_failure(self, entry):
        if not self._errors:
            return
        try:
            self._errors.clear_failure(entry)
        except Exception:
            pass

    @staticmethod
    def _discard(part):
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass

    def _sleep_cancellable(self, seconds):
        slept = 0.0
        while slept < seconds and not self._cancelled():
            time.sleep(0.5)
            slept += 0.5

    # ── reconciliation ───────────────────────────────────────
    def _reconcile(self, jobs):
        """Final safety net: make sure every discovered item has a final file
        on disk. Re-queue any stragglers (downloads only fail to appear if
        cancelled, but this guards against any edge case)."""
        missing = []
        for job in jobs:
            entry = entry_key(job["post_id"], job["index"])
            fn = (self._archive.get_filename(entry) if self._archive else None)
            if fn:
                path = target_path(self._destination, job["kind"], job["dt"], fn)
                if os.path.isfile(path):
                    continue
            missing.append(job)

        total = len(jobs)
        if not missing:
            self._info(f"Verified {total}/{total} media item(s) present on disk.")
            return

        self._info(f"Reconciling {len(missing)} missing item(s)...")
        for job in missing:
            if self._cancelled():
                break
            self._process_job(job)
        present = total - sum(
            1 for job in missing
            if not self._job_present(job)
        )
        self._info(f"Verified {present}/{total} media item(s) present on disk.")

    def _job_present(self, job):
        entry = entry_key(job["post_id"], job["index"])
        fn = (self._archive.get_filename(entry) if self._archive else None)
        if not fn:
            return False
        return os.path.isfile(target_path(self._destination, job["kind"], job["dt"], fn))

    # ── helpers ──────────────────────────────────────────────
    def _record(self, entry, job, filename, expected_size=None):
        if self._archive:
            year = f"{job['dt']:%Y}" if job["dt"] else "unknown"
            self._archive.record(entry, job["post_id"], filename, job["kind"], year,
                                 expected_size, job.get("path_key"))

    def _bump(self, kind):
        with self._count_lock:
            if kind == "download":
                self.downloaded_count += 1
            elif kind == "skip":
                self.skipped_count += 1
            elif kind == "error":
                self.error_count += 1

    def _progress_download(self, filename):
        if self._on_progress:
            self._on_progress({"type": "download", "message": filename})

    def _info(self, message):
        if self._on_progress:
            self._on_progress({"type": "info", "message": message})

    def _error(self, message):
        if self._on_progress:
            self._on_progress({"type": "error", "message": message})

    def _stats(self, cancelled):
        return {
            "downloaded": self.downloaded_count,
            "skipped": self.skipped_count,
            "errors": self.error_count,
            "cancelled": cancelled,
        }

    def _finish(self, on_complete):
        on_complete(self._stats(cancelled=self._cancelled()))
