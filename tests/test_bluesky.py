"""Bluesky (Creator tab): URL parsing, gallery-dl config, the from-disk archive
rebuild, link wiring and the extension's sign-in hand-off. Offline.

    python -m pytest tests/test_bluesky.py -q
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.bluesky as bsky
from backend.archive_sync import (
    reorganize_twitter_media, scan_files_for_entries, sync_archive,
)
from backend.config_builder import build_bluesky_config, cleanup_config
from backend.creator_runner import (
    bluesky_archive_path, filter_links, link_label, _ensure_archive,
)
from backend.gallery_dl_runner import GalleryDlRunner

DID = "did:plc:abcdefghijklmnopqrstuvwx"


def test_parse_profile_url():
    assert bsky.parse_profile_url("https://bsky.app/profile/someone.bsky.social") == "someone.bsky.social"
    assert bsky.parse_profile_url("bsky.app/profile/Someone.Example.com/post/3abcdefghijk2") == "someone.example.com"
    assert bsky.parse_profile_url(f"https://bsky.app/profile/{DID}") == DID
    assert bsky.parse_profile_url("https://fxbsky.app/profile/a.bsky.social") == "a.bsky.social"
    for bad in ("https://x.com/someone", "https://bsky.app/", "https://bsky.app/profile/nodot",
                "https://evilbsky.app.example/profile/a.b", "https://bsky.app/profile/did:plc:short"):
        assert bsky.parse_profile_url(bad) is None, bad


def test_archive_keyed_by_did_not_handle(tmp_path):
    link = {"platform": "bluesky", "did": DID, "handle": "old.bsky.social"}
    a = bluesky_archive_path(str(tmp_path), link, "unused")
    renamed = dict(link, handle="new.bsky.social")
    assert a == bluesky_archive_path(str(tmp_path), renamed, "unused")
    assert os.path.basename(a) == "bluesky_did-plc-abcdefghijklmnopqrstuvwx.db"
    # No archive dir → beside the media.
    assert bluesky_archive_path("", link, str(tmp_path / "Bluesky")).startswith(str(tmp_path / "Bluesky"))


def test_api_server_only_for_self_hosted():
    assert bsky.api_server_for("https://morel.us-east.host.bsky.network") is None
    assert bsky.api_server_for("https://bsky.social") is None
    assert bsky.api_server_for("https://pds.example.com/") == "https://pds.example.com"
    assert bsky.api_server_for("http://pds.example.com") is None   # never a bearer over http
    assert bsky.api_server_for("") is None


def _cfg(**kw):
    path = build_bluesky_config("P:/Real/X/Bluesky", **kw)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    finally:
        cleanup_config(path)


def test_config_layout_matches_twitter():
    c = _cfg()
    b = c["extractor"]["bluesky"]
    assert b["filename"] == "{date:%Y.%m.%d} - Bluesky - {post_id}_{num}.{extension}"
    assert b["directory"] == ["{date:%Y}"]
    assert b["archive-format"] == "{post_id}_{num}" and b["archive-prefix"] == "bluesky_"
    assert b["videos"] is True and b["reposts"] is False and b["quoted"] is False
    assert "username" not in b and "skip" not in b and "post-filter" not in b
    assert c["cache"] == {"file": None}       # a replaced app password must not be shadowed


def test_config_modes_and_auth():
    assert _cfg(mode="latest")["extractor"]["bluesky"]["skip"].startswith("abort:")
    assert _cfg(mode="redownload_year", year=2025)["extractor"]["bluesky"]["post-filter"] == "date.year == 2025"
    b = _cfg(username="me.bsky.social", password="abcd-efgh-ijkl-mnop")["extractor"]["bluesky"]
    assert b["username"] == "me.bsky.social" and b["password"] == "abcd-efgh-ijkl-mnop"
    assert "api-server" not in b
    b = _cfg(username="me.example.com", password="p", api_server="https://pds.example.com")["extractor"]["bluesky"]
    assert b["api-server"] == "https://pds.example.com"
    assert _cfg(videos=False)["extractor"]["bluesky"]["videos"] is False
    assert _cfg(images=False)["extractor"]["bluesky"]["image-filter"] == "extension == 'mp4'"


def test_archive_rebuilt_from_disk_after_image_split(tmp_path):
    dest = tmp_path / "Bluesky"
    (dest / "2026").mkdir(parents=True)
    for n in ("2026.11.07 - Bluesky - 3abcdefghijk2_1.jpg", "2026.11.07 - Bluesky - 3abcdefghijk2_2.png",
              "2026.11.03 - Bluesky - 3zyxwvutsrqp2_1.mp4", "2026.11.03 - Twitter - 123_1.jpg"):
        (dest / "2026" / n).write_bytes(b"x")
    assert reorganize_twitter_media(str(dest)) == 3          # the jpg + png + the twitter jpg
    assert (dest / "Images" / "2026" / "2026.11.07 - Bluesky - 3abcdefghijk2_1.jpg").is_file()
    assert (dest / "2026" / "2026.11.03 - Bluesky - 3zyxwvutsrqp2_1.mp4").is_file()

    entries = scan_files_for_entries(str(dest), site="bluesky")
    assert entries == {"bluesky_3abcdefghijk2_1", "bluesky_3abcdefghijk2_2", "bluesky_3zyxwvutsrqp2_1"}
    assert scan_files_for_entries(str(dest)) == {"twitter_123_0_1"}   # twitter unchanged

    db = tmp_path / "bluesky_x.db"
    _ensure_archive(str(db))
    assert sync_archive(str(db), str(dest), site="bluesky")["added"] == 3
    got = {r[0] for r in sqlite3.connect(db).execute("SELECT entry FROM archive")}
    assert got == entries


def test_links_scope_and_label():
    links = [{"platform": "bluesky", "did": DID, "handle": "a.bsky.social", "url": "u1"},
             {"platform": "twitter", "username": "a", "url": "u2"}]
    assert filter_links(links, "bluesky") == links[:1]
    assert link_label(links[0]) == "Bluesky @a.bsky.social"


def test_auth_error_is_site_aware():
    line = "[bluesky][error] AuthenticationError: \"AuthenticationRequired: Invalid identifier or password\""
    ev = GalleryDlRunner(platform="bluesky")._parse_line(line)
    assert ev["subtype"] == "auth" and ev["platform"] == "bluesky" and "Bluesky" in ev["message"]
    ev = GalleryDlRunner()._parse_line("[twitter][error] HttpError: '401 Unauthorized'")
    assert ev["subtype"] == "auth" and ev["platform"] == "twitter"


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._d = status, data

    def json(self):
        return self._d


class _FakePds:
    def __init__(self, existing=("Downloader",), session_ok=True):
        self.names = list(existing)
        self.session_ok = session_ok
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        if url.endswith("getSession"):
            return _Resp(200, {"did": DID, "handle": "me.bsky.social"}) if self.session_ok \
                else _Resp(400, {"error": "ExpiredToken", "message": "Token has expired"})
        if url.endswith("listAppPasswords"):
            return _Resp(200, {"passwords": [{"name": n} for n in self.names]})
        raise AssertionError(url)

    def post(self, url, json=None, **kw):
        self.calls.append(("POST", url, json))
        if url.endswith("revokeAppPassword"):
            self.names.remove(json["name"])
            return _Resp(200, {})
        if url.endswith("createAppPassword"):
            assert json["name"] not in self.names and json["privileged"] is False
            self.names.append(json["name"])
            return _Resp(200, {"name": json["name"], "password": "aaaa-bbbb-cccc-dddd"})
        raise AssertionError(url)


def test_app_password_replaces_our_old_one():
    pds = _FakePds()
    res = bsky.app_password_from_session("jwt", "https://x.host.bsky.network", http=pds)
    assert res == {"did": DID, "handle": "me.bsky.social", "password": "aaaa-bbbb-cccc-dddd",
                   "pds": "https://x.host.bsky.network"}
    assert [c[1].rsplit(".", 1)[1] for c in pds.calls] == [
        "getSession", "listAppPasswords", "revokeAppPassword", "createAppPassword"]


def test_app_password_refuses_expired_or_insecure():
    import pytest
    with pytest.raises(ValueError, match="Reload the bsky.app tab"):
        bsky.app_password_from_session("jwt", "https://x.host.bsky.network",
                                       http=_FakePds(session_ok=False))
    pds = _FakePds()
    with pytest.raises(ValueError):
        bsky.app_password_from_session("jwt", "http://x.host.bsky.network", http=pds)
    assert not pds.calls                                    # token never sent over http


def _api(state):
    from backend.api import Api
    api = Api.__new__(Api)
    saved, pushed = {}, []
    api.load_state = lambda: dict(state)
    api.save_state = lambda d: saved.update(d)
    api._push_js = lambda *a: pushed.append(a)
    return api, saved, pushed


def test_ext_session_mints_and_saves(monkeypatch):
    api, saved, pushed = _api({})
    monkeypatch.setattr(bsky, "app_password_from_session", lambda jwt, pds: {
        "did": DID, "handle": "me.bsky.social", "password": "pw-1", "pds": pds})
    monkeypatch.setattr(bsky, "verify_login", lambda u, p, pds=None: (True, "ok", {"did": DID, "handle": u}))
    res = api._on_ext_session("bluesky", {"did": DID, "accessJwt": "jwt",
                                          "service": "https://x.host.bsky.network"})
    assert res["ok"]
    assert saved == {"bluesky_username": "me.bsky.social", "bluesky_app_password": "pw-1",
                     "bluesky_pds": "https://x.host.bsky.network"}
    assert pushed[-1][0] == "onBlueskyFromExt"


def test_ext_session_keeps_working_password(monkeypatch):
    api, saved, _ = _api({"bluesky_username": "me.bsky.social", "bluesky_app_password": "old"})
    monkeypatch.setattr(bsky, "verify_login", lambda u, p, pds=None: (True, "ok", {"did": DID, "handle": u}))

    def boom(*a):
        raise AssertionError("must not mint a new password")
    monkeypatch.setattr(bsky, "app_password_from_session", boom)
    res = api._on_ext_session("bluesky", {"did": DID, "accessJwt": "jwt", "service": "https://x"})
    assert res["ok"] and "Already signed in" in res["message"] and saved["bluesky_app_password"] == "old"


def test_ext_session_failure_saves_nothing(monkeypatch):
    api, saved, pushed = _api({})

    def expired(*a):
        raise ValueError("Chrome's Bluesky session wasn't accepted")
    monkeypatch.setattr(bsky, "app_password_from_session", expired)
    res = api._on_ext_session("bluesky", {"did": DID, "accessJwt": "jwt", "service": "https://x"})
    assert not res["ok"] and not saved and not pushed


def test_resolve_link_keys_by_did(monkeypatch):
    from backend.api import Api
    api = Api.__new__(Api)
    monkeypatch.setattr(bsky, "get_profile", lambda actor: {
        "did": DID, "handle": "someone.bsky.social", "display_name": "Someone", "avatar": ""})
    info = api._resolve_link("https://bsky.app/profile/someone.bsky.social/post/3abcdefghijk2")
    assert info == {"valid": True, "platform": "bluesky", "did": DID, "handle": "someone.bsky.social",
                    "name": "Someone", "url": "https://bsky.app/profile/someone.bsky.social"}
    link = api._link_from_url("https://bsky.app/profile/someone.bsky.social")
    assert link["platform"] == "bluesky" and link["did"] == DID
    assert Api._summary([link])["bluesky"] == 1
