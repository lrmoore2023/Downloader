"""hmvmania + pmvhaven metadata scrapers (PMV tracker), plus the runner's
generic locked-site walk. Fixtures mirror payloads captured live 2026-09-19.

    python -m pytest tests/test_pmv_sites2.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.hmvmania_scraper as hmv
import backend.pmvhaven_scraper as pmvh
import backend.pmv_runner as prm
import backend.pmv_tracker as pt


class FakeResp:
    def __init__(self, status=200, text="", json_data=None, headers=None):
        self.status_code = status
        self.text = text
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, timeout=None, allow_redirects=True, headers=None):
        if params:
            url = url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        self.calls.append(url)
        r = self.routes.get(url)
        return r if r is not None else FakeResp(404)


class NoThrottle:
    def wait(self, c): pass
    def on_success(self): pass
    def on_throttled(self, r): pass


# ── hmvmania ────────────────────────────────────────────────────────

def _item(pid, slug, title, date):
    return f'''<item>
        <title>{title}</title>
        <link>https://hmvmania.com/video/{slug}/</link>
        <dc:creator><![CDATA[Minahakuba]]></dc:creator>
        <pubDate>{date}</pubDate>
        <guid isPermaLink="false">https://hmvmania.com/?post_type=video&#038;p={pid}</guid>
    </item>'''


def _feed(page, pages, items):
    return (f'<?xml version="1.0"?><rss><channel><title>Videos Archive - Page {page} of {pages} - HMV Mania</title>'
            + "".join(items) + "</channel></rss>")


FEED_P1 = _feed(1, 2, [_item(30022, "minahakuba-miho", "[Minahakuba] Miho Tsujinaka x Jadi", "Thu, 28 May 2026 22:00:49 +0000"),
                       _item(29787, "minahakuba-yumiko", "[Minahakuba] Yumiko Kimura x Senorita", "Tue, 26 May 2026 22:00:48 +0000")])
FEED_P2 = _feed(2, 2, [_item(27545, "minahakuba-natsuzuma", "[Minahakuba] Natsuzuma x IRIS OUT", "Sun, 09 Nov 2025 22:00:09 +0000")])
FEED_404 = '<?xml version="1.0"?><rss><channel><title>Page not found - HMV Mania</title></channel></rss>'


def test_hmv_urls():
    assert hmv.parse_author_url("https://hmvmania.com/author/minahakuba/") == {"slug": "minahakuba"}
    assert hmv.parse_author_url("https://hmvmania.com/author/MinaHakuba") == {"slug": "minahakuba"}
    assert hmv.parse_author_url("https://hmvmania.com/video/x/") is None
    assert hmv.feed_url("m", 1) == "https://hmvmania.com/author/m/feed/?post_type=video"
    assert hmv.feed_url("m", 3).endswith("?post_type=video&paged=3")


def test_hmv_parse_feed():
    f = hmv.parse_feed(FEED_P1)
    assert f["page"] == 1 and f["pages"] == 2 and not f["not_found"] and f["author"] == "Minahakuba"
    assert [i["id"] for i in f["items"]] == ["30022", "29787"]
    it = f["items"][0]
    assert it["title"] == "Miho Tsujinaka x Jadi"                      # "[Author] " stripped
    assert it["url"] == "https://hmvmania.com/video/minahakuba-miho/"
    assert it["date"] == "2026-05-28T22:00:49+00:00"
    assert hmv.parse_feed(FEED_404)["not_found"] and hmv.parse_feed(FEED_404)["items"] == []
    assert hmv.clean_title("Plain title") == "Plain title"
    assert hmv.clean_title("[X] - Y") == "Y"


def test_hmv_iter_feed_pages_and_author():
    s = FakeSession({hmv.feed_url("minahakuba", 1): FakeResp(200, FEED_P1),
                     hmv.feed_url("minahakuba", 2): FakeResp(200, FEED_P2),
                     hmv.feed_url("minahakuba", 3): FakeResp(404, FEED_404)})
    pages = list(hmv.iter_feed_pages(s, "minahakuba", NoThrottle()))
    assert [(p, len(i)) for p, i in pages] == [(1, 2), (2, 1)]
    assert len(s.calls) == 2                                           # "Page 2 of 2" ends the walk
    assert hmv.fetch_author(s, "minahakuba", NoThrottle()) == {"name": "Minahakuba", "pages": 2}
    with pytest.raises(hmv.NotFound):
        hmv.fetch_author(FakeSession({}), "nobody", NoThrottle())
    with pytest.raises(hmv.SiteError):
        hmv.fetch_text(FakeSession({"u": FakeResp(403)}), "u", NoThrottle())


# ── pmvhaven ────────────────────────────────────────────────────────

UID = "68fdeb86b99aaf24a4c0e454"
V_NEW = {"_id": "6a9156c8ba4dc9a99c32653d", "title": "PUMPING FOR BADDIES 2 - LizzyKink",
         "uploadDate": "2026-08-28T09:37:12.539Z", "releaseDate": "2026-08-28T09:37:12.538Z",
         "durationSeconds": 187, "isReleased": True, "uploaderId": UID}
V_4K = {"_id": "6a3a56967ec40f759a98c6d2", "title": "They Wanna Fuck - LizzyKink",
        "uploadDate": "2026-06-24T07:00:19.405Z", "width": 3840, "height": 2160, "durationSeconds": 159}
V_OLD = {"_id": "68fdeb8cb99aaf24a4c0e458", "title": "Night Grind - LizzyKink Tribute To The Goat Clubberlang69",
         "uploadDate": "2025-06-15T00:43:46.000Z", "width": 3840, "height": 2160, "durationSeconds": 473,
         "oldId": "684e1742e3b62c675d376341"}


def _videos_url(uid, page, limit=100):
    return f"{pmvh.API}/videos?uploader={uid}&limit={limit}&page={page}"


def test_pmvh_urls_and_slugs():
    assert pmvh.parse_profile_url(f"https://pmvhaven.com/profile/{UID}") == {"user_id": UID, "username": ""}
    assert pmvh.parse_profile_url("https://pmvhaven.com/profile/LizzyKink") == {"user_id": "", "username": "LizzyKink"}
    assert pmvh.parse_profile_url("https://pmvhaven.com/video/x_y") is None
    assert pmvh.video_url(V_NEW) == "https://pmvhaven.com/video/pumping-for-baddies-2-lizzykink_6a9156c8ba4dc9a99c32653d"
    assert pmvh.video_url(V_OLD) == "https://pmvhaven.com/video/night-grind-lizzykink-tribute-to-the-goat-clubberlang69_684e1742e3b62c675d376341"
    assert pmvh.slugify("Café  — Déjà Vu!") == "cafe-deja-vu"


def test_pmvh_parse_video():
    v = pmvh.parse_video(V_4K)
    assert v["quality"] == 2160 and v["duration"] == 159 and v["date"] == "2026-06-24T07:00:19+00:00"
    assert pmvh.parse_video(V_NEW)["quality"] is None                 # still processing: no dimensions
    assert pmvh.parse_video(V_OLD)["id"] == "68fdeb8cb99aaf24a4c0e458" # manifest id = current _id


def test_pmvh_iter_videos_follows_hasNext():
    s = FakeSession({
        _videos_url(UID, 1, 2): FakeResp(200, json_data={"success": True, "videos": [V_NEW, V_4K],
                                                         "pagination": {"page": 1, "totalPages": 2, "hasNext": True}}),
        _videos_url(UID, 2, 2): FakeResp(200, json_data={"success": True, "videos": [V_OLD],
                                                         "pagination": {"page": 2, "totalPages": 2, "hasNext": False}}),
    })
    pages = list(pmvh.iter_videos(s, UID, NoThrottle(), limit=2))
    assert [(p, len(v)) for p, v, _ in pages] == [(1, 2), (2, 1)]
    assert len(s.calls) == 2
    empty = FakeSession({_videos_url(UID, 1): FakeResp(200, json_data={"success": True, "videos": [], "pagination": {}})})
    assert list(pmvh.iter_videos(empty, UID, NoThrottle())) == []


def test_pmvh_profile_and_username_resolution():
    s = FakeSession({f"{pmvh.API}/users/{UID}": FakeResp(200, json_data={"success": True, "data": {"_id": UID, "username": "LizzyKink"}})})
    assert pmvh.fetch_profile(s, UID, NoThrottle()) == {"id": UID, "name": "LizzyKink", "username": "LizzyKink"}
    payload = f'[{{"data":1}},{{"user-profile-LizzyKink":2}},{{"userId":3,"username":4}},"{UID}","LizzyKink"]'
    html = f'<html><script type="application/json" id="__NUXT_DATA__" data-ssr="true">{payload}</script></html>'
    assert pmvh.user_id_from_profile_html(html) == UID
    assert pmvh.user_id_from_profile_html(f'<a href="/profile/x">user-profile-{UID}</a>') == UID
    assert pmvh.user_id_from_profile_html("<html></html>") == ""
    s2 = FakeSession({f"{pmvh.SITE}/profile/LizzyKink": FakeResp(200, html)})
    assert pmvh.resolve_username(s2, "LizzyKink", NoThrottle()) == UID


# ── runner: generic locked walk ─────────────────────────────────────

class Instant:
    def wait(self, c): pass
    def on_success(self): pass
    def on_throttled(self, r): pass


def _runner(tmp_path):
    r = prm.PmvRunner(manifest_root_for=lambda cid: str(tmp_path / "pmv" / cid), state={},
                      sessions={p: object() for p in pt.PLATFORMS})
    r._throttles = {k: Instant() for k in r._throttles}
    return r


def _job(platform, user_id, mode="latest", **link):
    lk = {"platform": platform, "user_id": user_id, "url": "u", "site_code": pt.default_site_code(platform)}
    lk.update(link)
    return {"creator": {"id": "pc_1", "name": "Mina"}, "link": lk, "mode": mode}


def test_runner_hmvmania_initial_then_incremental(tmp_path, monkeypatch):
    pages = [[("30022", "a"), ("29787", "b")], [("27545", "c")]]
    calls = []

    def iter_pages(session, slug, throttle, should_cancel):
        for n, items in enumerate(pages, 1):
            calls.append(n)
            yield n, [{"id": i, "title": t, "url": f"u/{i}", "date": None} for i, t in items]
    monkeypatch.setattr(prm.hmv, "iter_feed_pages", iter_pages)
    res = _runner(tmp_path).run([_job("hmvmania", "minahakuba")])
    m = pt.load_manifest(str(tmp_path / "pmv" / "pc_1" / "hmvmania_minahakuba.json"), "hmvmania", "minahakuba")
    assert [m["items"][i]["number"] for i in ("27545", "29787", "30022")] == [1, 2, 3]
    assert res["total_new"] == 3 and m["numbering"] == pt.LOCKED
    pages[:] = [[("31000", "new"), ("30022", "a")], [("29787", "b"), ("27545", "c")]]
    calls.clear()
    res = _runner(tmp_path).run([_job("hmvmania", "minahakuba")])
    assert calls == [1, 2]                                            # page 2 all known → stop
    m = pt.load_manifest(str(tmp_path / "pmv" / "pc_1" / "hmvmania_minahakuba.json"), "hmvmania", "minahakuba")
    assert m["items"]["31000"]["number"] == 4 and res["total_new"] == 1


def test_runner_pmvhaven_resolves_username_and_parses(tmp_path, monkeypatch):
    monkeypatch.setattr(prm.pmvh, "resolve_username", lambda *a, **k: UID)
    monkeypatch.setattr(prm.pmvh, "iter_videos",
                        lambda *a, **k: iter([(1, [V_NEW, V_4K, V_OLD], {"hasNext": False})]))
    res = _runner(tmp_path).run([_job("pmvhaven", "", username="LizzyKink")])
    assert res["total_new"] == 3 and not res["errors"]
    m = pt.load_manifest(str(tmp_path / "pmv" / "pc_1" / "pmvhaven_.json"), "pmvhaven", "")
    assert m["items"][V_OLD["_id"]]["number"] == 1 and m["items"][V_NEW["_id"]]["number"] == 3
    assert m["items"][V_4K["_id"]]["quality"] == 2160
    assert m["items"][V_OLD["_id"]]["url"].endswith("_684e1742e3b62c675d376341")


def test_runner_pmvhaven_unresolvable_username_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(prm.pmvh, "resolve_username", lambda *a, **k: "")
    res = _runner(tmp_path).run([_job("pmvhaven", "", username="ghost")])
    assert res["errors"] and "resolve" in res["errors"][0]["message"]
