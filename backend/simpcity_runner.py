"""Download engine for a tracked SimpCity thread (Creator tab, Real creators).

One run = walk the thread's pages (sequential, paced — SimpCity rate-limits
hard), and for every post:
  * download each embedded media item (turbo.cr videos, goonbox images, forum
    attachments, other direct images/videos) into
        <dest>/Unsorted/<year>/YYYY.MM.DD - Simpcity - <original name>
        <dest>/Unsorted/Images/<year>/…           (images)
    dated by the post the item first appeared in;
  * record every other link (bunkr / gofile / … albums and files, anything
    else) in the thread DB's review list.

Modes (same names as every other runner):
  full     walk every page; re-fetch anything recorded but missing on disk
  latest   resume from the last page walked (re-read: new posts land there)
  errors   retry failed media without walking
A thread with no walk recorded yet is always walked from page 1.

The walk stops — without marking anything — when the session is not signed in
(guests get 403 on threads); the run reports `needs_simpcity_auth` so the UI
can send the user to Settings ▸ SimpCity.
"""

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

from backend import cyberdrop_host as cdh
from backend import goonbox_host as gb
from backend import redgifs_host as rg
from backend import http_download as hd
from backend import simpcity_scraper as sc
from backend import turbo_host as th
from backend.coomerfans_scraper import target_path
from backend.download_errors import FailureStore
from backend.pawchive_scraper import add_index_suffix, sanitize_filename
from backend.rate_limit import AdaptiveThrottle
from backend.simpcity_archive import ThreadDB
from backend import unsorted_layout as layout

SITE_LABEL = "Simpcity"

# Page pacing. SimpCity sits behind DDoS-Guard and rate-limits page loads.
PAGE_INTERVAL = 3.0
PAGE_FLOOR = 2.0
PAGE_CEIL = 90.0
_TRANSIENT = (408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524)


class NeedsAuth(Exception):
    """The SimpCity session is missing/expired (or the forum shut us out)."""


def parse_post_date(iso):
    """ISO date (UTC) → local datetime, or None."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def build_name(dt, original, label=SITE_LABEL):
    """'2026.08.23 - Simpcity - clip.mp4'."""
    date = f"{dt:%Y.%m.%d}" if dt else "0000.00.00"
    return f"{date} - {label} - {sanitize_filename(original)}"


def _name_from_url(url, default):
    try:
        name = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    except Exception:
        name = ""
    return name or default


def attachment_name(url, ref):
    """'/attachments/clip-mp4.123/' → 'clip.mp4' (XenForo slugs the filename)."""
    m = re.search(r"/attachments/([^/]*?)\.(\d+)/?$", urlsplit(url).path)
    slug = m.group(1) if m else ""
    name = re.sub(r"-(\w{2,4})$", r".\1", slug) if slug else ""
    return name or f"{ref}.jpg"


class SimpcityRunner:
    def __init__(self, workers=3):
        self.workers = max(1, int(workers or 1))
        self.downloaded_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self._running = False
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._skip_ids = set()
        self._claimed = set()
        self._needs_auth = False
        self._on_progress = None
        self._on_error = None

    # ── public contract ─────────────────────────────────────────────

    @property
    def is_running(self):
        return self._running

    def cancel(self):
        self._cancel.set()

    def request_skip(self, entry):
        if entry:
            with self._lock:
                self._skip_ids.add(entry)

    def run(self, link, destination, mode, archive_path, on_progress, on_complete, on_error,
            errors_path=None, cookies_path=None, user_agent=None, include_images=True,
            include_videos=True, collect_links=True, session=None, page_interval=None):
        self._running = True
        self._cancel.clear()
        self._on_progress, self._on_error = on_progress, on_error
        self.downloaded_count = self.skipped_count = self.error_count = 0
        self._needs_auth = False
        self._queued = set()
        self._claimed = set()
        self._include = {"image": bool(include_images), "video": bool(include_videos)}
        self._collect_links = bool(collect_links)
        self._root = layout.root_of(destination)
        self._db = None
        self._store = None
        new_links = []
        try:
            thread = sc.parse_thread_url(link.get("url") or "")
            if not thread:
                raise ValueError(f"not a SimpCity thread URL: {link.get('url')}")
            self._thread = thread
            self._db = ThreadDB(archive_path)
            self._store = FailureStore(errors_path) if errors_path else None
            if session is None:
                if not (cookies_path and os.path.isfile(cookies_path)):
                    raise NeedsAuth("Not signed in to SimpCity — Settings ▸ SimpCity ▸ Sign in")
                session = sc.make_session(cookies_path=cookies_path, user_agent=user_agent)
            self._session = session
            self._turbo = th.make_session()
            self._goon = gb.GoonboxAPI()
            self._dl_sessions = threading.local()
            self._throttle = AdaptiveThrottle(page_interval or PAGE_INTERVAL,
                                              min(PAGE_FLOOR, page_interval or PAGE_FLOOR),
                                              PAGE_CEIL)

            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                self._pool = pool
                self._futures = []
                if mode == "errors":
                    self._queue_retries(include_gone=True)
                else:
                    new_links = self._walk(mode)
                    if not self._cancel.is_set():
                        # Anything recorded earlier but never finished (a failed
                        # file, a run cancelled mid-download) is retried every run.
                        self._queue_retries(include_gone=(mode == "full"))
                        if mode == "full":
                            self._queue_missing_on_disk()
                for f in self._futures:
                    try:
                        f.result()
                    except Exception as e:
                        self._error(f"download worker crashed: {e}")
            if new_links:
                self._info(f"{len(new_links)} new link(s) to review")
        except NeedsAuth as e:
            self._needs_auth = True
            self._error(str(e))
        except Exception as e:
            self.error_count += 1
            self._error(f"SimpCity run failed: {e}")
        finally:
            for res in (self._store, self._db):
                if res is not None:
                    try:
                        res.close()
                    except Exception:
                        pass
            self._running = False
            on_complete(self._stats())

    # ── walking ─────────────────────────────────────────────────────

    def _fetch_page(self, page):
        url = sc.page_url(self._thread["canonical"], page)
        backoff = 10
        for attempt in range(1, 7):
            self._throttle.wait(self._cancel.is_set)
            if self._cancel.is_set():
                return None, url
            try:
                r = self._session.get(url, timeout=(20, 60))
            except Exception as e:
                self._info(f"page {page}: {e.__class__.__name__} — retrying")
                self._sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            if r.status_code == 200:
                flag = sc.logged_in_flag(r.text)
                if flag is False:
                    raise NeedsAuth("SimpCity signed you out — Settings ▸ SimpCity ▸ Sign in again")
                self._throttle.on_success()
                return r.text, url
            if r.status_code == 404:
                return "", url          # past the end / thread gone
            if r.status_code in (401, 403):
                raise NeedsAuth(f"SimpCity refused the thread (HTTP {r.status_code}) — "
                                "sign in again at Settings ▸ SimpCity (or the forum is in maintenance)")
            if r.status_code in _TRANSIENT:
                ra = r.headers.get("Retry-After") or ""
                wait = float(ra) if ra.strip().isdigit() else backoff
                self._throttle.on_throttled(wait)
                self._info(f"page {page}: HTTP {r.status_code} — slowing down ({int(wait)}s)")
                backoff = min(backoff * 2, 120)
                continue
            raise RuntimeError(f"page {page}: HTTP {r.status_code}")
        raise RuntimeError(f"page {page}: still failing after retries")

    def _walk(self, mode):
        db = self._db
        start = 1
        walked_before = db.get("last_walk")
        if mode == "latest" and walked_before:
            start = max(1, int(db.get("last_page") or 1))
        page, last, new_posts, new_links = start, None, 0, []
        self._info("Reading the thread" + (f" from page {start}" if start > 1 else "") + "…")
        while not self._cancel.is_set():
            html, url = self._fetch_page(page)
            if html is None:
                break
            if html == "":
                if page == 1:
                    raise RuntimeError("thread not found (HTTP 404)")
                break
            parsed = sc.parse_page(html, base_url=url)
            if parsed["title"]:
                db.set(title=parsed["title"])
            last = max(parsed["last_page"] or page, page)
            if parsed["page"] and parsed["page"] < page:
                break                        # XenForo bounced us back: past the end
            for post in parsed["posts"]:
                fresh = not db.post_known(post["post_id"])
                n_media, n_links = self._handle_post(post, page, new_links)
                db.record_post(post["post_id"], page, post["date"], n_media, n_links)
                new_posts += fresh
            db.set(last_page=page, pages_total=last)
            self._info(f"page {page}/{last}: {len(parsed['posts'])} posts")
            if page >= last:
                break
            page += 1
        if not self._cancel.is_set():
            db.set(last_walk=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._info(f"Thread read: {new_posts} new post(s)")
        return new_links

    def _handle_post(self, post, page, new_links):
        n_media = n_links = 0
        for item in post["items"]:
            if item["kind"] == "media":
                n_media += 1
                key = self._db.note_media(item, post["post_id"], post["date"], page)
                row = self._db.media(key)
                if row and row["status"] in ("new", "failed"):
                    self._submit(row)
                elif row and row["status"] == "downloaded":
                    if self._file_present(row):
                        self.skipped_count += 1
                    else:
                        self._submit(row)
            else:
                n_links += 1
                if self._collect_links and self._db.upsert_link(
                        item, post["post_id"], post["date"], post["url"], page):
                    new_links.append(item)
        return n_media, n_links

    # ── queueing ────────────────────────────────────────────────────

    def _submit(self, row):
        key = row["key"]
        with self._lock:
            if key in self._queued:
                return
            self._queued.add(key)
        self._futures.append(self._pool.submit(self._download, row))

    def _queue_retries(self, include_gone):
        statuses = ("new", "failed", "gone") if include_gone else ("new", "failed")
        for row in self._db.media_by_status(*statuses):
            self._submit(row)

    def _queue_missing_on_disk(self):
        for row in self._db.media_by_status("downloaded"):
            if not self._file_present(row):
                self._submit(row)

    def _file_present(self, row):
        """Recorded file still on disk — also when it was moved to its canonical
        Unsorted spot by hand (the record is re-pointed, nothing re-downloaded)."""
        rel = layout.locate(self._root, row.get("path_key"))
        if rel and rel != row.get("path_key"):
            self._db.set_path_key(row["key"], rel)
            row["path_key"] = rel
        return bool(rel) or layout.legacy_present(self._root, row.get("path_key"))

    # ── downloading one item ────────────────────────────────────────

    def _entry(self, key):
        return f"simpcity_{self._thread['thread_id']}_{key}"

    def _download(self, row):
        if self._cancel.is_set():
            return
        key, kind = row["key"], (row.get("media") or "image")
        if not self._include.get(kind, True):
            return                                    # fetch prefs: not wanted
        if row["status"] == "downloaded" and self._file_present(row):
            with self._lock:
                self.skipped_count += 1
            return
        entry = self._entry(key)
        dt = parse_post_date(row.get("post_date"))
        try:
            info = self._resolve(row)
        except (th.TurboGone, gb.GoonboxGone, cdh.CyberdropGone, rg.RedgifsGone, hd.Gone):
            self._gone(row, entry, None, "removed from the host")
            return
        except Exception as e:
            self._failed(row, entry, None, f"couldn't resolve: {e}")
            return
        if info.get("media") and info["media"] != kind:
            kind = info["media"]
            if not self._include.get(kind, True):
                return
        rel, path = self._assign_path(row, kind, dt, info["filename"], info.get("size"))
        if rel is None:
            with self._lock:
                self.skipped_count += 1
            self._db.mark_downloaded(key, os.path.basename(path), self._relkey(path),
                                     info.get("size"), info.get("md5"))
            return
        name = os.path.basename(path)
        self._progress({"type": "dl_start", "id": entry, "message": name})
        try:
            res = hd.download(self._dl_session(row["host"]), info["url"], path,
                              expected_size=info.get("size"), md5=info.get("md5"),
                              is_video=(kind == "video"), refresh=info.get("refresh"),
                              headers=info.get("headers"), cancelled=self._cancel.is_set,
                              skip=lambda: self._skip_requested(entry), on_info=self._info)
        finally:
            self._progress({"type": "dl_stop", "id": entry})
            with self._lock:
                self._claimed.discard(path.lower())
        if res.ok:
            self._db.mark_downloaded(key, name, rel, res.size, info.get("md5"))
            if self._store:
                self._store.clear_failure(entry)
            with self._lock:
                self.downloaded_count += 1
            self._progress({"type": "download", "message": name, "path": path})
        elif res.status == "gone":
            self._gone(row, entry, name, res.reason)
        elif res.status == "skipped":
            self._db.mark_skipped(key)
            with self._lock:
                self.skipped_count += 1
            self._info(f"Skipped (won't re-download): {name}")
        elif res.status == "failed":
            self._failed(row, entry, name, res.reason, info.get("url"), res.http_status)

    def _resolve(self, row):
        """→ {url, filename, size?, md5?, media?, refresh?, headers?}."""
        host, ref, url = row["host"], row["ref"], row["url"]
        if host == "turbo":
            r = th.resolve(self._turbo, ref)

            def refresh():
                try:
                    return th.sign(self._turbo, ref)[0]
                except th.TurboGone:
                    raise hd.Gone()
            return {"url": r["url"], "filename": r["filename"] or f"{ref}.mp4", "size": r["size"],
                    "md5": r["md5"], "refresh": refresh}
        if host == "goonbox" and not ref.startswith("/"):
            img = self._goon.image(ref)
            mime = (img.get("mime") or "").lower()
            return {"url": img["url"], "filename": img["filename"], "size": img.get("size"),
                    "media": "video" if mime.startswith("video/") else "image"}
        if host == "cyberdrop":
            r = cdh.resolve(self._turbo, ref)

            def refresh_cd():
                try:
                    return cdh.sign(self._turbo, ref)
                except cdh.CyberdropGone:
                    raise hd.Gone()
            return {"url": r["url"], "filename": r["filename"], "size": r["size"],
                    "media": "image" if r["mime"].startswith("image/") else "video",
                    "refresh": refresh_cd}
        if host == "redgifs":
            r = rg.resolve(self._turbo, ref)
            return {"url": r["url"], "filename": r["filename"], "headers": rg.DOWNLOAD_HEADERS}
        if host == "twitter":
            fmt = re.search(r"format=(\w+)", url)
            return {"url": url, "filename": f"{ref}.{fmt.group(1) if fmt else 'jpg'}"}
        if host == "simpcity" and ref.startswith("att"):
            return {"url": url, "filename": attachment_name(url, ref)}
        return {"url": url, "filename": _name_from_url(url, f"{ref[-16:]}.bin")}

    def _dl_session(self, host):
        if host == "simpcity":
            return self._session
        s = getattr(self._dl_sessions, "s", None)
        if s is None:
            s = self._dl_sessions.s = th.make_session()
        return s

    def _relkey(self, path):
        return os.path.relpath(path, self._root).replace("\\", "/")

    def _assign_path(self, row, kind, dt, original, size):
        """(relkey, abs path) for a new download; (None, path) when an identical
        file is already on disk under that name (adopted, nothing to fetch).
        A recorded-but-missing file is re-fetched under its recorded name."""
        if row.get("path_key"):
            path = os.path.join(self._root, row["path_key"])
            with self._lock:
                self._claimed.add(path.lower())
            return row["path_key"], path
        base = build_name(dt, original)
        taken = self._db.filenames_taken()
        n = 1
        with self._lock:
            while True:
                name = base if n == 1 else add_index_suffix(base, n)
                path = target_path(self._root, kind, dt, name)
                rel = self._relkey(path)
                if path.lower() in self._claimed or rel in taken:
                    n += 1
                    continue
                if os.path.exists(path):
                    if size and os.path.getsize(path) == size:
                        return None, path    # same file already there → adopt
                    n += 1
                    continue
                self._claimed.add(path.lower())
                return rel, path

    def _gone(self, row, entry, name, reason):
        self._db.mark_failed(row["key"], reason, gone=True)
        if self._store:
            self._store.record_failure(entry, platform="simpcity", post_id=row.get("post_id"),
                                       filename=name, url=row.get("url"),
                                       page_url=f"{sc.BASE}/posts/{row.get('post_id')}/",
                                       media_kind=row.get("media"), reason=reason)
            self._store.mark_gone(entry)
        with self._lock:
            self.error_count += 1
        self._error(f"Gone ({reason}): {name or row.get('url')}")

    def _failed(self, row, entry, name, reason, url=None, status=None):
        self._db.mark_failed(row["key"], reason)
        if self._store:
            self._store.record_failure(entry, platform="simpcity", post_id=row.get("post_id"),
                                       filename=name, url=url or row.get("url"),
                                       page_url=f"{sc.BASE}/posts/{row.get('post_id')}/",
                                       media_kind=row.get("media"), status=status, reason=reason)
        with self._lock:
            self.error_count += 1
        self._error(f"Failed ({reason}): {name or row.get('url')}")

    # ── helpers ─────────────────────────────────────────────────────

    def _skip_requested(self, entry):
        with self._lock:
            return entry in self._skip_ids

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._cancel.is_set():
            time.sleep(0.2)

    def _progress(self, d):
        if self._on_progress:
            self._on_progress(d)

    def _info(self, message):
        self._progress({"type": "info", "message": message})

    def _error(self, message):
        if self._on_error:
            self._on_error({"type": "error", "message": message})

    def _stats(self):
        return {"downloaded": self.downloaded_count, "skipped": self.skipped_count,
                "errors": self.error_count, "cancelled": self._cancel.is_set(),
                "needs_simpcity_auth": self._needs_auth}
