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
)
from backend.coomerfans_archive import Archive, entry_key

# HTTP statuses that mean "this (signed) URL is dead — re-parse the post for a
# fresh one" rather than "retry the same URL".
_EXPIRED_STATUSES = (401, 403, 404, 410)
_GONE_REFRESH_LIMIT = 4   # give up (flag error) after this many refresh-resistant gone responses

# Transient HTTP statuses for page/post *reads* (server hiccups, rate limits,
# Cloudflare 52x). coomerfans intermittently 500s on a valid post; a retry a
# few seconds later succeeds. These are retried (with backoff) rather than
# dropping the post. 404/410 are NOT here — a missing post page is permanent.
_TRANSIENT_READ_STATUSES = (408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524)


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
        self._session = None
        self._destination = None
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
            on_progress, on_complete, on_error, cookies_path=None, year=None):
        self.downloaded_count = self.skipped_count = self.error_count = 0
        self._cancel_event.clear()
        self._claimed.clear()
        self._running = True
        self._on_progress = on_progress
        self._destination = destination
        self._mode = mode

        try:
            creator = parse_creator_url(creator_url)
            if not creator:
                on_error({"type": "error", "message": "Invalid creator URL"})
                on_complete(self._stats(cancelled=False))
                return
            self._service = creator["service"]
            self._session = make_session(cookies_path=cookies_path)
            self._archive = Archive(archive_path) if archive_path else None

            jobs = self._crawl_and_parse(creator_url, year)
            if self._cancelled():
                self._info("Cancelled during crawl.")
                self._finish(on_complete)
                return

            self._info(f"{len(jobs)} media item(s) to download "
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

    # ── crawl + parse ────────────────────────────────────────
    def _crawl_and_parse(self, creator_url, year):
        self._info("Crawling creator pages...")

        def on_page(page, n):
            self._info(f"Page {page}: {n} post(s)")

        post_urls = list(iter_post_urls(
            self._session, creator_url, on_page=on_page,
            should_cancel=self._cancelled, fetch=self._fetch_page,
        ))
        if self._cancelled():
            return []
        self._info(f"Found {len(post_urls)} post(s). Reading posts...")

        jobs = []
        jobs_lock = threading.Lock()
        failed = []
        failed_lock = threading.Lock()

        def handle_post(post_url, attempts):
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

            # Filenames use the post_id (the /p/{postId}/ number), not a title slug.
            name = str(info["post_id"])
            for idx, m in enumerate(info["media"], 1):
                if year and (not info["dt"] or info["dt"].year != int(year)):
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

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
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
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
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
            try:
                return func()
            except requests.HTTPError as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status not in _TRANSIENT_READ_STATUSES:
                    self._error(f"{what}: HTTP {status} (not retriable)")
                    return None
                err = f"HTTP {status}"
            except requests.RequestException as e:
                err = e.__class__.__name__
            if attempt < attempts:
                self._info(f"{what}: {err}; retry {attempt}/{attempts} in {b}s")
                self._sleep_cancellable(b)
                b = min(b * 2, 120)
            else:
                self._info(f"{what}: {err}; gave up after {attempts} attempt(s)")
        return None

    def _read_post(self, post_url, attempts):
        return self._request_with_retry(
            lambda: parse_post(self._session, post_url), f"post {post_url}", attempts)

    def _fetch_page(self, session, url):
        """Resilient page fetch for the crawl. Raises only after retries are
        exhausted on a non-transient error, so a transient blip no longer
        truncates pagination (which would silently lose later pages)."""
        html = self._request_with_retry(
            lambda: fetch_html(session, url), f"page {url}", attempts=10)
        if html is None and not self._cancelled():
            raise RuntimeError(f"Could not fetch creator page after retries: {url}")
        return html

    # ── download ─────────────────────────────────────────────
    def _download_all(self, jobs):
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futures = [ex.submit(self._process_job, job) for job in jobs]
            for _ in as_completed(futures):
                pass

    def _process_job(self, job):
        if self._cancelled():
            return
        entry = entry_key(job["post_id"], job["index"])

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
        """Stream to <dest>.part with resume + integrity-verified finalize.

        A file is only renamed to its final name once its byte count matches the
        size the server reported (and, for videos, ffprobe validates it). Signed
        URLs that expire (401/403/404/410) are refreshed by re-parsing the post.
        Retries network errors indefinitely; gives up only on cancel or on media
        that stays gone after several URL refreshes. Returns True on success.
        """
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        part = dest_path + ".part"
        url = job["url"]
        attempt = 0
        backoff = 5
        gone_refreshes = 0

        while not self._cancelled():
            attempt += 1

            # Proactively refresh a signed video URL that's already expired,
            # so we skip the guaranteed 410 round-trip.
            if job["kind"] == "video" and url_is_expired(url):
                fresh = refresh_media_url(self._session, job["post_url"], job["path_key"])
                if fresh:
                    url = fresh

            try:
                resume_pos = os.path.getsize(part) if os.path.exists(part) else 0
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
                        fresh = refresh_media_url(self._session, job["post_url"], job["path_key"])
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

                    # Error-page guard: HTML/JSON where media is expected (e.g. an
                    # expired URL that 200s with an error page) -> refresh, don't write it.
                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if r.status_code == 200 and ("text/html" in ctype or "application/json" in ctype):
                        fresh = refresh_media_url(self._session, job["post_url"], job["path_key"])
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
                self._info(f"Retry {attempt} for {filename}: {e}")
                self._sleep_cancellable(backoff)
                backoff = min(backoff * 2, 120)
        return False

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
