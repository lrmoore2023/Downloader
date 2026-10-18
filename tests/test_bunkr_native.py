"""Native bunkr engine: page parsing, per-node limit, cold-storage passes, gone vs outage."""

import os
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import album_track_runner as at
from backend import bunkr_host as bh
from tests.test_album_track import run, sandbox  # noqa: F401  (fixture)

ALBUM_HTML = r"""<meta property="og:title" content="atowerss">
<script>window.albumFiles = [
{
  id:  55080250 ,
  name: "98369ada.mp4",
  original: "clip_one.mp4",
  slug: "KwudtRKmBcVTF",
  type: "video/mp4",
  extension: "Video",
  size:  6878859 ,
  timestamp: "01:51:12 27/10/2025",
  thumbnail: "https://static.scdn.st/x.png",
  cdnEndpoint: "/98369ada.mp4"
},
{
  id:  55080245 ,
  name: "c8a481fe.mp4",
  original: "two \"quoted\".mp4",
  slug: "pr38YY01TKni2",
  type: "video/mp4",
  extension: "Video",
  size:  6715404 ,
  timestamp: "23:59:05 26/10/2025",
  thumbnail: "https://static.scdn.st/y.png",
  cdnEndpoint: "/c8a481fe.mp4"
},
];</script>"""


def test_parse_album_page():
    title, files = bh.parse_album_page(ALBUM_HTML)
    assert title == "atowerss"
    assert [(f["id"], f["slug"], f["original"], f["size"]) for f in files] == [
        ("55080250", "KwudtRKmBcVTF", "clip_one.mp4", 6878859),
        ("55080245", "pr38YY01TKni2", 'two "quoted".mp4', 6715404)]
    assert files[0]["uploaded"] == datetime(2025, 10, 27, 1, 51, 12, tzinfo=timezone.utc)
    assert bh.parse_album_page("<html>no files</html>") == ("", [])


def test_parse_file_page_and_url():
    html = ('<meta property="og:title" content="atowerss_video_1.mp4">'
            '<a class="btn ic-download-01" href="https://dl.bunkr.cr/file/63863101">')
    assert bh.parse_file_page(html) == {"id": "63863101", "name": "atowerss_video_1.mp4"}
    assert bh.parse_url("https://bunkr.pk/f/E4ZM1KwzCWSAc") == ("file", "E4ZM1KwzCWSAc")
    assert bh.parse_url("https://bunkr.site/v/abc") == ("file", "abc")
    assert bh.parse_url("https://bunkr.ph/a/fbQrHXsl") == ("album", "fbQrHXsl")
    assert bh.parse_url("https://gofile.io/d/x") is None


def test_resolve_sends_string_id():
    sent = {}

    class S:
        def post(self, url, json=None, **kw):
            sent.update(json)
            return type("R", (), {"status_code": 200, "json": lambda self: {
                "mediafiles": "https://c6no3-b.cdn.cr", "path": "/storage/media/x.mp4",
                "original": "x.mp4"}})()

        def get(self, url, params=None, **kw):
            return type("R", (), {"status_code": 200,
                                  "json": lambda self: {"token": "T", "ex": 9}})()
    r = bh.resolve(S(), 63863101)
    assert sent == {"id": "63863101"}         # an int gets "400 Invalid JSON payload"
    assert r == {"url": "https://c6no3-b.cdn.cr/storage/media/x.mp4?token=T&ex=9",
                 "node": "c6no3-b.cdn.cr", "original": "x.mp4"}


class FakeBunkr:
    def __init__(self, files, cold=None, gone=(), busy=0):
        self.files = files
        self.cold = dict(cold or {})           # file id → warm attempts that time out
        self.gone = set(gone)
        self.busy = busy                       # leading 503s from the node
        self.active = set()
        self.peak = 0
        self.lock = threading.Lock()

    def install(self, mp):
        mp.setattr(at.bh, "make_session", lambda: object())
        mp.setattr(at.bh, "album", lambda s, ref: ("atowerss", self.files))
        mp.setattr(at.bh, "file_info", lambda s, slug: {"id": "999", "name": "lone.mp4"})
        mp.setattr(at.bh, "resolve", self.resolve)
        mp.setattr(at.bh, "warm", self.warm)
        mp.setattr(at.bh, "sleep_cancellable", lambda sec, c: None)
        mp.setattr(at, "COLD_WAIT", 0)

    def resolve(self, s, fid):
        if fid in self.gone:
            raise bh.BunkrGone(fid)
        return {"url": f"https://node1/{fid}", "node": "node1", "original": None}

    def warm(self, s, url):
        fid = url.rsplit("/", 1)[-1]
        with self.lock:
            if self.busy:
                self.busy -= 1
                return 503, None, None
            if self.cold.get(fid):
                self.cold[fid] -= 1
                return None, None, None
        return 206, 5, datetime(2026, 9, 28, 6, 37, tzinfo=timezone.utc)


def _bfile(i):
    return {"id": str(i), "slug": f"s{i}", "name": f"n{i}.mp4", "original": f"clip{i}.mp4",
            "type": "video/mp4", "size": 5,
            "uploaded": datetime(2025, 10, 27, 12, 0, tzinfo=timezone.utc)}


def test_album_dates_node_limit_and_rerun(sandbox, monkeypatch):
    os.makedirs(sandbox["arch"])
    fake = FakeBunkr([_bfile(i) for i in range(6)])
    fake.install(monkeypatch)
    inner = at.hd.download

    def counting(session, url, path, **kw):
        with fake.lock:
            fake.active.add(url)
            fake.peak = max(fake.peak, len(fake.active))
        time.sleep(0.05)
        try:
            return inner(session, url, path, **kw)
        finally:
            with fake.lock:
                fake.active.discard(url)
    monkeypatch.setattr(at.hd, "download", counting)
    done, errs = run(sandbox, at.parse_album_url("https://bunkr.si/a/ALB"))
    assert done["downloaded"] == 6 and done["errors"] == 0 and done["title"] == "atowerss", errs
    assert 1 < fake.peak <= bh.PER_NODE             # parallel, but never a 3rd on a node
    root = os.path.join(sandbox["dest"], "Simpcity", "Bunkr", "2025")
    assert "2025.10.27 - Bunkr - clip0.mp4" in os.listdir(root)
    done, _ = run(sandbox, at.parse_album_url("https://bunkr.si/a/ALB"))
    assert done["downloaded"] == 0 and done["skipped"] == 6


def test_cold_file_is_retried_in_a_later_pass(sandbox, monkeypatch):
    os.makedirs(sandbox["arch"])
    FakeBunkr([_bfile(1), _bfile(2)], cold={"2": 1}).install(monkeypatch)
    done, errs = run(sandbox, at.parse_album_url("https://bunkr.cr/a/ALB"))
    assert done["downloaded"] == 2 and done["errors"] == 0, errs


def test_busy_node_waits_instead_of_failing(sandbox, monkeypatch):
    os.makedirs(sandbox["arch"])
    FakeBunkr([_bfile(1)], busy=2).install(monkeypatch)
    done, errs = run(sandbox, at.parse_album_url("https://bunkr.cr/a/ALB"))
    assert done["downloaded"] == 1 and done["errors"] == 0, errs


def test_still_cold_after_every_pass_is_recorded(sandbox, monkeypatch):
    os.makedirs(sandbox["arch"])
    FakeBunkr([_bfile(1)], cold={"1": 99}).install(monkeypatch)
    done, errs = run(sandbox, at.parse_album_url("https://bunkr.cr/a/ALB"))
    assert done["downloaded"] == 0 and done["errors"] == 1
    assert any("didn't serve" in e["message"] for e in errs)


def test_removed_file_and_removed_album(sandbox, monkeypatch):
    os.makedirs(sandbox["arch"])
    FakeBunkr([_bfile(1), _bfile(2)], gone=["2"]).install(monkeypatch)
    done, _ = run(sandbox, at.parse_album_url("https://bunkr.cr/a/ALB"))
    assert done["downloaded"] == 1 and done["errors"] == 1 and done["alive"] is True

    def gone(s, ref):
        raise bh.BunkrGone(ref)
    monkeypatch.setattr(at.bh, "album", gone)
    done, _ = run(sandbox, at.parse_album_url("https://bunkr.cr/a/DEAD"))
    assert done["alive"] is False


def test_outage_is_not_gone(sandbox, monkeypatch):
    os.makedirs(sandbox["arch"])
    FakeBunkr([]).install(monkeypatch)

    def down(s, ref):
        raise bh.BunkrUnavailable("bunkr didn't answer (HTTP 504)")
    monkeypatch.setattr(at.bh, "album", down)
    done, errs = run(sandbox, at.parse_album_url("https://bunkr.cr/a/SLOW"))
    assert done["alive"] is None and any("try again later" in e["message"] for e in errs)


def test_single_file_dated_by_last_modified(sandbox, monkeypatch):
    os.makedirs(sandbox["arch"])
    FakeBunkr([]).install(monkeypatch)
    done, errs = run(sandbox, at.parse_album_url("https://bunkr.pk/f/E4ZM1KwzCWSAc"),
                     origin_date="2026-09-01T12:00:00+00:00")
    assert done["downloaded"] == 1, errs
    assert os.path.isfile(os.path.join(sandbox["dest"], "Simpcity", "Bunkr", "2026",
                                       "2026.09.28 - Bunkr - lone.mp4"))
