"""Rebuilding the creator index from archive DBs — pawchive correlation.

app_state.json (the creator index) is the one thing the NAS archive DBs can't
replace, so after a loss the index is rebuilt from ground truth: each DB's
recorded filenames identify which folder owns that account. coomerfans and
twitter were already handled; this covers the pawchive pass, which reuses the
coomerfans correlation because both share an archive schema and target_path.

    python -m pytest tests/test_library_import_pawchive.py -q
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.library_import import _parse_pw_db, scan_library


def _mkdb(path, filenames):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE archive (entry TEXT PRIMARY KEY, post_id TEXT, "
                 "filename TEXT, kind TEXT, year TEXT, expected_size INT, path_key TEXT)")
    for i, fn in enumerate(filenames):
        conn.execute("INSERT INTO archive VALUES (?,?,?,?,?,?,?)",
                     (f"e{i}", str(i), fn, "image", "2026", 1, f"/p{i}"))
    conn.commit()
    conn.close()


def _mkfiles(folder, year, names, images=True):
    d = os.path.join(folder, "Images", year) if images else os.path.join(folder, year)
    os.makedirs(d, exist_ok=True)
    for n in names:
        open(os.path.join(d, n), "w").close()


# ── db name parsing ─────────────────────────────────────────────────

def test_parse_pw_db():
    assert _parse_pw_db("pawchive_patreon_4231621.db") == ("patreon", "4231621")


def test_parse_pw_db_service_with_no_id():
    assert _parse_pw_db("pawchive_broken.db") == (None, None)


def test_parse_pw_db_keeps_underscored_service():
    # rsplit on the LAST underscore, so a multi-word service survives.
    assert _parse_pw_db("pawchive_fan_box_99.db") == ("fan_box", "99")


# ── correlation ─────────────────────────────────────────────────────

def _setup(tmp_path, fnames_on_disk, fnames_in_db):
    root = tmp_path / "lib"; root.mkdir()
    folder = root / "Hooves Art"; folder.mkdir()
    _mkfiles(str(folder), "2026", fnames_on_disk)
    arch = tmp_path / "arch"; arch.mkdir()
    _mkdb(str(arch / "pawchive_patreon_4231621.db"), fnames_in_db)
    return [str(root)], str(arch), str(folder)


def test_pawchive_db_matches_its_folder(tmp_path):
    name = "2026.05.01 - Patreon - a.jpg"
    roots, arch, folder = _setup(tmp_path, [name], [name])
    cf, tw, pw, report = scan_library(roots, arch, session=None, resolve_online=False)
    assert report["pw_dbs"] == 1 and report["matched_pw"] == 1
    entry = pw["patreon_4231621"]
    assert entry["destination"] == folder
    assert entry["service"] == "patreon" and entry["user_id"] == "4231621"
    assert entry["url"].endswith("/patreon/user/4231621")


def test_name_falls_back_to_folder_not_numeric_id(tmp_path):
    # pawchive's API is behind Cloudflare, so offline the folder name is the
    # best label available -- and far more recognisable than the bare id.
    name = "2026.05.01 - Patreon - a.jpg"
    roots, arch, _ = _setup(tmp_path, [name], [name])
    _, _, pw, _ = scan_library(roots, arch, session=None, resolve_online=False)
    assert pw["patreon_4231621"]["name"] == "Hooves Art"


def test_unmatched_db_is_reported_not_guessed(tmp_path):
    # A DB whose files aren't on disk must NOT be attached to an arbitrary folder.
    roots, arch, _ = _setup(tmp_path, ["unrelated.jpg"], ["2026.05.01 - Patreon - a.jpg"])
    _, _, pw, report = scan_library(roots, arch, session=None, resolve_online=False)
    assert pw == {}
    assert report["matched_pw"] == 0
    assert "pawchive_patreon_4231621.db" in report["unmatched"]


def test_video_layout_matches_too(tmp_path):
    # Non-image kinds live in <folder>/<year>/, not Images/<year>/.
    root = tmp_path / "lib"; root.mkdir()
    folder = root / "Someone"; folder.mkdir()
    name = "2026.05.01 - Patreon - v.mp4"
    _mkfiles(str(folder), "2026", [name], images=False)
    arch = tmp_path / "arch"; arch.mkdir()
    _mkdb(str(arch / "pawchive_fanbox_77.db"), [name])
    _, _, pw, report = scan_library([str(root)], str(arch), session=None, resolve_online=False)
    assert report["matched_pw"] == 1 and pw["fanbox_77"]["destination"] == str(folder)


def test_coomerfans_and_pawchive_share_one_folder(tmp_path):
    # A creator archived from both sites groups into a single destination; the
    # merge attaches both links to it.
    root = tmp_path / "lib"; root.mkdir()
    folder = root / "Both"; folder.mkdir()
    pw_name, cf_name = "2026.05.01 - Patreon - a.jpg", "2025.02.27 - OF - b.jpg"
    _mkfiles(str(folder), "2026", [pw_name])
    _mkfiles(str(folder), "2025", [cf_name])
    arch = tmp_path / "arch"; arch.mkdir()
    _mkdb(str(arch / "pawchive_patreon_1.db"), [pw_name])
    _mkdb(str(arch / "coomerfans_onlyfans_2.db"), [cf_name])
    cf, _, pw, report = scan_library([str(root)], str(arch), session=None, resolve_online=False)
    assert report["matched_pw"] == 1 and report["matched_cf"] == 1
    assert pw["patreon_1"]["destination"] == cf["onlyfans_2"]["destination"] == str(folder)


# ── merge into the creator index ────────────────────────────────────

def test_merge_attaches_pawchive_links(tmp_path, monkeypatch):
    import threading
    import backend.api as api
    monkeypatch.setattr(api, "STATE_FILE", str(tmp_path / "app_state.json"))
    monkeypatch.setattr(api, "STATE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(api, "APP_DIR", str(tmp_path))
    app = api.Api.__new__(api.Api)
    app._state_lock = threading.Lock()

    dest = str(tmp_path / "Hooves Art")
    creators = {}
    added = app._merge_legacy_maps(creators, {}, {}, {
        "patreon_4231621": {"name": "Hooves Art", "service": "patreon",
                            "user_id": "4231621", "destination": dest,
                            "url": "https://pawchive.st/patreon/user/4231621",
                            "last_used": ""},
    })
    assert added == 1
    c = next(iter(creators.values()))
    link = c["links"][0]
    assert link["platform"] == "pawchive"
    assert link["service"] == "patreon" and link["user_id"] == "4231621"


def test_merge_is_idempotent(tmp_path, monkeypatch):
    # Re-running the import must not duplicate a link already present.
    import threading
    import backend.api as api
    monkeypatch.setattr(api, "STATE_FILE", str(tmp_path / "app_state.json"))
    monkeypatch.setattr(api, "STATE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(api, "APP_DIR", str(tmp_path))
    app = api.Api.__new__(api.Api)
    app._state_lock = threading.Lock()

    pw = {"patreon_1": {"name": "X", "service": "patreon", "user_id": "1",
                        "destination": str(tmp_path / "X"),
                        "url": "https://pawchive.st/patreon/user/1", "last_used": ""}}
    creators = {}
    assert app._merge_legacy_maps(creators, {}, {}, pw) == 1
    assert app._merge_legacy_maps(creators, {}, {}, pw) == 0
    assert len(next(iter(creators.values()))["links"]) == 1


# ── errors.db sidecars must never be read as archive DBs ────────────

def test_errors_sidecar_is_not_listed_as_an_archive_db(tmp_path):
    from backend.library_import import _list_dbs
    arch = tmp_path / "arch"; arch.mkdir()
    _mkdb(str(arch / "pawchive_patreon_1.db"), ["a.jpg"])
    # the FailureStore sidecar: same prefix, .db suffix, no `archive` table
    sqlite3.connect(str(arch / "pawchive_patreon_1.errors.db")).close()
    sqlite3.connect(str(arch / "coomerfans_onlyfans_2.errors.db")).close()
    names = {os.path.basename(p) for p in _list_dbs(str(arch))}
    assert names == {"pawchive_patreon_1.db"}


def test_sidecars_do_not_produce_unmatched_warnings(tmp_path):
    # The sidecar has no filenames, so before the fix it reported as unmatched.
    name = "2026.05.01 - Patreon - a.jpg"
    roots, arch, _ = _setup(tmp_path, [name], [name])
    sqlite3.connect(os.path.join(arch, "pawchive_patreon_4231621.errors.db")).close()
    _, _, pw, report = scan_library(roots, arch, session=None, resolve_online=False)
    assert report["pw_dbs"] == 1              # the sidecar is not counted
    assert report["unmatched"] == []
    assert report["matched_pw"] == 1


# ── creator folders nested below the chosen root ────────────────────

def test_finds_creators_two_levels_below_root(tmp_path):
    # P:/Real  ->  P:/Real/Furry/<creator>  — the layout that silently missed.
    from backend.library_import import _list_creator_folders
    root = tmp_path / "Real"
    creator = root / "Furry" / "Sillyliloomfie"
    creator.mkdir(parents=True)
    found = _list_creator_folders([str(root)])
    assert str(creator) in found
    assert str(root / "Furry") in found       # intermediate kept, harmless


def test_depth_limit_is_respected(tmp_path):
    from backend.library_import import _list_creator_folders
    deep = tmp_path / "r" / "a" / "b" / "c"
    deep.mkdir(parents=True)
    found = _list_creator_folders([str(tmp_path / "r")], max_depth=2)
    assert str(tmp_path / "r" / "a" / "b") in found
    assert str(deep) not in found


def test_nested_creator_actually_correlates(tmp_path):
    root = tmp_path / "Real"
    folder = root / "Furry" / "Someone"
    folder.mkdir(parents=True)
    name = "2026.05.01 - Patreon - a.jpg"
    _mkfiles(str(folder), "2026", [name])
    arch = tmp_path / "arch"; arch.mkdir()
    _mkdb(str(arch / "pawchive_patreon_9.db"), [name])
    _, _, pw, report = scan_library([str(root)], str(arch), session=None, resolve_online=False)
    assert report["matched_pw"] == 1
    assert pw["patreon_9"]["destination"] == str(folder)
    assert pw["patreon_9"]["name"] == "Someone"    # folder name, not the category


# ── twitter archive entry spellings ─────────────────────────────────

def test_tweet_ids_parse_with_and_without_the_separator(tmp_path):
    # Both spellings exist in the real archive dir depending on which gallery-dl
    # wrote the DB; requiring the underscore zeroed out every DB of the second
    # kind, so it matched no folder at all.
    from backend.library_import import _tw_db_tweetids
    p = str(tmp_path / "twitter_someone.db")
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE archive (entry TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO archive VALUES (?)", [
        ("twitter_1111111111111111111_0_1",),      # with separator
        ("twitter2222222222222222222_0_1",),       # without
        ("twitter2222222222222222222_0_2",),       # same tweet, second file
    ])
    conn.commit(); conn.close()
    assert _tw_db_tweetids(p) == {"1111111111111111111", "2222222222222222222"}
