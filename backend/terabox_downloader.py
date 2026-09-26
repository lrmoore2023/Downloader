"""Native Terabox share downloader (Albums tab + per-creator tracked links).

Terabox shares (`terabox.app/sharing/link?surl=X`, `…/s/1X`, and the same paths on
1024terabox.com / terasharelink.com / teraboxapp.com …) all resolve to one share
id, the `surl`. Characterised live against the web client's own endpoints:

  1. GET https://www.terabox.app/sharing/link?surl=X — sets browserid/csrfToken
     cookies and embeds `jsToken` as `fn%28%22<hex>%22%29` in the HTML.
  2. GET /api/shorturlinfo?shorturl=1X&root=1 — shareid, uk, sign, timestamp,
     randsk and the share's root entries. Works anonymously.
  3. GET /share/list?shorturl=X&dir=<path>&page=N&num=100 — a folder's entries
     (`isdir` is sometimes a string, sometimes an int). Works anonymously.
  4. GET /api/sharedownload?…&fid_list=[…]&extra={"sekey": randsk} — returns a
     `dlink` per file even anonymously…
  5. …but GET on that dlink answers **403 {"error_code":31045,"error_msg":"user not
     exists"}** without a signed-in session. A (free) account's cookies — `ndus`
     above all — are required to actually fetch bytes. They are captured in-app by
     `terabox_auth` (Settings ▸ Terabox ▸ Connect) and passed in as `cookies`.

The run contract mirrors GofileRunner (is_running / cancel / counters / blocking
run() that calls on_complete exactly once) so AlbumRunner drives it the same way.
Optional tracking (`archive_path`): a small SQLite record of every file id fetched,
so files the user later deletes are never downloaded again.
"""

import json
import os
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

from backend.download_errors import FailureStore

try:
    from curl_cffi import requests as _rq
    _IMPERSONATE = "chrome"
except Exception:                              # pragma: no cover
    import requests as _rq
    _IMPERSONATE = None

BASE = "https://www.terabox.app"
_COMMON = {"app_id": "250528", "web": "1", "channel": "dubox", "clienttype": "0"}
_CONCURRENCY = 4
_DLINK_BATCH = 50               # file ids per /api/sharedownload call
_AUTH_ERROR = 31045             # "user not exists": no signed-in session

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_HOST_RE = re.compile(r"(?:^|\.)(?:\d*tera(?:box|share)\w*|terasharelink|teraboxlink|"
                      r"freeterabox|1024tera|4funbox|mirrobox|nephobox|momerybox|tibibox)\.",
                      re.I)
_thread_local = threading.local()


def is_terabox_url(url):
    try:
        return bool(_HOST_RE.search((urlparse(url).hostname or "") + "."))
    except Exception:
        return False


def parse_surl(url):
    """The share id of any Terabox share link, or None.

    `?surl=X` is used as-is; a `/s/1X` path drops its leading '1' (the web client
    itself redirects /s/1X to ?surl=X)."""
    try:
        u = urlparse((url or "").strip())
    except Exception:
        return None
    q = parse_qs(u.query).get("surl")
    if q and q[0]:
        return q[0]
    m = re.search(r"/s/([A-Za-z0-9_-]+)", u.path or "")
    if m:
        s = m.group(1)
        return s[1:] if s.startswith("1") and len(s) > 1 else s
    return None


def archive_path_for(archive_dir, surl, fallback_dir):
    """Where a tracked share's record lives: beside the other per-link archive DBs
    in the NAS archive dir (so it survives a reinstall), else the app's state dir."""
    name = f"terabox_{re.sub(r'[^A-Za-z0-9_-]', '_', surl)}.db"
    base = (archive_dir or "").strip()
    if base and os.path.isdir(base):
        return os.path.join(base, name)
    os.makedirs(fallback_dir, exist_ok=True)
    return os.path.join(fallback_dir, name)


class TeraboxArchive:
    """Per-share record of downloaded files, keyed by Terabox's fs_id."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("""CREATE TABLE IF NOT EXISTS files (
            fs_id TEXT PRIMARY KEY, path TEXT, size INTEGER, recorded_at TEXT)""")
        self._conn.commit()

    def has(self, fs_id):
        with self._lock:
            return self._conn.execute("SELECT 1 FROM files WHERE fs_id = ?",
                                      (str(fs_id),)).fetchone() is not None

    def record(self, fs_id, path, size):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO files (fs_id, path, size, recorded_at) VALUES (?, ?, ?, ?)",
                (str(fs_id), path, int(size or 0),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
            self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


class NeedsAuth(Exception):
    pass


class TeraboxRunner:
    def __init__(self, platform="album", cookies=None, user_agent=None, archive_path=None):
        self._platform = platform
        self._cookies = dict(cookies or {})
        self._ua = user_agent or None
        self._archive_path = archive_path
        self._cancel = threading.Event()
        self._running = False
        self._lock = threading.Lock()
        self.downloaded_count = self.skipped_count = self.error_count = 0
        self.needs_auth = False
        self._errors = self._archive = None
        self._on_progress = self._on_error = None
        self._force = False

    @property
    def is_running(self):
        return self._running

    def cancel(self):
        self._cancel.set()

    # ── run ─────────────────────────────────────────────────────────────
    def run(self, url, download_dir, on_progress, on_complete, on_error,
            password=None, errors_path=None, force=False):
        self._running = True
        self._cancel.clear()
        self.downloaded_count = self.skipped_count = self.error_count = 0
        self.needs_auth = False
        self._on_progress, self._on_error, self._force = on_progress, on_error, force
        self._errors = FailureStore(errors_path) if errors_path else None
        subfolder = None
        try:
            surl = parse_surl(url)
            if not surl:
                on_error({"type": "error", "message": "Not a Terabox share link"})
                return
            if not self._cookies.get("ndus"):
                self.needs_auth = True
                on_error({"type": "error", "message":
                          "Terabox only lets signed-in accounts download — connect it "
                          "under Settings ▸ Terabox, then run this link again."})
                return
            if self._archive_path:
                self._archive = TeraboxArchive(self._archive_path)
            session = self._new_session()
            try:
                share = self._open_share(session, surl, password)
                files, subfolder = self._enumerate(session, surl, share)
            except _ShareError as e:
                on_error({"type": "error", "message": f"Terabox: {e}"})
                return
            except Exception as e:
                on_error({"type": "error", "message": f"Terabox listing failed: {e}"})
                return
            total = sum(f["size"] for f in files)
            on_progress({"type": "info", "message":
                         f"Reading the share's file list… {len(files)} files "
                         f"({total / 1e6:,.0f} MB)"})
            if not self._cancel.is_set():
                self._download_all(session, share, files, download_dir, url)
        finally:
            for res in (self._errors, self._archive):
                if res:
                    try:
                        res.close()
                    except Exception:
                        pass
            self._running = False
            stats = {"downloaded": self.downloaded_count, "skipped": self.skipped_count,
                     "errors": self.error_count, "subfolder": subfolder,
                     "cancelled": self._cancel.is_set() and not self.needs_auth,
                     "needs_terabox_auth": self.needs_auth}
            on_complete(stats)

    # ── share / listing ────────────────────────────────────────────────
    def _new_session(self):
        s = _rq.Session(impersonate=_IMPERSONATE) if _IMPERSONATE else _rq.Session()
        if self._ua:
            s.headers["User-Agent"] = self._ua
        for k, v in self._cookies.items():
            s.cookies.set(k, v, domain=".terabox.app")
        return s

    def _open_share(self, s, surl, password):
        page_url = f"{BASE}/sharing/link?surl={surl}"
        r = s.get(page_url, timeout=30)
        m = re.search(r"fn%28%22([0-9A-Fa-f]+)%22%29", r.text or "")
        if not m:
            raise _ShareError("couldn't read the share page (link removed or blocked?)")
        share = {"surl": surl, "page_url": page_url, "js": m.group(1)}
        if password:
            v = s.post(f"{BASE}/share/verify",
                       params={**_COMMON, "jsToken": share["js"], "surl": surl},
                       data={"pwd": password, "vcode": "", "vcode_str": ""},
                       headers={"Referer": page_url}, timeout=30).json()
            if v.get("errno") != 0:
                raise _ShareError(f"wrong share password (errno {v.get('errno')})")
            if v.get("randsk"):
                s.cookies.set("BOXCLND", v["randsk"], domain=".terabox.app")
        info = self._api(s, share, "/api/shorturlinfo", shorturl="1" + surl, root="1")
        if info.get("errno") != 0:
            raise _ShareError(
                "this share needs a password — add it after the link ( url | password )"
                if info.get("errno") in (-9, -12) else
                f"share unavailable (errno {info.get('errno')}) — expired or deleted?")
        share.update(shareid=info["shareid"], uk=info["uk"], sign=info["sign"],
                     timestamp=info["timestamp"], sekey=unquote(info.get("randsk") or ""),
                     root=info.get("list") or [])
        return share

    def _api(self, s, share, path, attempts=4, **params):
        last = None
        for att in range(attempts):
            try:
                r = s.get(BASE + path, params={**_COMMON, "jsToken": share["js"], **params},
                          headers={"Referer": share["page_url"]}, timeout=30)
                return r.json()
            except Exception as e:
                last = e
                self._sleep(min(2 ** att, 8))
        raise RuntimeError(f"{path}: {last}")

    def _enumerate(self, s, surl, share):
        """[{fs_id, rel, name, size}] for every file, rel relative to the share root's
        parent — a one-folder share keeps its folder name as the top directory."""
        root = share["root"]
        parent = os.path.dirname((root[0].get("path") or "/").rstrip("/")) if root else "/"
        files, stack = [], list(root)
        while stack and not self._cancel.is_set():
            it = stack.pop()
            if str(it.get("isdir")) == "1":
                page = 1
                while not self._cancel.is_set():
                    res = self._api(s, share, "/share/list", shorturl=surl, dir=it["path"],
                                    page=str(page), num="100", order="name", desc="0")
                    if res.get("errno") != 0:
                        raise _ShareError(f"couldn't list {it['path']} (errno {res.get('errno')})")
                    items = res.get("list") or []
                    stack.extend(items)
                    if len(items) < 100:
                        break
                    page += 1
                continue
            rel = os.path.relpath(it["path"], parent).replace("\\", "/")
            files.append({"fs_id": str(it["fs_id"]), "rel": rel,
                          "name": it.get("server_filename") or rel.rsplit("/", 1)[-1],
                          "size": int(it.get("size") or 0)})
        files.sort(key=lambda f: f["rel"].lower())
        subfolder = None
        if len(root) == 1 and str(root[0].get("isdir")) == "1":
            subfolder = self._safe(root[0].get("server_filename") or "")
        return files, subfolder

    def _dlinks(self, s, share, fs_ids):
        res = self._api(s, share, "/api/sharedownload",
                        shareid=share["shareid"], uk=share["uk"], sign=share["sign"],
                        timestamp=share["timestamp"], primaryid=share["shareid"],
                        product="share", nozip="0", type="nolimit",
                        fid_list=json.dumps([int(x) for x in fs_ids]),
                        extra=json.dumps({"sekey": share["sekey"]}))
        if res.get("errno") != 0:
            raise RuntimeError(f"download link request failed (errno {res.get('errno')})")
        return {str(it["fs_id"]): it.get("dlink") for it in res.get("list") or []}

    # ── downloading ────────────────────────────────────────────────────
    def _download_all(self, s, share, files, dest_root, page_url):
        todo = []
        for f in files:
            final = os.path.join(dest_root, *[self._safe(p) for p in f["rel"].split("/")])
            if self._skip(f, final):
                continue
            todo.append((f, final))
        with ThreadPoolExecutor(max_workers=_CONCURRENCY) as ex:
            for i in range(0, len(todo), _DLINK_BATCH):
                if self._cancel.is_set():
                    break
                batch = todo[i:i + _DLINK_BATCH]
                try:
                    links = self._dlinks(s, share, [f["fs_id"] for f, _ in batch])
                except Exception as e:
                    for f, _final in batch:
                        self._fail(f, page_url, str(e))
                    continue
                futs = [ex.submit(self._download_one, f, final, links.get(f["fs_id"]), page_url)
                        for f, final in batch]
                for fut in as_completed(futs):
                    try:
                        fut.result()
                    except Exception:
                        pass

    def _skip(self, f, final):
        if self._force:
            return False
        tracked = self._archive is not None and self._archive.has(f["fs_id"])
        present = False
        try:
            present = os.path.getsize(final) == f["size"] or (not f["size"] and os.path.getsize(final) > 0)
        except OSError:
            pass
        if present and self._archive is not None and not tracked:
            self._archive.record(f["fs_id"], f["rel"], f["size"])
        if tracked or present:
            with self._lock:
                self.skipped_count += 1
                self._on_progress({"type": "skip", "message": f["rel"]})
            return True
        return False

    def _download_one(self, f, final, dlink, page_url, attempts=5):
        if self._cancel.is_set():
            return
        if not dlink:
            self._fail(f, page_url, "no download link returned")
            return
        os.makedirs(os.path.dirname(final), exist_ok=True)
        part = final + ".part"
        s = self._thread_session()
        last = None
        for att in range(attempts):
            if self._cancel.is_set():
                return
            try:
                have = os.path.getsize(part) if os.path.isfile(part) else 0
                hdrs = {"Referer": BASE + "/"}
                if have:
                    hdrs["Range"] = f"bytes={have}-"
                r = s.get(dlink, headers=hdrs, timeout=60, stream=True, allow_redirects=True)
                try:
                    if r.status_code == 403:
                        body = b""
                        for ch in r.iter_content(512):
                            body = ch
                            break
                        if str(_AUTH_ERROR).encode() in body:
                            raise NeedsAuth()
                        raise RuntimeError(f"HTTP 403 {body[:120]!r}")
                    if r.status_code == 416 and have and have == f["size"]:
                        pass                                   # .part already complete
                    elif r.status_code >= 400:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    else:
                        mode = "ab" if (have and r.status_code == 206) else "wb"
                        with open(part, mode) as out:
                            for chunk in r.iter_content(1 << 16):
                                if self._cancel.is_set():
                                    raise _Cancelled()
                                if chunk:
                                    out.write(chunk)
                finally:
                    try:
                        r.close()
                    except Exception:
                        pass
                got = os.path.getsize(part)
                if f["size"] and got != f["size"]:
                    raise RuntimeError(f"size mismatch ({got} of {f['size']} bytes)")
                os.replace(part, final)
                if self._archive is not None:
                    self._archive.record(f["fs_id"], f["rel"], f["size"])
                with self._lock:
                    self.downloaded_count += 1
                    self._on_progress({"type": "download", "message": f["rel"], "path": final})
                return
            except _Cancelled:
                return                      # keep the .part: the next run resumes it
            except NeedsAuth:
                self._auth_failed()
                return
            except Exception as e:
                last = str(e) or "download failed"
                if "size mismatch" in last:
                    self._cleanup(part)     # don't resume onto bytes we can't trust
                if att < attempts - 1:
                    self._sleep(min(2 ** att, 16) + random.uniform(0, 1))
        self._fail(f, page_url, last or "download failed")

    def _auth_failed(self):
        with self._lock:
            if self.needs_auth:
                return
            self.needs_auth = True
            self._on_error({"type": "error", "message":
                            "Terabox refused the download: the saved sign-in is missing or "
                            "expired. Reconnect under Settings ▸ Terabox and run it again."})
        self._cancel.set()

    def _fail(self, f, page_url, reason):
        if self._cancel.is_set():
            return
        with self._lock:
            self.error_count += 1
            self._on_error({"type": "error", "message": f"{f['rel']}: {reason}"})
            if self._errors:
                try:
                    self._errors.record_failure(
                        f"{self._platform}_terabox_{f['fs_id']}", platform=self._platform,
                        url=page_url, page_url=page_url, filename=f["rel"], reason=reason[:300])
                except Exception:
                    pass

    # ── helpers ────────────────────────────────────────────────────────
    def _thread_session(self):
        s = getattr(_thread_local, "session", None)
        if s is None or getattr(_thread_local, "owner", None) is not self:
            s = self._new_session()
            _thread_local.session, _thread_local.owner = s, self
        return s

    def _sleep(self, secs):
        end = time.time() + secs
        while not self._cancel.is_set() and time.time() < end:
            time.sleep(min(0.5, end - time.time()))

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


class _ShareError(Exception):
    pass


class _Cancelled(Exception):
    pass
