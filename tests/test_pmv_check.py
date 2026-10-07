"""PmvCheck: feed-driven new-video check (fake feeds, fake runner).

    python -m pytest tests/test_pmv_check.py -q
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.pmv_check as pc
import backend.pmv_feeds as pf
import backend.pmv_tracker as pt

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def ago(**kw):
    return iso(NOW - timedelta(**kw))


class FakeSource:
    def __init__(self, platform, pages, following=None, available=(True, ""), resolve=None,
                 raise_on_page=None):
        self.platform = platform
        self._pages = pages
        self._following = following
        self._available = available
        self._resolve = resolve
        self._raise = raise_on_page
        self.pages_read = 0
        self.marked = []

    def available(self):
        return self._available

    def pages(self):
        for i, p in enumerate(self._pages):
            if self._raise and i == self._raise[0]:
                raise self._raise[1]
            self.pages_read += 1
            yield [dict(x) for x in p]

    def following(self):
        if isinstance(self._following, Exception):
            raise self._following
        return set(self._following or [])

    def mark_read(self, refs):
        self.marked.extend(refs)
        return len(refs)


def item(platform, vid, up, date, ref=None):
    return {"platform": platform, "video_id": vid, "uploader_id": up, "uploader_name": "",
            "title": f"t{vid}", "url": f"u{vid}", "date": date, "newest": date, "ref": ref}


class World:
    """Creators + manifests on disk, and a fake runner that 'fetches' by adding
    the videos the test says the site lists."""
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.creators = {}
        self.site = {}          # link_key -> [video ids the listing would show]
        self.jobs = []
        self.fail = set()

    def root(self, cid):
        return str(self.tmp / "pmv" / cid)

    def add(self, cid, platform, user_id, existing=(), service=None, last_fetch=None):
        link = {"platform": platform, "user_id": user_id, "username": f"user{user_id}", "url": "u"}
        if service:
            link["service"] = service
        self.creators.setdefault(cid, {"id": cid, "name": cid, "links": []})["links"].append(link)
        key = pt.link_key(link)
        m = pt.new_manifest(platform, user_id)
        pt.merge(m, [{"id": v, "title": v, "url": v, "pos": i, "date": "2026-09-01"} for i, v in enumerate(existing)],
                 full=True, complete=True, now="2026-09-01T00:00:00+00:00")
        m["last_fetch"] = last_fetch or ago(hours=1)
        pt.save_manifest(pt.manifest_path(self.root(cid), key), m)
        self.site[key] = list(existing)
        return key

    def run_jobs(self, jobs):
        errors = []
        for j in jobs:
            key = pt.link_key(j["link"])
            self.jobs.append(key)
            if key in self.fail:
                errors.append({"creator_id": j["creator"]["id"], "link_key": key, "message": "boom"})
                continue
            path = pt.manifest_path(self.root(j["creator"]["id"]), key)
            m = pt.load_manifest(path, j["link"]["platform"], j["link"]["user_id"])
            fetched = [{"id": v, "title": v, "url": v, "pos": i, "date": "2026-10-04"}
                       for i, v in enumerate(reversed(self.site[key]))]
            pt.merge(m, fetched, full=False, complete=False)
            m["last_fetch"] = iso(NOW)                       # as PmvRunner does
            pt.save_manifest(path, m)
        return {"errors": errors, "cancelled": False}

    def check(self, sources, mode="check", paw_fetch=None, state_since=None, now=NOW):
        path = str(self.tmp / "pmv" / "_feed_state.json")
        if state_since:
            st = pc.load_state(path)
            st["since"] = state_since
            pc.save_state(path, st)
        self.jobs = []
        return pc.PmvCheck(creators=self.creators, manifest_root_for=self.root, state_path=path,
                           sources=sources, pawchive_fetch=paw_fetch, run_jobs=self.run_jobs,
                           now=lambda: now, mode=mode).run()

    def state(self):
        return pc.load_state(str(self.tmp / "pmv" / "_feed_state.json"))


SINCE = ago(days=1)


def test_only_flagged_links_are_fetched(tmp_path):
    w = World(tmp_path)
    a = w.add("pc_a", "iwara", "ua", ["v1"])
    b = w.add("pc_b", "iwara", "ub", ["v2"])
    w.site[a].append("v9")
    src = FakeSource("iwara", [[item("iwara", "v9", "ua", ago(hours=2)),
                                item("iwara", "v1", "ua", ago(days=3))]], following=["ua", "ub"])
    res = w.check({"iwara": src}, state_since=SINCE)
    assert w.jobs == [a]                     # b untouched: followed, nothing new
    assert res["flagged_links"] == 1 and res["pending"] == 0
    assert w.state()["platforms"]["iwara"]["last_ok"] == iso(NOW)


def test_unfollowed_hmvmania_and_no_feed_links_always_fetched(tmp_path):
    w = World(tmp_path)
    a = w.add("pc_a", "iwara", "ua", ["v1"])
    b = w.add("pc_b", "iwara", "ub", ["v2"])            # not followed
    h = w.add("pc_c", "hmvmania", "slug", ["h1"])        # no feed at all
    r = w.add("pc_d", "rule34video", "77", ["r1"])       # signed out
    src = FakeSource("iwara", [[item("iwara", "v1", "ua", ago(days=3))]], following=["ua"])
    r34 = FakeSource("rule34video", [], available=(False, "no sign-in"))
    res = w.check({"iwara": src, "rule34video": r34}, state_since=SINCE)
    assert set(w.jobs) == {b, h, r}
    assert res["unfollowed"][0]["link_key"] == b
    assert res["platforms"]["rule34video"]["status"] == "off"


def test_feed_error_or_auth_fetches_every_link_of_that_site(tmp_path):
    w = World(tmp_path)
    a = w.add("pc_a", "iwara", "ua", ["v1"])
    b = w.add("pc_b", "iwara", "ub", ["v2"])
    src = FakeSource("iwara", [[item("iwara", "v1", "ua", ago(hours=1))]], following=["ua", "ub"],
                     raise_on_page=(0, pf.FeedAuthError("expired")))
    res = w.check({"iwara": src}, state_since=SINCE)
    assert set(w.jobs) == {a, b}
    assert res["platforms"]["iwara"]["status"] == "auth"
    assert "last_ok" not in w.state()["platforms"]["iwara"]       # watermark not advanced


def test_walk_stops_at_cutoff_and_gap_beyond_max_pages(tmp_path, monkeypatch):
    w = World(tmp_path)
    w.add("pc_a", "iwara", "ua", ["v1"])
    pages = [[item("iwara", f"x{i}", "other", ago(hours=1))] for i in range(3)] + \
            [[item("iwara", "old", "other", ago(days=5))]] + \
            [[item("iwara", "never", "other", ago(days=6))]]
    src = FakeSource("iwara", pages, following=["ua"])
    w.check({"iwara": src}, state_since=SINCE)
    assert src.pages_read == 4                          # stopped on the all-old page
    monkeypatch.setattr(pc, "MAX_PAGES", 2)
    src2 = FakeSource("iwara", pages, following=["ua"])
    w2 = World(tmp_path / "b")
    a2 = w2.add("pc_a", "iwara", "ua", ["v1"])
    res = w2.check({"iwara": src2}, state_since=SINCE)
    assert res["platforms"]["iwara"]["status"] == "gap" and w2.jobs == [a2]


def test_cutoff_is_last_check_minus_overlap_never_before_since(tmp_path):
    w = World(tmp_path)
    w.add("pc_a", "iwara", "ua", ["v1"])
    src = FakeSource("iwara", [[item("iwara", "v1", "ua", ago(hours=1))]], following=["ua"])
    w.check({"iwara": src}, state_since=ago(days=10), now=NOW - timedelta(days=5))
    # Last ok = NOW-5d → cutoff NOW-7d. An item 6 days old is inside the overlap.
    w.site[pt.link_key(w.creators["pc_a"]["links"][0])].append("v6")
    src = FakeSource("iwara", [[item("iwara", "v6", "ua", ago(days=6))],
                               [item("iwara", "v8", "ua", ago(days=8))]], following=["ua"])
    w.check({"iwara": src})
    assert w.jobs and src.pages_read == 2
    assert "v8" not in pc.load_state(str(tmp_path / "pmv" / "_feed_state.json"))["pending"]


def test_missing_video_kept_pending_and_retried(tmp_path):
    w = World(tmp_path)
    a = w.add("pc_a", "pmvhaven", "aa", ["p1"])
    # Feed shows p2 but the listing doesn't have it yet.
    src = FakeSource("pmvhaven", [[item("pmvhaven", "p2", "aa", ago(hours=1), ref="n2"),
                                   item("pmvhaven", "p1", "aa", ago(days=3), ref="n1")]],
                     following=["aa"])
    res = w.check({"pmvhaven": src}, state_since=SINCE)
    assert res["pending"] == 1 and src.marked == []     # never marked while missing
    # Next check: feed has nothing new, but the pending link is fetched again.
    w.site[a].append("p2")
    src = FakeSource("pmvhaven", [[item("pmvhaven", "p1", "aa", ago(days=3), ref="n1")]], following=["aa"])
    res = w.check({"pmvhaven": src})
    assert w.jobs == [a] and res["pending"] == 0


def test_mark_read_only_recorded_and_untracked(tmp_path):
    w = World(tmp_path)
    a = w.add("pc_a", "pmvhaven", "aa", ["p1"])
    b = w.add("pc_b", "pmvhaven", "bb", ["q1"])
    w.site[a].append("p2")
    w.fail.add(b)
    src = FakeSource("pmvhaven", [[item("pmvhaven", "p2", "aa", ago(hours=1), ref="n-new"),
                                   item("pmvhaven", "q2", "bb", ago(hours=1), ref="n-fail"),
                                   item("pmvhaven", "z1", "zz", ago(hours=1), ref="n-untracked"),
                                   item("pmvhaven", "p1", "aa", ago(hours=2), ref="n-known")]],
                     following=["aa", "bb"])
    res = w.check({"pmvhaven": src}, state_since=SINCE)
    assert sorted(src.marked) == ["n-known", "n-new", "n-untracked"]
    assert res["pending"] == 1                              # q2: its fetch failed
    assert "pmvhaven:zz" in w.state()["untracked"]


def test_dismissed_uploader_not_recorded(tmp_path):
    w = World(tmp_path)
    w.add("pc_a", "iwara", "ua", ["v1"])
    path = str(tmp_path / "pmv" / "_feed_state.json")
    st = pc.load_state(path)
    st["since"], st["dismissed"] = SINCE, ["iwara:zz"]
    pc.save_state(path, st)
    src = FakeSource("iwara", [[item("iwara", "z1", "zz", ago(hours=1))]], following=["ua"])
    w.check({"iwara": src})
    assert w.state()["untracked"] == {}


def test_r34_uploader_resolved_once_and_cached(tmp_path):
    w = World(tmp_path)
    a = w.add("pc_a", "rule34video", "77", ["1"])
    w.site[a].append("2")
    calls = []

    class R34(FakeSource):
        def resolve_uploader(self, it):
            calls.append(it["video_id"])
            return {"2": "77", "3": "88"}.get(it["video_id"])

    mk = lambda: R34("rule34video", [[item("rule34video", "2", "", ago(hours=1)),
                                      item("rule34video", "3", "", ago(hours=1)),
                                      item("rule34video", "4", "", ago(hours=1))]], following=["77"])
    w.check({"rule34video": mk()}, state_since=SINCE)
    assert w.jobs == [a] and sorted(calls) == ["2", "3", "4"]
    calls.clear()
    w.check({"rule34video": mk()})
    assert calls == []                                       # all seen → no page fetches


def test_r34_unresolved_item_holds_the_watermark(tmp_path):
    w = World(tmp_path)
    w.add("pc_a", "rule34video", "77", ["1"])

    class R34(FakeSource):
        def resolve_uploader(self, it):
            raise pf.FeedError("timeout")

    w.check({"rule34video": R34("rule34video", [[item("rule34video", "5", "", ago(hours=1))]],
                                following=["77"])}, state_since=SINCE)
    assert "last_ok" not in w.state()["platforms"]["rule34video"]


def test_pawchive_signature_rules(tmp_path):
    w = World(tmp_path)
    a = w.add("pc_a", "pawchive", "1", ["x"], service="patreon")
    b = w.add("pc_b", "pawchive", "2", ["y"], service="fanbox")
    c = w.add("pc_c", "pawchive", "3", ["z"], service="patreon")
    profiles = {"/patreon/user/3/profile": {"updated": ago(hours=3)}}
    favs = [{"service": "patreon", "id": "1", "updated": ago(hours=2)},        # updated since → fetch
            {"service": "fanbox", "id": "2", "updated": ago(days=4)}]          # older → baseline only

    def fetch(url):
        if "favorites" in url:
            return favs
        for k, v in profiles.items():
            if url.endswith(k):
                return v
        raise pf.FeedError("404")

    w.check({}, paw_fetch=fetch, state_since=SINCE)
    assert set(w.jobs) == {a, c}                             # c via its public profile
    w.check({}, paw_fetch=fetch)
    assert w.jobs == []                                      # nothing changed since
    favs[1]["updated"] = ago(minutes=5)
    w.check({}, paw_fetch=fetch)
    assert w.jobs == [b]


def test_pawchive_failed_fetch_keeps_old_signature(tmp_path):
    w = World(tmp_path)
    a = w.add("pc_a", "pawchive", "1", ["x"], service="patreon")
    favs = [{"service": "patreon", "id": "1", "updated": ago(hours=2)}]
    fetch = lambda url: favs
    w.fail.add(a)
    w.check({}, paw_fetch=fetch, state_since=SINCE)
    w.fail.clear()
    w.check({}, paw_fetch=fetch)
    assert w.jobs == [a]                                    # retried: signature never stored


def test_sweep_fetches_everything_but_pawchive_by_default(tmp_path):
    w = World(tmp_path)
    a = w.add("pc_a", "iwara", "ua", ["v1"])
    p = w.add("pc_b", "pawchive", "1", ["x"], service="patreon")
    w.check({}, mode="sweep", state_since=SINCE)
    assert w.jobs == [a]
    assert w.state()["last_sweep"] == iso(NOW)


def test_cancel_advances_nothing(tmp_path):
    import threading
    w = World(tmp_path)
    w.add("pc_a", "iwara", "ua", ["v1"])
    ev = threading.Event()
    ev.set()
    path = str(tmp_path / "pmv" / "_feed_state.json")
    res = pc.PmvCheck(creators=w.creators, manifest_root_for=w.root, state_path=path,
                      sources={"iwara": FakeSource("iwara", [[]], following=["ua"])},
                      run_jobs=w.run_jobs, cancel=ev, now=lambda: NOW).run()
    assert res["cancelled"] and not pc.load_state(path)["last_check"]


def test_relative_dates_are_upper_bounds():
    assert pf.relative_newest("22 hours ago", NOW) == NOW - timedelta(hours=22)
    assert pf.relative_newest("a day ago", NOW) == NOW - timedelta(days=1)
    assert pf.relative_newest("2 weeks ago", NOW) == NOW - timedelta(days=14)
    assert pf.relative_newest("whenever", NOW) is None


def test_parse_r34_feed_reads_cards():
    html = '''<div id="list_videos_videos_from_my_subscriptions_items">
      <div class="item thumb" data-video-card-id="4634703">
        <a class="th" href="https://rule34video.com/video/4634703/x/" title="Some title"><div class="time">1:32</div></a>
        <div class="added">2 days ago</div></div></div>'''
    items = pf.parse_r34_feed(html, NOW)
    assert items[0]["video_id"] == "4634703" and items[0]["title"] == "Some title"
    assert items[0]["newest"] == iso(NOW - timedelta(days=2)) and items[0]["uploader_id"] == ""


def test_parse_r34_feed_reads_v2_cards():
    html = '''<div class="ma-grid" id="list_videos_videos_from_my_subscriptions_items">
      <div class="item ma-v video_1" data-rdm-item data-id="4634703" data-title="Some title" data-dur="92"
           data-url="https://rule34video.com/video/4634703/x/">
        <a class="ma-thumb" href="https://rule34video.com/video/4634703/x/"><span class="ma-dur">1:32</span></a>
        <div class="ma-v__row"><a class="ma-v__t" href="https://rule34video.com/video/4634703/x/">Some title</a></div>
        <div class="ma-v__m"><span>1K views</span><span>2 days ago</span><span>90%</span></div></div></div>'''
    items = pf.parse_r34_feed(html, NOW)
    assert items[0]["video_id"] == "4634703" and items[0]["title"] == "Some title"
    assert items[0]["newest"] == iso(NOW - timedelta(days=2)) and items[0]["uploader_id"] == ""


def test_link_fetched_before_the_feed_window_is_fetched_once(tmp_path):
    """Live case 2026-10-02: a link last fetched Sep 20, uploads on Sep 23 and
    Oct 1, feed window starting Oct 2 — the feed alone never shows them."""
    w = World(tmp_path)
    stale = w.add("pc_a", "iwara", "ua", ["v1"], last_fetch=ago(days=12))
    fresh = w.add("pc_b", "iwara", "ub", ["v2"])
    w.site[stale] += ["sep23", "oct1"]
    src = lambda: FakeSource("iwara", [[item("iwara", "v2", "ub", ago(days=3))]], following=["ua", "ub"])
    res = w.check({"iwara": src()}, state_since=SINCE)
    assert w.jobs == [stale]
    assert res["reasons"][f"pc_a|{stale}"] == "last fetched before the feed window"
    m = pt.load_manifest(pt.manifest_path(w.root("pc_a"), stale), "iwara", "ua")
    assert {"sep23", "oct1"} <= set(m["items"])
    w.check({"iwara": src()})
    assert w.jobs == []                                     # gap closed for good


def test_pawchive_first_baseline_uses_the_links_last_fetch(tmp_path):
    w = World(tmp_path)
    a = w.add("pc_a", "pawchive", "1", ["x"], service="patreon", last_fetch=ago(days=12))
    b = w.add("pc_b", "pawchive", "2", ["y"], service="patreon", last_fetch=ago(days=1))
    favs = [{"service": "patreon", "id": "1", "updated": ago(days=5)},    # after a's fetch
            {"service": "patreon", "id": "2", "updated": ago(days=3)}]    # before b's fetch
    w.check({}, paw_fetch=lambda url: favs, state_since=SINCE)
    assert w.jobs == [a]


class DeepSource(FakeSource):
    cheap_depth = True


def test_cheap_feed_reads_back_to_the_stalest_creator_instead_of_fetching_each(tmp_path):
    """The user's point: iwara's subscriptions feed already holds the older
    uploads, so read further back rather than fetching every stale creator."""
    w = World(tmp_path)
    stale = w.add("pc_a", "iwara", "ua", ["v1"], last_fetch=ago(days=12))
    quiet = w.add("pc_b", "iwara", "ub", ["v2"], last_fetch=ago(days=12))
    w.site[stale] += ["oct1"]
    pages = [[item("iwara", "today", "zz", ago(hours=2))],
             [item("iwara", "oct1", "ua", ago(days=4))],          # older than `since`
             [item("iwara", "v1", "ua", ago(days=13))]]           # older than any last fetch
    src = DeepSource("iwara", pages, following=["ua", "ub"])
    res = w.check({"iwara": src}, state_since=SINCE)
    assert w.jobs == [stale]                    # flagged by the feed; quiet one untouched
    assert res["reasons"][f"pc_a|{stale}"] == "new in feed"
    assert src.pages_read == 3


def test_deep_window_is_capped_and_older_creators_fetched_directly(tmp_path):
    w = World(tmp_path)
    ancient = w.add("pc_a", "iwara", "ua", ["v1"], last_fetch=ago(days=200))
    src = DeepSource("iwara", [[item("iwara", "x", "ua", ago(days=90))]], following=["ua"])
    res = w.check({"iwara": src}, state_since=SINCE)
    assert res["reasons"][f"pc_a|{ancient}"] == "last fetched before the feed window"


def test_notifications_only_cover_back_to_the_oldest_one_kept(tmp_path):
    w = World(tmp_path)
    a = w.add("pc_a", "pmvhaven", "aa", ["p1"], last_fetch=ago(days=10))
    b = w.add("pc_b", "pmvhaven", "bb", ["q1"], last_fetch=ago(hours=2))

    class Notes(DeepSource):
        history_complete = False

    # The site kept notifications back to 3 days ago only; the feed then ends.
    src = Notes("pmvhaven", [[item("pmvhaven", "p1", "aa", ago(days=3), ref="n1")]], following=["aa", "bb"])
    res = w.check({"pmvhaven": src}, state_since=SINCE)
    assert w.jobs == [a]                     # a's last fetch predates what the feed can show
    assert res["reasons"][f"pc_a|{a}"] == "last fetched before the feed window"


def test_untracked_only_recorded_from_the_start_date(tmp_path):
    w = World(tmp_path)
    w.add("pc_a", "iwara", "ua", ["v1"], last_fetch=ago(days=12))
    src = DeepSource("iwara", [[item("iwara", "new", "zz", ago(hours=2)),
                                item("iwara", "old", "yy", ago(days=5))]], following=["ua"])
    w.check({"iwara": src}, state_since=SINCE)
    assert set(w.state()["untracked"]) == {"iwara:zz"}
