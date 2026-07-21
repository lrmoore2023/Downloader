"""Native filester downloader.

gallery-dl's filester extractor resolves download URLs to `cache*.filester.me`,
which are dead — every filester download times out. filester actually serves
media from its **v2 API**: `POST /v2/api/public/download {file_slug}` returns
`{server, file, token, name}`, and the bytes live at
`{server}/v2/{file}?token=...&download=true&n={name}` (server is currently
`https://c-fs.cdn.cr`). So we can't fix gallery-dl via config — we download
filester natively here instead.

Flow: unlock the folder if it's password-locked (see filester_unlock), enumerate
every file across `?page=N`, then per file resolve the v2 download URL and stream
it to disk. Mirrors the other runners' contract (is_running / cancel / counters /
run) so AlbumRunner drives it like the cyberdrop-dl and gallery-dl engines.
"""

import base64
import html as _html
import os
import re
import threading
import time
from urllib.parse import urlparse

from backend.download_errors import FailureStore

try:
    from curl_cffi import requests as _rq
    _IMPERSONATE = "chrome"
except Exception:                              # pragma: no cover
    import requests as _rq
    _IMPERSONATE = None

_NONCE = re.compile(r'id="nonce"[^>]*value="([^"]*)"')
_OG_TITLE = re.compile(r'property="og:title" content="([^"]*)"')
_FILE_ITEM = re.compile(r'class="file-item".*?</button>', re.DOTALL)
_DATA_NAME = re.compile(r'data-name="([^"]*)"')
_FILE_ID = re.compile(r"href='/d/([^']+)'")
_NEXT_MARK = ">→</a>"          # the "next page" arrow
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class FilesterRunner:
    def __init__(self, platform="album"):
        self._platform = platform
        self._cancel = threading.Event()
        self._running = False
        self.downloaded_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self._errors = None

    @property
    def is_running(self):
        return self._running

    def cancel(self):
        self._cancel.set()

    def run(self, url, download_dir, on_progress, on_complete, on_error,
            password=None, errors_path=None):
        self._running = True
        self._cancel.clear()
        self.downloaded_count = self.skipped_count = self.error_count = 0
        self._errors = FailureStore(errors_path) if errors_path else None
        subfolder = None
        try:
            p = urlparse((url or "").strip())
            root = f"{p.scheme or 'https'}://{p.hostname}"
            m = re.search(r"/f/([^/?#]+)", p.path or "")
            if not m:
                on_error({"type": "error", "message": "Not a filester folder link"})
                return
            slug = m.group(1)
            s = _rq.Session(impersonate=_IMPERSONATE) if _IMPERSONATE else _rq.Session()

            page1 = s.get(f"{root}/f/{slug}", timeout=30).text
            if _NONCE.search(page1):
                if not password:
                    on_error({"type": "error",
                              "message": "This filester folder is locked — add its password"})
                    return
                if not self._unlock(s, root, slug, password, page1):
                    on_error({"type": "error",
                              "message": "Filester unlock failed — check the password"})
                    return
                page1 = s.get(f"{root}/f/{slug}?page=1", timeout=30).text

            og = _OG_TITLE.search(page1)
            folder_name = (_html.unescape(og.group(1)).strip() if og else "") or slug
            subfolder = self._safe(f"{folder_name} (Filester)")
            dest_dir = os.path.join(download_dir, subfolder)
            os.makedirs(dest_dir, exist_ok=True)

            files = self._enumerate(s, root, slug, page1)
            on_progress({"type": "info", "message": f"Reading the album's file list… {len(files)} files"})

            for fid, fname in files:
                if self._cancel.is_set():
                    break
                self._download_one(s, root, url, fid, fname, dest_dir, on_progress, on_error)
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

    # ── steps ───────────────────────────────────────────────────────────
    def _unlock(self, s, root, slug, password, html_text):
        m = _NONCE.search(html_text)
        if not m:
            return True
        nonce = m.group(1)
        payload = f"{password}|{int(time.time() * 1000)}|{nonce}"
        enc = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        try:
            r = s.post(f"{root}/f/{slug}", data={"nonce": nonce, "password": enc},
                       headers={"referer": f"{root}/f/{slug}"}, timeout=30)
        except Exception:
            return False
        return not ("password-form" in r.text and "file-item" not in r.text)

    def _enumerate(self, s, root, slug, page1_html):
        """Walk every ?page=N, collecting (file_id, filename)."""
        files, seen = [], set()
        page, html_text = 1, page1_html
        while True:
            for block in _FILE_ITEM.findall(html_text):
                fid = _FILE_ID.search(block)
                if not fid or fid.group(1) in seen:
                    continue
                seen.add(fid.group(1))
                name = _DATA_NAME.search(block)
                files.append((fid.group(1),
                              _html.unescape(name.group(1)) if name else fid.group(1)))
            if _NEXT_MARK not in html_text or self._cancel.is_set():
                break
            page += 1
            try:
                html_text = s.get(f"{root}/f/{slug}?page={page}", timeout=30).text
            except Exception:
                break
        return files

    def _download_one(self, s, root, page_url, fid, fname, dest_dir,
                      on_progress, on_error, attempts=4):
        final = os.path.join(dest_dir, self._safe(fname))
        if os.path.exists(final) and os.path.getsize(final) > 0:
            self.skipped_count += 1
            on_progress({"type": "skip", "message": fname})
            return

        last_err = None
        for att in range(attempts):
            if self._cancel.is_set():
                return
            try:
                # Resolve a fresh signed URL each attempt (the token expires).
                d = s.post(f"{root}/v2/api/public/download",
                           json={"file_slug": fid},
                           headers={"content-type": "application/json"}, timeout=30).json()
                if not d.get("success"):
                    raise RuntimeError(d.get("message", "download API error"))
                media = f"{d['server']}/v2/{d['file']}?token={d['token']}&download=true&n={d.get('name') or fname}"
                part = final + ".part"
                # curl_cffi's Response isn't a context manager — close it manually.
                r = s.get(media, headers={"referer": root + "/"}, timeout=60, stream=True)
                try:
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
                self.downloaded_count += 1
                on_progress({"type": "download", "message": fname, "path": final})
                return
            except _Cancelled:
                self._cleanup(final + ".part")
                return
            except Exception as e:
                last_err = e
                self._cleanup(final + ".part")
                if att < attempts - 1 and not self._cancel.is_set():
                    time.sleep(min(2 ** att, 8))

        self.error_count += 1
        reason = str(last_err) or "download failed"
        on_error({"type": "error", "message": f"{fname}: {reason}"})
        if self._errors:
            try:
                self._errors.record_failure(
                    f"{self._platform}_filester_{fid}", platform=self._platform,
                    url=f"{root}/d/{fid}", page_url=page_url, filename=fname,
                    reason=reason[:300])
            except Exception:
                pass

    # ── helpers ─────────────────────────────────────────────────────────
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
