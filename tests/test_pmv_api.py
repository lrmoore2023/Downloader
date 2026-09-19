"""PMV tracker — Api layer: creator CRUD, checklist status writes, the fetch job
gate, and state persistence (snapshots + NAS mirror carry pmv_creators, never
the iwara password).

    python -m pytest tests/test_pmv_api.py -q
"""
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.api as api
import backend.pmv_tracker as pt


@pytest.fixture
def app(tmp_path, monkeypatch):
    """An Api with state + backups redirected to a throwaway sandbox."""
    monkeypatch.setattr(api, "STATE_FILE", str(tmp_path / "app_state.json"))
    monkeypatch.setattr(api, "STATE_BACKUP_DIR", str(tmp_path / "app_state.backups"))
    monkeypatch.setattr(api, "APP_DIR", str(tmp_path))
    a = api.Api.__new__(api.Api)          # skip __init__ (no MediaServer/window)
    a._state_lock = threading.Lock()
    a._pmv_thread = None
    a._pmv_runner = None
    a._pmv_cancel = threading.Event()
    a._window = None
    return a


R34 = {"platform": "rule34video", "user_id": "2472537", "username": "SadBernard",
       "display_name": "SadBernard", "url": "https://rule34video.com/members/2472537/", "site_code": "R34"}
IWA = {"platform": "iwara", "user_id": "af0f", "username": "user1833289", "display_name": "J",
       "url": "https://www.iwara.tv/profile/user1833289", "site_code": "Iwara"}
PAW = {"platform": "pawchive", "service": "patreon", "user_id": "42", "username": "M",
       "display_name": "M", "url": "https://pawchive.pw/patreon/user/42", "site_code": "Pawchive"}


def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network call in test")
    monkeypatch.setattr(api.r34, "make_session", boom)
    monkeypatch.setattr(api.iw, "make_session", boom)
    monkeypatch.setattr(api, "pw_make_session", boom)


# ── CRUD ────────────────────────────────────────────────────────────

def test_save_and_list_pmv_creator_preserves_order_and_defaults(app, monkeypatch):
    _no_network(monkeypatch)
    paw = dict(PAW); paw["site_code"] = ""          # blank → default
    res = app.save_pmv_creator({"name": "  SadBernard ", "tags": "3D, Furry,,3D",
                                "links": [paw, R34, IWA, dict(R34)]})
    cid = res["id"]
    assert cid.startswith("pc_")
    rec = app.get_pmv_creator(cid)
    assert rec["name"] == "SadBernard" and rec["tags"] == ["3D", "Furry"]
    assert [l["platform"] for l in rec["links"]] == ["pawchive", "rule34video", "iwara"]
    assert rec["links"][0]["site_code"] == "Pawchive"
    assert rec["created"]
    lst = app.list_pmv_creators()
    assert len(lst) == 1 and lst[0]["id"] == cid
    assert [s["link_key"] for s in lst[0]["links"]] == ["pawchive_patreon_42", "rule34video_2472537", "iwara_af0f"]
    assert lst[0]["counts"]["total"] == 0
    assert app.load_state()["last_pmv_creator"] == cid


def test_save_requires_name_and_rejects_unknown_links(app, monkeypatch):
    _no_network(monkeypatch)
    assert "error" in app.save_pmv_creator({"name": "", "links": [R34]})
    monkeypatch.setattr(app, "resolve_pmv_link", lambda url: {"valid": False})
    assert "error" in app.save_pmv_creator({"name": "X", "links": [{"url": "https://youtube.com/x"}]})
    assert app.list_pmv_creators() == []


def test_save_resolves_url_only_links(app, monkeypatch):
    monkeypatch.setattr(app, "resolve_pmv_link",
                        lambda url: dict(R34, valid=True) if "rule34video" in url else {"valid": False})
    res = app.save_pmv_creator({"name": "X", "links": [{"url": "https://rule34video.com/members/2472537/",
                                                         "site_code": "RV"}]})
    rec = app.get_pmv_creator(res["id"])
    assert rec["links"][0]["user_id"] == "2472537" and rec["links"][0]["site_code"] == "RV"


def test_update_keeps_id_and_created(app, monkeypatch):
    _no_network(monkeypatch)
    cid = app.save_pmv_creator({"name": "A", "links": [R34]})["id"]
    created = app.get_pmv_creator(cid)["created"]
    app.save_pmv_creator({"id": cid, "name": "B", "links": [IWA, R34]})
    rec = app.get_pmv_creator(cid)
    assert rec["name"] == "B" and rec["created"] == created
    assert [l["platform"] for l in rec["links"]] == ["iwara", "rule34video"]
    assert len(app.list_pmv_creators()) == 1


def test_delete_optionally_removes_manifests(app, monkeypatch, tmp_path):
    _no_network(monkeypatch)
    arch = tmp_path / "arch"; arch.mkdir()
    app.save_state({"archive_dir": str(arch)})
    cid = app.save_pmv_creator({"name": "A", "links": [R34]})["id"]
    root = app._pmv_root(cid)
    assert root.replace("\\", "/").startswith(str(arch).replace("\\", "/") + "/pmv/")
    os.makedirs(root, exist_ok=True)
    open(os.path.join(root, "rule34video_2472537.json"), "w").write("{}")
    app.delete_pmv_creator(cid)
    assert app.get_pmv_creator(cid) is None and os.path.isdir(root)
    cid2 = app.save_pmv_creator({"name": "B", "links": [R34]})["id"]
    root2 = app._pmv_root(cid2); os.makedirs(root2, exist_ok=True)
    app.delete_pmv_creator(cid2, delete_manifests=True)
    assert not os.path.isdir(root2)


def test_pmv_root_falls_back_to_app_dir(app, tmp_path):
    root = app._pmv_root("pc_x")
    assert root.replace("\\", "/") == (str(tmp_path).replace("\\", "/") + "/.pmv/pc_x")


# ── checklist ───────────────────────────────────────────────────────

def _seed(app, cid, link, ids):
    """Write a numbered manifest for one site."""
    key = pt.link_key(link)
    path = pt.manifest_path(app._pmv_root(cid), key)
    m = pt.new_manifest(link["platform"], link["user_id"])
    fetched = [{"id": str(i), "title": f"v{i}", "url": f"u/{i}", "pos": n} for n, i in enumerate(ids)]
    pt.merge_locked(m, fetched, full=True, complete=True, now="T0")
    pt.save_manifest(path, m)
    return key, path


def test_get_items_prefix_and_ordering(app, monkeypatch):
    _no_network(monkeypatch)
    cid = app.save_pmv_creator({"name": "SadBernard", "links": [R34, IWA]})["id"]
    _seed(app, cid, R34, [30, 20, 10])
    data = app.get_pmv_items(cid)
    assert [s["platform"] for s in data["sites"]] == ["rule34video", "iwara"]
    r = data["sites"][0]
    assert [it["number"] for it in r["items"]] == [3, 2, 1]
    assert r["items"][2]["prefix"] == "SadBernard - R34 - 01 - "
    assert r["width"] == 2 and r["initial_complete"]
    assert data["sites"][1]["items"] == [] and not data["sites"][1]["initial_complete"]
    assert "error" in app.get_pmv_items("pc_nope")


def test_set_status_records_number_at_check_and_counts(app, monkeypatch):
    _no_network(monkeypatch)
    cid = app.save_pmv_creator({"name": "S", "links": [R34]})["id"]
    key, path = _seed(app, cid, R34, [30, 20, 10])
    assert app.set_pmv_status(cid, key, "20", "downloaded") == {"ok": True, "updated": 1}
    assert app.set_pmv_status_bulk(cid, key, ["10", "999"], "skipped")["updated"] == 1
    m = pt.load_manifest(path, "rule34video", "2472537")
    assert m["items"]["20"]["status"] == "downloaded" and m["items"]["20"]["number_at_check"] == 2
    assert m["items"]["10"]["status"] == "skipped"
    c = app.list_pmv_creators()[0]["counts"]
    assert c["downloaded"] == 1 and c["skipped"] == 1 and c["unreviewed"] == 1
    assert "error" in app.set_pmv_status(cid, key, "20", "bogus")
    assert "error" in app.set_pmv_status(cid, "iwara_nope", "20", "downloaded")


def test_ack_shift(app, monkeypatch):
    _no_network(monkeypatch)
    cid = app.save_pmv_creator({"name": "S", "links": [R34]})["id"]
    key, path = _seed(app, cid, R34, [30, 20, 10])
    app.set_pmv_status(cid, key, "20", "downloaded")
    m = pt.load_manifest(path, "rule34video", "2472537")
    m["items"]["20"]["number"] = 5
    pt.save_manifest(path, m)
    assert app.get_pmv_items(cid)["sites"][0]["items"][0]["shifted"]
    assert app.ack_pmv_shift(cid, key, "20") == {"ok": True, "number": 5}
    assert not any(it["shifted"] for it in app.get_pmv_items(cid)["sites"][0]["items"])


def test_set_pmv_excluded_bulk_is_pawchive_only_and_renumbers(app, monkeypatch):
    _no_network(monkeypatch)
    cid = app.save_pmv_creator({"name": "S", "links": [PAW, R34]})["id"]
    key = pt.link_key(PAW)
    path = pt.manifest_path(app._pmv_root(cid), key)
    m = pt.new_manifest("pawchive", "42")
    pt.merge_chronological(m, [{"id": str(i), "title": f"p{i}", "url": f"u/{i}", "date": f"2026-0{i}-01", "pos": 0}
                               for i in (3, 2, 1)])
    pt.save_manifest(path, m)
    assert app.set_pmv_excluded_bulk(cid, key, ["2", "nope"], True) == {"ok": True, "updated": 1}
    rows = {it["id"]: (it["number"], it["excluded"]) for it in app.get_pmv_items(cid)["sites"][0]["items"]}
    assert rows == {"1": (1, False), "2": (None, True), "3": (2, False)}
    assert app.set_pmv_excluded_bulk(cid, key, ["2"], True)["updated"] == 0        # already excluded
    assert app.set_pmv_excluded_bulk(cid, key, ["2"], False)["updated"] == 1
    rows = {it["id"]: it["number"] for it in app.get_pmv_items(cid)["sites"][0]["items"]}
    assert rows == {"1": 1, "2": 2, "3": 3}
    r34key, _ = _seed(app, cid, R34, [30, 20, 10])
    assert "error" in app.set_pmv_excluded_bulk(cid, r34key, ["10"], True)


def test_renumber_pmv_site(app, monkeypatch):
    _no_network(monkeypatch)
    cid = app.save_pmv_creator({"name": "S", "links": [R34, PAW]})["id"]
    key, path = _seed(app, cid, R34, [30, 20, 10])
    m = pt.load_manifest(path, "rule34video", "2472537")
    for vid, d in (("10", "2026-01-01"), ("20", "2026-02-01"), ("30", "2026-03-01")):
        m["items"][vid]["date"] = d
    m["items"]["20"]["number"], m["items"]["30"]["number"] = 3, 2      # out of order
    pt.save_manifest(path, m)
    res = app.renumber_pmv_site(cid, key)
    assert res == {"ok": True, "changed": 2, "shifted": 0}
    rows = {it["id"]: it["number"] for it in app.get_pmv_items(cid)["sites"][0]["items"]}
    assert rows == {"10": 1, "20": 2, "30": 3}
    assert "error" in app.renumber_pmv_site(cid, "pawchive_patreon_42")      # chronological already
    assert "error" in app.renumber_pmv_site(cid, "nope")
    assert app.get_pmv_items(cid)["iwara_configured"] is False


# ── fetch job ───────────────────────────────────────────────────────

def test_start_pmv_fetch_builds_jobs_and_gates(app, monkeypatch):
    _no_network(monkeypatch)
    a = app.save_pmv_creator({"name": "A", "links": [R34, IWA]})["id"]
    b = app.save_pmv_creator({"name": "B", "links": [PAW]})["id"]
    captured = {}

    class FakeRunner:
        def __init__(self, **kw):
            captured["kw"] = kw
        def run(self, jobs):
            captured["jobs"] = jobs
            kw = captured["kw"]
            kw["on_progress"]({"type": "info", "message": "hi"})
            kw["on_complete"]({"cancelled": False, "total_new": 2, "errors": [],
                               "per_creator": {a: {"new": 2, "gone": 0, "errors": 0},
                                               b: {"new": 0, "gone": 0, "errors": 1}},
                               "iwara_token": "USER-TOK", "needs_cf_auth": False})
        def cancel(self):
            captured["cancelled"] = True
    monkeypatch.setattr(api, "PmvRunner", FakeRunner)
    pushed = []
    app._push_js = lambda fn, d: pushed.append((fn, d))

    res = app.start_pmv_fetch([])                         # [] / None = every creator
    assert res["status"] == "started" and res["jobs"] == 3 and res["creators"] == 2
    app._pmv_thread.join(5)
    assert [j["creator"]["id"] for j in captured["jobs"]] == [a, a, b]   # name order, link order
    assert [j["mode"] for j in captured["jobs"]] == ["latest"] * 3
    st = app.load_state()
    assert st["iwara_token"] == "USER-TOK"
    assert a in st["pmv_last_fetch"] and b not in st["pmv_last_fetch"]   # b errored, nothing new
    assert [p[0] for p in pushed] == ["onPmvProgress", "onPmvComplete"]

    res = app.start_pmv_fetch([b], mode="full", link_key="pawchive_patreon_42")
    assert res["status"] == "started" and res["jobs"] == 1
    app._pmv_thread.join(5)
    assert captured["jobs"][0]["link"]["platform"] == "pawchive" and captured["jobs"][0]["mode"] == "full"

    assert "error" in app.start_pmv_fetch([a], link_key="nope_1")
    assert app.pmv_fetch_status() == {"running": False}


def test_start_pmv_fetch_refuses_while_running(app, monkeypatch):
    _no_network(monkeypatch)
    app.save_pmv_creator({"name": "A", "links": [R34]})
    gate = threading.Event()

    class SlowRunner:
        def __init__(self, **kw): self.kw = kw
        def run(self, jobs): gate.wait(5); self.kw["on_complete"]({"per_creator": {}, "errors": []})
        def cancel(self): gate.set()
    monkeypatch.setattr(api, "PmvRunner", SlowRunner)
    app._push_js = lambda fn, d: None
    assert app.start_pmv_fetch()["status"] == "started"
    assert app.pmv_fetch_status()["running"]
    assert app.start_pmv_fetch()["error"] == "A PMV fetch is already running"
    assert app.cancel_pmv_fetch()["status"] == "cancelling"
    app._pmv_thread.join(5)
    assert not app.pmv_fetch_status()["running"]


def test_start_pmv_fetch_with_no_creators(app, monkeypatch):
    _no_network(monkeypatch)
    assert "error" in app.start_pmv_fetch()


# ── persistence ─────────────────────────────────────────────────────

def _snapshots(d):
    return sorted(n for n in os.listdir(d) if n.startswith("app_state-")) if os.path.isdir(d) else []


def test_pmv_creators_roll_a_snapshot_but_window_saves_do_not(app, monkeypatch, tmp_path):
    _no_network(monkeypatch)
    backups = str(tmp_path / "app_state.backups")
    app.save_state({"creators": {"c1": {"name": "x", "links": [{"url": "u"}]}}})
    n0 = len(_snapshots(backups))
    app.save_state({"window": {"width": 1}})
    assert len(_snapshots(backups)) == n0
    app.save_pmv_creator({"name": "A", "links": [R34]})
    assert len(_snapshots(backups)) == n0 + 1
    app.save_state({"pmv_last_fetch": {"pc_x": "now"}})
    assert len(_snapshots(backups)) == n0 + 1


def test_nas_mirror_carries_pmv_creators_and_no_iwara_password(app, monkeypatch, tmp_path):
    _no_network(monkeypatch)
    arch = tmp_path / "arch"; arch.mkdir()
    app.save_state({"archive_dir": str(arch), "iwara_email": "me@x", "iwara_password": "SECRET-PW",
                    "iwara_token": "SECRET-JWT",
                    "creators": {"c1": {"name": "x", "links": [{"url": "u"}]}}})
    app.save_pmv_creator({"name": "A", "links": [R34]})
    nas = str(arch / api.NAS_BACKUP_DIRNAME)
    end = time.time() + 5
    while time.time() < end and len(_snapshots(nas)) < 2:
        time.sleep(0.02)
    raw = open(os.path.join(nas, _snapshots(nas)[-1]), encoding="utf-8").read()
    data = json.loads(raw)
    assert list(data["pmv_creators"].values())[0]["name"] == "A"
    assert "SECRET-PW" not in raw and "SECRET-JWT" not in raw and "me@x" not in raw


def test_recover_from_backup_restores_pmv_creators(app, monkeypatch, tmp_path):
    _no_network(monkeypatch)
    app.save_state({"creators": {"c1": {"name": "x", "links": [{"url": "u"}]}}})
    cid = app.save_pmv_creator({"name": "A", "links": [R34]})["id"]
    app.save_state({"pmv_creators": {}})
    assert app.list_pmv_creators() == []
    res = app.recover_from_backup()
    assert res["pmv_added"] == 1
    assert app.get_pmv_creator(cid)["name"] == "A"


def test_default_state_has_pmv_keys(app):
    d = app._default_state()
    for k in ("pmv_creators", "pmv_last_fetch", "last_pmv_creator", "pmv_sort",
              "iwara_email", "iwara_password", "iwara_token"):
        assert k in d


# ── iwara settings ──────────────────────────────────────────────────

def test_iwara_login_status_and_test_login(app, monkeypatch):
    _no_network(monkeypatch)
    assert app.iwara_login_status() == {"configured": False, "token_valid": False, "expires": ""}
    assert app.iwara_test_login()["ok"] is False

    class FakeAuth:
        def __init__(self, session, email, password, user_token=None, **kw):
            self.user_token, self.user_token_changed, self.message = "NEW-TOK", True, ""
            self.ok = (email, password) == ("me@x", "pw")
        def access_token(self, force_refresh=False):
            if not self.ok:
                self.message = "login failed: invalid email or password"
            return "ACC" if self.ok else None
    monkeypatch.setattr(api.iw, "IwaraAuth", FakeAuth)
    monkeypatch.setattr(api.iw, "make_session", lambda: object())
    app.save_state({"iwara_email": "me@x", "iwara_password": "pw"})
    assert app.iwara_test_login() == {"ok": True, "message": "Logged in"}
    assert app.load_state()["iwara_token"] == "NEW-TOK"
    app.save_state({"iwara_password": "wrong"})
    assert "invalid" in app.iwara_test_login()["message"]


def test_resolve_pmv_link_offline_paths(app, monkeypatch):
    # Network lookups are best-effort: with the session factories failing, the
    # URL parsers alone must still classify links.
    _no_network(monkeypatch)
    r = app.resolve_pmv_link("https://rule34video.com/members/2472537/")
    assert r["valid"] and r["platform"] == "rule34video" and r["user_id"] == "2472537" and r["site_code"] == "R34"
    i = app.resolve_pmv_link("https://www.iwara.tv/profile/user1833289/videos")
    assert i["valid"] and i["username"] == "user1833289" and i["user_id"] == "" and i["note"]
    p = app.resolve_pmv_link("https://pawchive.pw/patreon/user/147694273/post/133542612")
    assert p == {"valid": False}                    # post URL, not a creator
    p = app.resolve_pmv_link("https://pawchive.pw/patreon/user/147694273")
    assert p["valid"] and p["service"] == "patreon" and p["user_id"] == "147694273"
    assert app.resolve_pmv_link("https://youtube.com/@x") == {"valid": False}
    assert app.resolve_pmv_link("https://rule34video.com/tags/pmv/") == {"valid": False}
