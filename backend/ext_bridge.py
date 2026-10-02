"""Localhost receiver for the PMV Prefix Chrome extension.

rule34video allows one session per account: signing the app in logs the user's
browser out and vice versa. So instead of a second login, the extension's popup
hands over the session the browser already has (same cookies → same session, no
new login, nobody gets signed out). This tiny server accepts exactly that.

* Bound to 127.0.0.1 on a fixed port (the extension has to know where to find
  the app), so nothing off-machine can reach it.
* Only requests carrying a `chrome-extension://` Origin are accepted. A web page
  cannot forge Origin, so a site open in the browser cannot push cookies in.
* The handler only passes the payload to `on_session`; the Api verifies the jar
  with a members-only request before keeping any of it.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 47834
MAX_BODY = 256 * 1024


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        pass


def _ext_origin(origin):
    return bool(origin) and origin.startswith("chrome-extension://")


class _Handler(BaseHTTPRequestHandler):
    server_version = "DownloaderBridge/1"

    def log_message(self, *args):
        pass

    def _send(self, code, payload, origin=""):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if _ext_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        origin = self.headers.get("Origin", "")
        if not _ext_origin(origin):
            self._send(403, {"ok": False, "message": "forbidden"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self):
        origin = self.headers.get("Origin", "")
        if self.path.rstrip("/") == "/ping" and _ext_origin(origin):
            self._send(200, {"ok": True, "app": "Downloader"}, origin)
            return
        self._send(404, {"ok": False}, origin)

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        if not _ext_origin(origin):
            self._send(403, {"ok": False, "message": "forbidden"})
            return
        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["pmv", "session"]:
            self._send(404, {"ok": False, "message": "unknown path"}, origin)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            self._send(400, {"ok": False, "message": "bad body"}, origin)
            return
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            self._send(400, {"ok": False, "message": "bad JSON"}, origin)
            return
        try:
            res = self.server.on_session(parts[2], payload) or {}
        except Exception as e:
            res = {"ok": False, "message": f"{e.__class__.__name__}: {e}"}
        self._send(200, res, origin)


class ExtBridge:
    def __init__(self, on_session, port=PORT):
        self._on_session = on_session
        self.port = port
        self._server = None
        self.error = ""

    def start(self):
        """Start serving on a daemon thread. Returns False (and records why) if
        the port is taken — e.g. a second copy of the app is open."""
        if self._server is not None:
            return True
        try:
            srv = _Server(("127.0.0.1", self.port), _Handler)
        except OSError as e:
            self.error = str(e)
            return False
        srv.on_session = self._on_session
        self._server = srv
        threading.Thread(target=srv.serve_forever, daemon=True, name="ext-bridge").start()
        return True

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
