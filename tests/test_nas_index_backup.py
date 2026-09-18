"""Off-machine mirror of the creator index.

app_state.json lived only on the app's own drive. When that drive died the
entire creator index went with it while every archive DB survived on the NAS —
so the index is now mirrored into the archive dir beside them, and recovery
reads from there when this machine has no backups of its own (fresh install).

Credentials are deliberately excluded: the archive dir is a shared network share.

    python -m pytest tests/test_nas_index_backup.py -q
"""
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.api as api


def _app(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "STATE_FILE", str(tmp_path / "app_state.json"))
    monkeypatch.setattr(api, "STATE_BACKUP_DIR", str(tmp_path / "app_state.backups"))
    monkeypatch.setattr(api, "APP_DIR", str(tmp_path))
    a = api.Api.__new__(api.Api)
    a._state_lock = threading.Lock()
    return a


def _snapshots(nas_dir):
    if not os.path.isdir(nas_dir):
        return []
    return sorted(n for n in os.listdir(nas_dir)
                  if n.startswith("app_state-") and n.endswith(".json"))


def _wait_for_snapshot(nas_dir, timeout=5.0, want=1):
    """The mirror runs on a daemon thread so a slow NAS can't stall a save."""
    end = time.time() + timeout
    while time.time() < end:
        if len(_snapshots(nas_dir)) >= want:
            return _snapshots(nas_dir)
        time.sleep(0.02)
    return _snapshots(nas_dir)


CREATOR = {"c1": {"name": "Someone", "destination": "P:/Real/Furry/Someone",
                  "links": [{"platform": "pawchive", "service": "patreon",
                             "user_id": "42", "url": "https://pawchive.st/patreon/user/42"}]}}


def test_index_is_mirrored_to_the_archive_dir(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    arch = tmp_path / "arch"; arch.mkdir()
    app.save_state({"archive_dir": str(arch), "creators": CREATOR})
    nas = str(arch / api.NAS_BACKUP_DIRNAME)
    snaps = _wait_for_snapshot(nas)
    assert len(snaps) == 1
    data = json.load(open(os.path.join(nas, snaps[0]), encoding="utf-8"))
    assert data["creators"]["c1"]["links"][0]["user_id"] == "42"
    assert data["saved_at"]


def test_mirror_carries_no_credentials(tmp_path, monkeypatch):
    # The archive dir is a shared network share — tokens must not land there.
    app = _app(tmp_path, monkeypatch)
    arch = tmp_path / "arch"; arch.mkdir()
    app.save_state({"archive_dir": str(arch), "creators": CREATOR,
                    "discord_token": "SECRET-TOKEN",
                    "derpibooru_api_key": "SECRET-KEY",
                    "pawchive_cookies_path": "C:/secret/cookies.txt"})
    nas = str(arch / api.NAS_BACKUP_DIRNAME)
    snaps = _wait_for_snapshot(nas)
    raw = open(os.path.join(nas, snaps[0]), encoding="utf-8").read()
    assert "SECRET-TOKEN" not in raw
    assert "SECRET-KEY" not in raw
    assert "cookies.txt" not in raw
    assert set(json.loads(raw)) == {"creators", "archive_dir", "library_root", "saved_at"}


def test_empty_index_is_never_mirrored(tmp_path, monkeypatch):
    # An empty creator index is the failure state; never propagate it off-machine.
    app = _app(tmp_path, monkeypatch)
    arch = tmp_path / "arch"; arch.mkdir()
    app.save_state({"archive_dir": str(arch), "creators": {}})
    time.sleep(0.3)
    assert _snapshots(str(arch / api.NAS_BACKUP_DIRNAME)) == []


def test_no_archive_dir_is_a_no_op(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    app.save_state({"archive_dir": "", "creators": CREATOR})
    time.sleep(0.2)          # nothing to assert but "did not raise"
    assert app.load_state()["creators"] == CREATOR


def test_unwritable_nas_never_breaks_the_save(tmp_path, monkeypatch):
    # NAS offline: the local save and local backup must still succeed.
    app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(api.os, "makedirs", _boom_for(str(tmp_path / "gone")))
    app.save_state({"archive_dir": str(tmp_path / "gone"), "creators": CREATOR})
    time.sleep(0.3)
    assert app.load_state()["creators"]["c1"]["name"] == "Someone"


def _boom_for(prefix):
    real = os.makedirs

    def fake(path, *a, **k):
        if str(path).startswith(prefix):
            raise OSError("NAS offline")
        return real(path, *a, **k)
    return fake


def test_snapshots_are_pruned(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    arch = tmp_path / "arch"; arch.mkdir()
    nas = str(arch / api.NAS_BACKUP_DIRNAME)
    monkeypatch.setattr(api, "NAS_BACKUP_KEEP", 3)
    stamps = iter([f"2026-09-18_00-00-{i:02d}" for i in range(8)])
    monkeypatch.setattr(app, "_backup_stamp", lambda: next(stamps))
    for i in range(8):
        c = {f"c{i}": dict(CREATOR["c1"])}
        app.save_state({"archive_dir": str(arch), "creators": c})
        _wait_for_snapshot(nas, want=min(i + 1, 3))
    time.sleep(0.3)
    assert len(_snapshots(nas)) <= 3


def test_recovers_the_index_from_the_nas_alone(tmp_path, monkeypatch):
    # The real scenario: fresh install, no local backups, index gone — but the
    # NAS archive dir is still there next to the archive DBs.
    app = _app(tmp_path, monkeypatch)
    arch = tmp_path / "arch"; arch.mkdir()
    app.save_state({"archive_dir": str(arch), "creators": CREATOR})
    _wait_for_snapshot(str(arch / api.NAS_BACKUP_DIRNAME))

    # wipe this machine: live index emptied, local backups gone
    import shutil
    shutil.rmtree(str(tmp_path / "app_state.backups"), ignore_errors=True)
    app.save_state({"creators": {}})
    assert app.load_state()["creators"] == {}

    res = app.recover_from_backup()
    assert res["creators_total"] == 1
    got = app.load_state()["creators"]["c1"]
    assert got["links"][0]["user_id"] == "42"
