"""TeraboxRunner against a fake terabox.app built from live-captured responses
(page jsToken, shorturlinfo, share/list with int-or-string isdir, sharedownload,
dlink 403 31045 without a session).

    python -m pytest tests/test_terabox_downloader.py -q
"""
import json
import os
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.terabox_downloader as tb

SURL = "pJ9Wxqc4K8mZ7rJUxOiFlw"
FILES = {  # fs_id: (owner path, bytes)
    "11": ("/Share/a.png", b"A" * 300),
    "12": ("/Share/b.png", b"B" * 500),
    "21": ("/Share/sub/c.png", b"C" * 700),
}


class Resp:
    def __init__(self, code=200, body=b"", js=None):
        self.status_code = code
        self._body = json.dumps(js).encode() if js is not None else body
        self.text = self._body.decode("utf-8", "replace")
        self.headers = {}

    def json(self):
        return json.loads(self._body)

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    def close(self):
        pass


class FakeTerabox:
    def __init__(self, signed_in=True):
        self.signed_in = signed_in
        self.ranges = []
        self.dlink_calls = 0

    def session(self):
        srv = self

        class S:
            headers = {}

            class cookies:
                @staticmethod
                def set(*a, **k):
                    pass

            def get(self, url, params=None, headers=None, **k):
                return srv.get(url, params or {}, headers or {})

            def post(self, url, **k):
                return Resp(js={"errno": 0, "randsk": "x"})
        return S()

    def get(self, url, params, headers):
        u = urlparse(url)
        if u.path == "/sharing/link":
            return Resp(body=b"<script>decodeURIComponent(`fn%28%22ABC123%22%29`)</script>")
        if u.path == "/api/shorturlinfo":
            return Resp(js={"errno": 0, "shareid": 1, "uk": 2, "sign": "s", "timestamp": 3,
                            "randsk": "k%2B", "list": [{"isdir": "1", "path": "/Share",
                                                        "server_filename": "Share", "fs_id": "1"}]})
        if u.path == "/share/list":
            d = params["dir"]
            kids = [{"isdir": 0, "path": p, "server_filename": p.rsplit("/", 1)[1],
                     "fs_id": int(fid), "size": len(b)}
                    for fid, (p, b) in FILES.items() if p.rsplit("/", 1)[0] == d]
            if d == "/Share":
                kids.append({"isdir": 1, "path": "/Share/sub", "server_filename": "sub", "fs_id": 2})
            return Resp(js={"errno": 0, "list": kids})
        if u.path == "/api/sharedownload":
            self.dlink_calls += 1
            ids = json.loads(params["fid_list"])
            return Resp(js={"errno": 0, "list": [{"fs_id": i, "dlink": f"https://d.terabox.app/file/{i}"}
                                                 for i in ids]})
        if u.netloc == "d.terabox.app":
            if not self.signed_in:
                return Resp(403, b'{"error_code":31045,"error_msg":"user not exists"}')
            body = FILES[u.path.rsplit("/", 1)[1]][1]
            rng = headers.get("Range")
            if rng:
                self.ranges.append(rng)
                start = int(rng[6:-1])
                r = Resp(206, body[start:])
                return r
            return Resp(200, body)
        return Resp(404)


def _run(tmp_path, srv, cookies={"ndus": "x"}, track=True, force=False):
    orig = tb.TeraboxRunner._new_session
    tb.TeraboxRunner._new_session = lambda self: srv.session()
    tb._thread_local.__dict__.clear()
    events, errors, done = [], [], []
    try:
        r = tb.TeraboxRunner(cookies=cookies,
                             archive_path=str(tmp_path / "arc.db") if track else None)
        r.run(f"https://www.terabox.app/sharing/link?surl={SURL}", str(tmp_path / "dest"),
              events.append, done.append, errors.append,
              errors_path=str(tmp_path / "err.db"), force=force)
    finally:
        tb.TeraboxRunner._new_session = orig
    assert len(done) == 1
    return done[0], events, errors


def test_parse_surl_variants():
    assert tb.parse_surl(f"https://www.terabox.app/sharing/link?surl={SURL}") == SURL
    assert tb.parse_surl(f"https://1024terabox.com/s/1{SURL}") == SURL
    assert tb.parse_surl("https://gofile.io/d/abc") is None


def test_downloads_the_whole_tree_keeping_folders(tmp_path):
    st, ev, err = _run(tmp_path, FakeTerabox())
    assert st["downloaded"] == 3 and not err and st["subfolder"] == "Share"
    dest = tmp_path / "dest" / "Share"
    assert (dest / "a.png").read_bytes() == FILES["11"][1]
    assert (dest / "sub" / "c.png").read_bytes() == FILES["21"][1]


def test_tracked_file_deleted_later_is_not_refetched(tmp_path):
    _run(tmp_path, FakeTerabox())
    os.remove(tmp_path / "dest" / "Share" / "a.png")
    st, _, _ = _run(tmp_path, FakeTerabox())
    assert st["downloaded"] == 0 and st["skipped"] == 3
    assert not (tmp_path / "dest" / "Share" / "a.png").exists()


def test_untracked_run_refetches_what_is_missing_and_leaves_no_record(tmp_path):
    _run(tmp_path, FakeTerabox(), track=False)
    os.remove(tmp_path / "dest" / "Share" / "a.png")
    st, _, _ = _run(tmp_path, FakeTerabox(), track=False)
    assert st["downloaded"] == 1 and st["skipped"] == 2
    assert not (tmp_path / "arc.db").exists()


def test_force_redownloads_even_tracked_files(tmp_path):
    _run(tmp_path, FakeTerabox())
    st, _, _ = _run(tmp_path, FakeTerabox(), force=True)
    assert st["downloaded"] == 3


def test_resumes_a_partial_file(tmp_path):
    srv = FakeTerabox()
    d = tmp_path / "dest" / "Share"
    d.mkdir(parents=True)
    (d / "b.png.part").write_bytes(FILES["12"][1][:200])
    st, _, _ = _run(tmp_path, srv)
    assert "bytes=200-" in srv.ranges
    assert (d / "b.png").read_bytes() == FILES["12"][1]


def test_not_signed_in_stops_once_with_one_message(tmp_path):
    st, _, err = _run(tmp_path, FakeTerabox(signed_in=False))
    assert st["needs_terabox_auth"] and st["errors"] == 0 and len(err) == 1
    assert "Reconnect" in err[0]["message"]
    assert not list((tmp_path / "dest").rglob("*.part"))


def test_no_session_at_all_is_refused_up_front(tmp_path):
    srv = FakeTerabox()
    st, _, err = _run(tmp_path, srv, cookies={})
    assert st["needs_terabox_auth"] and srv.dlink_calls == 0 and "Settings" in err[0]["message"]


def test_album_runner_routes_terabox_with_tracking(tmp_path, monkeypatch):
    import backend.album_runner as ar
    seen = {}

    class Fake:
        def __init__(self, platform="album", cookies=None, user_agent=None, archive_path=None):
            seen.update(cookies=cookies, archive=archive_path)

        def cancel(self):
            pass

        def run(self, url, dest, prog, on_complete, on_error, **k):
            on_complete({"downloaded": 1, "skipped": 0, "errors": 0, "subfolder": "Share",
                         "needs_terabox_auth": False})
    monkeypatch.setattr(ar, "TeraboxRunner", Fake)
    url = f"https://www.terabox.app/sharing/link?surl={SURL}"
    out = []
    runner = ar.AlbumRunner(str(tmp_path), terabox={"cookies": {"ndus": "x"},
                                                    "archive_dir": str(tmp_path)})
    runner.run(str(tmp_path), [url], lambda d: None, out.append, lambda d: None, track_urls=[url])
    assert out[0]["results"][0]["site"] == "terabox" and seen["cookies"] == {"ndus": "x"}
    assert seen["archive"] == str(tmp_path / f"terabox_{SURL}.db")
    runner.run(str(tmp_path), [url], lambda d: None, out.append, lambda d: None)
    assert seen["archive"] is None
