"""site_login: sign-in capture by verification, cookie files, session wiring.

    python -m pytest tests/test_site_login.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.site_login as sl
import backend.r34video_scraper as r34
import backend.pmvhaven_scraper as pmvh


def _c(name, value, domain=".rule34video.com"):
    return {"name": name, "value": value, "domain": domain, "path": "/", "secure": True}


class FakeWindow:
    """Cookie jar that changes over successive polls."""
    def __init__(self, frames):
        self.frames = list(frames)
        self.polls = 0

    def get_cookies(self):
        i = min(self.polls, len(self.frames) - 1)
        self.polls += 1
        return self.frames[i]

    def evaluate_js(self, s):
        return "Mozilla/5.0 Chrome/140.0"


def test_capture_waits_for_a_verified_jar(tmp_path):
    out = tmp_path / "c.txt"
    anon = [_c("PHPSESSID", "a")]
    signed = [_c("PHPSESSID", "b"), _c("kt_member", "m"), _c("other", "x", ".example.com")]
    win = FakeWindow([anon, anon, signed])
    seen = []

    def verifier(platform, path, ua):
        txt = open(path, encoding="utf-8").read()
        seen.append(txt)
        return ("kt_member" in txt), "?"

    res = sl.capture_login(win, "rule34video", cookies_path=str(out), poll=0.2,
                           verify_every=0, verifier=verifier)
    assert res["ok"] and res["user_agent"].startswith("Mozilla")
    body = out.read_text(encoding="utf-8")
    assert "kt_member" in body and "example.com" not in body      # other domains dropped
    assert len(seen) == 2                                          # unchanged jar not re-tested


def test_failed_attempt_never_touches_saved_file(tmp_path):
    out = tmp_path / "c.txt"
    out.write_text("previous", encoding="utf-8")
    closed = {"n": 0}

    def is_closed():
        closed["n"] += 1
        return closed["n"] > 3

    res = sl.capture_login(FakeWindow([[_c("PHPSESSID", "a")]]), "rule34video",
                           cookies_path=str(out), poll=0.2, verify_every=0,
                           verifier=lambda *a: (False, "no"), is_closed=is_closed)
    assert not res["ok"] and "closed" in res["message"]
    assert out.read_text(encoding="utf-8") == "previous"


def test_cookie_file_round_trips_into_r34_session(tmp_path):
    p = tmp_path / "r.txt"
    sl.write_cookies([_c("kt_member", "m1")], str(p))
    s = r34.make_session(cookies_path=str(p))
    assert s.cookies.get("kt_member") == "m1"
    assert r34.make_session().cookies.get("kt_member") is None


def test_pmvhaven_api_key_header():
    assert pmvh.make_session(api_key=" pmvh_x ").headers["Authorization"] == "Bearer pmvh_x"
    assert "Authorization" not in pmvh.make_session().headers


def test_nas_payload_never_carries_feed_credentials():
    from backend.api import Api
    api = Api.__new__(Api)
    state = {"creators": {}, "pmv_creators": {}, "pmvhaven_api_key": "pmvh_secret",
             "r34video_cookies_path": "x", "iwara_password": "p"}
    payload = api._nas_backup_payload(state)
    text = repr(payload)
    assert "pmvh_secret" not in text and "r34video_cookies_path" not in text


# ── extension bridge ────────────────────────────────────────────────

def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_bridge_only_accepts_extension_origin():
    import json as _json
    import urllib.request
    import urllib.error
    from backend.ext_bridge import ExtBridge
    got = []
    b = ExtBridge(lambda platform, payload: (got.append((platform, payload)) or {"ok": True, "message": "k"}),
                  port=_free_port())
    assert b.start()
    try:
        url = f"http://127.0.0.1:{b.port}/pmv/session/rule34video"
        body = _json.dumps({"cookies": [{"name": "a", "value": "b"}]}).encode()

        def post(origin):
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, _json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None

        assert post(None)[0] == 403
        assert post("https://evil.example")[0] == 403
        code, res = post("chrome-extension://abc")
        assert code == 200 and res["ok"]
        assert got == [("rule34video", {"cookies": [{"name": "a", "value": "b"}]})]
    finally:
        b.stop()


def test_ext_session_rejected_unless_verified(tmp_path, monkeypatch):
    from backend.api import Api
    api = Api.__new__(Api)
    saved = {}
    api.save_state = lambda d: saved.update(d)
    api._push_js = lambda *a: None
    monkeypatch.setattr(sl, "default_cookies_path", lambda p: str(tmp_path / "r.txt"))
    payload = {"cookies": [_c("kt_member", "m")]}
    monkeypatch.setattr(sl, "verify", lambda *a, **k: (False, "Not signed in"))
    assert not api._on_ext_session("rule34video", payload)["ok"] and not saved
    monkeypatch.setattr(sl, "verify", lambda *a, **k: (True, "Signed in"))
    assert api._on_ext_session("rule34video", payload)["ok"]
    assert saved["r34video_cookies_path"].endswith("r.txt") and saved["r34video_signed_in_at"]
    assert not api._on_ext_session("rule34video", {"cookies": [_c("x", "y", ".other.com")]})["ok"]
