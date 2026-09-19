"""PmvRunner: sequential metadata walks folded into manifests.

    python -m pytest tests/test_pmv_runner.py -q
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.pmv_runner as prm
import backend.pmv_tracker as pt
import backend.r34video_scraper as r34
import backend.iwara_scraper as iw
import backend.pawchive_scraper as pw


class Instant:
    """Throttle stand-in: never sleeps."""
    def wait(self, is_cancelled): pass
    def on_success(self): pass
    def on_throttled(self, retry_after): pass


def _runner(tmp_path, state=None, progress=None, cancel=None):
    r = prm.PmvRunner(manifest_root_for=lambda cid: str(tmp_path / "pmv" / cid),
                      state=state or {}, cancel=cancel,
                      on_progress=(progress.append if progress is not None else None),
                      sessions={"rule34video": object(), "iwara": object(), "pawchive": object()})
    r._throttles = {k: Instant() for k in r._throttles}
    return r


def _job(cid="pc_1", name="SadBernard", platform="rule34video", user_id="2472537", mode="latest", **link):
    lk = {"platform": platform, "user_id": user_id, "url": "u", "site_code": pt.default_site_code(platform)}
    lk.update(link)
    return {"creator": {"id": cid, "name": name}, "link": lk, "mode": mode}


def _r34_pages(monkeypatch, pages, details=None, calls=None):
    """pages: list of lists of ids (newest first). details: id -> dict."""
    calls = calls if calls is not None else []

    def iter_pages(session, uid, throttle, should_cancel):
        for n, ids in enumerate(pages, 1):
            calls.append(("page", n))
            yield n, [{"id": str(i), "title": f"v{i}", "url": f"https://rule34video.com/video/{i}/x/",
                       "duration_text": "1:00", "duration": 60} for i in ids]

    def detail(session, url, throttle, should_cancel):
        vid = r34.video_id_from_url(url)
        calls.append(("detail", vid))
        d = (details or {}).get(vid)
        if isinstance(d, Exception):
            raise d
        return d or {"date": "2026-01-01", "duration": 61, "quality": 1080}

    monkeypatch.setattr(prm.r34, "iter_listing_pages", iter_pages)
    monkeypatch.setattr(prm.r34, "fetch_video_detail", detail)
    return calls


def _load(tmp_path, cid, key, platform="rule34video", uid="2472537"):
    return pt.load_manifest(str(tmp_path / "pmv" / cid / f"{key}.json"), platform, uid)


# ── rule34video ─────────────────────────────────────────────────────

def test_r34_initial_walk_numbers_and_fetches_details(tmp_path, monkeypatch):
    calls = _r34_pages(monkeypatch, [[30, 20], [10]], details={"30": {"date": "2026-03-01", "quality": 2160}})
    events = []
    res = _runner(tmp_path, progress=events).run([_job()])
    m = _load(tmp_path, "pc_1", "rule34video_2472537")
    assert [m["items"][i]["number"] for i in ("10", "20", "30")] == [1, 2, 3]
    assert m["items"]["30"]["quality"] == 2160 and m["items"]["30"]["date"] == "2026-03-01"
    assert m["initial_complete"] and m["last_full_scan"] and m["last_error"] == ""
    assert sorted(c for c in calls if c[0] == "detail") == [("detail", "10"), ("detail", "20"), ("detail", "30")]
    assert res["total_new"] == 3 and res["per_creator"]["pc_1"] == {"new": 3, "gone": 0, "errors": 0}
    assert not res["cancelled"] and res["errors"] == []
    assert [e["type"] for e in events][0] == "start" and events[-1]["type"] == "link_done"


def test_r34_incremental_stops_after_all_known_page_and_details_only_new(tmp_path, monkeypatch):
    _r34_pages(monkeypatch, [[30, 20], [10]])
    _runner(tmp_path).run([_job()])
    # Two new on top; page 2 = [30, 20] fully known → stop; page 3 never fetched.
    calls = _r34_pages(monkeypatch, [[50, 40], [30, 20], [10]])
    res = _runner(tmp_path).run([_job()])
    assert [c for c in calls if c[0] == "page"] == [("page", 1), ("page", 2)]
    assert sorted(c[1] for c in calls if c[0] == "detail") == ["40", "50"]
    m = _load(tmp_path, "pc_1", "rule34video_2472537")
    assert m["items"]["40"]["number"] == 4 and m["items"]["50"]["number"] == 5
    assert res["total_new"] == 2
    assert not any(it["gone"] for it in m["items"].values())


def test_r34_full_rescan_marks_gone_and_walks_everything(tmp_path, monkeypatch):
    _r34_pages(monkeypatch, [[30, 20], [10]])
    _runner(tmp_path).run([_job()])
    calls = _r34_pages(monkeypatch, [[30], [10]])
    res = _runner(tmp_path).run([_job(mode="full")])
    assert [c for c in calls if c[0] == "page"] == [("page", 1), ("page", 2)]
    m = _load(tmp_path, "pc_1", "rule34video_2472537")
    assert m["items"]["20"]["gone"] and m["items"]["20"]["number"] == 2
    assert res["per_creator"]["pc_1"]["gone"] == 1


def test_r34_detail_failure_is_retried_next_run(tmp_path, monkeypatch):
    _r34_pages(monkeypatch, [[20, 10]], details={"20": r34.SiteError("boom")})
    _runner(tmp_path).run([_job()])
    m = _load(tmp_path, "pc_1", "rule34video_2472537")
    assert m["items"]["20"]["detail_pending"] and m["items"]["20"]["number"] == 2
    calls = _r34_pages(monkeypatch, [[20, 10]])
    _runner(tmp_path).run([_job()])
    assert [c[1] for c in calls if c[0] == "detail"] == ["20"]
    m = _load(tmp_path, "pc_1", "rule34video_2472537")
    assert not m["items"]["20"]["detail_pending"] and m["items"]["20"]["quality"] == 1080


def test_r34_empty_listing_on_numbered_manifest_is_an_error_not_a_wipe(tmp_path, monkeypatch):
    _r34_pages(monkeypatch, [[20, 10]])
    _runner(tmp_path).run([_job()])
    _r34_pages(monkeypatch, [])
    res = _runner(tmp_path).run([_job(mode="full")])
    m = _load(tmp_path, "pc_1", "rule34video_2472537")
    assert len(m["items"]) == 2 and not any(it["gone"] for it in m["items"].values())
    assert res["errors"] and "empty" in res["errors"][0]["message"]
    assert "empty" in m["last_error"]


def test_r34_site_error_continues_to_next_creator(tmp_path, monkeypatch):
    def boom(session, uid, throttle, should_cancel):
        if uid == "bad":
            raise r34.SiteError("HTTP 500")
        yield 1, [{"id": "1", "title": "t", "url": "https://rule34video.com/video/1/x/", "duration": 1}]
    monkeypatch.setattr(prm.r34, "iter_listing_pages", boom)
    monkeypatch.setattr(prm.r34, "fetch_video_detail", lambda *a: {"date": "2026-01-01", "quality": 720})
    res = _runner(tmp_path).run([_job(cid="pc_bad", user_id="bad"), _job(cid="pc_ok", user_id="ok")])
    assert res["per_creator"]["pc_bad"]["errors"] == 1
    assert res["per_creator"]["pc_ok"]["new"] == 1
    assert _load(tmp_path, "pc_ok", "rule34video_ok", uid="ok")["items"]["1"]["number"] == 1


def test_cancel_mid_walk_records_nothing(tmp_path, monkeypatch):
    cancel = threading.Event()

    def iter_pages(session, uid, throttle, should_cancel):
        yield 1, [{"id": "2", "title": "t", "url": "https://rule34video.com/video/2/x/", "duration": 1}]
        cancel.set()
        yield 2, [{"id": "1", "title": "t", "url": "https://rule34video.com/video/1/x/", "duration": 1}]
    monkeypatch.setattr(prm.r34, "iter_listing_pages", iter_pages)
    res = _runner(tmp_path, cancel=cancel).run([_job(), _job(cid="pc_2")])
    assert res["cancelled"]
    assert not os.path.exists(str(tmp_path / "pmv" / "pc_1" / "rule34video_2472537.json"))
    assert "pc_2" not in res["per_creator"]


# ── iwara ───────────────────────────────────────────────────────────

def _iw_pages(monkeypatch, pages, raise_auth_when_token=None, seen=None):
    seen = seen if seen is not None else []

    def iter_videos(session, uuid, token, throttle, should_cancel, limit=50):
        seen.append(token)
        if raise_auth_when_token is not None and token == raise_auth_when_token:
            raise iw.AuthError("HTTP 401")
        total = sum(len(p) for p in pages)
        for n, ids in enumerate(pages):
            yield n, [{"id": i, "slug": "s", "title": f"t{i}", "createdAt": f"2026-01-{k + 1:02d}T00:00:00.000Z",
                       "file": {"width": 3840, "height": 2160}} for k, i in enumerate(ids)], total
    monkeypatch.setattr(prm.iw, "iter_videos", iter_videos)
    return seen


def test_iwara_anonymous_initial_and_incremental(tmp_path, monkeypatch):
    seen = _iw_pages(monkeypatch, [["c", "b"], ["a"]])
    res = _runner(tmp_path).run([_job(platform="iwara", user_id="uuid-1", name="Jietoman")])
    m = _load(tmp_path, "pc_1", "iwara_uuid-1", "iwara", "uuid-1")
    assert [m["items"][i]["number"] for i in ("a", "b", "c")] == [1, 2, 3]
    assert m["items"]["c"]["quality"] == 2160 and m["items"]["c"]["url"].endswith("/video/c/s")
    assert seen == [None] and res["total_new"] == 3 and not res["iwara_auth_failed"]


def test_iwara_login_token_used_and_persisted(tmp_path, monkeypatch):
    seen = _iw_pages(monkeypatch, [["a"]])

    class FakeAuth:
        enabled, auth_failed, message, user_token_changed, user_token = True, False, "", True, "USER-TOK"
        def access_token(self, force_refresh=False): return "ACCESS"
    r = _runner(tmp_path, state={"iwara_email": "e", "iwara_password": "p"})
    r._iwara_auth = FakeAuth()
    res = r.run([_job(platform="iwara", user_id="u")])
    assert seen == ["ACCESS"] and res["iwara_token"] == "USER-TOK"


def test_iwara_401_falls_back_to_anonymous(tmp_path, monkeypatch):
    seen = _iw_pages(monkeypatch, [["a"]], raise_auth_when_token="ACCESS")

    class FakeAuth:
        enabled, auth_failed, message, user_token_changed, user_token = True, False, "", False, None
        def access_token(self, force_refresh=False): return "ACCESS"
    r = _runner(tmp_path, state={"iwara_email": "e", "iwara_password": "p"})
    r._iwara_auth = FakeAuth()
    res = r.run([_job(platform="iwara", user_id="u")])
    assert seen == ["ACCESS", "ACCESS", None]          # refresh once, then anonymous
    assert res["iwara_auth_failed"] and res["total_new"] == 1


def test_iwara_resolves_uuid_from_username_when_missing(tmp_path, monkeypatch):
    _iw_pages(monkeypatch, [["a"]])
    monkeypatch.setattr(prm.iw, "fetch_profile", lambda *a, **k: {"id": "resolved", "name": "N", "username": "user1"})
    res = _runner(tmp_path).run([_job(platform="iwara", user_id="", username="user1")])
    assert res["total_new"] == 1
    assert os.path.exists(str(tmp_path / "pmv" / "pc_1" / "iwara_.json"))


# ── pawchive ────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, data=None, server="cloudflare", body=""):
        self.status_code = status
        self._data = data
        self.headers = {"server": server, "content-type": "application/json"}
        self.text = body
    def json(self): return self._data


def _pw_posts(ids_dates, media=None, links=None):
    out = []
    for pid, d in ids_dates:
        raw = {"id": pid, "user": "42", "service": "patreon", "title": f"post {pid}",
               "published": d, "content": "", "embed": {}, "attachments": [], "file": {},
               "preview_state": "scraped"}
        if media and pid in media:
            raw["file"] = {"name": media[pid], "path": f"/aa/bb/{media[pid]}"}
        if links and pid in links:
            raw["content"] = f'<p><a href="{links[pid]}">x</a></p>'
        out.append(raw)
    return out


def test_pawchive_full_walk_chronological_numbering(tmp_path, monkeypatch):
    posts = _pw_posts([("3", "2026-03-01T00:00:00"), ("1", "2026-01-01T00:00:00")],
                      media={"3": "clip.mp4"}, links={"1": "https://mega.nz/file/abc"})

    class S:
        def get(self, url, timeout=None):
            return _Resp(200, posts if "?o=" not in url else [])
    r = _runner(tmp_path)
    r._sessions["pawchive"] = S()
    res = r.run([_job(platform="pawchive", user_id="42", service="patreon")])
    m = _load(tmp_path, "pc_1", "pawchive_patreon_42", "pawchive", "42")
    assert m["numbering"] == pt.CHRONOLOGICAL
    assert m["items"]["1"]["number"] == 1 and m["items"]["3"]["number"] == 2
    assert m["items"]["3"]["media_kinds"] == ["video"] and m["items"]["1"]["link_hosts"] == ["mega.nz"]
    assert m["items"]["1"]["url"].endswith("/patreon/user/42/post/1")
    assert res["total_new"] == 2

    # Back-fill: post 2 appears with an older date → renumbered, flagged.
    posts[:] = _pw_posts([("3", "2026-03-01T00:00:00"), ("2", "2026-02-01T00:00:00"),
                          ("1", "2026-01-01T00:00:00")])
    r = _runner(tmp_path)
    r._sessions["pawchive"] = S()
    r.run([_job(platform="pawchive", user_id="42", service="patreon")])
    m = _load(tmp_path, "pc_1", "pawchive_patreon_42", "pawchive", "42")
    assert [m["items"][i]["number"] for i in ("1", "2", "3")] == [1, 2, 3]
    assert m["items"]["2"]["backfilled"]


def test_pawchive_cloudflare_challenge_sets_needs_cf_auth_and_continues(tmp_path, monkeypatch):
    monkeypatch.setattr(prm.pw, "is_cloudflare_challenge", lambda r: r.status_code == 403)

    class S:
        def get(self, url, timeout=None):
            return _Resp(403, body="Just a moment...")
    r = _runner(tmp_path)
    r._sessions["pawchive"] = S()
    _r34_pages(monkeypatch, [[1]])
    res = r.run([_job(cid="pc_pw", platform="pawchive", user_id="42", service="patreon"),
                 _job(cid="pc_r", user_id="7")])
    assert res["needs_cf_auth"]
    assert res["per_creator"]["pc_pw"]["errors"] == 1
    assert res["per_creator"]["pc_r"]["new"] == 1
    m = _load(tmp_path, "pc_pw", "pawchive_patreon_42", "pawchive", "42")
    assert "Cloudflare" in m["last_error"] and m["items"] == {}


def test_pawchive_retries_5xx_then_succeeds(tmp_path, monkeypatch):
    posts = _pw_posts([("1", "2026-01-01T00:00:00")])
    calls = []

    class S:
        def get(self, url, timeout=None):
            calls.append(url)
            if len(calls) == 1:
                return _Resp(503)
            return _Resp(200, posts if "?o=" not in url else [])
    r = _runner(tmp_path)
    r._sessions["pawchive"] = S()
    res = r.run([_job(platform="pawchive", user_id="42", service="patreon")])
    assert res["total_new"] == 1 and len(calls) == 2


def test_unsupported_platform_is_an_error(tmp_path):
    res = _runner(tmp_path).run([_job(platform="youtube", user_id="x")])
    assert res["errors"] and "unsupported" in res["errors"][0]["message"]
