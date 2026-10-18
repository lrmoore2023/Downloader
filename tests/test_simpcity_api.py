"""Api wiring for SimpCity threads: link parsing, the Real-only gate, dispatch."""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.api as api
from backend import creator_runner as cr

THREAD = "https://simpcity.cr/threads/atowerss.244743/page-11#post-123"
CANON = "https://simpcity.cr/threads/atowerss.244743/"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "STATE_FILE", str(tmp_path / "app_state.json"))
    monkeypatch.setattr(api, "STATE_BACKUP_DIR", str(tmp_path / "app_state.backups"))
    monkeypatch.setattr(api, "APP_DIR", str(tmp_path))
    a = api.Api.__new__(api.Api)
    a._state_lock = threading.Lock()
    return a


def _dest(tmp_path, *parts):
    return str(tmp_path.joinpath(*parts)).replace("\\", "/")


def test_resolve_simpcity_thread(app):
    info = app.resolve_link(THREAD)
    assert info == {"valid": True, "platform": "simpcity", "thread_id": "244743",
                    "name": "atowerss", "url": CANON}


def test_resolve_gate_by_destination(app, tmp_path):
    assert app.resolve_link(THREAD, _dest(tmp_path, "Real", "Creators", "Atowerss"))["valid"]
    assert app.resolve_link(THREAD, _dest(tmp_path, "Real", "Furry", "Someone"))["valid"]
    bad = app.resolve_link(THREAD, _dest(tmp_path, "Hentai", "2D", "Artist"))
    assert bad["valid"] is False and "Real" in bad["reason"]
    bad = app.resolve_link(THREAD, _dest(tmp_path, "Furry", "2D", "Artist"))
    assert bad["valid"] is False
    # Other platforms are never gated.
    assert app.resolve_link("https://x.com/someone", _dest(tmp_path, "Hentai", "2D", "A"))["valid"]


def test_save_creator_accepts_real_and_dedupes_pages(app, tmp_path):
    dest = _dest(tmp_path, "Real", "Creators", "Atowerss")
    res = app.save_creator({"name": "Atowerss", "destination": dest,
                            "links": [{"url": THREAD}, {"url": CANON + "page-3"}]})
    assert "error" not in res, res
    c = app.load_state()["creators"][res["id"]]
    assert c["links"] == [{"platform": "simpcity", "thread_id": "244743",
                           "name": "atowerss", "url": CANON}]
    assert app._summary(c["links"])["simpcity"] == 1


def test_save_creator_refuses_non_real(app, tmp_path):
    res = app.save_creator({"name": "A", "destination": _dest(tmp_path, "Hentai", "3D", "A"),
                            "links": [{"url": THREAD}]})
    assert "error" in res and "Real" in res["error"]
    assert not app.load_state().get("creators")


def test_archive_path_is_thread_keyed(app, tmp_path):
    link = {"platform": "simpcity", "thread_id": "244743", "url": CANON}
    arch = tmp_path / "arch"
    arch.mkdir()
    p1 = app._archive_path_for_link(link, str(arch), "/x/one")
    p2 = app._archive_path_for_link(link, str(arch), "/y/two")
    assert p1 == p2 == os.path.join(str(arch), "simpcity_244743.db")
    assert cr.errors_db_path(p1).endswith("simpcity_244743.errors.db")


def test_filter_and_label():
    links = [{"platform": "simpcity", "thread_id": "1", "name": "t", "url": CANON},
             {"platform": "twitter", "username": "u", "url": "https://x.com/u"}]
    assert cr.filter_links(links, "simpcity") == links[:1]
    assert cr.link_label(links[0]) == "SimpCity t"


def test_runner_dispatches_simpcity(monkeypatch, tmp_path):
    seen = {}

    class FakeRunner:
        def __init__(self, workers=3):
            pass

        def run(self, **kw):
            seen.update(kw)
            kw["on_complete"]({"downloaded": 2, "skipped": 1, "errors": 0,
                               "needs_simpcity_auth": True})

        def cancel(self):
            pass

    monkeypatch.setattr(cr, "SimpcityRunner", FakeRunner)
    creator = {"destination": str(tmp_path / "Real" / "Creators" / "A"),
               "links": [{"platform": "simpcity", "thread_id": "9", "name": "t", "url": CANON}]}
    done = {}
    r = cr.CreatorRunner()
    r.run(creator, "everything", "redownload_year", 2024, str(tmp_path), None, None,
          lambda d: None, done.update, lambda d: None,
          simpcity_cookies_path="c.txt", simpcity_user_agent="UA")
    assert seen["mode"] == "full"                       # a year means nothing to a thread
    assert seen["cookies_path"] == "c.txt" and seen["user_agent"] == "UA"
    assert seen["archive_path"] == os.path.join(str(tmp_path), "simpcity_9.db")
    assert done["downloaded"] == 2 and done["needs_simpcity_auth"] is True


# ── review list + tracked albums ────────────────────────────────────

from backend.simpcity_archive import ThreadDB  # noqa: E402


@pytest.fixture
def real(app, tmp_path):
    arch = tmp_path / "arch"
    arch.mkdir()
    app.save_state({"archive_dir": str(arch)})
    dest = _dest(tmp_path, "Real", "Creators", "Atowerss")
    res = app.save_creator({"name": "Atowerss", "destination": dest, "links": [{"url": THREAD}]})
    db = ThreadDB(os.path.join(str(arch), "simpcity_244743.db"))
    db.set(last_walk="2026-10-17T00:00:00+00:00", title="Atowerss", pages_total=11, last_page=11)
    for url, host, kind, ref, date in [
            ("https://bunkr.pk/a/7Z7niuP1", "bunkr", "album", "7Z7niuP1", "2025-10-27T10:00:00+00:00"),
            ("https://bunkr.pk/f/E4ZM1KwzCWSAc", "bunkr", "file", "E4ZM1KwzCWSAc", "2025-10-28T10:00:00+00:00"),
            ("https://mega.nz/folder/x", "mega", "other", "https://mega.nz/folder/x", "2025-01-01T10:00:00+00:00")]:
        db.upsert_link({"url": url, "host": host, "kind": kind, "ref": ref}, "77", date,
                       "https://simpcity.cr/posts/77/", 11)
    db.close()
    app._creator_runner = None
    app._creator_aux_thread = None
    return {"app": app, "id": res["id"], "arch": str(arch), "dest": dest}


def test_list_simpcity(real):
    out = real["app"].list_simpcity(real["id"])
    [t] = out["threads"]
    assert t["walked"] and t["title"] == "Atowerss" and t["pages_total"] == "11"
    by = {r["url"]: r for r in out["review"]}
    assert by["https://bunkr.pk/a/7Z7niuP1"]["can_track"] is True
    assert by["https://bunkr.pk/f/E4ZM1KwzCWSAc"]["can_track"] is False
    assert by["https://bunkr.pk/f/E4ZM1KwzCWSAc"]["can_download"] is True
    assert by["https://mega.nz/folder/x"]["can_download"] is False
    assert [r["post_date"][:10] for r in out["review"]] == ["2025-10-28", "2025-10-27", "2025-01-01"]
    assert out["albums"] == []


def test_dismiss_and_restore(real):
    a, cid = real["app"], real["id"]
    assert a.set_simpcity_link_status(cid, "244743", "https://mega.nz/folder/x", "dismissed")["ok"]
    st = {r["url"]: r["status"] for r in a.list_simpcity(cid)["review"]}
    assert st["https://mega.nz/folder/x"] == "dismissed"
    a.set_simpcity_link_status(cid, "244743", "https://mega.nz/folder/x", "pending")
    st = {r["url"]: r["status"] for r in a.list_simpcity(cid)["review"]}
    assert st["https://mega.nz/folder/x"] == "pending"
    assert "error" in a.set_simpcity_link_status(cid, "244743", "x", "tracked")


def test_track_adds_album_link_and_runs(real, monkeypatch):
    a, cid = real["app"], real["id"]
    a._creator_runner = None
    a._creator_aux_thread = None
    launched = {}
    monkeypatch.setattr(a, "_launch_creator_run",
                        lambda cid_, creator, scope, mode, after=None:
                        launched.update(creator=creator, scope=scope, mode=mode, after=after),
                        raising=False)
    res = a.simpcity_link_action(cid, "244743", "https://bunkr.pk/a/7Z7niuP1", "track")
    assert res["status"] == "started" and res["url"] == "https://bunkr.cr/a/7Z7niuP1"
    c = a.load_state()["creators"][cid]
    album = [l for l in c["links"] if l["platform"] == "album"]
    assert album == [{"platform": "album", "site": "bunkr", "album_id": "7Z7niuP1", "name": "",
                      "url": "https://bunkr.cr/a/7Z7niuP1",
                      "origin_date": "2025-10-27T10:00:00+00:00", "origin_thread": "244743"}]
    assert launched["scope"] == "link:https://bunkr.cr/a/7Z7niuP1"
    # the run reports the album reachable → the review row flips to tracked
    runner = type("R", (), {"album_results": [{"url": "https://bunkr.cr/a/7Z7niuP1", "alive": True,
                                               "title": "atowerss", "file_count": 105}]})()
    launched["after"](runner, {})
    out = a.list_simpcity(cid)
    st = {r["url"]: r["status"] for r in out["review"]}
    assert st["https://bunkr.pk/a/7Z7niuP1"] == "tracked"
    assert [x["album_id"] for x in out["albums"]] == ["7Z7niuP1"]
    # a plain Configure save keeps the origin fields
    a.save_creator({"id": cid, "name": "Atowerss", "destination": real["dest"],
                    "links": [{"url": l["url"]} for l in c["links"]]})
    kept = [l for l in a.load_state()["creators"][cid]["links"] if l["platform"] == "album"][0]
    assert kept["origin_date"].startswith("2025-10-27") and kept["origin_thread"] == "244743"
    # untrack removes the link but the thread's decision stays
    assert a.untrack_album(cid, "https://bunkr.cr/a/7Z7niuP1")["ok"]
    assert not [l for l in a.load_state()["creators"][cid]["links"] if l["platform"] == "album"]


def test_download_once_does_not_track(real, monkeypatch):
    a, cid = real["app"], real["id"]
    a._creator_runner = None
    a._creator_aux_thread = None
    launched = {}
    monkeypatch.setattr(a, "_launch_creator_run",
                        lambda cid_, creator, scope, mode, after=None:
                        launched.update(creator=creator, scope=scope, after=after), raising=False)
    res = a.simpcity_link_action(cid, "244743", "https://bunkr.pk/f/E4ZM1KwzCWSAc", "download")
    assert res["status"] == "started"
    assert [l["kind"] for l in launched["creator"]["links"]] == ["file"]
    assert not [l for l in a.load_state()["creators"][cid]["links"] if l["platform"] == "album"]
    # a dead result leaves the row actionable
    dead = type("R", (), {"album_results": [{"url": "https://bunkr.cr/f/E4ZM1KwzCWSAc", "alive": False}]})()
    launched["after"](dead, {})
    st = {r["url"]: r["status"] for r in a.list_simpcity(cid)["review"]}
    assert st["https://bunkr.pk/f/E4ZM1KwzCWSAc"] == "pending"
    ok = type("R", (), {"album_results": [{"url": "https://bunkr.cr/f/E4ZM1KwzCWSAc", "alive": True}]})()
    launched["after"](ok, {})
    st = {r["url"]: r["status"] for r in a.list_simpcity(cid)["review"]}
    assert st["https://bunkr.pk/f/E4ZM1KwzCWSAc"] == "downloaded"


def test_action_refusals(real):
    a, cid = real["app"], real["id"]
    a._creator_runner = None
    a._creator_aux_thread = None
    assert "error" in a.simpcity_link_action(cid, "244743", "https://mega.nz/folder/x", "download")
    assert "error" in a.simpcity_link_action(cid, "244743", "https://bunkr.pk/f/E4ZM1KwzCWSAc", "track")
    assert "error" in a.simpcity_link_action(cid, "244743", "https://bunkr.pk/a/x", "nope")


def test_check_links_records_liveness(real, monkeypatch):
    a, cid = real["app"], real["id"]
    import backend.album_track_runner as at
    monkeypatch.setattr(at, "probe_alive", lambda u: False if "mega" in u else True)
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    res = a.check_simpcity_links(cid)
    assert res == {"checked": 3, "dead": 1}
    alive = {r["url"]: r["alive"] for r in a.list_simpcity(cid)["review"]}
    assert alive["https://mega.nz/folder/x"] == "dead"
    assert alive["https://bunkr.pk/a/7Z7niuP1"] == "live"


def test_album_link_in_configure(app, tmp_path):
    dest = _dest(tmp_path, "Real", "Furry", "X")
    info = app.resolve_link("https://bunkr.si/a/7Z7niuP1", dest)
    assert info == {"valid": True, "platform": "album", "site": "bunkr", "album_id": "7Z7niuP1",
                    "name": "", "url": "https://bunkr.cr/a/7Z7niuP1"}
    single = app.resolve_link("https://bunkr.pk/f/E4ZM1KwzCWSAc", dest)
    assert single["valid"] is False and "single file" in single["reason"]
    assert app.resolve_link("https://gofile.io/d/abc", _dest(tmp_path, "Hentai", "2D", "A"))["valid"] is False
