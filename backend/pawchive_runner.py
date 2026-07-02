"""Download engine for pawchive.st.

Mirrors CoomerfansRunner's public contract (run/cancel/is_running + the
downloaded/skipped/error counters) so backend.creator_runner can drive it the
same way. Differences from coomerfans:

  * pawchive has a JSON API, so the crawl paginates the listing endpoint (which
    already carries full post detail) instead of scraping HTML — one request per
    50 posts, with a per-post detail fallback when the listing omits it.
  * media URLs are content-addressed (no signing / no expiry), so there is no
    URL-refresh dance — downloads are a straight resumable stream.
  * on-site media are named after the file's OWN name, e.g.
    "2026.05.12 - Patreon - Label stream night X teaser.mp4".
  * external links found in a post body/embed are handled: single *direct* files
    (catbox etc.) are auto-downloaded and verified; everything else (mega, gdrive,
    gofile, reference pages) plus any direct file that fails is written to the
    per-creator manifest (PawchiveLinks) for manual download — nothing is dropped.

Curation rule (see memory curation-deletion-workflow): 'latest' skips whole
posts already in the archive (never resurrects deletions); 'full' re-downloads
missing files by design; 'redownload_year' overwrites a single year.
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote, urlsplit

import requests

from backend.pawchive_scraper import (
    make_session, parse_creator_url, iter_posts, fetch_post, fetch_json,
    parse_post, fetch_creator_name, build_filename, add_index_suffix,
    target_path, site_code, _kind_and_ext,
)
from backend.pawchive_archive import Archive, entry_key, ext_entry_key
from backend.pawchive_links import PawchiveLinks
# Reuse the proven low-level helpers verbatim.
from backend.coomerfans_runner import (
    expected_total, ffprobe_ok, _TRANSIENT_READ_STATUSES,
)


def _url_basename(url):
    path = urlsplit(url).path
    return unquote(path.rsplit("/", 1)[-1]) or "file"


class _AdaptiveThrottle:
    """Self-tuning request pacer shared by all workers (AIMD).

    Each caller reserves the next time slot, so N workers issue at most one
    request per `interval` overall. The interval adapts: it eases *down* toward
    `floor` on every clean response (probing for the fastest safe rate) and jumps
    *up* toward `ceil` on a 429 (respecting Retry-After), so the whole pool backs
    off together instead of each worker hammering the same wall. Used with a tight
    profile for the rate-limit-sensitive API and a loose one for the file CDN."""

    def __init__(self, interval, floor, ceil):
        self.interval = interval
        self.floor = floor
        self.ceil = ceil
        self._lock = threading.Lock()
        self._next = 0.0   # monotonic time of the next allowed request

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
        # Additive-ish speed-up: ease the interval down toward the floor.
        with self._lock:
            if self.interval > self.floor:
                self.interval = max(self.floor, self.interval * 0.9)

    def on_throttled(self, retry_after):
        # Multiplicative back-off + honor the server's Retry-After for the next slot.
        with self._lock:
            self.interval = min(self.ceil, max(self.interval * 2, 0.5))
            self._next = max(self._next, time.monotonic() + retry_after)


class PawchiveRunner:
    def __init__(self, workers=5, connect_timeout=30, read_timeout=300,
                 chunk_size=1 << 20, max_download_attempts=10):
        self.workers = max(1, int(workers))
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.chunk_size = chunk_size
        self.max_download_attempts = max_download_attempts
        # Two pacers: the API (pawchive.st/api) rate-limits hard, so start tight
        # and adapt; the file CDN (file.pawchive.st) tolerates concurrency, so
        # start effectively unthrottled and only back off if it actually 429s.
        self._api_throttle = _AdaptiveThrottle(0.3, floor=0.12, ceil=5.0)
        self._file_throttle = _AdaptiveThrottle(0.0, floor=0.0, ceil=3.0)

        self.downloaded_count = 0
        self.skipped_count = 0
        self.error_count = 0

        self._running = False
        self._cancel_event = threading.Event()
        self._count_lock = threading.Lock()
        self._fname_lock = threading.Lock()
        self._ext_lock = threading.Lock()
        self._claimed = set()
        self._ext_results = {}      # post_id -> {url: state dict}

        self._on_progress = None
        self._archive = None
        self._links = None
        self._session = None
        self._destination = None
        self._service = None
        self._mode = "full"

    # ── public contract ──────────────────────────────────────
    @property
    def is_running(self):
        return self._running

    def cancel(self):
        self._cancel_event.set()

    def _cancelled(self):
        return self._cancel_event.is_set()

    def run(self, creator_url, destination, mode, archive_path, links_path,
            on_progress, on_complete, on_error, cookies_path=None, year=None):
        self.downloaded_count = self.skipped_count = self.error_count = 0
        self._cancel_event.clear()
        self._claimed.clear()
        self._ext_results = {}
        self._running = True
        self._on_progress = on_progress
        self._destination = destination
        self._mode = mode

        try:
            creator = parse_creator_url(creator_url)
            if not creator:
                on_error({"type": "error", "message": "Invalid pawchive creator URL"})
                on_complete(self._stats(cancelled=False))
                return
            self._service = creator["service"]
            self._user_id = creator["user_id"]
            self._session = make_session(cookies_path=cookies_path)
            self._archive = Archive(archive_path) if archive_path else None
            self._links = PawchiveLinks(links_path) if links_path else None
            if self._links:
                # Batch manifest writes: mutate in memory during the crawl and
                # flush once, instead of hammering the NAS once per post.
                self._links.autosave = False

            name = fetch_creator_name(self._session, self._service, self._user_id)
            if name:
                self._info(f"Creator: {name} ({site_code(self._service)})")

            media_jobs, ext_jobs, posts = self._crawl(year)
            if self._cancelled():
                self._info("Cancelled during crawl.")
                self._finish(on_complete)
                return

            self._info(f"{len(media_jobs)} media item(s) + {len(ext_jobs)} direct "
                       f"external file(s) to fetch ({self.skipped_count} already present).")
            self._download_all(media_jobs + ext_jobs)
            if not self._cancelled():
                self._reconcile(media_jobs)
            self._finalize_manifest(posts)

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
        self._info("Crawling creator posts...")

        def on_page(page, n):
            self._info(f"Page {page}: {n} post(s)")

        media_jobs, ext_jobs, posts = [], [], {}
        yr = int(year) if year else None

        for raw in iter_posts(self._session, self._service, self._user_id,
                              on_page=on_page, should_cancel=self._cancelled,
                              fetch=self._fetch_json):
            if self._cancelled():
                break
            post = parse_post(raw)
            # Some listing entries aren't fully imported; fetch detail so we never
            # miss attachments/links.
            if not post["detail_fetched"]:
                full = self._read_post(post["post_id"])
                if full is not None:
                    post = parse_post(full)

            if yr is not None and (not post["dt"] or post["dt"].year != yr):
                continue
            if (self._mode == "latest" and self._archive
                    and self._archive.post_seen(post["post_id"])):
                continue

            posts[post["post_id"]] = post
            for idx, m in enumerate(post["media"], 1):
                media_jobs.append({
                    "jobkind": "media",
                    "post_id": post["post_id"],
                    "index": idx,
                    "entry": entry_key(post["post_id"], idx),
                    "media_kind": m["kind"],
                    "url": m["url"],
                    "name": m["name"],
                    "dt": post["dt"],
                })
            ei = 0
            for l in post["external_links"]:
                if l["kind"] == "direct":
                    ei += 1
                    ext_jobs.append({
                        "jobkind": "ext",
                        "post_id": post["post_id"],
                        "index": ei,
                        "entry": ext_entry_key(post["post_id"], ei),
                        "url": l["url"],
                        "dt": post["dt"],
                    })
            # Record the post's text + all links now (direct ones start pending;
            # _finalize_manifest upgrades them to grabbed/failed after downloads).
            if self._links:
                self._links.upsert_post(post, {})

        # One NAS write for the whole crawl (autosave is off during the loop).
        if self._links:
            try:
                self._links.save()
            except OSError as e:
                self._info(f"manifest write note: {e}")
        return media_jobs, ext_jobs, posts

    def _fetch_json(self, session, url):
        """Resilient listing fetch: transient server errors back off & retry so a
        blip never truncates pagination (which would silently lose later pages)."""
        data = self._request_with_retry(lambda: fetch_json(session, url),
                                        f"listing {url}", attempts=10)
        return data

    def _read_post(self, post_id):
        return self._request_with_retry(
            lambda: fetch_post(self._session, self._service, self._user_id, post_id),
            f"post {post_id}", attempts=6)

    @staticmethod
    def _retry_after(resp, default):
        """Seconds to wait from a Retry-After header, else `default` (clamped)."""
        try:
            ra = (resp.headers.get("Retry-After") or "").strip() if resp is not None else ""
            if ra.isdigit():
                return max(1, min(int(ra), 300))
        except Exception:
            pass
        return default

    def _request_with_retry(self, func, what, attempts, backoff=3):
        b = backoff
        for attempt in range(1, attempts + 1):
            if self._cancelled():
                return None
            self._api_throttle.wait(self._cancelled)
            if self._cancelled():
                return None
            try:
                result = func()
                self._api_throttle.on_success()   # clean → probe a bit faster
                return result
            except requests.HTTPError as e:
                resp = getattr(e, "response", None)
                status = getattr(resp, "status_code", None)
                if status not in _TRANSIENT_READ_STATUSES:
                    self._error(f"{what}: HTTP {status} (not retriable)")
                    return None
                wait_s = self._retry_after(resp, b)
                if status == 429:
                    self._api_throttle.on_throttled(wait_s)   # back the whole pool off
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
    def _download_all(self, jobs):
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futures = [ex.submit(self._process_job, job) for job in jobs]
            for _ in as_completed(futures):
                pass

    def _process_job(self, job):
        if self._cancelled():
            return
        try:
            if job["jobkind"] == "media":
                self._process_media(job)
            else:
                self._process_ext(job)
        except Exception as e:
            self._bump("error")
            self._error(f"job failed ({job.get('url')}): {e}")

    def _process_media(self, job):
        entry = job["entry"]
        redownload = self._mode == "redownload_year"
        recorded = self._archive.get_filename(entry) if self._archive else None
        if recorded:
            prev = target_path(self._destination, job["media_kind"], job["dt"], recorded)
            if os.path.isfile(prev) and not redownload:
                self._bump("skip")
                return
            self._download_stream(job["url"], prev, entry, job, recorded,
                                  job["media_kind"] == "video")
            return
        dest_path, filename = self._assign_path(job["dt"], job["media_kind"], job["name"])
        if os.path.isfile(dest_path) and not redownload:
            self._bump("skip")
            self._record(entry, job, filename, job["media_kind"])
            return
        self._download_stream(job["url"], dest_path, entry, job, filename,
                              job["media_kind"] == "video")

    def _process_ext(self, job):
        """Auto-grab a single direct external file; record its outcome so the
        manifest can surface any failure for manual download."""
        entry = job["entry"]
        name = _url_basename(job["url"])
        kind, _ext = _kind_and_ext(name)
        redownload = self._mode == "redownload_year"

        recorded = self._archive.get_filename(entry) if self._archive else None
        if recorded and not redownload:
            prev = target_path(self._destination, kind, job["dt"], recorded)
            if os.path.isfile(prev):
                self._bump("skip")
                self._set_ext_state(job, "grabbed", filename=recorded)
                return

        dest_path, filename = self._assign_path(job["dt"], kind, name)
        ok = self._download_stream(job["url"], dest_path, entry, job, filename,
                                   kind == "video", bounded=True)
        if ok:
            self._set_ext_state(job, "grabbed", filename=filename)
        elif not self._cancelled():
            self._set_ext_state(job, "failed",
                                note=f"auto-download failed; intended name: {filename}")

    def _set_ext_state(self, job, status, filename=None, note=None):
        st = {"status": status}
        if filename:
            st["filename"] = filename
        if note:
            st["note"] = note
        with self._ext_lock:
            self._ext_results.setdefault(job["post_id"], {})[job["url"]] = st

    def _assign_path(self, dt, media_kind, original_name):
        """Collision-free '<date> - SITE - <name>' path; '_n' before the ext on
        clash so nothing is ever overwritten."""
        base = build_filename(dt, self._service, original_name)
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

    def _download_stream(self, url, dest_path, entry, job, filename, is_video,
                         bounded=False):
        """Stream to <dest>.part with HTTP Range resume + size/ffprobe verify,
        then atomically finalize. Retries transient/network errors with backoff;
        `bounded` caps attempts (used for external files, which shouldn't hang a
        run) whereas on-site media retries up to max_download_attempts. Returns
        True on success, False on give-up/cancel."""
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        part = dest_path + ".part"
        attempt = 0
        backoff = 5
        cap = self.max_download_attempts

        while not self._cancelled():
            attempt += 1
            if bounded and attempt > cap:
                return False   # external files shouldn't hang a run indefinitely
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

                    if r.status_code in (401, 403, 404, 410):
                        self._error(f"external file gone (HTTP {r.status_code}): {url}")
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
                        if bounded and attempt >= cap:
                            self._error(f"expected a file, got HTML: {url}")
                            return False
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

                size = os.path.getsize(part) if os.path.exists(part) else 0
                if total is not None and size < total:
                    self._info(f"Incomplete ({size}/{total}) for {filename}, resuming...")
                    self._sleep_cancellable(2)
                    continue
                if total is not None and size > total:
                    self._discard(part)
                    continue

                self._file_throttle.on_success()   # clean transfer → ease back up
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

            if bounded and attempt >= cap:
                return False
        return False

    def _finalize(self, part, dest_path, entry, job, filename, expected_size, is_video):
        if is_video and not ffprobe_ok(part):
            self._discard(part)
            self._info(f"ffprobe validation failed for {filename}, re-downloading")
            return False
        os.replace(part, dest_path)
        self._bump("download")
        media_kind = job.get("media_kind") or ("video" if is_video else "image")
        self._record(entry, job, filename, media_kind, expected_size)
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
    def _reconcile(self, media_jobs):
        missing = [j for j in media_jobs if not self._job_present(j)]
        total = len(media_jobs)
        if not missing:
            self._info(f"Verified {total}/{total} media item(s) present on disk.")
            return
        self._info(f"Reconciling {len(missing)} missing item(s)...")
        for job in missing:
            if self._cancelled():
                break
            self._process_media(job)
        present = sum(1 for j in media_jobs if self._job_present(j))
        self._info(f"Verified {present}/{total} media item(s) present on disk.")

    def _job_present(self, job):
        fn = self._archive.get_filename(job["entry"]) if self._archive else None
        if not fn:
            return False
        return os.path.isfile(target_path(self._destination, job["media_kind"],
                                          job["dt"], fn))

    # ── manifest finalize ────────────────────────────────────
    def _finalize_manifest(self, posts):
        """Re-record posts whose direct links were attempted, so grabbed/failed
        statuses land in the manifest (upsert preserves user 'resolved' flags)."""
        if not self._links:
            return
        with self._ext_lock:
            results = dict(self._ext_results)
        for pid, states in results.items():
            post = posts.get(pid)
            if post is None:
                continue
            self._links.upsert_post(post, states)
        try:
            self._links.save()
        except OSError as e:
            self._info(f"manifest write note: {e}")

    # ── helpers ──────────────────────────────────────────────
    def _record(self, entry, job, filename, media_kind, expected_size=None):
        if self._archive:
            year = f"{job['dt']:%Y}" if job["dt"] else "unknown"
            self._archive.record(entry, job["post_id"], filename, media_kind, year,
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
