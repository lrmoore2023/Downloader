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
