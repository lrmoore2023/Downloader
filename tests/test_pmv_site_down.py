"""A PMV run where a site is down: the other sites must still be checked, and
the down site must not cost a retry budget per link.

    python -m pytest tests/test_pmv_site_down.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.pmv_check as pc
import backend.pmv_feeds as pf
import backend.pmv_runner as prm
import backend.pmv_tracker as pt
from backend.site_health import SiteHealth, outage_kind


@pytest.mark.parametrize("msg,kind", [
    ("rule34video: ConnectTimeout after 4 attempts: https://…", "connection"),
    ("iwara: ReadTimeout after 4 attempts", "connection"),
    ("rule34video: ConnectionError", "connection"),
    ("twitter: x.com not responding (nothing for 180s)", "connection"),
    ("hmvmania: HTTP 503 after 4 attempts: https://…", "http"),
    ("pawchive: HTTP 502", "http"),
    ("pmvhaven: HTTP 429 after 4 attempts", "http"),
    ("rule34video: member page not found (deleted account?)", None),
    ("pawchive: Cloudflare challenge — click Reconnect in Settings", None),
    ("hmvmania: HTTP 403 (Cloudflare) — the feed endpoint was blocked", None),
    ("twitter: X rejected the session", None),
])
def test_outage_kind(msg, kind):
    assert outage_kind(msg) == kind


def test_health_trip_rules():
    h = SiteHealth()
    assert h.failed("iwara", "ConnectTimeout after 4 attempts")          # connection: at once
    assert not h.failed("r34", "HTTP 503 after 4 attempts")              # http: needs two
    assert h.failed("r34", "HTTP 503 after 4 attempts")
    assert not h.failed("pmvh", "HTTP 500 after 4 attempts")
    h.ok("pmvh")                                                         # a success resets
    assert not h.failed("pmvh", "HTTP 500 after 4 attempts")
    assert not h.failed("paw", "not found (404)")
    assert not h.failed("feed", "ConnectTimeout", weak=True)             # a feed is one strike
    assert h.failed("feed", "ConnectTimeout after 4 attempts")
    assert set(h.down()) == {"iwara", "r34", "feed"}


# ── runner ──────────────────────────────────────────────────────────

def _runner(tmp_path, health=None):
    return prm.PmvRunner(manifest_root_for=lambda cid: str(tmp_path / "pmv" / cid), state={},
                         sessions={"rule34video": object(), "iwara": object(), "pawchive": object(),
                                   "hmvmania": object(), "pmvhaven": object()}, health=health)


def _job(cid, platform, uid):
    return {"creator": {"id": cid, "name": cid},
            "link": {"platform": platform, "user_id": uid, "url": "u"}, "mode": "latest"}


def test_down_site_is_tried_once_and_the_others_still_run(tmp_path):
    r = _runner(tmp_path)
    calls = []

    def dead(job, manifest):
        calls.append(("r34", job["link"]["user_id"]))
        raise prm.LinkError("rule34video: ConnectTimeout after 4 attempts: https://rule34video.com/…")

    def alive(job, manifest):
        calls.append(("hmv", job["link"]["user_id"]))
        return [{"id": "a1", "title": "x", "url": "u", "pos": 0, "date": "2026-10-01"}], True, True

    r._fetch_r34, r._fetch_hmvmania = dead, alive
    jobs = [_job("c1", "rule34video", "1"), _job("c1", "hmvmania", "h1"),
            _job("c2", "rule34video", "2"), _job("c3", "rule34video", "3"), _job("c3", "hmvmania", "h3")]
    res = r.run(jobs)
    assert calls == [("r34", "1"), ("hmv", "h1"), ("hmv", "h3")]     # r34 2 and 3 never requested
    assert res["links_done"] == 2 and res["total_new"] == 2
    assert len(res["errors"]) == 3
    assert res["sites_down"]["rule34video"]["skipped"] == 2
    # Skipped links carry a recorded error, so the next run tries them again.
    m = pt.load_manifest(pt.manifest_path(str(tmp_path / "pmv" / "c2"), "rule34video_2"), "rule34video", "2")
    assert "looks down" in m["last_error"] and not m["last_fetch"]


def test_one_bad_page_does_not_take_a_healthy_site_out(tmp_path):
    r = _runner(tmp_path)
    calls = []

    def flaky(job, manifest):
        calls.append(job["link"]["user_id"])
        if job["link"]["user_id"] == "1":
            raise prm.LinkError("hmvmania: HTTP 500 after 4 attempts")
        return [], True, True

    r._fetch_hmvmania = flaky
    res = r.run([_job(f"c{i}", "hmvmania", str(i)) for i in (1, 2, 3)])
    assert calls == ["1", "2", "3"] and res["sites_down"] == {}


def test_per_link_errors_never_trip_the_site(tmp_path):
    r = _runner(tmp_path)
    calls = []

    def gone(job, manifest):
        calls.append(1)
        raise prm.LinkError("rule34video: member page not found (deleted account?)")

    r._fetch_r34 = gone
    r.run([_job(f"c{i}", "rule34video", str(i)) for i in range(4)])
    assert len(calls) == 4


# ── check ───────────────────────────────────────────────────────────

class DeadHeads:
    """A pmvhaven-like source whose feed and per-profile head both time out."""
    platform = "pmvhaven"
    history_complete = False

    def __init__(self):
        self.heads = 0

    def available(self):
        return True, ""

    def pages(self):
        raise pf.FeedError("pmvhaven: ConnectTimeout after 4 attempts: /api/notifications")
        yield []

    def following(self):
        return set()

    def head(self, link):
        self.heads += 1
        raise pf.FeedError("pmvhaven: ConnectTimeout after 4 attempts: /api/videos")


def _creators_and_manifests(tmp_path, specs):
    creators = {}
    for cid, platform, uid in specs:
        link = {"platform": platform, "user_id": uid, "username": uid, "url": "u"}
        creators.setdefault(cid, {"id": cid, "name": cid, "links": []})["links"].append(link)
        m = pt.new_manifest(platform, uid)
        pt.merge(m, [{"id": "old", "title": "o", "url": "u", "pos": 0, "date": "2026-09-01"}],
                 full=True, complete=True)
        m["last_fetch"] = pt.now_iso()
        pt.save_manifest(pt.manifest_path(str(tmp_path / "pmv" / cid), pt.link_key(link)), m)
    return creators


def test_check_stops_head_checks_on_a_dead_site_and_runner_skips_it(tmp_path):
    creators = _creators_and_manifests(tmp_path, [("a", "pmvhaven", "p1"), ("b", "pmvhaven", "p2"),
                                                  ("c", "pmvhaven", "p3"), ("d", "twitter", "jade"),
                                                  ("e", "hmvmania", "slug")])
    health = SiteHealth()
    runner = _runner(tmp_path, health)
    fetched = []

    def ok(job, manifest):
        fetched.append(job["link"]["platform"])
        return [{"id": "old", "title": "o", "url": "u", "pos": 0, "date": "2026-09-01"}], False, True

    def never(job, manifest):
        raise AssertionError("a down site must not be fetched")

    runner._fetch_twitter = runner._fetch_hmvmania = ok
    runner._fetch_pmvhaven = never
    src = DeadHeads()
    res = pc.PmvCheck(creators=creators, manifest_root_for=lambda cid: str(tmp_path / "pmv" / cid),
                      state_path=str(tmp_path / "pmv" / "_feed_state.json"), sources={"pmvhaven": src},
                      run_jobs=runner.run, health=health).run()
    assert src.heads == 1                         # feed strike + one head failure → down
    assert sorted(fetched) == ["hmvmania", "twitter"]
    assert res["reasons"]["d|twitter_jade"] == "no feed"
    assert res["sites_down"]["pmvhaven"]["skipped"] == 3
    assert len(res["runner"]["errors"]) == 3
