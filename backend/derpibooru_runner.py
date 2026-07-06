"""Download engine for derpibooru.org.

Mirrors CoomerfansRunner/PawchiveRunner's public contract (run/cancel/is_running
+ the downloaded/skipped/error counters) so backend.creator_runner can drive it
the same way. Like pawchive it's a JSON-API engine, but simpler:

  * derpibooru is a *booru*: the crawl paginates a search endpoint (`artist:gunya`)
    rather than a per-account listing. Each result is a single image/video file
    (no attachments, no multi-image posts), so there is no page-order ordinal and
    no external-links manifest.
  * media URLs (view_url) are direct, unsigned and non-expiring, so there is no
    URL-refresh dance — downloads are a straight resumable stream.
  * files are named after the image ID, e.g. "2026.05.05 - Derpibooru - 3456789.png".

Auth: an account API key (`&key=`) + the Everything filter (filter_id) are threaded
in from settings so gated images/videos are visible.

Curation rule (see the other engines): 'latest' skips images already in the
archive (never resurrects deletions); 'full' re-downloads missing files by design;
'redownload_year' ("Download Year") fetches a single year server-side, skipping
anything already present (it never re-downloads or overwrites).

Rate-limit compliance (derpibooru.org/pages/api):
  * The crawl only hits the search path, capped at ~1.6 req/s — under the
    documented 20 requests / 10s search limit. File downloads (from the separate
    derpicdn.net CDN) are paced under the ~6 req/s "normal" ceiling.
  * On a 501 anti-bot challenge the client backs off ≥5s before retrying, as
    required. On a 500 with an empty body (a 15-minute IP block) it stops sending
    API requests entirely, because any request during the block resets the timer.
  * Failures back off exponentially; each page/file is fetched once (the archive
    prevents re-fetching), so the access pattern is inherently cache-friendly.
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

import requests

from backend.derpibooru_scraper import (
    make_session, parse_creator_url, iter_images, parse_image, fetch_json,
    build_filename, add_index_suffix, target_path, query_label, site_code,
    EVERYTHING_FILTER,
)
from backend.derpibooru_archive import Archive, entry_key
# Reuse the proven low-level helpers verbatim.
from backend.coomerfans_runner import (
    expected_total, ffprobe_ok, _TRANSIENT_READ_STATUSES,
)

# Videos are the heavy transfers; run them in their own small pool so a burst of
# big webm/mp4 files can't hog every worker or hammer the CDN into 429s.
_LARGE_KINDS = ("video",)


class _AdaptiveThrottle:
    """Self-tuning request pacer shared by all workers (AIMD).

    Each caller reserves the next time slot, so N workers issue at most one
    request per `interval` overall. The interval eases *down* toward `floor` on
    every clean response and jumps *up* toward `ceil` on a 429 (respecting
    Retry-After), so the whole pool backs off together instead of each worker
    hammering the same wall."""

    def __init__(self, interval, floor, ceil, recover=0.9):
        self.interval = interval
        self.floor = floor
        self.ceil = ceil
        self.recover = recover
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self, is_cancelled):
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        while True:
            remaining = start - time.monotonic()
            if remaining <= 0 or is_cancelled():
                return
            time.sleep(min(0.2, remaining))

    def on_success(self):
        with self._lock:
            if self.interval > self.floor:
                self.interval = max(self.floor, self.interval * self.recover)

    def on_throttled(self, retry_after):
        with self._lock:
            self.interval = min(self.ceil, max(self.interval * 2, 0.5))
            self._next = max(self._next, time.monotonic() + retry_after)


class DerpibooruRunner:
    def __init__(self, workers=5, connect_timeout=30, read_timeout=300,
                 chunk_size=1 << 20, max_download_attempts=10, large_workers=2):
        self.workers = max(1, int(workers))
        self.large_workers = max(1, int(large_workers))
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.chunk_size = chunk_size
        self.max_download_attempts = max_download_attempts
        # The search API rate-limits hard, so start tight and adapt; the image
        # CDN tolerates concurrency but we still stay polite.
        #
        # Derpibooru's documented limits (derpibooru.org/pages/api):
        #   * search path (/api/v1/json/search*): 20 requests / 10s  → ≈2 req/s
        #   * normal requests:                     30 requests / 5s   → ≈6 req/s
        # The whole crawl hits the search path, so the API pacer must never issue
        # faster than the search limit. floor=0.6s caps us at ~16 reqs in any 10s
        # window (~83% of the 20/10s limit — margin for the bursty window), and it
        # starts a touch slower still.
        self._api_throttle = _AdaptiveThrottle(0.7, floor=0.6, ceil=30.0)
        # Files come from the derpicdn.net CDN (a separate host). Keep initiations
        # under the ~6 req/s "normal" ceiling in case the CDN shares it; transfers
        # still overlap across the worker pools.
        self._file_throttle = _AdaptiveThrottle(0.2, floor=0.2, ceil=15.0,
                                                recover=0.95)
        # Set when the server signals a hard 15-min IP block (HTTP 500, empty
        # body). Once set, we stop hitting the API entirely — any further request
        # would reset the 15-minute timer.
        self._blocked = False

        self.downloaded_count = 0
        self.skipped_count = 0
        self.error_count = 0

        self._running = False
        self._cancel_event = threading.Event()
        self._count_lock = threading.Lock()
        self._fname_lock = threading.Lock()
        self._claimed = set()

        self._on_progress = None
        self._archive = None
        self._session = None
        self._destination = None
        self._query = None
        self._api_key = None
        self._filter_id = EVERYTHING_FILTER
        self._mode = "full"

    # ── public contract ──────────────────────────────────────
    @property
    def is_running(self):
        return self._running

    def cancel(self):
        self._cancel_event.set()

    def _cancelled(self):
        return self._cancel_event.is_set()

    def run(self, creator_url, destination, mode, archive_path,
            on_progress, on_complete, on_error,
            api_key=None, filter_id=None, year=None):
        self.downloaded_count = self.skipped_count = self.error_count = 0
        self._cancel_event.clear()
        self._claimed.clear()
        self._blocked = False
        self._running = True
        self._on_progress = on_progress
        self._destination = destination
        self._mode = mode
        self._api_key = api_key or None
        self._filter_id = str(filter_id or EVERYTHING_FILTER)

        try:
            creator = parse_creator_url(creator_url)
            if not creator:
                on_error({"type": "error", "message": "Invalid derpibooru search URL"})
                on_complete(self._stats(cancelled=False))
                return
            self._query = creator["query"]
            self._session = make_session(api_key=self._api_key)
            self._archive = Archive(archive_path) if archive_path else None

            self._info(f"Query: {self._query}  ({site_code()}"
                       f"{'' if self._api_key else ' — no API key: logged-out visibility'})")

            jobs = self._crawl(year)
            if self._cancelled():
                self._info("Cancelled during crawl.")
                self._finish(on_complete)
                return
            if self._blocked:
                # Search was cut short by a 15-min IP block. Download whatever we
                # gathered (the CDN is a separate host, unaffected) and tell the
                # user to re-run later to pick up the rest — a 'latest'/'full'
                # re-run resumes from the archive.
                self._error("Search stopped early due to a derpibooru rate-block — "
                            "downloading what was found so far. Wait ~15 minutes, "
                            "then run again to fetch the rest.")

            self._info(f"{len(jobs)} image(s) to fetch "
                       f"({self.skipped_count} already present).")
            self._download_all(jobs)
            if not self._cancelled():
                self._reconcile(jobs)

            self._finish(on_complete)
        except Exception as e:
            on_error({"type": "error", "message": f"Fatal: {e}"})
            self._finish(on_complete)
        finally:
            if self._archive:
                self._archive.close()
            self._running = False

    # ── crawl + build jobs ───────────────────────────────────
    def _crawl(self, year):
        self._info("Searching derpibooru...")

        def on_page(page, n):
            self._info(f"Page {page}: {n} image(s)")

        jobs = []
        yr = int(year) if year else None
        # Year-scoped download ('Download Year'): push the year into the search so
        # the server returns ONLY that year's images — we never page through other
        # years. Philomena matches a bare year to the whole year range.
        query = f"{self._query},created_at:{yr}" if yr is not None else self._query

        for raw in iter_images(self._session, query,
                               api_key=self._api_key, filter_id=self._filter_id,
                               on_page=on_page, should_cancel=self._cancelled,
                               fetch=self._fetch_json):
            if self._cancelled():
                break
            img = parse_image(raw)
            if not img["image_id"] or not img["url"]:
                continue
            if yr is not None and (not img["dt"] or img["dt"].year != yr):
                continue   # safety net; the server query already scopes the year
            entry = entry_key(img["image_id"])
            # 'latest' tops up: skip images already recorded (whole-image skip,
            # like pawchive's post-level skip).
            if (self._mode == "latest" and self._archive
                    and self._archive.has(entry)):
                continue
            jobs.append({
                "entry": entry,
                "image_id": img["image_id"],
                "media_kind": img["kind"],
                "url": img["url"],
                "dt": img["dt"],
                "ext": img["ext"],
            })
        return jobs

    def _fetch_json(self, session, url):
        """Resilient search fetch: transient server errors back off & retry so a
        blip never truncates pagination (which would silently lose later pages)."""
        return self._request_with_retry(
            lambda u: fetch_json(session, u), url, "search", attempts=10)

    @staticmethod
    def _retry_after(resp, default):
        try:
            ra = (resp.headers.get("Retry-After") or "").strip() if resp is not None else ""
            if ra.isdigit():
                return max(1, min(int(ra), 300))
        except Exception:
            pass
        return default

    @staticmethod
    def _body_empty(resp):
        """True if the response has no body — the hard-block signal (HTTP 500
        with an empty response body)."""
        try:
            return not (resp.content or b"").strip()
        except Exception:
            return False

    def _request_with_retry(self, call, url, what, attempts, backoff=3):
        # Minimum backoff for an anti-bot challenge: derpibooru requires the client
        # to send no requests for at least 5s after a 501 challenge.
        CHALLENGE_BACKOFF = 6
        b = backoff
        for attempt in range(1, attempts + 1):
            if self._cancelled() or self._blocked:
                return None
            self._api_throttle.wait(self._cancelled)
            if self._cancelled():
                return None
            try:
                result = call(url)
                self._api_throttle.on_success()
                return result
            except requests.HTTPError as e:
                resp = getattr(e, "response", None)
                status = getattr(resp, "status_code", None)
                # Hard 15-min IP block: HTTP 500 with an empty body. Any further
                # request from this IP RESETS the 15-minute timer, so we must stop
                # entirely — not retry.
                if status == 500 and self._body_empty(resp):
                    self._blocked = True
                    self._error("HTTP 500 empty body — derpibooru has issued a "
                                "15-minute IP block. Stopping all API requests so "
                                "the block isn't extended; wait ~15 min before "
                                "retrying.")
                    return None
                # Anti-bot challenge: HTTP 501 (text/html). Back off ≥5s, then
                # retry; also slow the whole pool down.
                if status == 501:
                    wait_s = max(5, self._retry_after(resp, CHALLENGE_BACKOFF))
                    self._api_throttle.on_throttled(wait_s)
                    if attempt < attempts:
                        self._info(f"{what}: anti-bot challenge (501); backing off "
                                   f"{wait_s}s (retry {attempt}/{attempts})")
                        self._sleep_cancellable(wait_s)
                        continue
                    self._error(f"{what}: repeated anti-bot challenges (501); "
                                "giving up — try again in a few minutes")
                    return None
                if status not in _TRANSIENT_READ_STATUSES:
                    self._error(f"{what}: HTTP {status} (not retriable)")
                    return None
                wait_s = self._retry_after(resp, b)
                if status == 429:
                    self._api_throttle.on_throttled(wait_s)
                err = f"HTTP {status}"
            except requests.RequestException as e:
                err = e.__class__.__name__
                wait_s = b
            if attempt < attempts:
                self._info(f"{what}: {err}; retry {attempt}/{attempts} in {wait_s}s")
                self._sleep_cancellable(wait_s)
                b = min(b * 2, 120)
            else:
                self._info(f"{what}: {err}; gave up after {attempts} attempt(s)")
        return None

    # ── download ─────────────────────────────────────────────
    def _job_is_large(self, job):
        return job.get("media_kind") in _LARGE_KINDS

    def _download_all(self, jobs):
        """Images at full concurrency, videos in a separate smaller pool at the
        same time — so a burst of big webm/mp4 can't occupy every worker."""
        small = [j for j in jobs if not self._job_is_large(j)]
        large = [j for j in jobs if self._job_is_large(j)]
        with ThreadPoolExecutor(max_workers=self.workers) as ex_small, \
                ThreadPoolExecutor(max_workers=self.large_workers) as ex_large:
            futures = [ex_small.submit(self._process_job, j) for j in small]
            futures += [ex_large.submit(self._process_job, j) for j in large]
            for _ in as_completed(futures):
                pass

    def _process_job(self, job):
        if self._cancelled():
            return
        try:
            self._process_media(job)
        except Exception as e:
            self._bump("error")
            self._error(f"job failed ({job.get('url')}): {e}")

    def _process_media(self, job):
        entry = job["entry"]
        recorded = self._archive.get_filename(entry) if self._archive else None
        if recorded:
            prev = target_path(self._destination, job["media_kind"], job["dt"], recorded)
            if os.path.isfile(prev):
                self._bump("skip")   # already downloaded — never re-fetch
                return
            # In the archive but gone from disk -> re-fetch to the same name.
            self._download_stream(job["url"], prev, entry, job, recorded,
                                  job["media_kind"] == "video")
            return
        # Correctly-named file already on disk (prior download whose archive was
        # cleared, etc.) -> adopt it and skip instead of downloading a duplicate.
        natural = build_filename(job["dt"], job["image_id"], job["ext"])
        natural_path = target_path(self._destination, job["media_kind"], job["dt"], natural)
        with self._fname_lock:
            if natural_path not in self._claimed and os.path.isfile(natural_path):
                self._claimed.add(natural_path)
                self._bump("skip")
                self._record(entry, job, natural, job["media_kind"])
                return
        dest_path, filename = self._assign_path(job["dt"], job["media_kind"],
                                                job["image_id"], job["ext"])
        self._download_stream(job["url"], dest_path, entry, job, filename,
                              job["media_kind"] == "video")

    def _assign_path(self, dt, media_kind, image_id, ext):
        """Collision-free '<date> - Derpibooru - <id>.<ext>' path; '_n' before the
        ext on clash so nothing is ever overwritten (rare — IDs are unique)."""
        base = build_filename(dt, image_id, ext)
        with self._fname_lock:
            path = target_path(self._destination, media_kind, dt, base)
            if path not in self._claimed and not os.path.isfile(path):
                self._claimed.add(path)
                return path, base
            n = 1
            while True:
                cand = add_index_suffix(base, n)
                cand_path = target_path(self._destination, media_kind, dt, cand)
                if cand_path not in self._claimed and not os.path.isfile(cand_path):
                    self._claimed.add(cand_path)
                    return cand_path, cand
                n += 1

    def _download_stream(self, url, dest_path, entry, job, filename, is_video):
        """Stream to <dest>.part with HTTP Range resume + size/ffprobe verify,
        then atomically finalize. Retries transient/network errors with backoff.
        Returns True on success, False on give-up/cancel."""
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        part = dest_path + ".part"
        attempt = 0
        backoff = 5

        while not self._cancelled():
            attempt += 1
            try:
                resume_pos = os.path.getsize(part) if os.path.exists(part) else 0
                headers = {}
                open_mode = "wb"
                if resume_pos:
                    headers["Range"] = f"bytes={resume_pos}-"
                    open_mode = "ab"

                self._file_throttle.wait(self._cancelled)
                if self._cancelled():
                    return False
                with self._session.get(
                    url, stream=True, headers=headers,
                    timeout=(self.connect_timeout, self.read_timeout),
                ) as r:
                    total = expected_total(r, resume_pos)

                    if r.status_code == 429:
                        wait_s = self._retry_after(r, backoff)
                        self._file_throttle.on_throttled(wait_s)
                        self._info(f"rate limited (429), waiting {wait_s}s: {filename}")
                        self._sleep_cancellable(wait_s)
                        backoff = min(backoff * 2, 120)
                        continue

                    # Anti-bot challenge (501) / server error (500). Back off ≥5s
                    # and slow the pool — never hammer through a challenge.
                    if r.status_code in (500, 501):
                        wait_s = max(5, self._retry_after(r, backoff))
                        self._file_throttle.on_throttled(wait_s)
                        self._info(f"server busy (HTTP {r.status_code}), backing off "
                                   f"{wait_s}s: {filename}")
                        self._sleep_cancellable(wait_s)
                        backoff = min(backoff * 2, 120)
                        continue

                    if r.status_code in (401, 403, 404, 410):
                        self._error(f"image gone (HTTP {r.status_code}): {url}")
                        return False

                    if r.status_code == 416:
                        if (resume_pos and total is not None and resume_pos == total
                                and os.path.exists(part)):
                            if self._finalize(part, dest_path, entry, job, filename,
                                              total, is_video):
                                return True
                            continue
                        self._discard(part)
                        continue

                    if resume_pos and r.status_code == 200:
                        resume_pos = 0
                        open_mode = "wb"

                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if r.status_code == 200 and "text/html" in ctype:
                        # a landing/error page where a file was expected
                        self._sleep_cancellable(backoff)
                        backoff = min(backoff * 2, 120)
                        if attempt >= self.max_download_attempts:
                            self._error(f"expected a file, got HTML: {url}")
                            return False
                        continue

                    r.raise_for_status()
                    with open(part, open_mode) as f:
                        for chunk in r.iter_content(chunk_size=self.chunk_size):
                            if self._cancelled():
                                return False
                            if chunk:
                                f.write(chunk)

                size = os.path.getsize(part) if os.path.exists(part) else 0
                if total is not None and size < total:
                    self._info(f"Incomplete ({size}/{total}) for {filename}, resuming...")
                    self._sleep_cancellable(2)
                    continue
                if total is not None and size > total:
                    self._discard(part)
                    continue

                self._file_throttle.on_success()
                if self._finalize(part, dest_path, entry, job, filename,
                                  total if total is not None else size, is_video):
                    return True
                continue
            except Exception as e:
                if self._cancelled():
                    return False
                self._info(f"Retry {attempt} for {filename}: {e}")
                self._sleep_cancellable(backoff)
                backoff = min(backoff * 2, 120)
            if attempt >= self.max_download_attempts:
                self._error(f"gave up after {attempt} attempt(s): {filename}")
                return False
        return False

    def _finalize(self, part, dest_path, entry, job, filename, expected_size, is_video):
        if is_video and not ffprobe_ok(part):
            self._discard(part)
            self._info(f"ffprobe validation failed for {filename}, re-downloading")
            return False
        os.replace(part, dest_path)
        self._bump("download")
        self._record(entry, job, filename, job["media_kind"], expected_size)
        self._progress_download(filename)
        return True

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
        missing = [j for j in jobs if not self._job_present(j)]
        total = len(jobs)
        if not missing:
            self._info(f"Verified {total}/{total} image(s) present on disk.")
            return
        self._info(f"Reconciling {len(missing)} missing item(s)...")
        for job in missing:
            if self._cancelled():
                break
            self._process_media(job)
        present = sum(1 for j in jobs if self._job_present(j))
        self._info(f"Verified {present}/{total} image(s) present on disk.")

    def _job_present(self, job):
        fn = self._archive.get_filename(job["entry"]) if self._archive else None
        if not fn:
            return False
        return os.path.isfile(target_path(self._destination, job["media_kind"],
                                          job["dt"], fn))

    # ── helpers ──────────────────────────────────────────────
    def _record(self, entry, job, filename, media_kind, expected_size=None):
        if self._archive:
            year = f"{job['dt']:%Y}" if job["dt"] else "unknown"
            self._archive.record(entry, job["image_id"], filename, media_kind, year,
                                 expected_size, None)

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
        return {"downloaded": self.downloaded_count, "skipped": self.skipped_count,
                "errors": self.error_count, "cancelled": cancelled}

    def _finish(self, on_complete):
        on_complete(self._stats(cancelled=self._cancelled()))
