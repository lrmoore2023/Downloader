"""Local media server for the in-app viewer.

A tiny HTTP server bound to 127.0.0.1 that streams a creator's media **straight
from its original location** (the NAS) — nothing is ever copied or cached. It
supports HTTP Range so the `<video>` element can seek smoothly, and sends
`Cache-Control: no-store` so neither the webview nor anything else retains the
bytes.

Two endpoints (every request must carry the session `t=<token>`; the server
binds to loopback only):
  GET /media?p=<abs path>  — stream the original file (Range-aware, no-store)
  GET /thumb?p=<abs path>  — a small cached JPEG preview (ffmpeg), the ONLY
                             thing written to disk; videos themselves are never cached.

Access is gated by an allowlist of roots (the creators' destination folders),
refreshed by the Api via configure(); a path must resolve under one of them and
have a media extension, or the request is refused.
"""

import hashlib
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from backend.coomerfans_scraper import IMAGE_EXTS, VIDEO_EXTS

_ALLOWED_EXTS = IMAGE_EXTS | VIDEO_EXTS
_CHUNK = 1024 * 1024            # 1 MB reads — fewer NAS round-trips
# Cap an open-ended (`bytes=N-`) media request to this window so the server
# never commits to pushing a whole multi-hundred-MB file: the player gets a fast
# first segment and drives the rest with follow-up ranges (snappier start + seek).
_OPEN_RANGE_WINDOW = 8 * 1024 * 1024


def _ext(path):
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


def _content_type(path):
    ctype, _ = mimetypes.guess_type(path)
    if ctype:
        return ctype
    e = _ext(path)
    return {"mkv": "video/x-matroska", "m4v": "video/mp4",
            "webp": "image/webp"}.get(e, "application/octet-stream")


class MediaServer:
    def __init__(self):
        self.token = secrets.token_urlsafe(18)
        self.port = None
        self._httpd = None
        self._thread = None
        self._roots = []          # normalized allowed root prefixes
        self._thumbs_dir = None
        self._lock = threading.Lock()
        self._ff_sem = threading.Semaphore(4)

    # ── lifecycle ────────────────────────────────────────────────
    def start(self):
        if self._httpd:
            return
        server = _QuietServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        server.media = self          # handler reaches us via self.server.media
        self.port = server.server_address[1]
        self._httpd = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def base_url(self):
        return f"http://127.0.0.1:{self.port}" if self.port else ""

    def configure(self, allowed_roots, thumbs_dir):
        with self._lock:
            self._roots = [self._norm(r) for r in (allowed_roots or []) if r]
            self._thumbs_dir = thumbs_dir

    # ── helpers ──────────────────────────────────────────────────
    @staticmethod
    def _norm(p):
        return os.path.normpath(p or "").replace("\\", "/").rstrip("/").lower()

    def is_allowed(self, path):
        if _ext(path) not in _ALLOWED_EXTS:
            return False
        n = self._norm(path)
        with self._lock:
            roots = list(self._roots)
        return any(n == r or n.startswith(r + "/") for r in roots)

    def thumbs_dir(self):
        with self._lock:
            return self._thumbs_dir

    def thumb_path(self, path):
        td = self.thumbs_dir()
        if not td:
            return None
        return os.path.join(td, hashlib.sha1(self._norm(path).encode("utf-8")).hexdigest() + ".jpg")

    def generate_thumb(self, src, thumb):
        exe = shutil.which("ffmpeg")
        if not exe:
            return False
        is_video = _ext(src) in VIDEO_EXTS
        try:
            os.makedirs(os.path.dirname(thumb), exist_ok=True)
        except OSError:
            return False
        tmp = thumb + ".tmp.jpg"

        def run(cmd):
            with self._ff_sem:
                try:
                    return subprocess.run(cmd, capture_output=True, timeout=90).returncode
                except Exception:
                    return -1

        scale = "scale='min(400,iw)':-2"
        if is_video:
            rc = run([exe, "-y", "-ss", "1", "-i", src, "-frames:v", "1", "-vf", scale, "-q:v", "5", tmp])
            if rc != 0 or not os.path.isfile(tmp):   # clip shorter than 1s → grab the first frame
                rc = run([exe, "-y", "-i", src, "-frames:v", "1", "-vf", scale, "-q:v", "5", tmp])
        else:
            rc = run([exe, "-y", "-i", src, "-vf", scale, "-q:v", "5", tmp])

        if rc != 0 or not os.path.isfile(tmp):
            try:
                os.path.isfile(tmp) and os.remove(tmp)
            except OSError:
                pass
            return False
        try:
            os.replace(tmp, thumb)
        except OSError:
            return False
        return True


class _QuietServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that doesn't dump a traceback every time a media
    client aborts a connection (normal when a <video> seeks / closes)."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, *args):
        pass   # stay quiet

    def handle_one_request(self):
        # Swallow the abort that happens when a media client drops a kept-alive
        # connection between requests, so it never bubbles up as a traceback.
        try:
            super().handle_one_request()
        except (ConnectionError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def do_HEAD(self):
        self._dispatch(head=True)

    def do_GET(self):
        self._dispatch(head=False)

    # ── routing ──────────────────────────────────────────────────
    def _dispatch(self, head):
        media = self.server.media
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if qs.get("t", [""])[0] != media.token:
            self.send_error(403, "Forbidden")
            return
        path = qs.get("p", [""])[0]
        if not path:
            self.send_error(400, "Missing path")
            return

        if parsed.path == "/media":
            if not media.is_allowed(path):
                self.send_error(403, "Forbidden")
                return
            if not os.path.isfile(path):
                self.send_error(404, "Not found")
                return
            self._serve_file(path, _content_type(path), no_store=True, head=head)
        elif parsed.path == "/thumb":
            self._serve_thumb(path, head=head)
        else:
            self.send_error(404, "Not found")

    def _serve_thumb(self, path, head):
        media = self.server.media
        if not media.is_allowed(path):
            self.send_error(403, "Forbidden")
            return
        if not os.path.isfile(path):
            self.send_error(404, "Not found")
            return
        thumb = media.thumb_path(path)
        if not thumb:
            self.send_error(503, "No thumbnail cache configured")
            return
        if not (os.path.isfile(thumb) and os.path.getsize(thumb) > 0):
            if not media.generate_thumb(path, thumb):
                self.send_error(502, "Thumbnail generation failed")
                return
        self._serve_file(thumb, "image/jpeg", no_store=False, head=head)

    # ── file streaming with Range ─────────────────────────────────
    def _serve_file(self, path, content_type, no_store, head):
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_error(404, "Not found")
            return

        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                s, e = m.group(1), m.group(2)
                open_ended = False
                if s == "" and e:                       # suffix: last N bytes
                    start, end = max(0, size - int(e)), size - 1
                else:
                    start = int(s) if s else 0
                    if e:
                        end = int(e)
                    else:                               # "bytes=N-" — no end given
                        end = size - 1
                        open_ended = True
                end = min(end, size - 1)
                # Bound an open-ended media range so we hand back a quick first
                # segment instead of the whole file; the player asks for more.
                if open_ended and no_store:
                    end = min(end, start + _OPEN_RANGE_WINDOW - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if no_store:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        else:
            self.send_header("Cache-Control", "public, max-age=86400")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return

        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    data = f.read(min(_CHUNK, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass   # client seeked/closed — normal for video scrubbing
        except OSError:
            pass   # NAS dropped mid-stream
