"""Tracked albums for Real creators (bunkr, gofile, cyberdrop, pixeldrain,
filester, turbo.cr and goonbox albums — and one-off single files).

An album can be a creator link (added in Configure, or "Download & track" from
a SimpCity thread's review list) or a one-off "Download" from that list. Either
way its files land in

    <dest>/Unsorted/<year>/YYYY.MM.DD - <Site> - <original name>     (Images/<year> for images)

dated by the host's upload date when it publishes one (bunkr, gofile, turbo,
goonbox), else the date of the post the link came from, else today.

Every album keeps its own record at `<archive_dir>/album_<site>_<id>.db`
(derived from the site + album id only, so re-adding never re-downloads): the
files already fetched (by host file id), the album title, and whether the album
was still there the last time it was checked — albums die, and a dead one keeps
its files and history but is flagged.

Engines:
  gofile                     GofileRunner.list_folder + http_download (4 workers)
  turbo / goonbox            native resolvers + http_download
  bunkr                      native (bunkr_host): album listing with sizes + upload
                             times, at most 2 downloads per CDN node (the 3rd gets a
                             503), cold-storage files warmed and retried later in the
                             same run
  cyberdrop / pixeldrain     cyberdrop-dl into a staging folder, then each finished
                             file is moved to its dated name (cyberdrop-dl can't
                             template filenames)
  filester                   FilesterRunner into staging, same move step
"""

import json
import os
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from backend import album_sites
from backend import bunkr_host as bh
from backend import goonbox_host as gb
from backend import http_download as hd
from backend import turbo_host as th
from backend.download_errors import FailureStore
from backend.pawchive_scraper import add_index_suffix, sanitize_filename
from backend import unsorted_layout as layout

STAGING = ".incoming"
SITE_LABELS = {"bunkr": "Bunkr", "gofile": "Gofile", "cyberdrop": "Cyberdrop",
               "pixeldrain": "Pixeldrain", "filester": "Filester", "turbo": "Turbo",
               "goonbox": "GoonBox"}
CYBERDROP_SITES = ("cyberdrop", "pixeldrain")
COLD_ROUNDS = 3          # bunkr: passes over files that didn't start (cold storage)
COLD_WAIT = 60           # seconds between those passes
TRACKABLE_SITES = tuple(SITE_LABELS)


def site_label(site):
    return SITE_LABELS.get(site, (site or "Album").title())


def album_archive_path(archive_dir, site, album_id, destination):
    archive_dir = (archive_dir or "").strip()
    safe = "".join(ch for ch in str(album_id) if ch.isalnum() or ch in "-_") or "unknown"
    if archive_dir and os.path.isdir(archive_dir):
        return os.path.join(archive_dir, f"album_{site}_{safe}.db")
    return os.path.join(destination, f".album-{site}-{safe}-archive.db")


def canonical_url(site, kind, ref, url):
    """One spelling per album, so the same bunkr album under a rotated domain
    (bunkr.cr / .pk / .si …) dedupes to one creator link."""
    if site == "bunkr":
        return f"https://bunkr.cr/{'a' if kind == 'album' else 'f'}/{ref}"
    if site == "gofile":
        return f"https://gofile.io/d/{ref}"
    if site == "turbo":
        return f"https://turbo.cr/a/{ref}"
    if site == "goonbox":
        return f"https://goonbox.cr/a/{ref}"
    if site == "pixeldrain":
        return f"https://pixeldrain.com/{'l' if kind == 'album' else 'u'}/{ref}"
    return url


def parse_album_url(url):
    """→ {site, kind album|file, ref, url} for a host we have an engine for, else None."""
    from backend.simpcity_scraper import classify
    item = classify(url) if url else None
    if not item or item["kind"] not in ("album", "file") or item["host"] not in TRACKABLE_SITES:
        return None
    return {"site": item["host"], "kind": item["kind"], "ref": item["ref"],
            "url": canonical_url(item["host"], item["kind"], item["ref"], url)}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_local(value, local=False):
    """epoch seconds / ms / ISO string / 'YYYY-MM-DD' → datetime, or None.

    Host upload dates stay in UTC — that is the calendar day bunkr/gofile/turbo
    print next to the file. `local=True` is for SimpCity post dates, which the
    thread runner names in local time."""
    if value in (None, "", 0):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
            v = float(value)
            if v > 1e12:
                v /= 1000.0
            dt = datetime.fromtimestamp(v, timezone.utc)
            return dt.astimezone() if local else dt
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt                      # a bare date: already the host's calendar day
        return dt.astimezone() if local else dt.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


class AlbumGone(Exception):
    pass


# ── per-album record ────────────────────────────────────────────────

class AlbumDB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        with self._lock:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
                CREATE TABLE IF NOT EXISTS files (
                    file_key TEXT PRIMARY KEY, filename TEXT, path_key TEXT,
                    size INTEGER, upload_date TEXT, updated TEXT);
            """)
            self._db.commit()

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass

    def get(self, k, default=None):
        with self._lock:
            r = self._db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set(self, **kv):
        with self._lock:
            for k, v in kv.items():
                self._db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)",
                                 (k, None if v is None else str(v)))
            self._db.commit()

    def file(self, key):
        with self._lock:
            r = self._db.execute("SELECT filename, path_key, size FROM files WHERE file_key=?",
                                 (key,)).fetchone()
        return {"filename": r[0], "path_key": r[1], "size": r[2]} if r else None

    def record(self, key, filename, path_key, size, upload_date=None):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?)",
                             (key, filename, path_key, size, upload_date, _now()))
            self._db.commit()

    def path_keys(self):
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT path_key FROM files")}

    def count(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    def mark_alive(self, alive, title=None):
        """Record a check. A definite "gone" (404/notFound) sets dead_since; a
        check that couldn't reach the host (bunkr times out — 504 — even for
        an album id that never existed, so this can't be called "dead") keeps
        `unreachable_since` + a streak instead, so the UI can say
        "unreachable since …" honestly."""
        now = _now()
        val = "live" if alive is True else "dead" if alive is False else "unknown"
        dead_since = self.get("dead_since")
        unreachable = self.get("unreachable_since")
        streak = int(self.get("fail_streak") or 0)
        if val == "dead":
            dead_since = dead_since or now
            unreachable, streak = None, 0
        elif val == "live":
            dead_since = unreachable = None
            streak = 0
        else:
            unreachable = unreachable or now
            streak += 1
        self.set(alive=val, last_checked=now, dead_since=dead_since,
                 unreachable_since=unreachable, fail_streak=streak)
        if title:
            self.set(title=title)


def read_status(path):
    """Album summary for the UI without creating a DB: {alive, last_checked,
    dead_since, title, file_count, last_run} or None."""
    if not path or not os.path.isfile(path):
        return None
    db = AlbumDB(path)
    try:
        return {"alive": db.get("alive", "unknown"), "last_checked": db.get("last_checked"),
                "dead_since": db.get("dead_since"), "title": db.get("title"),
                "unreachable_since": db.get("unreachable_since"),
                "fail_streak": int(db.get("fail_streak") or 0),
                "file_count": db.count(), "last_run": db.get("last_run")}
    finally:
        db.close()


# ── runner ──────────────────────────────────────────────────────────

class AlbumTrackRunner:
    def __init__(self, workers=4):
        self.workers = max(1, int(workers or 1))
        self.downloaded_count = self.skipped_count = self.error_count = 0
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._skip_ids = set()
        self._running = False
        self._sub = None

    @property
    def is_running(self):
        return self._running

    def cancel(self):
        self._cancel.set()
        sub = self._sub
        if sub is not None:
            try:
                sub.cancel()
            except Exception:
                pass

    def request_skip(self, entry):
        if entry:
            with self._lock:
                self._skip_ids.add(entry)

    def run(self, item, destination, mode, archive_path, on_progress, on_complete, on_error,
            errors_path=None, engine_state_dir=None, origin_date=None, force=False):
        """item = {site, kind, ref, url[, password]}. Always calls on_complete with
        {downloaded, skipped, errors, cancelled, alive, title, file_count}."""
        self._running = True
        self._cancel.clear()
        self.downloaded_count = self.skipped_count = self.error_count = 0
        self._on_progress, self._on_error = on_progress, on_error
        self._item = item
        self._site = item["site"]
        self._label = site_label(self._site)
        self._root = layout.root_of(destination)
        self._origin = _to_local(origin_date, local=True)
        self._claimed = set()
        self._db = AlbumDB(archive_path)
        self._store = FailureStore(errors_path) if errors_path else None
        alive, title = None, None
        try:
            if mode == "errors" or mode == "redownload_year":
                mode = "full"
            site = self._site
            if site == "gofile":
                alive, title = self._run_gofile(force)
            elif site == "turbo":
                alive, title = self._run_turbo()
            elif site == "goonbox":
                alive, title = self._run_goonbox()
            elif site == "bunkr":
                alive, title = self._run_bunkr()
            elif site in CYBERDROP_SITES:
                alive, title = self._run_cyberdrop(engine_state_dir, force)
            elif site == "filester":
                alive, title = self._run_filester()
            else:
                raise ValueError(f"no engine for {site}")
        except AlbumGone as e:
            alive = False
            self._error(f"{self._label} {item['kind']} is gone ({e}) — files already "
                        "downloaded are kept")
        except Exception as e:
            with self._lock:
                self.error_count += 1
            self._error(f"{self._label}: {e}")
        finally:
            if (not self._cancel.is_set() and alive is not False and self.error_count == 0
                    and self.downloaded_count == 0 and self._db.count() == 0):
                # Nothing arrived and nothing is on record: whatever the engine
                # said, that is not a successful download.
                with self._lock:
                    self.error_count += 1
                self._error(f"{self._label}: nothing was downloaded from {item['url']}")
            if not self._cancel.is_set():
                self._db.mark_alive(alive, title)
                self._db.set(last_run=_now())
            count = self._db.count()
            title = title or self._db.get("title")
            for res in (self._db, self._store):
                if res is not None:
                    try:
                        res.close()
                    except Exception:
                        pass
            self._running = False
            on_complete({"downloaded": self.downloaded_count, "skipped": self.skipped_count,
                         "errors": self.error_count, "cancelled": self._cancel.is_set(),
                         "alive": alive, "title": title, "file_count": count})

    # ── naming ──────────────────────────────────────────────────────

    def _target(self, dt, original, size=None):
        """(relkey, abs path) — or (None, path) when an identical file is
        already there under that name (adopted)."""
        dt = dt or self._origin or datetime.now().astimezone()
        base = f"{dt:%Y.%m.%d} - {self._label} - {sanitize_filename(original)}"
        taken = self._db.path_keys()
        n = 1
        with self._lock:
            while True:
                name = base if n == 1 else add_index_suffix(base, n)
                path = layout.target(self._root, dt, name)
                rel = layout.relkey(self._root, path)
                if path.lower() in self._claimed or rel in taken:
                    n += 1
                    continue
                if os.path.exists(path):
                    if size and os.path.getsize(path) == size:
                        return None, path
                    n += 1
                    continue
                self._claimed.add(path.lower())
                return rel, path

    def _have(self, key):
        """True if this host file was fetched before and is still on disk — also
        when it was moved by hand to its canonical Unsorted spot (the record is
        re-pointed so it isn't fetched again)."""
        rec = self._db.file(key)
        if not rec:
            return False
        rel = layout.locate(self._root, rec["path_key"])
        if rel and rel != rec["path_key"]:
            self._db.record(key, rel.rsplit("/", 1)[-1], rel, rec["size"])
        return bool(rel) or layout.legacy_present(self._root, rec["path_key"])

    # ── shared native download step ─────────────────────────────────

    def _fetch(self, key, original, url, size=None, md5=None, dt=None, session=None,
               headers=None, refresh=None, page_url=None, defer=False, attempts=8):
        """Download one host file to its dated name. Returns the outcome:
        ok / skipped / failed / gone / cancelled, or "retry" when `defer` is set and
        it failed — the caller comes back to it later instead of recording an error."""
        if self._cancel.is_set():
            return "cancelled"
        if self._have(key):
            with self._lock:
                self.skipped_count += 1
            return "skipped"
        rec = self._db.file(key)
        if rec and rec["path_key"]:                     # recorded but missing → same name
            rel = rec["path_key"]
            path = os.path.join(self._root, rel)
        else:
            rel, path = self._target(dt, original, size)
            if rel is None:
                self._db.record(key, os.path.basename(path),
                                os.path.relpath(path, self._root).replace("\\", "/"), size,
                                dt.isoformat() if dt else None)
                with self._lock:
                    self.skipped_count += 1
                return "skipped"
        name = os.path.basename(path)
        entry = f"album_{self._site}_{key}"
        self._progress({"type": "dl_start", "id": entry, "message": name})
        try:
            res = hd.download(session, url, path, expected_size=size or None, md5=md5,
                              headers=headers, refresh=refresh, cancelled=self._cancel.is_set,
                              skip=lambda: self._skip_requested(entry), on_info=self._info,
                              max_attempts=attempts)
        finally:
            self._progress({"type": "dl_stop", "id": entry})
            with self._lock:
                self._claimed.discard(path.lower())
        if res.ok:
            self._db.record(key, name, rel, res.size, dt.isoformat() if dt else None)
            if self._store:
                self._store.clear_failure(entry)
            with self._lock:
                self.downloaded_count += 1
            self._progress({"type": "download", "message": name, "path": path})
            return "ok"
        if res.status == "failed" and defer:
            return "retry"
        if res.status in ("failed", "gone"):
            with self._lock:
                self.error_count += 1
            if self._store:
                self._store.record_failure(entry, platform="album", filename=name, url=url,
                                           page_url=page_url or self._item["url"],
                                           status=res.http_status, reason=res.reason)
                if res.status == "gone":
                    self._store.mark_gone(entry)
            self._error(f"{'Gone' if res.status == 'gone' else 'Failed'} ({res.reason}): {name}")
        return res.status

    def _pool(self, jobs):
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = [ex.submit(j) for j in jobs]
            for f in futs:
                if self._cancel.is_set():
                    break
                try:
                    f.result()
                except Exception as e:
                    with self._lock:
                        self.error_count += 1
                    self._error(f"{self._label}: {e}")

    # ── engines ─────────────────────────────────────────────────────

    def _run_gofile(self, force):
        from backend import gofile_downloader as gf
        try:
            listing = gf.GofileRunner().list_folder(self._item["url"], self._item.get("password"))
        except gf.GofileGone as e:
            raise AlbumGone(str(e))
        files = listing["files"]
        self._info(f"Reading the folder… {len(files)} file(s)")
        headers = gf.download_headers(listing["token"])
        sessions = threading.local()

        def job(f):
            def go():
                s = getattr(sessions, "s", None)
                if s is None:
                    s = sessions.s = gf.make_session()
                self._fetch(f["id"], f["name"], f["link"], size=f["size"] or None,
                            md5=f.get("md5"), dt=_to_local(f.get("create_time")),
                            session=s, headers=headers)
            return go
        self._pool([job(f) for f in files])
        return True, listing["name"]

    def _run_turbo(self):
        s = th.make_session()
        try:
            title, ids = th.album_files(s, self._item["ref"])
        except th.TurboGone as e:
            raise AlbumGone(str(e))
        self._info(f"Reading the album… {len(ids)} file(s)")

        def job(fid):
            def go():
                if self._have(fid):
                    with self._lock:
                        self.skipped_count += 1
                    return
                try:
                    info = th.resolve(s, fid)
                except th.TurboGone:
                    with self._lock:
                        self.error_count += 1
                    self._error(f"Gone: turbo file {fid}")
                    return

                def refresh():
                    try:
                        return th.sign(s, fid)[0]
                    except th.TurboGone:
                        raise hd.Gone()
                self._fetch(fid, info["filename"], info["url"], size=info["size"],
                            md5=info["md5"], dt=_to_local(info["upload_date"]),
                            session=th.make_session(), refresh=refresh,
                            page_url=th.file_page_url(fid))
            return go
        self._pool([job(i) for i in ids])
        return True, title

    def _run_goonbox(self):
        api = gb.GoonboxAPI()
        try:
            title, images = api.album(self._item["ref"])
        except gb.GoonboxGone as e:
            raise AlbumGone(str(e))
        self._info(f"Reading the album… {len(images)} file(s)")
        jobs = [(lambda im=im: self._fetch(im["id"], im["filename"], im["url"],
                                           size=im.get("size"), dt=_to_local(im.get("created_at")),
                                           session=api.s))
                for im in images if im.get("url")]
        self._pool(jobs)
        return True, title

    def _run_bunkr(self):
        """Native bunkr: list → for each new file resolve, warm, download — at most
        two at once per CDN node, and files that won't start (cold storage) are
        warmed and retried in later passes instead of failing."""
        s = bh.make_session()
        item = self._item
        try:
            if item["kind"] == "album":
                title, files = bh.album(s, item["ref"])
                jobs = [{"key": str(f["id"]), "file_id": str(f["id"]),
                         "name": f["original"] or f["name"] or f["slug"], "size": f["size"],
                         "dt": f["uploaded"], "page": f"https://bunkr.cr/f/{f['slug']}"}
                        for f in files]
            else:
                info = bh.file_info(s, item["ref"])
                title = None
                jobs = [{"key": info["id"], "file_id": info["id"],
                         "name": info["name"] or item["ref"], "size": None, "dt": None,
                         "page": item["url"]}]
        except bh.BunkrGone as e:
            raise AlbumGone(f"404 {e}")
        except bh.BunkrUnavailable as e:
            with self._lock:
                self.error_count += 1
            self._error(f"Bunkr: {e} — try again later")
            return None, None
        todo = [j for j in jobs if not self._have(j["key"])]
        with self._lock:
            self.skipped_count += len(jobs) - len(todo)
        self._info(f"Reading the {'album' if item['kind'] == 'album' else 'file'}… "
                   f"{len(jobs)} file(s), {len(todo)} new")
        limiter = bh.NodeLimiter()
        sessions = threading.local()

        def session():
            ss = getattr(sessions, "s", None)
            if ss is None:
                ss = sessions.s = bh.make_session()
            return ss

        def one(job, final):
            if self._cancel.is_set() or self._have(job["key"]):
                return "skipped"
            ss = session()
            try:
                r = bh.resolve(ss, job["file_id"])
            except bh.BunkrGone:
                return self._bunkr_gone(job)
            except Exception as e:
                if final:
                    return self._bunkr_failed(job, f"couldn't get a download link: {e}")
                return "retry"
            if r["node"].endswith("scdn.st"):
                self._info(f"{job['name']}: bunkr serves this one from cold storage — the "
                           "first download can be slow (it speeds up once warmed)")
            with limiter(r["node"]):
                # The first byte pulls a cold file out of storage (seconds); it also
                # gives the real size and the upload time for a lone file.
                status, total, modified = None, None, None
                for _ in range(4):
                    status, total, modified = bh.warm(ss, r["url"])
                    if status != 503 or self._cancel.is_set():
                        break
                    bh.sleep_cancellable(8, self._cancel.is_set)   # node busy
                if status == 404:
                    return self._bunkr_gone(job)
                if status is None or status >= 400:
                    if final:
                        return self._bunkr_failed(job, f"bunkr CDN didn't serve it "
                                                       f"({'timeout' if status is None else status})")
                    return "retry"

                def refresh():
                    try:
                        return bh.resolve(ss, job["file_id"])["url"]
                    except bh.BunkrGone:
                        raise hd.Gone()
                return self._fetch(job["key"], job["name"], r["url"],
                                   size=job["size"] or total, dt=job["dt"] or modified,
                                   session=ss, headers=bh.DOWNLOAD_HEADERS, refresh=refresh,
                                   page_url=job["page"], defer=not final,
                                   attempts=8 if final else 3)

        for rnd in range(COLD_ROUNDS):
            if not todo or self._cancel.is_set():
                break
            final = rnd == COLD_ROUNDS - 1
            retry = []
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                futs = {ex.submit(one, j, final): j for j in todo}
                for f, j in futs.items():
                    try:
                        if f.result() == "retry":
                            retry.append(j)
                    except Exception as e:
                        with self._lock:
                            self.error_count += 1
                        self._error(f"{self._label}: {e}")
            todo = retry
            if todo and not final and not self._cancel.is_set():
                self._info(f"{len(todo)} file(s) didn't start — bunkr is probably pulling them "
                           f"out of cold storage; trying them again in {COLD_WAIT}s")
                bh.sleep_cancellable(COLD_WAIT, self._cancel.is_set)
        return True, title

    def _bunkr_gone(self, job):
        entry = f"album_bunkr_{job['key']}"
        with self._lock:
            self.error_count += 1
        if self._store:
            self._store.record_failure(entry, platform="album", filename=job["name"],
                                       url=job["page"], page_url=self._item["url"],
                                       status=404, reason="removed from bunkr")
            self._store.mark_gone(entry)
        self._error(f"Gone (removed from bunkr): {job['name']}")
        return "gone"

    def _bunkr_failed(self, job, reason):
        entry = f"album_bunkr_{job['key']}"
        with self._lock:
            self.error_count += 1
        if self._store:
            self._store.record_failure(entry, platform="album", filename=job["name"],
                                       url=job["page"], page_url=self._item["url"],
                                       reason=reason)
        self._error(f"Failed ({reason}): {job['name']}")
        return "failed"

    def _run_cyberdrop(self, state_dir, force):
        from backend.cyberdrop_dl_runner import CyberdropDlRunner
        staging = os.path.join(self._root, STAGING)
        os.makedirs(staging, exist_ok=True)
        # A cancelled run can leave finished files in staging that cyberdrop-dl
        # already lists as downloaded — file them before anything else.
        self._file_staging(staging, {})
        state_dir = state_dir or os.path.join(self._root, ".cyberdrop-state")
        site = album_sites.detect_site(self._item["url"]) or {}
        runner = CyberdropDlRunner(platform="album")
        self._sub = runner
        stats = {}
        runner.run(self._item["url"], staging,
                   on_progress=self._staged_progress,
                   on_complete=stats.update, on_error=self._on_error, errors_path=None,
                   state_dir=state_dir, ignore_history=bool(force),
                   tuning=site.get("tuning") or {"per_domain": 2, "segments": 2, "jitter": 1})
        self._sub = None
        results = self._read_results(os.path.join(state_dir, "logs", "downloader.results.jsonl"))
        self._file_staging(staging, results)
        with self._lock:
            self.skipped_count += stats.get("skipped", 0) or 0
            self.error_count += stats.get("errors", 0) or 0
        scrape = (stats.get("scrape_error") or "").lower()
        if scrape and ("404" in scrape or "410" in scrape or "not found" in scrape):
            raise AlbumGone(stats["scrape_error"])
        title = None
        for rec in results.values():
            title = os.path.basename(os.path.dirname(rec.get("path") or "")) or None
            break
        if title:
            title = title.rsplit(" (", 1)[0]
            if title.lower() == "loose files":          # cyberdrop-dl's bucket for lone files
                title = None
        return (None if scrape else True), title

    @staticmethod
    def _read_results(path):
        """cyberdrop-dl's per-file results, keyed by normalised file path."""
        out = {}
        try:
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    p = rec.get("path")
                    if p:
                        out[os.path.normcase(os.path.abspath(p))] = rec
        except OSError:
            pass
        return out

    def _file_staging(self, staging, results):
        """Move every finished file out of staging to its dated name."""
        for dirpath, _dirs, names in os.walk(staging):
            for nm in names:
                if nm.endswith((".part", ".tmp")) or nm.startswith("."):
                    continue
                src = os.path.join(dirpath, nm)
                rec = results.get(os.path.normcase(os.path.abspath(src))) or {}
                try:
                    size = os.path.getsize(src)
                except OSError:
                    continue
                dt = _to_local(rec.get("uploaded_at")) or self._origin or \
                    _to_local(os.path.getmtime(src), local=True)
                key = rec.get("url") or f"name:{nm}:{size}"
                original = rec.get("original_filename") or rec.get("filename") or nm
                if self._have(key):
                    os.remove(src)                       # already filed (re-run overlap)
                    continue
                rel, dst = self._target(dt, original, size)
                if rel is None:                          # identical file already there
                    os.remove(src)
                    rel = os.path.relpath(dst, self._root).replace("\\", "/")
                else:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.move(src, dst)
                    with self._lock:
                        self._claimed.discard(dst.lower())
                        self.downloaded_count += 1
                    self._progress({"type": "download", "message": os.path.basename(dst),
                                    "path": dst})
                self._db.record(key, os.path.basename(dst), rel, size,
                                dt.isoformat() if dt else None)
        # Drop the now-empty "<Album> (Bunkr)" folders.
        for dirpath, dirs, names in sorted(os.walk(staging), key=lambda w: -len(w[0])):
            if dirpath != staging and not os.listdir(dirpath):
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass

    def _run_filester(self):
        if self._item["kind"] == "file":
            return self._run_filester_file()
        return self._run_filester_folder()

    def _run_filester_file(self):
        """A lone filester file (`/d/<slug>`): the slug is the file_slug of the same
        public download API the folder engine uses (filester's own file_dl.js does
        exactly this). Missing file → 404 "File not found". No upload date is
        published, so the file takes the date of the post it came from."""
        from urllib.parse import urlsplit
        from backend import filester_downloader as fsd
        p = urlsplit(self._item["url"])
        root = f"https://{p.netloc or 'filester.me'}"
        slug = self._item["ref"]
        s = fsd._rq.Session(impersonate=fsd._IMPERSONATE) if fsd._IMPERSONATE else fsd._rq.Session()

        def resolve():
            r = s.post(f"{root}/v2/api/public/download", json={"file_slug": slug},
                       headers={"content-type": "application/json"}, timeout=30)
            if r.status_code == 404:
                raise AlbumGone("404 File not found")
            d = r.json()
            if not d.get("success"):
                raise RuntimeError(d.get("message") or "filester download API error")
            from urllib.parse import quote
            name = d.get("name") or slug
            return (f"{d['server']}/v2/{d['file']}?token={quote(str(d['token']))}"
                    f"&download=true&n={quote(name)}", name)

        url, name = resolve()
        size = None
        try:
            r = s.get(url, headers={"referer": root + "/", "Range": "bytes=0-0"}, timeout=60)
            import re as _re
            m = _re.search(r"/\s*(\d+)\s*$", r.headers.get("Content-Range") or "")
            size = int(m.group(1)) if m else None
            r.close()
        except Exception:
            pass

        def refresh():
            try:
                return resolve()[0]
            except AlbumGone:
                raise hd.Gone()
        self._fetch(slug, name, url, size=size, session=s, headers={"referer": root + "/"},
                    refresh=refresh, page_url=self._item["url"])
        return True, None

    def _run_filester_folder(self):
        from backend.filester_downloader import FilesterRunner
        staging = os.path.join(self._root, STAGING)
        os.makedirs(staging, exist_ok=True)
        self._file_staging(staging, {})
        runner = FilesterRunner(platform="album")
        self._sub = runner
        stats, errs = {}, []

        def on_err(d):
            errs.append(d.get("message") or "")
            with self._lock:
                self.error_count += 1          # the engine reports some failures only here
            self._error(d.get("message") or "")
        runner.run(self._item["url"], staging,
                   on_progress=self._staged_progress,
                   on_complete=stats.update, on_error=on_err,
                   password=self._item.get("password"))
        self._sub = None
        self._file_staging(staging, {})
        with self._lock:
            # Per-file failures are counted by the engine AND reported via on_err.
            self.error_count = max(self.error_count, stats.get("errors", 0) or 0)
        if any("404" in e or "not found" in e.lower() for e in errs):
            raise AlbumGone("not found")
        title = (stats.get("subfolder") or "").rsplit(" (", 1)[0] or None
        return True, title

    # ── helpers ─────────────────────────────────────────────────────

    def _staged_progress(self, d):
        """An engine writing into staging: its per-file 'download' is only a
        fetch — the file counts once it is filed under its dated name."""
        if d.get("type") == "download":
            self._info(f"fetched {d.get('message') or ''}")
        elif d.get("type") != "skip":
            self._progress(d)

    def _skip_requested(self, entry):
        with self._lock:
            return entry in self._skip_ids

    def _progress(self, d):
        if self._on_progress:
            self._on_progress(d)

    def _info(self, msg):
        self._progress({"type": "info", "message": msg})

    def _error(self, msg):
        if self._on_error:
            self._on_error({"type": "error", "message": msg})


# ── liveness probes (review list) ───────────────────────────────────

def probe_alive(url, timeout=(15, 30)):
    """True / False / None (couldn't tell) for any review-list link. Light: one
    or two requests, never a download."""
    info = parse_album_url(url)
    try:
        if info and info["site"] == "turbo":
            return th.probe_alive(th.make_session(), url)
        if info and info["site"] == "goonbox":
            return gb.GoonboxAPI().probe_alive(url)
        if info and info["site"] == "gofile":
            from backend import gofile_downloader as gf
            try:
                gf.GofileRunner().list_folder(url)
                return True
            except gf.GofileGone:
                return False
            except gf.GofilePassword:
                return True
        if info and info["site"] == "bunkr":
            return bh.probe_alive(bh.make_session(), url)
        if info and info["site"] == "pixeldrain":
            from backend.pawchive_scraper import make_session
            api = (f"https://pixeldrain.com/api/list/{info['ref']}" if info["kind"] == "album"
                   else f"https://pixeldrain.com/api/file/{info['ref']}/info")
            r = make_session().get(api, timeout=timeout)
            return False if r.status_code == 404 else True if r.status_code == 200 else None
        from backend.pawchive_scraper import make_session
        s = make_session()
        s.headers["Accept"] = "text/html,*/*"
        r = s.get(url, timeout=timeout, allow_redirects=True)
        if r.status_code in (404, 410):
            return False
        if r.status_code == 200:
            low = (r.text or "")[:200000].lower()
            if info and info["site"] == "bunkr":
                # Removed bunkr files/albums answer 200 with a "not found" page.
                if "<title>404" in low or "album not found" in low or "file not found" in low:
                    return False
            return True
        return None
    except Exception:
        return None
