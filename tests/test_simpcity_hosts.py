"""turbo.cr / goonbox.cr resolvers and the shared resumable downloader."""

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import goonbox_host as gb
from backend import http_download as hd
from backend import turbo_host as th


# ── fake HTTP ───────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), 7):
            yield self._body[i:i + 7]

    def close(self):
        self.closed = True


class RangeServer:
    """Serves `data` honouring Range; `script` can override the first responses."""

    def __init__(self, data, script=None, cut_first=None):
        self.data = data
        self.script = list(script or [])
        self.cut_first = cut_first
        self.calls = []

    def get(self, url, stream=False, headers=None, timeout=None, **kw):
        headers = headers or {}
        self.calls.append((url, dict(headers)))
        if self.script:
            item = self.script.pop(0)
            if item is not None:
                return item
        rng = headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            body = self.data[start:]
            return FakeResp(206, body, {"Content-Range": f"bytes {start}-{len(self.data)-1}/{len(self.data)}",
                                        "Content-Length": str(len(body)),
                                        "Content-Type": "video/mp4"})
        body = self.data
        if self.cut_first is not None:
            body, self.cut_first = body[:self.cut_first], None
        return FakeResp(200, body, {"Content-Length": str(len(self.data)), "Content-Type": "video/mp4"})


DATA = bytes(range(256)) * 40
MD5 = hashlib.md5(DATA).hexdigest()


def _dl(tmp_path, server, **kw):
    dest = str(tmp_path / "out.mp4")
    kw.setdefault("sleep", lambda s: None)
    return dest, hd.download(server, "https://cdn/x.mp4", dest, **kw)


def test_download_plain(tmp_path):
    dest, res = _dl(tmp_path, RangeServer(DATA), expected_size=len(DATA), md5=MD5)
    assert res.ok and open(dest, "rb").read() == DATA
    assert not os.path.exists(dest + ".part")


def test_download_resumes_after_truncation(tmp_path):
    srv = RangeServer(DATA, cut_first=1000)
    dest, res = _dl(tmp_path, srv, md5=MD5)
    assert res.ok and open(dest, "rb").read() == DATA
    assert srv.calls[1][1].get("Range") == "bytes=1000-"


def test_download_misaligned_resume_restarts(tmp_path):
    bad = FakeResp(206, DATA[50:], {"Content-Range": f"bytes 50-{len(DATA)-1}/{len(DATA)}"})
    srv = RangeServer(DATA, script=[None, bad], cut_first=100)
    dest, res = _dl(tmp_path, srv, md5=MD5)
    assert res.ok and open(dest, "rb").read() == DATA


def test_download_refreshes_expired_url(tmp_path):
    srv = RangeServer(DATA, script=[FakeResp(403)])
    dest, res = _dl(tmp_path, srv, refresh=lambda: "https://cdn/fresh.mp4")
    assert res.ok
    assert srv.calls[-1][0] == "https://cdn/fresh.mp4"


def test_download_gone(tmp_path):
    srv = RangeServer(DATA, script=[FakeResp(404)] * 5)
    dest, res = _dl(tmp_path, srv)
    assert res.status == "gone" and not os.path.exists(dest)


def test_download_refresh_reports_gone(tmp_path):
    def refresh():
        raise hd.Gone()
    srv = RangeServer(DATA, script=[FakeResp(410)])
    _, res = _dl(tmp_path, srv, refresh=refresh)
    assert res.status == "gone"


def test_download_checksum_mismatch_retries_then_fails(tmp_path):
    dest, res = _dl(tmp_path, RangeServer(DATA), md5="0" * 32)
    assert res.status == "failed" and "checksum" in res.reason
    assert not os.path.exists(dest) and not os.path.exists(dest + ".part")


def test_download_html_instead_of_file_is_not_written(tmp_path):
    page = FakeResp(200, b"<html>blocked</html>", {"Content-Type": "text/html"})
    srv = RangeServer(DATA, script=[page])
    dest, res = _dl(tmp_path, srv, md5=MD5)
    assert res.ok and open(dest, "rb").read() == DATA


def test_download_cancel(tmp_path):
    dest, res = _dl(tmp_path, RangeServer(DATA), cancelled=lambda: True)
    assert res.status == "cancelled" and not os.path.exists(dest)


def test_download_network_errors_exhaust(tmp_path):
    class Boom:
        def get(self, *a, **k):
            raise ConnectionError("down")
    dest, res = _dl(tmp_path, Boom(), max_attempts=3)
    assert res.status == "failed" and "down" in res.reason


# ── turbo ───────────────────────────────────────────────────────────

TURBO_PAGE = """
<span>video/mp4</span><span class="text-faint">·</span>
<span>ID: <span class="mono text-ink">zcIToefVY52jO</span></span><span class="text-faint">·</span>
<span>2026-08-23</span>
<div class="text-[11px] text-faint mt-1 tnum">Exact: <span id="fileSizeBytes">9.901727e&#43;06</span> bytes</div>
<div class="text-xs text-muted">Checksum (MD5)</div>
<div class="mono text-sm mt-1 truncate">AB9f8c25a02605c796ff0b97c0f81f8c</div>
<div class="flex"><dt class="text-muted">Filename</dt><dd class="truncate">yKVpX7sOCcB18_M5.mp4</dd></div>
"""


def test_turbo_parse_url():
    assert th.parse_url("https://turbo.cr/d/zcIToefVY52jO") == ("file", "zcIToefVY52jO")
    assert th.parse_url("https://turbo.cr/embed/abc_1-2") == ("file", "abc_1-2")
    assert th.parse_url("https://saint2.su/embed/abc") == ("file", "abc")
    assert th.parse_url("https://turbovid.cr/v/abc") == ("file", "abc")
    assert th.parse_url("https://turbo.cr/a/AlBuM") == ("album", "AlBuM")
    assert th.parse_url("https://turbo.cr/") is None
    assert th.parse_url("https://bunkr.cr/a/x") is None


def test_turbo_parse_file_page():
    m = th.parse_file_page(TURBO_PAGE)
    assert m == {"md5": "ab9f8c25a02605c796ff0b97c0f81f8c", "size": 9901727,
                 "upload_date": "2026-08-23", "filename": "yKVpX7sOCcB18_M5.mp4"}
    assert th.parse_file_page("") == {"md5": None, "size": None, "upload_date": None,
                                      "filename": None}


def test_turbo_parse_album_page():
    html = """<h1 class="x">My <span>Album</span></h1><tbody id="fileTbody">
      <tr data-id="aaa"></tr><tr class="r" data-id="bbb"></tr><tr data-id="aaa"></tr></tbody>
      <a class="btn" href="/a/x?page=2">Next</a>"""
    title, ids, has_next = th.parse_album_page(html)
    assert title == "My Album" and ids == ["aaa", "bbb"] and has_next


def test_turbo_sign_gone():
    class S:
        def get(self, *a, **k):
            return type("R", (), {"status_code": 404})()
    try:
        th.sign(S(), "nope")
        assert False
    except th.TurboGone:
        pass


# ── goonbox ─────────────────────────────────────────────────────────

def test_goonbox_parse_url():
    assert gb.parse_url("https://goonbox.cr/img/tSk0Ku9") == ("image", "tSk0Ku9")
    assert gb.parse_url("https://goonbox.cr/img/some-title.tSk0Ku9") == ("image", "tSk0Ku9")
    assert gb.parse_url("https://goonbox.cr/a/Ab12") == ("album", "Ab12")
    u = "https://simp6.cuckcapital.cr/images4/x.jpg"
    assert gb.parse_url(u) == ("direct", u)
    assert gb.parse_url("https://example.com/img/x") is None


def test_goonbox_thumb_to_full():
    assert gb.thumb_to_full("https://simp6.jpg5.su/images3/abc.md.jpg") == \
        "https://simp6.jpg5.su/images3/abc.jpg"
    assert gb.thumb_to_full("https://x/a/b.th.png") == "https://x/a/b.png"
    assert gb.thumb_to_full("https://x/a/b.png") == "https://x/a/b.png"


class FakeCookies(dict):
    pass


class GoonSession:
    def __init__(self, image_status=200):
        self.cookies = FakeCookies()
        self.image_status = image_status
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, headers))
        if url.endswith("/sanctum/csrf-cookie"):
            self.cookies["XSRF-TOKEN"] = "tok%3D"
            return FakeJson(204, {})
        if "/api/images/" in url:
            return FakeJson(self.image_status, {"image": {
                "original_url": "https://simp6.cuckcapital.cr/images4/o.jpg",
                "original_filename": "o.jpg", "size_bytes": 10,
                "created_at": "2026-08-23T06:08:52.000000Z"}})
        return FakeJson(404, {})


class FakeJson:
    def __init__(self, status, data):
        self.status_code = status
        self._d = data

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_goonbox_image_handshake_and_header():
    s = GoonSession()
    info = gb.GoonboxAPI(s).image("tSk0Ku9")
    assert info["url"].endswith("/o.jpg") and info["size"] == 10 and info["filename"] == "o.jpg"
    assert s.calls[0][0].endswith("/sanctum/csrf-cookie")
    assert s.calls[1][1]["X-XSRF-TOKEN"] == "tok="


def test_goonbox_gone():
    try:
        gb.GoonboxAPI(GoonSession(image_status=404)).image("x")
        assert False
    except gb.GoonboxGone:
        pass
