"""Download engine for a Discord channel.

Mirrors CoomerfansRunner/PawchiveRunner/DerpibooruRunner's public contract
(run/cancel/is_running + request_skip + the downloaded/skipped/error counters) so
backend.creator_runner can drive it the same way.

It's a JSON-API engine like derpibooru, but message-oriented:

  * The crawl pages a channel's message history newest-first
    (GET /channels/{id}/messages?before=...), pulling each message's
    *attachments* and Discord-hosted *embed media* as downloadable files, and
    recording *external links* (mega/drive/twitter/...) it can't fetch directly
    into a per-channel manifest for manual download (mirrors pawchive).
  * Auth is a single token (bot or user) in the Authorization header, threaded in
    from settings. File downloads hit the CDN on a separate, *unauthenticated*
    session — the token is never sent to the CDN.
  * Attachment CDN URLs are signed and expire (~24h), but they're freshly signed
    when the message is fetched, so a straight resumable stream works. A stale
    URL is handled by 'Redownload Errors' (a full re-crawl re-fetches fresh URLs).
  * files preserve the original attachment name, e.g.
    "2026.05.05 - Discord - cool_render.png".

Curation rule (see the other engines): 'latest' stops once the crawl reaches a
message already downloaded (never resurrects deletions); 'full' re-downloads
missing files by design; 'redownload_year' ("Download Year") pages only that
year's messages (snowflake-bounded), skipping anything already present.
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from backend.discord_scraper import (
    make_session, parse_creator_url, iter_messages, parse_message,
    fetch_json, channel_name, message_url, build_filename, add_index_suffix,
    target_path, site_code, year_bounds,
)
from backend.discord_archive import Archive
from backend.download_errors import FailureStore
from backend.pawchive_links import PawchiveLinks
# Reuse the proven low-level helpers verbatim.
from backend.coomerfans_runner import (
    expected_total, ffprobe_ok, _TRANSIENT_READ_STATUSES,
)

# Videos are the heavy transfers; run them in their own small pool so a burst of
# big mp4/webm files can't hog every worker or hammer the CDN.
_LARGE_KINDS = ("video",)

# Flush the external-links manifest every N messages so the "Links needing
# attention" panel fills in progressively during a long crawl.
_MANIFEST_FLUSH_EVERY = 50


class DiscordRunner:
    def __init__(self, workers=5, connect_timeout=30, read_timeout=300,
                 chunk_size=1 << 20, max_download_attempts=10, large_workers=2):
        self.workers = max(1, int(workers))
        self.large_workers = max(1, int(large_workers))
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.chunk_size = chunk_size
        self.max_download_attempts = max_download_attempts

        self.downloaded_count = 0
        self.skipped_count = 0
        self.error_count = 0

        self._running = False
        self._cancel_event = threading.Event()
        self._count_lock = threading.Lock()
        self._fname_lock = threading.Lock()
        self._claimed = set()

        # Per-file skip: entry keys the user asked to abandon mid-run.
        self._skip_lock = threading.Lock()
        self._skip_ids = set()

        self._on_progress = None
        self._archive = None
        self._errors = None
        self._links = None
        self._session = None       # authenticated API session (has the token)
        self._file_session = None  # unauthenticated CDN session (no token)
        self._destination = None
        self._guild_id = None
        self._channel_id = None
        self._mode = "full"
        self._auth_failed = False

    # ── public contract ──────────────────────────────────────
    @property
    def is_running(self):
        return self._running

    def cancel(self):
        self._cancel_event.set()

    def _cancelled(self):
        return self._cancel_event.is_set()

    def request_skip(self, entry):
        with self._skip_lock:
            self._skip_ids.add(entry)

    def _skip_requested(self, entry):
        with self._skip_lock:
            return entry in self._skip_ids

    def run(self, creator_url, destination, mode, archive_path,
            on_progress, on_complete, on_error,
            token=None, token_type="user", year=None,
            errors_path=None, links_path=None):
        self.downloaded_count = self.skipped_count = self.error_count = 0
        self._cancel_event.clear()
        self._claimed.clear()
        with self._skip_lock:
            self._skip_ids.clear()
        self._running = True
        self._auth_failed = False
        self._on_progress = on_progress
        self._destination = destination
        self._mode = mode

        try:
            creator = parse_creator_url(creator_url)
            if not creator:
                on_error({"type": "error", "message": "Invalid Discord channel URL"})
                on_complete(self._stats(cancelled=False))
                return
            if not token:
                on_error({"type": "error",
                          "message": "No Discord token set — add one in Settings."})
                on_complete(self._stats(cancelled=False))
                return
            self._guild_id = creator["guild_id"]
            self._channel_id = creator["channel_id"]
            self._session = make_session(token=token, token_type=token_type)
            self._file_session = make_session(token=None)
            self._archive = Archive(archive_path) if archive_path else None
            self._errors = FailureStore(errors_path) if errors_path else None
            if links_path:
                self._links = PawchiveLinks(links_path)
                self._links.autosave = False   # one NAS write per batch, not per message

            name = channel_name(self._session, self._channel_id) or f"#{self._channel_id}"
            self._info(f"Channel: {name}  ({site_code()} — "
                       f"{(token_type or 'user').lower()} token)")

            jobs = self._crawl(year)
            if self._cancelled():
                self._info("Cancelled during crawl.")
                self._finish(on_complete)
                return
            if self._auth_failed:
                self._finish(on_complete)
                return

            self._info(f"{len(jobs)} file(s) to fetch "
                       f"({self.skipped_count} already present).")
            self._download_all(jobs)
            if not self._cancelled():
                self._reconcile(jobs)
            self._save_links(final=True)

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

    # ── crawl + build jobs ───────────────────────────────────
    def _crawl(self, year):
        self._info("Reading channel history...")

        def on_page(page, n):
            self._info(f"Page {page}: {n} message(s)")

        before = after_bound = None
        yr = int(year) if year else None
        if yr is not None:
            after_bound, before = year_bounds(yr)

        jobs = []
        posts_since_flush = 0
        for raw in iter_messages(self._session, self._channel_id,
                                 on_page=on_page, should_cancel=self._cancelled,
                                 fetch=self._fetch_json,
                                 before=before, after_bound=after_bound):
            if self._cancelled() or self._auth_failed:
                break
            msg = parse_message(raw)
            if yr is not None and (not msg["dt"] or msg["dt"].year != yr):
                continue

            # 'latest' tops up: once we reach a message already downloaded, the
            # rest of history (older) is already known — stop the crawl.
            if (self._mode == "latest" and self._archive
                    and self._archive.post_seen(msg["message_id"])):
                break

            # Record this message's external links in the manifest.
            if self._links and msg["links"]:
                self._links.upsert_post(self._as_post(msg), {})
                posts_since_flush += 1
                if posts_since_flush >= _MANIFEST_FLUSH_EVERY:
                    posts_since_flush = 0
                    self._save_links()

            for item in msg["media"]:
                entry = item["entry"]
                if self._archive and self._archive.is_skipped(entry):
                    continue                            # user skipped it before
                if (self._mode == "latest" and self._archive
                        and self._archive.has(entry)):
                    continue
                jobs.append({
                    "entry": entry,
                    "message_id": msg["message_id"],
                    "url": item["url"],
                    "filename": item["filename"],
                    "ext": item["ext"],
                    "media_kind": item["media_kind"],
                    "dt": msg["dt"],
                    "page_url": message_url(self._guild_id, self._channel_id,
                                            msg["message_id"]),
                })
        return jobs

    def _as_post(self, msg):
        """Shape a parsed message as a PawchiveLinks 'post' (the manifest store is
        platform-agnostic; service='discord' gives the right filename prefix)."""
        content = msg.get("content") or ""
        title = content.strip().splitlines()[0][:120] if content.strip() else ""
        return {
            "post_id": msg["message_id"],
            "external_links": msg["links"],
            "content_html": content,
            "dt": msg["dt"],
            "service": "discord",
            "title": title,
            "url": message_url(self._guild_id, self._channel_id, msg["message_id"]),
        }

    def _save_links(self, final=False):
        if not self._links:
            return
        try:
            if final:
                # Preserve checkboxes the user ticked mid-crawl before the final write.
                self._links.reload_resolved_from_disk()
            self._links.save()
        except OSError as e:
            self._info(f"links manifest note: {e}")

    # ── API fetch with retry / 429 handling ──────────────────
    def _fetch_json(self, session, url):
        return self._request_with_retry(
            lambda u: fetch_json(session, u), url, "messages", attempts=8)

    @staticmethod
    def _retry_after(resp, default):
        """Seconds to wait from a 429/Retry-After, capped. Discord puts the value
        in the JSON body ('retry_after', float seconds) and/or the header."""
        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("retry_after") is not None:
                return max(1, min(int(float(body["retry_after"]) + 1), 300))
        except Exception:
            pass
        try:
            ra = (resp.headers.get("Retry-After") or "").strip()
            if ra:
                return max(1, min(int(float(ra) + 1), 300))
        except Exception:
            pass
        return default

    def _request_with_retry(self, call, url, what, attempts, backoff=3):
        b = backoff
        for attempt in range(1, attempts + 1):
            if self._cancelled() or self._auth_failed:
                return None
            try:
                result = call(url)
                return result
            except requests.HTTPError as e:
                resp = getattr(e, "response", None)
                status = getattr(resp, "status_code", None)
                if status in (401, 403):
                    self._auth_failed = True
                    self._error(f"HTTP {status}: token rejected or no access to this "
                                "channel — check the token and its permissions "
                                "(View Channel + Read Message History; bots need the "
                                "Message Content intent).")
                    return None
                if status == 404:
                    self._auth_failed = True
                    self._error("HTTP 404: channel not found (wrong URL, or the token "
                                "can't see it).")
                    return None
                if status == 429:
                    wait_s = self._retry_after(resp, b)
                    if attempt < attempts:
                        self._info(f"{what}: rate limited (429); waiting {wait_s}s "
                                   f"(retry {attempt}/{attempts})")
                        self._sleep_cancellable(wait_s)
                        continue
                    self._error(f"{what}: repeatedly rate limited; giving up")
                    return None
                if status not in _TRANSIENT_READ_STATUSES:
                    self._error(f"{what}: HTTP {status} (not retriable)")
                    return None
                err = f"HTTP {status}"
                wait_s = self._retry_after(resp, b)
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
        if self._skip_requested(entry):
            self._skip_and_remember(job)
            return
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
        # Correctly-named file already on disk (archive cleared, etc.) -> adopt it.
        natural = build_filename(job["dt"], job["filename"])
        natural_path = target_path(self._destination, job["media_kind"], job["dt"], natural)
        with self._fname_lock:
            if natural_path not in self._claimed and os.path.isfile(natural_path):
                self._claimed.add(natural_path)
                self._bump("skip")
                self._record(entry, job, natural, job["media_kind"])
                return
        dest_path, filename = self._assign_path(job)
        self._download_stream(job["url"], dest_path, entry, job, filename,
                              job["media_kind"] == "video")

    def _assign_path(self, job):
        """Collision-free '<date> - Discord - <name>' path; '_n' before the ext on
        clash so nothing is ever overwritten."""
        base = build_filename(job["dt"], job["filename"])
        with self._fname_lock:
            path = target_path(self._destination, job["media_kind"], job["dt"], base)
            if path not in self._claimed and not os.path.isfile(path):
                self._claimed.add(path)
                return path, base
            n = 1
            while True:
                cand = add_index_suffix(base, n)
                cand_path = target_path(self._destination, job["media_kind"], job["dt"], cand)
                if cand_path not in self._claimed and not os.path.isfile(cand_path):
                    self._claimed.add(cand_path)
                    return cand_path, cand
                n += 1

    def _download_stream(self, url, dest_path, entry, job, filename, is_video):
        """Stream to <dest>.part with HTTP Range resume + size/ffprobe verify, then
        atomically finalize. Retries transient/network errors with backoff. Records
        a durable failure on give-up. Returns True on success."""
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        part = dest_path + ".part"
        attempt = 0
        backoff = 5
        last_reason = "unknown"

        while not self._cancelled():
            if self._skip_requested(entry):
                self._discard(part)
                self._skip_and_remember(job)
                return False
            attempt += 1
            try:
                resume_pos = os.path.getsize(part) if os.path.exists(part) else 0
                headers = {}
                open_mode = "wb"
                if resume_pos:
                    headers["Range"] = f"bytes={resume_pos}-"
                    open_mode = "ab"

                with self._file_session.get(
                    url, stream=True, headers=headers,
                    timeout=(self.connect_timeout, self.read_timeout),
                ) as r:
                    total = expected_total(r, resume_pos)

                    if r.status_code == 429:
                        wait_s = self._retry_after(r, backoff)
                        self._info(f"rate limited (429), waiting {wait_s}s: {filename}")
                        self._sleep_cancellable(wait_s)
                        backoff = min(backoff * 2, 120)
                        continue

                    if r.status_code in (500, 502, 503, 504):
                        self._info(f"server busy (HTTP {r.status_code}), backing off "
                                   f"{backoff}s: {filename}")
                        self._sleep_cancellable(backoff)
                        backoff = min(backoff * 2, 120)
                        last_reason = f"HTTP {r.status_code}"
                        if attempt >= self.max_download_attempts:
                            break
                        continue

                    if r.status_code in (401, 403, 404, 410):
                        # A signed URL that expired, or the attachment is gone. A
                        # full re-run re-fetches a fresh URL from the message.
                        last_reason = f"HTTP {r.status_code} (url expired or gone)"
                        self._error(f"file unavailable ({last_reason}): {filename}")
                        self._record_failure(entry, job, filename, r.status_code, last_reason)
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
                        self._sleep_cancellable(backoff)
                        backoff = min(backoff * 2, 120)
                        last_reason = "expected a file, got HTML"
                        if attempt >= self.max_download_attempts:
                            break
                        continue

                    r.raise_for_status()
                    with open(part, open_mode) as f:
                        for chunk in r.iter_content(chunk_size=self.chunk_size):
                            if self._cancelled():
                                return False
                            if self._skip_requested(entry):
                                self._discard(part)
                                self._skip_and_remember(job)
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

                if self._finalize(part, dest_path, entry, job, filename,
                                  total if total is not None else size, is_video):
                    return True
                continue
            except Exception as e:
                if self._cancelled():
                    return False
                last_reason = e.__class__.__name__
                self._info(f"Retry {attempt} for {filename}: {e}")
                self._sleep_cancellable(backoff)
                backoff = min(backoff * 2, 120)
            if attempt >= self.max_download_attempts:
                break
        if not self._cancelled():
            self._error(f"gave up after {attempt} attempt(s): {filename}")
            self._record_failure(entry, job, filename, None, last_reason)
        return False

    def _finalize(self, part, dest_path, entry, job, filename, expected_size, is_video):
        if is_video and not ffprobe_ok(part):
            self._discard(part)
            self._info(f"ffprobe validation failed for {filename}, re-downloading")
            return False
        os.replace(part, dest_path)
        self._bump("download")
        self._record(entry, job, filename, job["media_kind"], expected_size)
        if self._errors:
            self._errors.clear_failure(entry)
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
            if total:
                self._info(f"Verified {total}/{total} file(s) present on disk.")
            return
        self._info(f"Reconciling {len(missing)} missing item(s)...")
        for job in missing:
            if self._cancelled():
                break
            self._process_media(job)
        present = sum(1 for j in jobs if self._job_present(j))
        self._info(f"Verified {present}/{total} file(s) present on disk.")

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
            self._archive.record(entry, job["message_id"], filename, media_kind,
                                 year, expected_size, None)

    def _skip_and_remember(self, job):
        """Honour a per-file skip: mark it in the archive so future runs never
        re-fetch it, and count it as skipped."""
        if self._archive:
            year = f"{job['dt']:%Y}" if job["dt"] else "unknown"
            self._archive.set_skipped(job["entry"], job["message_id"], "",
                                      job["media_kind"], year)
        if self._errors:
            self._errors.clear_failure(job["entry"])
        self._bump("skip")

    def _record_failure(self, entry, job, filename, status, reason):
        if not self._errors:
            return
        self._errors.record_failure(
            entry, platform="discord", filename=filename, url=job.get("url"),
            page_url=job.get("page_url"), media_kind=job.get("media_kind"),
            status=status, reason=reason)

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
