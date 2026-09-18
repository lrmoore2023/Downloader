"""Flagging tracked posts that hold a video or an archive.

A links-only (track-only) creator downloads nothing, so the useful signal is
"which posts still have a file in them" — surfaced in the URL panel as a badge
next to the post page link. Covers: kind recording (including not-yet-imported
'deferred' attachments and archive extensions), the flagged() query, and the
fact that a post with a video but NO external links still reaches the UI.

    python -m pytest tests/test_track_flags.py -q
"""
import os
import sys
import threading
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.api as api
from backend.pawchive_links import PawchiveLinks, post_media_kinds, FLAG_KINDS

DIRECT = {"url": "https://files.catbox.moe/a.mp4", "label": "", "host": "files.catbox.moe", "kind": "direct"}
MANUAL = {"url": "https://mega.nz/folder/x", "label": "", "host": "mega.nz", "kind": "manual"}


def _post(pid="1", links=(), media=(), deferred=(), title="T", day=1):
    return {
        "post_id": pid, "title": title, "url": f"https://pawchive.st/p/{pid}",
        "service": "patreon", "dt": datetime(2026, 5, day, 12, 0),
        "content_html": "", "external_links": list(links),
        "media": [{"kind": k, "url": f"https://f/{i}", "name": f"{i}"} for i, k in enumerate(media)],
        "deferred_media": [{"kind": k, "name": f"d{i}"} for i, k in enumerate(deferred)],
    }


# ── kind recording ──────────────────────────────────────────────────

def test_flag_kinds_are_video_and_archive():
    assert set(FLAG_KINDS) == {"video", "archive"}


def test_post_media_kinds_dedupes_and_sorts():
    assert post_media_kinds(_post(media=("image", "video", "image"))) == ["image", "video"]


def test_post_media_kinds_includes_deferred():
    # A deferred attachment has no bytes yet, but its extension is already known
    # — it still means "this post has a video in it".
    assert post_media_kinds(_post(media=(), deferred=("video",))) == ["video"]


def test_post_media_kinds_empty_for_no_media():
    assert post_media_kinds(_post()) == []


def test_upsert_records_kinds(tmp_path):
    m = PawchiveLinks(str(tmp_path / "_pawchive_links.json"))
    m.upsert_post(_post(media=("image", "archive")))
    assert m.data["posts"]["1"]["media_kinds"] == ["archive", "image"]


def test_empty_read_never_erases_known_kinds(tmp_path):
    # A post pawchive hasn't imported yet reports no media. Re-upserting it must
    # not clear a kind an earlier run already established.
    m = PawchiveLinks(str(tmp_path / "_pawchive_links.json"))
    m.upsert_post(_post(media=("video",)))
    m.upsert_post(_post(media=()))
    assert m.data["posts"]["1"]["media_kinds"] == ["video"]


# ── flagged() ───────────────────────────────────────────────────────

def test_flagged_reports_video_post(tmp_path):
    m = PawchiveLinks(str(tmp_path / "_pawchive_links.json"))
    m.upsert_post(_post(media=("video",)))
    f = m.flagged()
    assert len(f) == 1
    assert f[0]["media_kinds"] == ["video"]
    assert f[0]["post_url"] == "https://pawchive.st/p/1"


def test_flagged_reports_archive_post(tmp_path):
    m = PawchiveLinks(str(tmp_path / "_pawchive_links.json"))
    m.upsert_post(_post(media=("archive",)))
    assert m.flagged()[0]["media_kinds"] == ["archive"]


def test_images_only_post_is_not_flagged(tmp_path):
    m = PawchiveLinks(str(tmp_path / "_pawchive_links.json"))
    m.upsert_post(_post(media=("image", "image")))
    assert m.flagged() == []


def test_flagged_drops_non_flag_kinds(tmp_path):
    # An image alongside a video must not show up as its own badge.
    m = PawchiveLinks(str(tmp_path / "_pawchive_links.json"))
    m.upsert_post(_post(media=("image", "video")))
    assert m.flagged()[0]["media_kinds"] == ["video"]


def test_video_post_without_links_is_invisible_to_pending_but_flagged(tmp_path):
    # The whole point of flagged(): pending() iterates LINKS, so a post whose
    # only content is a video would otherwise never reach the panel.
    m = PawchiveLinks(str(tmp_path / "_pawchive_links.json"))
    m.upsert_post(_post(media=("video",), links=()))
    assert m.pending() == []
    f = m.flagged()
    assert len(f) == 1 and f[0]["outstanding"] is False


def test_flagged_marks_posts_that_already_have_rows(tmp_path):
    # This post already renders via pending(), so the panel badges that existing
    # group instead of adding a second one.
    m = PawchiveLinks(str(tmp_path / "_pawchive_links.json"))
    m.upsert_post(_post(media=("video",), links=[MANUAL]))
    assert m.flagged()[0]["outstanding"] is True


def test_resolved_links_leave_post_flagged_but_not_outstanding(tmp_path):
    m = PawchiveLinks(str(tmp_path / "_pawchive_links.json"))
    m.upsert_post(_post(media=("video",), links=[MANUAL]))
    m.mark_resolved(next(iter(m.pending()))["key"])
    f = m.flagged()
    assert len(f) == 1 and f[0]["outstanding"] is False


def test_pending_rows_carry_media_kinds(tmp_path):
    m = PawchiveLinks(str(tmp_path / "_pawchive_links.json"))
    m.upsert_post(_post(media=("video",), links=[MANUAL]))
    assert m.pending()[0]["media_kinds"] == ["video"]


def test_flagged_is_newest_first(tmp_path):
    m = PawchiveLinks(str(tmp_path / "_pawchive_links.json"))
    m.upsert_post(_post(pid="old", media=("video",), day=1))
    m.upsert_post(_post(pid="new", media=("archive",), day=9))
    assert [f["post_id"] for f in m.flagged()] == ["new", "old"]


def test_kinds_survive_a_reload(tmp_path):
    p = str(tmp_path / "_pawchive_links.json")
    PawchiveLinks(p).upsert_post(_post(media=("video",)))
    assert PawchiveLinks(p).flagged()[0]["media_kinds"] == ["video"]


# ── Api pass-through ────────────────────────────────────────────────

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


def _tracked_creator(app, tmp_path):
    arch = tmp_path / "archives"
    arch.mkdir(exist_ok=True)
    app.save_state({"creators": {}, "archive_dir": str(arch)})
    cid = app.save_creator({"name": "Tracky", "destination": "",
                            "fetch": {"images": False, "videos": False, "links": True},
                            "links": [{"url": PW_URL}]})["id"]
    root = app._manifest_root(cid, app.load_state()["creators"][cid])
    os.makedirs(root, exist_ok=True)
    return cid, PawchiveLinks(os.path.join(root, "_pawchive_links.json"))


def test_api_surfaces_linkless_video_post(app, tmp_path):
    # End-to-end for the case the panel could not previously show at all.
    cid, m = _tracked_creator(app, tmp_path)
    m.upsert_post(_post(media=("video",), links=()))
    out = app.list_pending_links(cid)
    assert out["reachable"] is True
    assert out["items"] == []                       # no links to check off
    assert len(out["linkless"]) == 1                # ...but the post still arrives
    assert out["linkless"][0]["media_kinds"] == ["video"]
    assert out["linkless"][0]["post_url"] == "https://pawchive.st/p/1"
    assert out["counts"]["flagged"] == 1


def test_api_badges_a_post_that_already_has_links(app, tmp_path):
    cid, m = _tracked_creator(app, tmp_path)
    m.upsert_post(_post(media=("archive",), links=[MANUAL]))
    out = app.list_pending_links(cid)
    assert out["items"][0]["media_kinds"] == ["archive"]
    assert out["linkless"] == []                    # badged in place, not duplicated
    assert out["counts"]["flagged"] == 1


def test_api_ignores_image_only_posts(app, tmp_path):
    cid, m = _tracked_creator(app, tmp_path)
    m.upsert_post(_post(media=("image",), links=[MANUAL]))
    out = app.list_pending_links(cid)
    assert out["linkless"] == [] and out["counts"]["flagged"] == 0
