"""Native gofile.io album downloader.

gofile serves whole folders (`https://gofile.io/d/<code>`) that can hold thousands
of files. Downloading needs a free **guest account token** plus a locally-computed
**website token** — no paid account required. The protocol here is cross-checked
against the bundled gallery-dl (`gallery_dl/extractor/gofile.py`) and cyberdrop-dl
(`cyberdrop_dl/crawlers/gofile.py`) implementations:

  1. `POST https://api.gofile.io/accounts` -> `data.token` (a guest token). Reuse
     ONE token for the whole run — minting per file is the fast path to a 429/ban.
     Cached ~24h, re-minted if the API reports `wrongToken`.
  2. wt = sha256(f"{UA}::en-US::{token}::{int(time()//14400)}::{SALT}") sent as the
     `X-Website-Token` header. The UA hashed MUST equal the UA header actually sent,
     so we pin one UA constant. SALT is captured from gofile's wt.obf.js; if it ever
     rotates the API 401s with `wrongToken` and this constant needs a one-line bump.
  3. List a folder with `GET /contents/{code}?pageSize=1000&page=N` (+ headers
     Authorization/X-Website-Token/X-BL). Paginate while `metadata.hasNextPage`, and
     recurse `children` whose `type == "folder"`. Nested folders are preserved as
     subdirectories under the album folder.
  4. Password folders: pass `password=sha256hex(pw)` as a query param.
  5. Download each file's `link` (a `storeN.gofile.io/download/...` URL) with the
     token as `Cookie: accountToken=<token>` — a bare GET 401/403s.

Unlike the sequential FilesterRunner, downloads run through a small ThreadPoolExecutor
(default 4 concurrent — the safe ceiling before gofile starts dropping/429ing) while
the folder listing stays single-threaded and throttled to 4 requests / 10s (the limit
cyberdrop-dl uses). Mirrors the other runners' contract (is_running / cancel /
counters / run) so AlbumRunner drives it like the filester engine.
"""

import hashlib
import os
import random
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from backend.download_errors import FailureStore

try:
    from curl_cffi import requests as _rq
    _IMPERSONATE = "chrome"
except Exception:                              # pragma: no cover
    import requests as _rq
    _IMPERSONATE = None

_API = "https://api.gofile.io"
# Must match the UA hashed into the website token AND the UA header we send.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_SALT = "9844d94d963d30"        # gofile wt.obf.js salt (see module docstring)
_LANG = "en-US"
_CONCURRENCY = 4                # safe ceiling for gofile CDN before drops/429s
_TOKEN_TTL = 86400              # reuse one guest token for ~24h

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Module-level guest-token cache, shared across runs/links in one app session.
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE = {"token": None, "ts": 0.0}
# One session per worker thread (curl_cffi sessions aren't safe to share).
_thread_local = threading.local()


def _get_token(session, force_new=False):
    """Return a (cached) gofile guest token, minting a fresh one if needed."""
    with _TOKEN_LOCK:
        now = time.time()
        cached = _TOKEN_CACHE["token"]
        if (not force_new and cached
                and (now - _TOKEN_CACHE["ts"]) < _TOKEN_TTL):
            return cached
        r = session.post(f"{_API}/accounts", json={},
                         headers={"Origin": "https://gofile.io",
                                  "Referer": "https://gofile.io/"},
                         timeout=30).json()
        if r.get("status") != "ok" or not (r.get("data") or {}).get("token"):
            raise RuntimeError("could not create a gofile guest account")
        token = r["data"]["token"]
        _TOKEN_CACHE["token"] = token
        _TOKEN_CACHE["ts"] = now
        return token


def _website_token(token):
    data = f"{_UA}::{_LANG}::{token}::{int(time.time() // 14400)}::{_SALT}"
    return hashlib.sha256(data.encode()).hexdigest()


class _RateGate:
    """Simple sliding-window limiter: at most `n` calls per `per` seconds."""

    def __init__(self, n, per):
        self._n, self._per = n, per
        self._times = deque()
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.time()
            while self._times and now - self._times[0] > self._per:
                self._times.popleft()
            if len(self._times) >= self._n:
                sleep_for = self._per - (now - self._times[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.time()
                while self._times and now - self._times[0] > self._per:
                    self._times.popleft()
            self._times.append(time.time())


class GofileRunner:
    def __init__(self, platform="album"):
        self._platform = platform
        self._cancel = threading.Event()
        self._running = False
        self._lock = threading.Lock()          # guards counters/callbacks/errors
        self.downloaded_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self._errors = None
        self._on_progress = None
        self._on_error = None
        self._force = False
        self._gate = _RateGate(4, 10)          # gofile API: 4 requests / 10s

    @property
    def is_running(self):
        return self._running

    def cancel(self):
        self._cancel.set()

    def run(self, url, download_dir, on_progress, on_complete, on_error,
            password=None, errors_path=None, force=False):
        self._running = True
        self._cancel.clear()
        self.downloaded_count = self.skipped_count = self.error_count = 0
        self._errors = FailureStore(errors_path) if errors_path else None
        self._on_progress = on_progress
        self._on_error = on_error
        self._force = force
        subfolder = None
        try:
            m = re.search(r"/d/([^/?#]+)", urlparse((url or "").strip()).path or "")
            if not m:
                on_error({"type": "error", "message": "Not a gofile folder link"})
                return
            content_id = m.group(1)
            session = (_rq.Session(impersonate=_IMPERSONATE)
                       if _IMPERSONATE else _rq.Session())
            session.headers["User-Agent"] = _UA
            pw_hash = (hashlib.sha256(password.encode("utf-8")).hexdigest()
                       if password else None)

            try:
                token = _get_token(session)
            except Exception as e:
                on_error({"type": "error", "message": f"gofile: {e}"})
                return

            try:
                folder_name, files = self._enumerate(session, content_id, token, pw_hash)
            except _TokenError:
                # Token expired / salt rotated — mint a fresh one and retry once.
                token = _get_token(session, force_new=True)
                folder_name, files = self._enumerate(session, content_id, token, pw_hash)
            except _PasswordError:
                msg = ("Wrong gofile password" if password
                       else "This gofile folder is locked — add its password")
                on_error({"type": "error", "message": msg})
                return
            except Exception as e:
                on_error({"type": "error", "message": f"gofile listing failed: {e}"})
                return

            subfolder = self._safe(f"{folder_name} (Gofile)")
            dest_dir = os.path.join(download_dir, subfolder)
            os.makedirs(dest_dir, exist_ok=True)
            on_progress({"type": "info",
                         "message": f"Reading the album's file list… {len(files)} files"})
            if self._cancel.is_set():
                return

            self._download_all(token, files, dest_dir, url)
        finally:
            if self._errors:
                try:
                    self._errors.close()
                except Exception:
                    pass
            self._running = False
            stats = self._stats(subfolder)
            stats["cancelled"] = self._cancel.is_set()
            on_complete(stats)

    # ── listing ─────────────────────────────────────────────────────────
    def _enumerate(self, session, root_id, token, pw_hash):
        """Walk the folder tree, returning (folder_name, [(id, rel_dir, name, size, link)]).

        rel_dir preserves nested subfolders relative to the album root.
        """
        files, seen = [], set()
        folder_name = root_id
        stack = [(root_id, "")]         # (content_id, rel_dir)
        first = True
        while stack:
            if self._cancel.is_set():
                break
            content_id, rel_dir = stack.pop()
            page = 1
            while True:
                if self._cancel.is_set():
                    break
                resp = self._contents(session, content_id, token, pw_hash, page)
                status = resp.get("status", "")
                if status != "ok":
                    low = status.lower()
                    if "password" in low:
                        raise _PasswordError(status)
                    if "wrongtoken" in low:
                        raise _TokenError(status)
                    raise RuntimeError(status or "unknown error")
                data = resp.get("data") or {}
                if first:
                    folder_name = data.get("name") or root_id
                    first = False
                children = data.get("children")
                if children is None:
                    # gofile omits children when a password is required/wrong.
                    raise _PasswordError("passwordRequired")
                for child in children.values():
                    ctype = child.get("type")
                    if ctype == "file":
                        fid = child.get("id")
                        if fid in seen:
                            continue
                        seen.add(fid)
                        link = child.get("link")
                        if (not link or link == "overloaded") and child.get("directLink"):
                            link = child.get("directLink")
                        if link and link != "overloaded":
                            files.append((fid, rel_dir,
                                          child.get("name") or fid,
                                          child.get("size") or 0, link))
                    elif ctype == "folder":
                        name = self._safe(child.get("name") or child.get("id"))
                        sub_rel = os.path.join(rel_dir, name) if rel_dir else name
                        stack.append((child.get("id"), sub_rel))
                meta = resp.get("metadata") or {}
                if not meta.get("hasNextPage"):
                    break
                page += 1
        return folder_name, files

    def _contents(self, session, content_id, token, pw_hash, page):
        self._gate.wait()
        params = {
            "contentFilter": "",
            "sortField": "name",
            "sortDirection": "1",
            "pageSize": "1000",
            "page": str(page),
        }
        if pw_hash:
            params["password"] = pw_hash
        headers = {
            "Authorization": "Bearer " + token,
            "X-Website-Token": _website_token(token),
            "X-BL": _LANG,
        }
        return session.get(f"{_API}/contents/{content_id}",
                          params=params, headers=headers, timeout=30).json()

    # ── downloading ─────────────────────────────────────────────────────
    def _download_all(self, token, files, dest_dir, page_url):
        ex = ThreadPoolExecutor(max_workers=_CONCURRENCY)
        try:
            futures = []
            for item in files:
                if self._cancel.is_set():
                    break
                futures.append(ex.submit(self._download_one, token, item,
                                         dest_dir, page_url))
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:
                    pass
                if self._cancel.is_set():
                    break
        finally:
            # cancel_futures drops not-yet-started work; running workers see the
            # cancel event and exit promptly.
            ex.shutdown(wait=True, cancel_futures=True)

    def _download_one(self, token, item, dest_dir, page_url, attempts=5):
        fid, rel_dir, fname, size, link = item
        if self._cancel.is_set():
            return
        out_dir = os.path.join(dest_dir, rel_dir) if rel_dir else dest_dir
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError:
            pass
        final = os.path.join(out_dir, self._safe(fname))

        if not self._force and os.path.exists(final):
            try:
                on_disk = os.path.getsize(final)
                if (size and on_disk == size) or (not size and on_disk > 0):
                    with self._lock:
                        self.skipped_count += 1
                        self._on_progress({"type": "skip", "message": fname})
                    return
            except OSError:
                pass

        s = self._session(token)
        last_err = None
        for att in range(attempts):
            if self._cancel.is_set():
                return
            part = final + ".part"
            try:
                r = s.get(link,
                          headers={"Referer": "https://gofile.io/",
                                   "Origin": "https://gofile.io"},
                          cookies={"accountToken": token},
                          timeout=60, stream=True)
                try:
                    if r.status_code == 429:
                        raise _RateLimited()
                    if r.status_code >= 400:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    with open(part, "wb") as f:
                        for chunk in r.iter_content(1 << 16):
                            if self._cancel.is_set():
                                raise _Cancelled()
                            if chunk:
                                f.write(chunk)
                finally:
                    try:
                        r.close()
                    except Exception:
                        pass
                os.replace(part, final)
                with self._lock:
                    self.downloaded_count += 1
                    self._on_progress({"type": "download", "message": fname, "path": final})
                return
            except _Cancelled:
                self._cleanup(part)
                return
            except _RateLimited:
                last_err = "HTTP 429 (rate limited)"
                self._cleanup(part)
                if att < attempts - 1 and not self._cancel.is_set():
                    # Back off hard on 429: ~5s, 15s, 45s (capped) + jitter.
                    self._sleep(min(5 * (3 ** att), 60) + random.uniform(0, 2))
            except Exception as e:
                last_err = str(e) or "download failed"
                self._cleanup(part)
                if att < attempts - 1 and not self._cancel.is_set():
                    self._sleep(min(2 ** att, 8) + random.uniform(0, 1))

        if self._cancel.is_set():
            return                              # cancel-noise fix: not a real failure
        reason = last_err or "download failed"
        with self._lock:
            self.error_count += 1
            self._on_error({"type": "error", "message": f"{fname}: {reason}"})
            if self._errors:
                try:
                    self._errors.record_failure(
                        f"{self._platform}_gofile_{fid}", platform=self._platform,
                        url=link, page_url=page_url, filename=fname,
                        reason=reason[:300])
                except Exception:
                    pass

    # ── helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _session(token):
        """A per-thread curl_cffi session (sessions aren't safe to share)."""
        s = getattr(_thread_local, "session", None)
        if s is None:
            s = (_rq.Session(impersonate=_IMPERSONATE)
                 if _IMPERSONATE else _rq.Session())
            s.headers["User-Agent"] = _UA
            _thread_local.session = s
        return s

    def _sleep(self, secs):
        """Sleep in small slices so a cancel is honoured promptly."""
        end = time.time() + secs
        while True:
            remaining = end - time.time()
            if remaining <= 0 or self._cancel.is_set():
                return
            time.sleep(min(0.5, remaining))

    @staticmethod
    def _cleanup(path):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    @staticmethod
    def _safe(name):
        name = _INVALID.sub("_", name or "").strip().rstrip(". ")
        return name or "file"

    def _stats(self, subfolder):
        return {
            "downloaded": self.downloaded_count,
            "skipped": self.skipped_count,
            "errors": self.error_count,
            "subfolder": subfolder,
        }


class _Cancelled(Exception):
    pass


class _RateLimited(Exception):
    pass


class _PasswordError(Exception):
    pass


class _TokenError(Exception):
    pass
