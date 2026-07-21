"""Offline unit checks for the native gofile downloader (no network).

Drives GofileRunner against a fake curl_cffi session backed by an in-memory
"server" (guest-token minting, paginated /contents listings, nested folders,
password gating, CDN byte streaming, 429s). Covers enumeration/pagination,
recursion + preserved subfolders, sha256 password hashing, the website-token
header, skip-existing-by-size vs force, 429 backoff, failure recording, token
re-mint, cancel behaviour, and AlbumRunner dispatch. Run directly:

    python tests/test_gofile_downloader.py
"""
import hashlib
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import album_sites
from backend import album_runner
from backend.album_runner import AlbumRunner
from backend import gofile_downloader
from backend.gofile_downloader import GofileRunner, _API
from backend.download_errors import FailureStore

# Tests exercise logic, not timing: never actually sleep or throttle.
gofile_downloader.GofileRunner._sleep = lambda self, secs: None
gofile_downloader._RateGate.wait = lambda self: None


# ── fake curl_cffi session ──────────────────────────────────────────
class FakeResp:
    def __init__(self, json_data=None, status_code=200, content=b""):
        self._json = json_data
        self.status_code = status_code
        self._content = content

    def json(self):
        return self._json

    def iter_content(self, n):
        for i in range(0, len(self._content), n):
            yield self._content[i:i + n]

    def close(self):
        pass


class FakeSession:
    def __init__(self, server, **kw):
        self.headers = {}
        self._server = server

    def post(self, url, json=None, headers=None, timeout=None):
        if url.endswith("/accounts"):
            self._server["accounts_calls"] += 1
            return FakeResp({"status": "ok",
                             "data": {"token": f"tok-{self._server['accounts_calls']}"}})
        return FakeResp({"status": "error"})

    def get(self, url, params=None, headers=None, cookies=None,
            timeout=None, stream=False):
        if url.startswith(_API + "/contents/"):
            self._server["contents_headers"].append(headers or {})
            self._server["contents_params"].append(params or {})
            if self._server.get("wrongtoken_pending"):
                self._server["wrongtoken_pending"] = False
                return FakeResp({"status": "error-wrongToken"})
            cid = url.rsplit("/", 1)[1]
            pages = self._server["contents"].get(cid, [])
            page = int((params or {}).get("page", "1"))
            if 1 <= page <= len(pages):
                return FakeResp(pages[page - 1])
            return FakeResp({"status": "ok", "data": {"children": {}},
                             "metadata": {"hasNextPage": False}})
        # CDN byte download
        self._server["download_cookies"].append(cookies or {})
        flaky = self._server["flaky"]
        if flaky.get(url, 0) > 0:
            flaky[url] -= 1
            return FakeResp(status_code=429)
        data = self._server["files"].get(url)
        if data is None:
            return FakeResp(status_code=404)
        return FakeResp(content=data)


class _FakeRq:
    def __init__(self, server):
        self._server = server

    def Session(self, **kw):
        return FakeSession(self._server, **kw)


# ── builders / harness ──────────────────────────────────────────────
def _file(fid, name, link, size):
    return {"id": fid, "type": "file", "name": name, "link": link, "size": size}


def _folder(fid, name):
    return {"id": fid, "type": "folder", "name": name}


def _page(children, name="Album", has_next=False):
    return {"status": "ok",
            "data": {"name": name, "children": {c["id"]: c for c in children}},
            "metadata": {"hasNextPage": has_next}}


def _server(**over):
    s = {
        "accounts_calls": 0,
        "contents": {},           # content_id -> [page_json, ...]
        "files": {},              # link -> bytes
        "flaky": {},              # link -> remaining 429 responses
        "contents_headers": [],
        "contents_params": [],
        "download_cookies": [],
        "wrongtoken_pending": False,
    }
    s.update(over)
    return s


def _reset_token():
    gofile_downloader._TOKEN_CACHE["token"] = None
    gofile_downloader._TOKEN_CACHE["ts"] = 0.0


def run_gofile(url, server, password=None, force=False, want_errors=False,
               pre=None, on_prog=None):
    """Run GofileRunner against `server`. Returns an events dict incl. a
    `files_on_disk` snapshot taken before the temp dir is cleaned up."""
    _reset_token()
    gofile_downloader._thread_local = threading.local()
    orig = gofile_downloader._rq
    gofile_downloader._rq = _FakeRq(server)
    events = {"complete": None, "progress": [], "error": [],
              "files_on_disk": {}, "failures": []}
    errors_path = None
    try:
        with tempfile.TemporaryDirectory() as dest:
            if want_errors:
                errors_path = os.path.join(dest, "album_errors.db")
            if pre:
                pre(dest)
            r = GofileRunner()

            def prog(d):
                events["progress"].append(d)
                if on_prog:
                    on_prog(r, d)

            r.run(url, dest, prog,
                  on_complete=lambda s: events.__setitem__("complete", s),
                  on_error=lambda d: events["error"].append(d),
                  password=password, force=force, errors_path=errors_path)

            for root, _, fs in os.walk(dest):
                for f in fs:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, dest).replace("\\", "/")
                    events["files_on_disk"][rel] = os.path.getsize(full)
            if errors_path and os.path.exists(errors_path):
                store = FailureStore(errors_path)
                events["failures"] = store.list_failures(state="failed")
                store.close()
    finally:
        gofile_downloader._rq = orig
    return events


# ── site registry ───────────────────────────────────────────────────
def test_detect_site_gofile():
    for url in ("https://gofile.io/d/ddEJVe", "https://www.gofile.io/d/abc"):
        site = album_sites.detect_site(url)
        assert site is not None and site["key"] == "gofile"
        assert site["engine"] == "gofile"


# ── enumeration / pagination / recursion ────────────────────────────
def test_pagination_downloads_both_pages():
    srv = _server()
    srv["contents"]["root"] = [
        _page([_file("f1", "a.mp4", "cdn://a", 3)], has_next=True),
        _page([_file("f2", "b.mp4", "cdn://b", 3)], has_next=False),
    ]
    srv["files"] = {"cdn://a": b"aaa", "cdn://b": b"bbb"}
    ev = run_gofile("https://gofile.io/d/root", srv)
    assert ev["complete"]["downloaded"] == 2
    assert ev["files_on_disk"] == {"Album (Gofile)/a.mp4": 3, "Album (Gofile)/b.mp4": 3}


def test_nested_folder_structure_preserved():
    srv = _server()
    srv["contents"]["root"] = [
        _page([_file("f1", "top.mp4", "cdn://top", 3), _folder("sub", "clips")],
              name="MyAlbum")]
    srv["contents"]["sub"] = [_page([_file("f2", "inner.mp4", "cdn://inner", 3)],
                                    name="clips")]
    srv["files"] = {"cdn://top": b"xxx", "cdn://inner": b"yyy"}
    ev = run_gofile("https://gofile.io/d/root", srv)
    assert ev["complete"]["downloaded"] == 2
    assert "MyAlbum (Gofile)/top.mp4" in ev["files_on_disk"]
    assert "MyAlbum (Gofile)/clips/inner.mp4" in ev["files_on_disk"]


def test_website_token_and_auth_headers_sent():
    srv = _server()
    srv["contents"]["root"] = [_page([_file("f1", "a.mp4", "cdn://a", 3)])]
    srv["files"] = {"cdn://a": b"aaa"}
    run_gofile("https://gofile.io/d/root", srv)
    h = srv["contents_headers"][0]
    assert h.get("X-Website-Token") and len(h["X-Website-Token"]) == 64
    assert h.get("Authorization", "").startswith("Bearer tok-")
    assert h.get("X-BL") == "en-US"


def test_download_uses_account_token_cookie():
    srv = _server()
    srv["contents"]["root"] = [_page([_file("f1", "a.mp4", "cdn://a", 3)])]
    srv["files"] = {"cdn://a": b"aaa"}
    run_gofile("https://gofile.io/d/root", srv)
    assert srv["download_cookies"], "no CDN download issued"
    assert srv["download_cookies"][0].get("accountToken", "").startswith("tok-")


# ── password handling ───────────────────────────────────────────────
def test_password_is_sha256_query_param():
    srv = _server()
    srv["contents"]["root"] = [_page([_file("f1", "a.mp4", "cdn://a", 3)])]
    srv["files"] = {"cdn://a": b"aaa"}
    run_gofile("https://gofile.io/d/root", srv, password="secret")
    expected = hashlib.sha256(b"secret").hexdigest()
    assert srv["contents_params"][0].get("password") == expected


def test_password_required_reports_error():
    srv = _server()
    srv["contents"]["root"] = [{"status": "error-passwordRequired"}]
    ev = run_gofile("https://gofile.io/d/root", srv)
    assert ev["complete"]["downloaded"] == 0
    assert any("locked" in (e.get("message") or "").lower() for e in ev["error"])


# ── skip-existing vs force ──────────────────────────────────────────
def test_skip_existing_by_size():
    srv = _server()
    srv["contents"]["root"] = [_page([_file("f1", "a.mp4", "cdn://a", 3)])]
    srv["files"] = {"cdn://a": b"aaa"}

    def pre(dest):
        d = os.path.join(dest, "Album (Gofile)")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "a.mp4"), "wb") as f:
            f.write(b"aaa")            # already the right size

    ev = run_gofile("https://gofile.io/d/root", srv, pre=pre)
    assert ev["complete"]["skipped"] == 1
    assert ev["complete"]["downloaded"] == 0
    assert srv["download_cookies"] == []    # never hit the CDN


def test_force_redownloads_existing():
    srv = _server()
    srv["contents"]["root"] = [_page([_file("f1", "a.mp4", "cdn://a", 3)])]
    srv["files"] = {"cdn://a": b"aaa"}

    def pre(dest):
        d = os.path.join(dest, "Album (Gofile)")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "a.mp4"), "wb") as f:
            f.write(b"aaa")

    ev = run_gofile("https://gofile.io/d/root", srv, force=True, pre=pre)
    assert ev["complete"]["downloaded"] == 1
    assert ev["complete"]["skipped"] == 0
    assert srv["download_cookies"], "force should re-fetch present files"


# ── 429 backoff + failure recording ─────────────────────────────────
def test_429_then_success():
    srv = _server()
    srv["contents"]["root"] = [_page([_file("f1", "a.mp4", "cdn://a", 3)])]
    srv["files"] = {"cdn://a": b"aaa"}
    srv["flaky"] = {"cdn://a": 2}       # two 429s, then 200
    ev = run_gofile("https://gofile.io/d/root", srv)
    assert ev["complete"]["downloaded"] == 1
    assert ev["complete"]["errors"] == 0


def test_failure_recorded_with_page_url():
    srv = _server()
    srv["contents"]["root"] = [_page([_file("f1", "a.mp4", "cdn://gone", 3)])]
    srv["files"] = {}                    # link 404s forever
    url = "https://gofile.io/d/root"
    ev = run_gofile(url, srv, want_errors=True)
    assert ev["complete"]["errors"] == 1
    assert len(ev["failures"]) == 1
    row = ev["failures"][0]
    assert row["entry"] == "album_gofile_f1"
    assert row["page_url"] == url
    assert row["filename"] == "a.mp4"


# ── token re-mint ───────────────────────────────────────────────────
def test_wrongtoken_triggers_remint():
    srv = _server(wrongtoken_pending=True)
    srv["contents"]["root"] = [_page([_file("f1", "a.mp4", "cdn://a", 3)])]
    srv["files"] = {"cdn://a": b"aaa"}
    ev = run_gofile("https://gofile.io/d/root", srv)
    assert ev["complete"]["downloaded"] == 1
    assert srv["accounts_calls"] == 2   # initial mint + one re-mint


# ── cancel ──────────────────────────────────────────────────────────
def test_cancel_before_downloads_records_no_failures():
    srv = _server()
    srv["contents"]["root"] = [_page([_file("f1", "a.mp4", "cdn://a", 3)],
                                     has_next=False)]
    srv["files"] = {"cdn://a": b"aaa"}

    def on_prog(runner, d):
        if d.get("type") == "info":     # "Reading the album's file list…"
            runner.cancel()

    ev = run_gofile("https://gofile.io/d/root", srv, want_errors=True, on_prog=on_prog)
    assert ev["complete"]["cancelled"] is True
    assert ev["complete"]["downloaded"] == 0
    assert ev["complete"]["errors"] == 0
    assert ev["failures"] == []         # cancel-noise fix: no spurious failures


# ── bad link / on_complete contract ─────────────────────────────────
def test_bad_link_still_completes_once():
    srv = _server()
    ev = run_gofile("https://gofile.io/not-a-folder", srv)
    assert ev["complete"] is not None
    assert ev["complete"]["downloaded"] == 0
    assert any("gofile folder link" in (e.get("message") or "") for e in ev["error"])


# ── AlbumRunner dispatch + force passthrough ────────────────────────
class _CaptureRunner:
    def __init__(self, calls):
        self._calls = calls
        self.is_running = False

    def cancel(self):
        pass

    def run(self, url, dest, on_progress, on_complete=None, on_error=None,
            password=None, errors_path=None, force=False):
        self._calls.append({"url": url, "password": password, "force": force})
        on_complete({"downloaded": 7, "skipped": 0, "errors": 0,
                     "subfolder": "Album (Gofile)", "cancelled": False})


def test_albumrunner_dispatches_gofile_with_force():
    calls = []
    orig = album_runner.GofileRunner
    album_runner.GofileRunner = lambda *a, **k: _CaptureRunner(calls)
    events = {"complete": None}
    try:
        with tempfile.TemporaryDirectory() as dest, tempfile.TemporaryDirectory() as app:
            r = AlbumRunner(app)
            r.run(destination=dest,
                  links=["https://gofile.io/d/root | pw"],
                  on_progress=lambda d: None,
                  on_complete=lambda d: events.__setitem__("complete", d),
                  on_error=lambda d: None,
                  force_urls=["https://gofile.io/d/root"])
    finally:
        album_runner.GofileRunner = orig
    assert len(calls) == 1
    assert calls[0]["password"] == "pw"
    assert calls[0]["force"] is True
    res = events["complete"]["results"][0]
    assert res["site"] == "gofile"
    assert res["downloaded"] == 7
    assert res["subfolder"] == "Album (Gofile)"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
        passed += 1
    print(f"\n{passed} passed")
