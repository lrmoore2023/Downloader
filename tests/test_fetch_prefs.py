"""Per-creator "what to fetch" prefs (Images / Videos / External links) and
track-only (folder-less) creators.

Covers: the prefs helpers' legacy defaults, the manifest 'skipped' status for
not-auto-grabbed direct links, the pawchive crawl's kind filtering + seen-marker
rows, coomerfans kind filtering, and the Api layer (save_creator with a blank
destination, tc_ ids, the tracked manifest root, and pending-links reachability
without a destination).

    python -m pytest tests/test_fetch_prefs.py -q
"""
import os
import sys
import threading
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.api as api
import backend.pawchive_runner as pr
from backend.creator_runner import fetch_prefs, is_track_only
from backend.coomerfans_runner import CoomerfansRunner
from backend.pawchive_archive import Archive
from backend.pawchive_links import PawchiveLinks


# ── fetch_prefs / is_track_only ─────────────────────────────────────

def test_fetch_prefs_defaults():
    # Legacy records (no `fetch` key) and malformed values fetch everything.
    assert fetch_prefs({}) == {"images": True, "videos": True, "links": True}
    assert fetch_prefs({"fetch": None}) == {"images": True, "videos": True, "links": True}
    assert fetch_prefs({"fetch": "garbage"}) == {"images": True, "videos": True, "links": True}
    assert fetch_prefs(None) == {"images": True, "videos": True, "links": True}


def test_fetch_prefs_partial():
    p = fetch_prefs({"fetch": {"videos": False}})
    assert p == {"images": True, "videos": False, "links": True}


def test_is_track_only():
    assert not is_track_only({})
    assert not is_track_only({"fetch": {"images": False}})
    assert is_track_only({"fetch": {"images": False, "videos": False}})
    assert is_track_only({"fetch": {"images": False, "videos": False, "links": True}})


# ── PawchiveLinks: 'skipped' direct links ───────────────────────────

def _post(pid="1", links=(), title="T"):
    return {
        "post_id": pid, "title": title, "url": f"https://pawchive.st/p/{pid}",
        "service": "patreon", "dt": datetime(2026, 5, 1, 12, 0),
        "content_html": "", "external_links": list(links),
    }


DIRECT = {"url": "https://files.catbox.moe/a.mp4", "label": "", "host": "files.catbox.moe", "kind": "direct"}
MANUAL = {"url": "https://mega.nz/folder/x", "label": "", "host": "mega.nz", "kind": "manual"}


def test_skipped_direct_is_outstanding(tmp_path):
    m = PawchiveLinks(str(tmp_path / "_pawchive_links.json"))
    m.upsert_post(_post(links=[DIRECT, MANUAL]),
                  {DIRECT["url"]: {"status": "skipped"}})
    pend = m.pending()
    assert {i["url"] for i in pend} == {DIRECT["url"], MANUAL["url"]}
    skipped = next(i for i in pend if i["url"] == DIRECT["url"])
    assert skipped["status"] == "skipped"
    assert m.counts()["outstanding"] == 2


def test_skipped_never_downgrades_grabbed(tmp_path):
    m = PawchiveLinks(str(tmp_path / "_pawchive_links.json"))
    m.upsert_post(_post(links=[DIRECT]), {DIRECT["url"]: {"status": "grabbed", "filename": "a.mp4"}})
    # A later track-only re-scan of the same post must not resurrect it.
    m.upsert_post(_post(links=[DIRECT]), {DIRECT["url"]: {"status": "skipped"}})
    link = m.data["posts"]["1"]["links"][DIRECT["url"]]
    assert link["status"] == "grabbed"
    assert link["filename"] == "a.mp4"
    assert m.pending() == []


def test_resolved_hides_skipped(tmp_path):
    m = PawchiveLinks(str(tmp_path / "_pawchive_links.json"))
    m.upsert_post(_post(links=[DIRECT]), {DIRECT["url"]: {"status": "skipped"}})
    key = m.pending()[0]["key"]
    assert m.mark_resolved(key, True)
    assert m.pending() == []
    # Re-scan keeps it hidden (resolved survives upsert).
    m.upsert_post(_post(links=[DIRECT]), {DIRECT["url"]: {"status": "skipped"}})
    assert m.pending() == []


# ── Pawchive crawl: kind filtering + markers ────────────────────────

def _media(kind, name):
    return {"kind": kind, "url": f"https://c1.pawchive.st/{name}", "name": name}


def _parsed(pid, media, links=(), preview_state="scraped", deferred=()):
    return {
        "post_id": pid, "dt": datetime(2026, 5, 1, 12, 0), "detail_fetched": True,
        "media": list(media), "external_links": list(links),
        "preview_state": preview_state, "deferred_media": list(deferred),
        "title": f"Post {pid}", "url": f"https://pawchive.st/p/{pid}",
        "service": "patreon", "content_html": "",
    }


def _make_runner(tmp_path, monkeypatch, posts, mode="full",
                 include_images=True, include_videos=True, links=True):
    """A PawchiveRunner wired up just enough to call _crawl offline."""
    monkeypatch.setattr(pr, "iter_posts", lambda *a, **k: iter(posts))
    monkeypatch.setattr(pr, "parse_post", lambda raw: raw)
    r = pr.PawchiveRunner(workers=1)
    r._include_images = include_images
    r._include_videos = include_videos
    r._grab_direct = include_images or include_videos
    r._on_progress = lambda d: None
    r._mode = mode
    r._session = None
    r._service, r._user_id = "patreon", "42"
    r._destination = str(tmp_path)
    r._write_root = str(tmp_path)
    r._archive = Archive(str(tmp_path / "pawchive_patreon_42.db"))
    r._errors = None
    r._links = PawchiveLinks(str(tmp_path / "_pawchive_links.json")) if links else None
    if r._links:
        r._links.autosave = False
    r._crawled_post_ids = set()
    r._known_failure_entries = set()
    r._dismissed_ids = set()
    return r


def test_crawl_images_only_excludes_videos_and_archives(tmp_path, monkeypatch):
    posts = [_parsed("10", [_media("image", "a.jpg"), _media("video", "b.mp4"),
                            _media("archive", "c.zip"), _media("image", "d.jpg")])]
    r = _make_runner(tmp_path, monkeypatch, posts, include_videos=False)
    media_jobs, ext_jobs, _ = r._crawl()
    kinds = [(j["media_kind"], j["index"]) for j in media_jobs]
    assert kinds == [("image", 1), ("image", 4)]        # entry index = all-media position
    ordinals = [j["ordinal"] for j in media_jobs]
    assert ordinals == [1, 2]                           # ordinals dense over included images
    # Post still has downloadable content → NOT marker-tracked.
    assert not r._archive.post_seen("10") if hasattr(r._archive, "post_seen") else True
    r._archive.close()


def test_crawl_track_only_builds_no_jobs_and_marks_seen(tmp_path, monkeypatch):
    posts = [
        _parsed("20", [_media("image", "a.jpg")], links=[DIRECT, MANUAL]),
        _parsed("21", [], links=[MANUAL]),                       # text/link-only post
        _parsed("22", [_media("video", "v.mp4")], preview_state="pending"),
    ]
    r = _make_runner(tmp_path, monkeypatch, posts,
                     include_images=False, include_videos=False)
    media_jobs, ext_jobs, _ = r._crawl()
    assert media_jobs == [] and ext_jobs == []
    # Direct link recorded as 'skipped' (surfaced for manual grab), manual pending.
    pend = r._links.pending()
    st = {i["url"]: i["status"] for i in pend}
    assert st[DIRECT["url"]] == "skipped"
    assert st[MANUAL["url"]] == "pending"
    # Post 20 (media fully excluded) marked seen; 21 (no media) and 22 (pending) not.
    assert r._archive.post_seen("20")
    assert not r._archive.post_seen("21")
    assert not r._archive.post_seen("22")
    r._archive.close()


def test_crawl_cancelled_records_no_markers(tmp_path, monkeypatch):
    posts = [_parsed("30", [_media("image", "a.jpg")])]
    r = _make_runner(tmp_path, monkeypatch, posts,
                     include_images=False, include_videos=False)
    r._cancel_event.set()
    r._crawl()
    assert not r._archive.post_seen("30")
    r._archive.close()


def test_crawl_latest_skips_marker_seen_posts(tmp_path, monkeypatch):
    posts = [_parsed("40", [_media("image", "a.jpg")], links=[MANUAL])]
    r = _make_runner(tmp_path, monkeypatch, posts,
                     include_images=False, include_videos=False)
    r._crawl()
    assert r._archive.post_seen("40")
    r._archive.close()
    # Second, 'latest' run over the same listing: the post is skipped outright.
    r2 = _make_runner(tmp_path, monkeypatch, posts, mode="latest",
                      include_images=False, include_videos=False)
    media_jobs, ext_jobs, crawled = r2._crawl()
    assert crawled == {}                       # not re-processed
    r2._archive.close()


# ── Coomerfans: kind switch ─────────────────────────────────────────

def test_coomerfans_kind_included():
    r = CoomerfansRunner(workers=1)
    r._include_images, r._include_videos = True, False
    assert r._kind_included("image")
    assert not r._kind_included("video")
    r._include_images, r._include_videos = False, True
    assert not r._kind_included("image")
    assert r._kind_included("video")


# ── Api layer ───────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path, monkeypatch):
    """An Api with state + backups redirected to a throwaway sandbox."""
    monkeypatch.setattr(api, "STATE_FILE", str(tmp_path / "app_state.json"))
    monkeypatch.setattr(api, "STATE_BACKUP_DIR", str(tmp_path / "app_state.backups"))
    monkeypatch.setattr(api, "APP_DIR", str(tmp_path))
    a = api.Api.__new__(api.Api)          # skip __init__ (no MediaServer/window)
    a._state_lock = threading.Lock()
    return a


PW_URL = "https://pawchive.st/patreon/user/42"


def test_save_creator_blank_dest_requires_track_only(app):
    res = app.save_creator({"name": "X", "destination": "",
                            "fetch": {"images": True, "videos": False, "links": True},
                            "links": [{"url": PW_URL}]})
    assert "error" in res
    res = app.save_creator({"name": "", "destination": "",
                            "fetch": {"images": False, "videos": False, "links": True},
                            "links": [{"url": PW_URL}]})
    assert "error" in res                  # folder-less creators need a name


def test_save_creator_track_only(app, tmp_path):
    res = app.save_creator({"name": "Tracky", "destination": "",
                            "fetch": {"images": False, "videos": False, "links": True},
                            "links": [{"url": PW_URL}]})
    assert res.get("id", "").startswith("tc_")
    cid = res["id"]
    c = app.load_state()["creators"][cid]
    assert c["destination"] == ""
    assert c["category"] == "Tracked"
    assert c["fetch"] == {"images": False, "videos": False, "links": True}
    # get_creator surfaces the prefs; list_creators buckets it under Tracked.
    assert app.get_creator(cid)["fetch"]["images"] is False
    listed = next(x for x in app.list_creators() if x["id"] == cid)
    assert listed["category"] == "Tracked"
    # Editing keeps the same tc_ id.
    res2 = app.save_creator({"id": cid, "name": "Tracky2", "destination": "",
                             "fetch": {"images": False, "videos": False, "links": True},
                             "links": [{"url": PW_URL}]})
    assert res2["id"] == cid


def test_save_creator_legacy_untouched(app, tmp_path):
    dest = str(tmp_path / "lib" / "Foo")
    res = app.save_creator({"name": "Foo", "destination": dest,
                            "links": [{"url": PW_URL}]})
    c = app.load_state()["creators"][res["id"]]
    assert c["fetch"] == {"images": True, "videos": True, "links": True}
    assert os.path.isdir(dest)


def test_manifest_root_and_migration(app, tmp_path):
    arch = tmp_path / "archives"
    arch.mkdir()
    app.save_state({"creators": {}, "archive_dir": str(arch)})
    res = app.save_creator({"name": "Tracky", "destination": "",
                            "fetch": {"images": False, "videos": False, "links": True},
                            "links": [{"url": PW_URL}]})
    cid = res["id"]
    c = app.load_state()["creators"][cid]
    root = app._manifest_root(cid, c)
    assert root == os.path.join(str(arch), "tracked", cid)
    # Simulate a completed track run, then give the creator a folder: the
    # manifest must move into it (checkmarks survive).
    os.makedirs(root, exist_ok=True)
    mpath = os.path.join(root, "_pawchive_links.json")
    with open(mpath, "w", encoding="utf-8") as f:
        f.write('{"version": 1, "posts": {}}')
    dest = str(tmp_path / "lib" / "Tracky")
    res2 = app.save_creator({"id": cid, "name": "Tracky", "destination": dest,
                             "fetch": {"images": True, "videos": True, "links": True},
                             "links": [{"url": PW_URL}]})
    assert not res2.get("error")
    assert os.path.isfile(os.path.join(dest, "_pawchive_links.json"))
    assert not os.path.exists(mpath)
    # ...and back: clearing the folder moves it into the new tracked home.
    res3 = app.save_creator({"id": res2["id"], "name": "Tracky", "destination": "",
                             "fetch": {"images": False, "videos": False, "links": True},
                             "links": [{"url": PW_URL}]})
    new_root = app._manifest_root(res3["id"], app.load_state()["creators"][res3["id"]])
    assert os.path.isfile(os.path.join(new_root, "_pawchive_links.json"))


def test_list_pending_links_reachable_without_dest(app, tmp_path):
    arch = tmp_path / "archives"
    arch.mkdir()
    app.save_state({"creators": {}, "archive_dir": str(arch)})
    res = app.save_creator({"name": "Tracky", "destination": "",
                            "fetch": {"images": False, "videos": False, "links": True},
                            "links": [{"url": PW_URL}]})
    cid = res["id"]
    # No manifest yet: reachable, just empty.
    out = app.list_pending_links(cid)
    assert out["reachable"] is True and out["items"] == []
    # With a manifest holding a skipped direct + manual link: both listed.
    root = app._manifest_root(cid, app.load_state()["creators"][cid])
    os.makedirs(root, exist_ok=True)
    m = PawchiveLinks(os.path.join(root, "_pawchive_links.json"))
    m.upsert_post(_post(links=[DIRECT, MANUAL]), {DIRECT["url"]: {"status": "skipped"}})
    out = app.list_pending_links(cid)
    assert out["reachable"] is True
    assert {i["url"] for i in out["items"]} == {DIRECT["url"], MANUAL["url"]}
    # Check-off works dest-less too.
    key = out["items"][0]["key"]
    r = app.set_link_resolved(cid, key, True)
    assert r.get("ok") is True


def test_track_only_scope_guard(app):
    res = app.save_creator({"name": "CfOnly", "destination": "",
                            "fetch": {"images": False, "videos": False, "links": True},
                            "links": [{"url": "https://coomerfans.com/u/onlyfans/1/x"}]})
    cid = res["id"]
    app._creator_runner = None
    app._creator_aux_thread = None
    out = app.start_creator_download(cid, scope="everything", mode="full")
    assert "error" in out and "links-only" in out["error"]
