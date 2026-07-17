"""State persistence must be crash- and race-proof: a transient/unreadable
read of app_state.json must NEVER wipe the creator index.

Regression guard for the incident where a concurrent atomic replace made one
read fail, the old load_state renamed the (good) file to .corrupt and returned
an empty default, and later saves persisted that empty state — silently
destroying all 78 creators. The fix: retry reads, quarantine only genuinely
unparsable files (under a unique name), self-heal from a rolling backup, and
snapshot the creator index on every change.

    python -m pytest tests/test_state_backup.py -q
"""
import os
import sys
import json
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.api as api


CREATOR = {
    "name": "Foo",
    "destination": "P:/X/Foo",
    "links": [{"platform": "pawchive", "url": "https://pawchive.pw/x"}],
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    """An Api with state + backups redirected to a throwaway sandbox."""
    monkeypatch.setattr(api, "STATE_FILE", str(tmp_path / "app_state.json"))
    monkeypatch.setattr(api, "STATE_BACKUP_DIR", str(tmp_path / "app_state.backups"))
    a = api.Api.__new__(api.Api)          # skip __init__ (no MediaServer/window)
    a._state_lock = threading.Lock()
    return a


def _creators(a):
    return a.load_state().get("creators", {})


def _backups():
    d = api.STATE_BACKUP_DIR
    if not os.path.isdir(d):
        return []
    return [n for n in os.listdir(d) if n.startswith("app_state-") and n.endswith(".json")]


def test_save_writes_rolling_backup(app):
    app.save_state({"creators": {"p:/x/foo": CREATOR}})
    assert len(_creators(app)) == 1
    assert len(_backups()) >= 1


def test_window_only_save_does_not_churn_backups(app):
    app.save_state({"creators": {"p:/x/foo": CREATOR}})
    before = len(_backups())
    app.save_state({"window": {"w": 100}})       # creators unchanged
    assert len(_backups()) == before


def test_corrupt_live_file_self_heals_from_backup(app):
    """The core regression: an unreadable live file must recover the creator
    index from backup, not reset it to empty."""
    app.save_state({"creators": {"p:/x/foo": CREATOR}})
    with open(api.STATE_FILE, "w", encoding="utf-8") as f:
        f.write("\x00\x00 not json {{{")
    healed = app.load_state()
    assert len(healed.get("creators", {})) == 1        # NOT 0
    # the damaged file is quarantined under a unique name, not silently dropped
    quarantined = [n for n in os.listdir(os.path.dirname(api.STATE_FILE))
                   if ".corrupt" in n]
    assert quarantined


def test_blank_live_file_self_heals(app):
    """A zero-byte / partial-write read (the race look-alike) must also heal."""
    app.save_state({"creators": {"p:/x/foo": CREATOR}})
    with open(api.STATE_FILE, "w", encoding="utf-8") as f:
        f.write("")
    assert len(app.load_state().get("creators", {})) == 1


def test_quarantine_never_clobbers_a_previous_copy(app):
    """Two corruption events must not overwrite each other's forensic copy."""
    app.save_state({"creators": {"p:/x/foo": CREATOR}})
    for _ in range(2):
        with open(api.STATE_FILE, "w", encoding="utf-8") as f:
            f.write("garbage {{{")
        app.load_state()
    quarantined = [n for n in os.listdir(os.path.dirname(api.STATE_FILE))
                   if ".corrupt" in n]
    assert len(quarantined) >= 2


def test_recover_from_backup_restores_modern_records(app):
    """recover_from_backup must restore whole modern creator records (not just
    legacy flat maps) after the live index is wiped to empty."""
    app.save_state({"creators": {"p:/x/foo": CREATOR}})
    app.save_state({"creators": {}})                   # simulate the wipe
    assert len(_creators(app)) == 0
    res = app.recover_from_backup()
    assert res["creators_added"] >= 1
    assert len(_creators(app)) >= 1


def test_never_backs_up_empty_creator_index(app):
    """An empty creator index is the failure state — it must never become a
    snapshot that could later be 'restored' over good data."""
    app.save_state({"creators": {}})
    assert _backups() == []
