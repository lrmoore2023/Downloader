"""rule34video + iwara metadata scrapers (PMV tracker).

Fixtures mirror payloads captured live on 2026-09-18 (SadBernard on rule34video,
Jietoman on iwara).

    python -m pytest tests/test_pmv_scrapers.py -q
"""
import json
import os
import sys
import base64
import time

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.r34video_scraper as r34
import backend.iwara_scraper as iw


# ── fakes ───────────────────────────────────────────────────────────

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
    """Scripted responses keyed by exact URL (GET) or path (POST)."""

    def __init__(self, routes=None, posts=None):
        self.routes = routes or {}
        self.posts = posts or {}
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        if params:
            url = url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        self.calls.append(("GET", url, headers or {}))
        r = self.routes.get(url)
        if r is None:
            return FakeResp(404)
        if isinstance(r, list):
            return r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, Exception):
            raise r
        return r

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, headers or {}, data))
        r = self.posts.get(url)
        if callable(r):
            return r(data, headers)
        return r or FakeResp(404)


class NoThrottle:
    def __init__(self):
        self.throttled = []

    def wait(self, is_cancelled):
        pass

    def on_success(self):
        pass

    def on_throttled(self, retry_after):
        self.throttled.append(retry_after)


# ── rule34video fixtures ────────────────────────────────────────────

def _card(vid, slug, title, time_text):
    return f'''
    <div class="item thumb video_1   " data-video-card-id="{vid}" >
        <a data-href="https://rule34video.com/popup-video/{vid}/?popup_id=1" class="js-click hidden"></a>
        <a class="th js-open-popup" href="https://rule34video.com/video/{vid}/{slug}/"  title="{title}">
            <div class="img wrap_image">
                <div class="quality"><svg class="custom-svg custom-hd"><use xlink:href="#custom-hd"></use></svg></div>
                <div class="time">{time_text}</div>
            </div>
            <div class="thumb_title">{title}</div>
        </a>
    </div>'''


LISTING_BLOCK = ('<div class="thumbs clearfix" id="list_videos_uploaded_videos_items">'
                 + _card(4608707, "break-free-persona-5-hmv", "Break Free - Persona 5 HMV", "3:38")
                 + _card(4608699, "up-sombra-hmv", "Up - Sombra HMV", "2:46")
                 + '</div><div class="pagination" id="list_videos_uploaded_videos_pagination"></div>')

MEMBER_PAGE = ('<html><head><title>SadBernard&#039;s Page</title></head><body>'
               '<a class="btn" data-block-id="list_videos_uploaded_videos" data-parameters="">All Videos (16)</a>'
               + LISTING_BLOCK
               + '<div id="list_videos_favourite_videos">'
               + _card(999, "someone-elses", "Not Mine", "1:00")
               + '</div></body></html>')

VIDEO_PAGE_4K = '''<html><head><title>Confident - Brigitte HMV</title></head><body>
<script type="application/ld+json">{"@type":"VideoObject","name":"Confident - Brigitte HMV",
 "uploadDate": "2026-09-14", "duration": "PT0H3M40S"}</script>
<script>var flashvars = { video_url: 'https://x/360.mp4', video_url_text: '360p',
 video_alt_url: 'https://x/480.mp4', video_alt_url_text: '480p',
 video_alt_url2: 'https://x/720.mp4', video_alt_url2_text: '720p', video_alt_url2_hd: '1',
 video_alt_url3: 'https://x/1080.mp4', video_alt_url3_text: '1080p',
 video_alt_url4: 'https://x/2160.mp4', video_alt_url4_text: '4k' };</script>
<a href="https://rule34video.com/members/2472537/">SadBernard</a>
<a href="dl">MP4 2160p</a><a href="dl">MP4 1080p</a></body></html>'''

VIDEO_PAGE_720 = '''<html><head><title>The Paris Song - HMV</title></head><body>
<script type="application/ld+json">{"uploadDate": "2026-06-20", "duration": "PT0H3M40S"}</script>
<script>var flashvars = { video_url_text: '360p', video_alt_url_text: '480p', video_alt_url2_text: '720p' };</script>
</body></html>'''

VIDEO_PAGE_DL_ONLY = '<html><body><span>MP4 1080p</span><span>MP4 720p</span></body></html>'


# ── rule34video tests ───────────────────────────────────────────────

def test_r34_urls():
    assert r34.parse_member_url("https://rule34video.com/members/2472537/") == {"user_id": "2472537"}
    assert r34.parse_member_url("https://rule34video.com/members/2472537") == {"user_id": "2472537"}
    assert r34.parse_member_url("https://rule34video.com/video/4444829/x/") is None
    assert r34.listing_url("7", 2).endswith("&from_videos=02")
    assert r34.listing_url("7", 12).endswith("&from_videos=12")
    assert r34.video_id_from_url("https://rule34video.com/video/4444829/the-paris-song-hmv/") == "4444829"


def test_r34_parse_member_page():
    info = r34.parse_member_page(MEMBER_PAGE)
    assert info == {"name": "SadBernard", "total": 16}


def test_r34_parse_listing_only_reads_uploaded_container():
    items = r34.parse_listing(MEMBER_PAGE)
    assert [i["id"] for i in items] == ["4608707", "4608699"]
    assert items[0]["title"] == "Break Free - Persona 5 HMV"
    assert items[0]["url"] == "https://rule34video.com/video/4608707/break-free-persona-5-hmv/"
    assert items[0]["duration"] == 218 and items[0]["duration_text"] == "3:38"


def test_r34_parse_listing_async_block():
    items = r34.parse_listing(LISTING_BLOCK)
    assert len(items) == 2
    assert r34.parse_listing("<div></div>") == []


def test_r34_durations():
    assert r34.parse_duration_text("3:38") == 218
    assert r34.parse_duration_text("1:02:03") == 3723
    assert r34.parse_duration_text("") is None
    assert r34.parse_iso8601_duration("PT0H3M40S") == 220
    assert r34.parse_iso8601_duration("PT12S") == 12
    assert r34.parse_iso8601_duration("bogus") is None


def test_r34_parse_video_page_4k():
    d = r34.parse_video_page(VIDEO_PAGE_4K)
    assert d["date"] == "2026-09-14"
    assert d["duration"] == 220
    assert d["quality"] == 2160
    assert d["uploader_id"] == "2472537"
    assert d["title"] == "Confident - Brigitte HMV"


def test_r34_parse_video_page_720_and_fallback():
    assert r34.parse_video_page(VIDEO_PAGE_720)["quality"] == 720
    assert r34.parse_video_page(VIDEO_PAGE_720)["date"] == "2026-06-20"
    assert r34.parse_video_page(VIDEO_PAGE_DL_ONLY)["quality"] == 1080
    assert r34.parse_video_page("")["quality"] is None


def test_r34_iter_listing_pages_stops_on_404():
    s = FakeSession({
        r34.listing_url("1", 1): FakeResp(200, LISTING_BLOCK),
        r34.listing_url("1", 2): FakeResp(200, LISTING_BLOCK.replace("4608707", "111").replace("4608699", "222")),
        # page 3 → 404 (not in routes)
    })
    pages = list(r34.iter_listing_pages(s, "1", NoThrottle()))
    assert [p for p, _ in pages] == [1, 2]
    assert [i["id"] for i in pages[1][1]] == ["111", "222"]
    assert len(s.calls) == 3


def test_r34_fetch_text_retries_then_succeeds_and_raises_on_other_errors():
    t = NoThrottle()
    s = FakeSession({"u": [FakeResp(503, headers={"Retry-After": "3"}), FakeResp(200, "ok")]})
    assert r34.fetch_text(s, "u", t) == "ok"
    assert t.throttled == [3.0]
    s = FakeSession({"u": FakeResp(500)})
    with pytest.raises(r34.SiteError):
        r34.fetch_text(s, "u", t, retries=1)
    s = FakeSession({"u": FakeResp(403)})
    with pytest.raises(r34.SiteError):
        r34.fetch_text(s, "u", t)
    s = FakeSession({"u": requests.ConnectionError("boom")})
    with pytest.raises(r34.SiteError):
        r34.fetch_text(s, "u", t, retries=1)
    with pytest.raises(r34.NotFound):
        r34.fetch_text(FakeSession(), "missing", t)


# ── iwara fixtures ──────────────────────────────────────────────────

IW_VIDEO_4K = {"id": "6vQpTzYELfZQ4h", "slug": "hmv-futa-bordello", "title": "【HMV-FUTA BORDELLO】",
               "createdAt": "2026-09-10T00:08:28.000Z", "private": False, "unlisted": False,
               "rating": "ecchi", "file": {"width": 3840, "height": 2160, "duration": 245}}
IW_VIDEO_OLD = {"id": "5akn6caeqztwrjjyr", "slug": "hmv-gimme-moretifa", "title": "【HMV-Gimme more】Tifa",
                "createdAt": "2021-04-21T22:23:11.000Z", "private": True, "unlisted": False,
                "file": {"width": None, "height": None, "duration": None}}
IW_PROFILE = {"user": {"id": "af0f4a6f-7a04-447d-92ab-5a08d329be01", "name": "𝓙𝓲𝓮𝓽𝓸𝓶𝓪𝓷",
                       "username": "user1833289"}}


def _jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"hdr.{payload}.sig"


def _videos_url(uuid, page, limit=50):
    return f"{iw.API}/videos?user={uuid}&sort=date&page={page}&limit={limit}"


# ── iwara tests ─────────────────────────────────────────────────────

def test_iwara_urls():
    assert iw.parse_profile_url("https://www.iwara.tv/profile/user1833289/videos") == {"username": "user1833289"}
    assert iw.parse_profile_url("https://iwara.tv/profile/lumymmd") == {"username": "lumymmd"}
    assert iw.parse_profile_url("https://www.iwara.tv/video/abc/slug") is None
    assert iw.video_url("5akn6caeqztwrjjyr", "hmv-gimme-moretifa") == \
        "https://www.iwara.tv/video/5akn6caeqztwrjjyr/hmv-gimme-moretifa"
    assert iw.video_url("abc") == "https://www.iwara.tv/video/abc"


def test_iwara_parse_video():
    v = iw.parse_video(IW_VIDEO_4K)
    assert v["quality"] == 2160 and v["duration"] == 245
    assert v["date"] == "2026-09-10T00:08:28+00:00"
    assert v["url"].endswith("/video/6vQpTzYELfZQ4h/hmv-futa-bordello")
    assert v["private"] is False
    o = iw.parse_video(IW_VIDEO_OLD)
    assert o["quality"] is None and o["duration"] is None and o["private"] is True
    assert o["date"] == "2021-04-21T22:23:11+00:00"
    assert iw.parse_profile(IW_PROFILE)["id"] == "af0f4a6f-7a04-447d-92ab-5a08d329be01"


def test_iwara_iter_videos_stops_on_short_page_not_count():
    # Live, `count` under-reported on page 0 (51) vs page 1 (83); a full page
    # must keep walking even when count says we're done.
    uuid = "u-1"
    s = FakeSession({
        _videos_url(uuid, 0, 2): FakeResp(200, json_data={"count": 2, "limit": 2, "page": 0,
                                                          "results": [IW_VIDEO_4K, IW_VIDEO_4K]}),
        _videos_url(uuid, 1, 2): FakeResp(200, json_data={"count": 3, "limit": 2, "page": 1,
                                                          "results": [IW_VIDEO_OLD]}),
    })
    pages = list(iw.iter_videos(s, uuid, token="tok", throttle=NoThrottle(), limit=2))
    assert [(p, len(r), c) for p, r, c in pages] == [(0, 2, 2), (1, 1, 3)]
    assert s.calls[0][2] == {"Authorization": "Bearer tok"}
    assert len(s.calls) == 2                 # short page 1 ended the walk; no page 2 call


def test_iwara_iter_videos_anonymous_and_empty():
    s = FakeSession({_videos_url("u", 0): FakeResp(200, json_data={"count": 0, "results": []})})
    assert list(iw.iter_videos(s, "u", throttle=NoThrottle())) == []
    assert s.calls[0][2] == {}


def test_iwara_fetch_json_errors():
    t = NoThrottle()
    with pytest.raises(iw.AuthError):
        iw.fetch_json(FakeSession({"u": FakeResp(401)}), "u", throttle=t)
    with pytest.raises(iw.NotFound):
        iw.fetch_json(FakeSession(), "u", throttle=t)
    with pytest.raises(iw.SiteError):
        iw.fetch_json(FakeSession({"u": FakeResp(200, text="nope")}), "u", throttle=t)
    assert iw.fetch_json(FakeSession({"u": [FakeResp(429), FakeResp(200, json_data={"a": 1})]}),
                         "u", throttle=t) == {"a": 1}


def test_iwara_fetch_profile():
    s = FakeSession({f"{iw.API}/profile/user1833289": FakeResp(200, json_data=IW_PROFILE)})
    p = iw.fetch_profile(s, "user1833289", throttle=NoThrottle())
    assert p["username"] == "user1833289" and p["id"].startswith("af0f")
    with pytest.raises(iw.SiteError):
        iw.fetch_profile(FakeSession({f"{iw.API}/profile/x": FakeResp(200, json_data={})}), "x")


def test_jwt_exp_and_validity():
    assert iw.jwt_exp(_jwt(1700000000)) == 1700000000
    assert iw.jwt_exp("garbage") is None
    assert iw.token_valid(_jwt(2000), now=1000)
    assert not iw.token_valid(_jwt(1050), now=1000)          # inside the 60 s margin
    assert not iw.token_valid("", now=1000)


def _auth_session(login_ok=True, refresh_status=200):
    now = 1_000_000
    user_tok, access_tok = _jwt(now + 3600 * 24), _jwt(now + 3600)

    def login(data, headers):
        body = json.loads(data)
        if login_ok and body == {"email": "e@x", "password": "pw"}:
            return FakeResp(200, json_data={"token": user_tok})
        return FakeResp(400, json_data={"message": "errors.invalidLogin"})

    def token(data, headers):
        assert data == b""
        if headers.get("Authorization") == f"Bearer {user_tok}" and refresh_status == 200:
            return FakeResp(200, json_data={"accessToken": access_tok})
        return FakeResp(refresh_status, json_data={"message": "errors.unauthorized"})

    s = FakeSession(posts={f"{iw.API}/user/login": login, f"{iw.API}/user/token": token})
    return s, now, user_tok, access_tok


def test_iwara_auth_full_login_then_cached():
    s, now, user_tok, access_tok = _auth_session()
    a = iw.IwaraAuth(s, "e@x", "pw", now=lambda: now)
    assert a.access_token() == access_tok
    assert a.user_token == user_tok and a.user_token_changed
    assert [c[1] for c in s.calls] == [f"{iw.API}/user/login", f"{iw.API}/user/token"]
    assert a.access_token() == access_tok          # cached: no new calls
    assert len(s.calls) == 2


def test_iwara_auth_reuses_saved_user_token():
    s, now, user_tok, access_tok = _auth_session()
    a = iw.IwaraAuth(s, "e@x", "pw", user_token=user_tok, now=lambda: now)
    assert a.access_token() == access_tok
    assert [c[1] for c in s.calls] == [f"{iw.API}/user/token"]
    assert not a.user_token_changed


def test_iwara_auth_relogs_when_refresh_rejected():
    s, now, user_tok, access_tok = _auth_session()
    stale = _jwt(now + 99999) + "x"                  # unexpired-looking but wrong
    a = iw.IwaraAuth(s, "e@x", "pw", user_token=stale, now=lambda: now)
    assert a.access_token() == access_tok
    assert a.user_token == user_tok and a.user_token_changed
    assert [c[1] for c in s.calls] == [f"{iw.API}/user/token", f"{iw.API}/user/login", f"{iw.API}/user/token"]


def test_iwara_auth_bad_credentials_degrade_to_anonymous():
    s, now, *_ = _auth_session(login_ok=False)
    a = iw.IwaraAuth(s, "e@x", "wrong", now=lambda: now)
    assert a.access_token() is None
    assert a.auth_failed and "invalid email or password" in a.message
    assert a.access_token() is None                # stays anonymous, no exception


def test_iwara_auth_disabled_without_credentials():
    a = iw.IwaraAuth(FakeSession(), "", "")
    assert not a.enabled and a.access_token() is None and not a.auth_failed
