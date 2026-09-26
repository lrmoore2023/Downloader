"""Per-creator tracked links: classification, storage (kept out of the crawled
links and carried through Configure edits), and fetch routing through the
Albums engines — each link into its own folder, tracked or not.

    python -m pytest tests/test_extra_links.py -q
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.api as api
from backend import extra_links as xl

TB = "https://www.terabox.app/sharing/link?surl=pJ9Wxqc4K8mZ7rJUxOiFlw"
MEGA = "https://mega.nz/folder/abc#key"
PW_URL = "https://pawchive.st/patreon/user/42"


def test_classify():
    assert xl.classify(TB) == ("terabox", True)
    assert xl.classify("https://gofile.io/d/x") == ("gofile", True)
    assert xl.classify(MEGA) == ("mega", False)
    assert xl.classify("https://drive.google.com/drive/folders/x") == ("gdrive", False)
    assert xl.classify("https://example.com/x") == ("other", False)


def test_resolve_dest(tmp_path):
    c = {"destination": str(tmp_path)}
    assert xl.resolve_dest(c, {"folder": "Images/terabox"})[0] == str(tmp_path / "Images" / "terabox")
    assert xl.resolve_dest(c, {"folder": "../escape"})[0] == str(tmp_path / "escape")
    absd = str(tmp_path / "abs")
    assert xl.resolve_dest({"destination": ""}, {"folder": absd})[0] == absd
    dest, why = xl.resolve_dest({"destination": ""}, {"folder": "terabox"})
    assert dest is None and "absolute" in why


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "STATE_FILE", str(tmp_path / "app_state.json"))
    monkeypatch.setattr(api, "STATE_BACKUP_DIR", str(tmp_path / "app_state.backups"))
    monkeypatch.setattr(api, "APP_DIR", str(tmp_path))
    a = api.Api.__new__(api.Api)
    a._state_lock = threading.Lock()
    a._album_runner = a._album_thread = None
    a._creator_runner = a._creator_aux_thread = None
    a._window = None
    return a


def _creator(app, tmp_path):
    dest = tmp_path / "Creator"
    dest.mkdir()
    app.save_state({"creators": {}, "archive_dir": str(tmp_path)})
    return app.save_creator({"name": "C", "destination": str(dest), "links": [{"url": PW_URL}]})["id"], dest


def test_add_update_remove_and_views(app, tmp_path):
    cid, dest = _creator(app, tmp_path)
    res = app.add_extra_links(cid, f"{TB} | pw123\nnot a link\n{TB}", "Images/terabox", True)
    assert res["added"] == 1                             # dupes and junk dropped
    res = app.add_extra_links(cid, MEGA)
    tb, mega = res["extra_links"]
    assert tb["site"] == "terabox" and tb["downloadable"] and tb["has_password"]
    assert "password" not in tb                          # never sent to the page
    assert os.path.normpath(tb["dest"]) == str(dest / "Images" / "terabox")
    assert mega["site"] == "mega" and not mega["downloadable"] and mega["folder"] == "mega"
    res = app.update_extra_link(cid, TB, {"track": False, "folder": "tb"})
    assert res["extra_links"][0]["track"] is False and res["extra_links"][0]["folder"] == "tb"
    res = app.remove_extra_link(cid, MEGA)
    assert [l["url"] for l in res["extra_links"]] == [TB]


def test_extra_links_survive_a_configure_edit_and_stay_out_of_links(app, tmp_path):
    cid, dest = _creator(app, tmp_path)
    app.add_extra_links(cid, TB)
    app.save_creator({"id": cid, "name": "C2", "destination": str(dest), "links": [{"url": PW_URL}]})
    c = app.get_creator(cid)
    assert [l["url"] for l in c["extra_links"]] == [TB]
    assert all("terabox" not in l.get("url", "") for l in c["links"])


def test_fetch_routes_each_link_to_its_folder(app, tmp_path, monkeypatch):
    cid, dest = _creator(app, tmp_path)
    app.add_extra_links(cid, f"{TB} | pw", "Images/terabox", True)
    app.add_extra_links(cid, "https://gofile.io/d/abc", "", False)
    calls, pushed = [], []

    class FakeRunner:
        def __init__(self, *a, **k):
            self.terabox = k.get("terabox")

        is_running = False

        def run(self, destination, links, on_progress, on_complete, on_error, track_urls=None, **k):
            calls.append((destination, links, track_urls))
            on_complete({"downloaded": 2, "skipped": 1, "errors": 0})
    monkeypatch.setattr(api, "AlbumRunner", FakeRunner)
    monkeypatch.setattr(app, "_push_js", lambda f, d: pushed.append((f, d)), raising=False)
    res = app.fetch_extra_links(cid, [TB, "https://gofile.io/d/abc"])
    assert res["status"] == "started"
    app._album_thread.join(5)
    norm = [(os.path.normpath(d), l, t) for d, l, t in calls]
    assert norm[0] == (str(dest / "Images" / "terabox"), [f"{TB} | pw"], [TB])
    assert norm[1] == (str(dest / "gofile"), ["https://gofile.io/d/abc"], [])
    assert os.path.isdir(dest / "Images" / "terabox")
    done = [d for f, d in pushed if f == "onExtraLinkComplete"][0]
    assert done["downloaded"] == 4 and done["creator_id"] == cid
    last = app.get_creator(cid)["extra_links"][0]
    assert last["last_run"] and last["last_result"] == {"downloaded": 2, "skipped": 1, "errors": 0}


def test_fetch_refuses_unsupported_hosts(app, tmp_path):
    cid, _ = _creator(app, tmp_path)
    app.add_extra_links(cid, MEGA)
    res = app.fetch_extra_links(cid, [MEGA])
    assert "can't be downloaded yet" in res["error"]
