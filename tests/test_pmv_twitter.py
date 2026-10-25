"""X/Twitter in the PMV tab: URL parsing, the gallery-dl child-process walk
(with a fake process), video-only items, and the runner's chronological merge.

    python -m pytest tests/test_pmv_twitter.py -q
"""
import io
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.pmv_runner as prm
import backend.pmv_tracker as pt
import backend.twitter_pmv as twp


# ── URLs ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,user", [
    ("https://x.com/jjadesyx/media", "jjadesyx"),
    ("https://x.com/jjadesyx", "jjadesyx"),
    ("https://twitter.com/JJadeSyx/status/123", "JJadeSyx"),
    ("x.com/some_one?lang=en", "some_one"),
    ("https://mobile.twitter.com/abc/", "abc"),
])
def test_parse_profile_url(url, user):
    assert twp.parse_profile_url(url) == {"username": user}


@pytest.mark.parametrize("url", ["https://x.com/i/bookmarks", "https://x.com/home", "https://x.com/search?q=a",
                                 "https://foox.com/abc", "https://example.com/x.com"])
def test_parse_profile_url_rejects(url):
    assert twp.parse_profile_url(url) is None


def test_auth_options(tmp_path):
    good = tmp_path / "c.txt"
    good.write_text("# Netscape\n.x.com\tTRUE\t/\tTRUE\t0\tauth_token\tabc\n", encoding="utf-8")
    assert twp.auth_options({"cookies_path": str(good)}) == (str(good), "", "")
    assert twp.auth_options({"cookies_browser": "firefox"}) == ("", "firefox", "")
    path, browser, problem = twp.auth_options({"cookies_path": str(tmp_path / "missing.txt")})
    assert not path and not browser and "Settings" in problem
    assert twp.auth_options({})[2]


# ── tweets → items ──────────────────────────────────────────────────

def _tweet(tid, date, files, content="hello  world", author="jjadesyx"):
    return {"id": str(tid), "date": date, "content": content, "author": author, "files": files}


def _vid(w=1920, h=1080, dur=61.4, num=1):
    return {"type": "video", "width": w, "height": h, "duration": dur, "extension": "mp4", "num": num}


def test_video_items_keep_only_video_posts():
    tweets = [
        _tweet(30, "2026-10-03T10:00:00+00:00", [_vid(), _vid(1280, 720, 200.0, 2)]),
        _tweet(20, "2026-10-02T10:00:00+00:00", [{"type": "photo", "width": 10, "height": 10}]),
        _tweet(15, "2026-10-02T09:00:00+00:00", [{"type": "animated_gif", "width": 10, "height": 10}]),
        _tweet(10, "2026-10-01T10:00:00+00:00", [{"type": "photo"}, _vid(720, 1280, 9.6)]),
    ]
    items = twp.video_items(tweets, "jjadesyx")
    assert [i["id"] for i in items] == ["30", "10"]
    a, b = items
    assert a["title"] == "hello world" and a["video_count"] == 2
    assert a["quality"] == 1080 and a["duration"] == 200
    assert a["url"] == "https://x.com/jjadesyx/status/30"
    assert b["quality"] == 720 and b["video_count"] == 1
    assert a["pos"] < b["pos"]                               # newest first, as walked


# ── the child-process walk ──────────────────────────────────────────

class FakeProc:
    """Stands in for the gallery-dl child: yields the given stdout lines, then
    optionally hangs until killed."""
    def __init__(self, lines, hang=False, delay=0.0):
        self.killed = threading.Event()
        self._rc = None

        def gen():
            for ln in lines:
                if delay:
                    time.sleep(delay)
                if self.killed.is_set():
                    return
                yield (json.dumps(ln) if isinstance(ln, dict) else ln) + "\n"
            if hang:
                self.killed.wait(30)
        self.stdout = gen()
        self.stderr = io.StringIO("")

    def poll(self):
        return self._rc

    def kill(self):
        self.killed.set()
        self._rc = -9

    def wait(self, timeout=None):
        self._rc = self._rc if self._rc is not None else 0
        return self._rc


def _lines(*tweets, status=0, stopped=False, logs=()):
    out = [{"t": "user", "id": "1", "name": "jjadesyx", "nick": "Jade", "media_count": len(tweets)}]
    for tid, typ in tweets:
        out.append({"t": "tweet", "id": str(tid), "date": "2026-10-0%dT12:00:00+00:00" % (tid % 9 + 1),
                    "content": f"t{tid}", "author": "jjadesyx"})
        out.append({"t": "file", "tweet_id": str(tid), "num": 1, "type": typ, "width": 1920,
                    "height": 1080, "duration": 30})
    out.extend({"t": "log", "level": lvl, "msg": msg} for lvl, msg in logs)
    out.append({"t": "done", "status": status, "stopped": stopped})
    return out


def test_walk_media_collects_tweets_and_files():
    seen = []
    res = twp.walk_media("jjadesyx", popen=lambda cmd: FakeProc(_lines((3, "video"), (2, "photo"))),
                         on_event=lambda k, d: seen.append(k))
    assert [t["id"] for t in res["tweets"]] == ["3", "2"]
    assert res["tweets"][0]["files"][0]["type"] == "video"
    assert res["user"]["media_count"] == 2 and not res["stopped"]
    assert seen.count("tweet") == 2


def test_walk_media_passes_stop_below():
    cmds = []

    def popen(cmd):
        cmds.append(cmd)
        return FakeProc(_lines((5, "video"), stopped=True))
    res = twp.walk_media("u", stop_below=99, stop_after=7, popen=popen)
    assert cmds[0][-2:] == ["99", "7"] and cmds[0][-3] == "https://x.com/u/media"
    assert res["stopped"]


@pytest.mark.parametrize("msg,exc,needle", [
    ("AuthRequired: authenticated cookies needed to access this timeline", twp.AuthError, "Settings"),
    ("Rate limit exceeded", twp.SiteError, "rate limited"),
    ("Unable to download data:  ConnectionError: HTTPSConnectionPool", twp.SiteError, "unreachable"),
    ("HttpError: '503 Service Unavailable' for 'https://x.com/i/api'", twp.SiteError, "HTTP 503"),
])
def test_walk_media_errors_are_classified(msg, exc, needle):
    with pytest.raises(exc) as ei:
        twp.walk_media("u", popen=lambda cmd: FakeProc(_lines(status=4, logs=[("error", msg)])))
    assert needle in str(ei.value)


def test_walk_media_partial_walk_is_an_error_not_a_result():
    lines = _lines((3, "video"), status=4, logs=[("error", "HttpError: '500 Internal' for x")])
    with pytest.raises(twp.SiteError):
        twp.walk_media("u", popen=lambda cmd: FakeProc(lines))


def test_walk_media_no_done_line_is_an_error():
    lines = _lines((3, "video"))[:-1]
    with pytest.raises(twp.SiteError, match="unexpectedly"):
        twp.walk_media("u", popen=lambda cmd: FakeProc(lines))


def test_walk_media_kills_a_silent_child():
    procs = []

    def popen(cmd):
        procs.append(FakeProc(_lines((3, "video"))[:2], hang=True))
        return procs[0]
    t0 = time.monotonic()
    with pytest.raises(twp.SiteError, match="not responding"):
        twp.walk_media("u", popen=popen, idle_timeout=0.5)
    assert time.monotonic() - t0 < 5 and procs[0].killed.is_set()


def test_walk_media_cancel_kills_the_child():
    procs, cancel = [], threading.Event()

    def popen(cmd):
        procs.append(FakeProc(_lines((3, "video"))[:2], hang=True))
        threading.Timer(0.3, cancel.set).start()
        return procs[0]
    res = twp.walk_media("u", popen=popen, should_cancel=cancel.is_set)
    assert res["cancelled"] and procs[0].killed.is_set()


def test_walk_media_removes_its_config(tmp_path, monkeypatch):
    made = []
    real = twp.tempfile.mkstemp

    def mk(**kw):
        fd, p = real(dir=str(tmp_path), **kw)
        made.append(p)
        return fd, p
    monkeypatch.setattr(twp.tempfile, "mkstemp", mk)
    twp.walk_media("u", cookies_path="C:/c.txt", popen=lambda cmd: FakeProc(_lines()))
    assert made and not os.path.exists(made[0])


def test_build_config_never_waits_on_rate_limits():
    cfg = twp.build_config(cookies_path="C:\\a\\c.txt")["extractor"]["twitter"]
    assert cfg["ratelimit"] == "abort" and cfg["cookies"] == "C:/a/c.txt"
    assert cfg["retweets"] is False and cfg["videos"] is True


# ── runner: chronological, incremental, ∅ ───────────────────────────

class FakeWalk:
    def __init__(self):
        self.timeline = []          # [(tweet id, type)] newest first
        self.calls = []
        self.error = None

    def __call__(self, username, *, cookies_path, cookies_browser, stop_below, should_cancel, on_event):
        self.calls.append(stop_below)
        if self.error:
            raise self.error
        tweets = []
        run = 0
        for tid, typ in self.timeline:
            tweets.append(_tweet(tid, f"2026-10-{tid % 28 + 1:02d}T12:00:00+00:00", [{"type": typ}]
                                 if typ != "video" else [_vid()], content=f"t{tid}"))
            on_event("tweet", tweets[-1])
            if stop_below and tid <= stop_below:
                run += 1
                if run >= 2:
                    break
        return {"tweets": tweets, "user": {"media_count": len(self.timeline)}, "stopped": False,
                "cancelled": False}


def _runner(tmp_path, walk, progress=None):
    return prm.PmvRunner(manifest_root_for=lambda cid: str(tmp_path / "pmv" / cid), state={},
                         on_progress=(progress.append if progress is not None else None),
                         twitter_walk=walk)


def _job(mode="latest"):
    link = {"platform": "twitter", "user_id": "jjadesyx", "username": "jjadesyx",
            "url": "https://x.com/jjadesyx/media", "site_code": "Twitter"}
    return {"creator": {"id": "pc_1", "name": "Jade"}, "link": link, "mode": mode}


def _manifest(tmp_path):
    return pt.load_manifest(str(tmp_path / "pmv" / "pc_1" / "twitter_jjadesyx.json"), "twitter", "jjadesyx")


def test_runner_first_walk_numbers_video_posts_by_date(tmp_path):
    walk = FakeWalk()
    walk.timeline = [(9, "video"), (8, "photo"), (5, "video"), (3, "animated_gif"), (2, "video")]
    res = _runner(tmp_path, walk).run([_job()])
    assert res["errors"] == [] and res["total_new"] == 3
    m = _manifest(tmp_path)
    assert m["numbering"] == pt.CHRONOLOGICAL and m["initial_complete"]
    assert {k: v["number"] for k, v in m["items"].items()} == {"2": 1, "5": 2, "9": 3}
    assert walk.calls == [0]                                  # first walk is full


def test_runner_incremental_walk_adds_without_marking_gone(tmp_path):
    walk = FakeWalk()
    walk.timeline = [(9, "video"), (5, "video"), (2, "video")]
    r = _runner(tmp_path, walk)
    r.run([_job()])
    walk.timeline = [(12, "video"), (9, "video"), (5, "video"), (2, "video")]
    res = r.run([_job()])
    assert walk.calls[-1] == 9 and res["total_new"] == 1
    m = _manifest(tmp_path)
    assert m["items"]["12"]["number"] == 4
    assert not any(it["gone"] for it in m["items"].values())   # 2 wasn't walked, still not gone


def test_runner_full_rescan_marks_deleted_posts_gone(tmp_path):
    walk = FakeWalk()
    walk.timeline = [(9, "video"), (5, "video"), (2, "video")]
    r = _runner(tmp_path, walk)
    r.run([_job()])
    walk.timeline = [(9, "video"), (2, "video")]
    r.run([_job(mode="full")])
    assert walk.calls[-1] == 0
    m = _manifest(tmp_path)
    assert m["items"]["5"]["gone"] and m["items"]["5"]["number"] == 2   # keeps its place


def test_twitter_posts_can_be_left_out_of_the_count(tmp_path):
    walk = FakeWalk()
    walk.timeline = [(9, "video"), (5, "video"), (2, "video")]
    _runner(tmp_path, walk).run([_job()])
    m = _manifest(tmp_path)
    pt.set_excluded(m["items"]["5"], True)
    pt.renumber_chronological(m)
    assert m["items"]["5"]["number"] is None and m["items"]["9"]["number"] == 2
    assert pt.counts(m)["excluded"] == 1


def test_runner_auth_problem_is_flagged_not_an_outage(tmp_path):
    walk = FakeWalk()
    walk.error = twp.AuthError("X rejected the session")
    res = _runner(tmp_path, walk).run([_job(), _job()])
    assert res["twitter_auth"] and len(res["errors"]) == 2 and len(walk.calls) == 2
    assert res["sites_down"] == {}


def test_runner_without_cookies_fails_fast(tmp_path):
    r = prm.PmvRunner(manifest_root_for=lambda cid: str(tmp_path / "pmv" / cid), state={})
    res = r.run([_job()])
    assert "Settings" in res["twitter_auth"] and len(res["errors"]) == 1


def test_link_key_and_prefix():
    link = {"platform": "twitter", "user_id": "jjadesyx"}
    assert pt.link_key(link) == "twitter_jjadesyx"
    assert pt.format_prefix("Jade", pt.link_site_code(link), 3,
                            date="2026-10-20T23:10:00+00:00") == "Jade - Twitter - 2026.10.20 - "
