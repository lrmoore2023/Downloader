"""Tracked albums: URL parsing, dated filing, per-album record, liveness, engines."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from backend import album_track_runner as at
from backend import http_download as hd


def test_parse_album_url_and_canonical():
    assert at.parse_album_url("https://bunkr.si/a/7Z7niuP1") == {
        "site": "bunkr", "kind": "album", "ref": "7Z7niuP1", "url": "https://bunkr.cr/a/7Z7niuP1"}
    assert at.parse_album_url("https://bunkr.pk/f/E4Z")["kind"] == "file"
    assert at.parse_album_url("https://gofile.io/d/AbC")["url"] == "https://gofile.io/d/AbC"
    assert at.parse_album_url("https://turbo.cr/a/x")["site"] == "turbo"
    assert at.parse_album_url("https://goonbox.cr/a/x")["site"] == "goonbox"
    assert at.parse_album_url("https://pixeldrain.com/l/x")["url"] == "https://pixeldrain.com/l/x"
    assert at.parse_album_url("https://turbo.cr/d/x") is None          # a file = thread media
    assert at.parse_album_url("https://mega.nz/folder/x") is None
    assert at.parse_album_url("") is None


def test_archive_path(tmp_path):
    p = at.album_archive_path(str(tmp_path), "bunkr", "7Z7niuP1", "/dest")
    assert p == os.path.join(str(tmp_path), "album_bunkr_7Z7niuP1.db")
    assert at.album_archive_path("", "gofile", "a/b", "/dest").endswith(".album-gofile-ab-archive.db")


def test_to_local():
    assert at._to_local(None) is None
    assert at._to_local("2026-08-23").year == 2026
    assert at._to_local(1761530000).year == 2025
    assert at._to_local(1761530000000).year == 2025               # epoch ms
    assert at._to_local("2026-08-23T06:08:52.000000Z").tzinfo is not None
    assert at._to_local("garbage") is None


def test_albumdb_liveness(tmp_path):
    db = at.AlbumDB(str(tmp_path / "a.db"))
    db.mark_alive(None)
    db.mark_alive(None)
    assert db.get("alive") == "unknown" and db.get("fail_streak") == "2"
    first = db.get("unreachable_since")
    assert first
    db.mark_alive(False, title="T")
    assert db.get("alive") == "dead" and db.get("dead_since") and db.get("unreachable_since") is None
    db.mark_alive(True)
    assert db.get("dead_since") is None and db.get("title") == "T"
    db.close()
    st = at.read_status(str(tmp_path / "a.db"))
    assert st["alive"] == "live" and st["file_count"] == 0
    assert at.read_status(str(tmp_path / "missing.db")) is None


# ── engines (no network) ────────────────────────────────────────────

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    calls = []

    def fake_download(session, url, path, **kw):
        calls.append(url)
        if "FAIL" in url:
            return hd.Result("failed", reason="HTTP 500")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"x" * 5)
        return hd.Result("ok", 5, 200)

    monkeypatch.setattr(at.hd, "download", fake_download)
    return {"dest": str(tmp_path / "Creator"), "arch": str(tmp_path / "arch"), "calls": calls}


def run(sb, item, **kw):
    done, errs = {}, []
    at.AlbumTrackRunner(workers=2).run(
        item, sb["dest"], "full",
        at.album_archive_path(sb["arch"], item["site"], item["ref"], sb["dest"]),
        lambda d: None, done.update, errs.append,
        errors_path=os.path.join(sb["arch"], "e.errors.db"), **kw)
    return done, errs


def test_turbo_album_dated_and_incremental(sandbox, monkeypatch):
    os.makedirs(sandbox["arch"])
    ids = ["a1", "b2"]
    monkeypatch.setattr(at.th, "make_session", lambda: object())
    monkeypatch.setattr(at.th, "album_files", lambda s, ref: ("My Album", list(ids)))
    monkeypatch.setattr(at.th, "resolve", lambda s, fid: {
        "url": f"https://cdn/{fid}", "filename": f"{fid}.mp4", "size": 5, "md5": None,
        "upload_date": "2025-10-27"})
    item = at.parse_album_url("https://turbo.cr/a/ALB")
    done, errs = run(sandbox, item)
    assert done["downloaded"] == 2 and done["alive"] is True and done["title"] == "My Album", errs
    root = os.path.join(sandbox["dest"], "Unsorted", "2025")
    assert sorted(os.listdir(root)) == ["2025.10.27 - Turbo - a1.mp4", "2025.10.27 - Turbo - b2.mp4"]
    ids.append("c3")
    sandbox["calls"].clear()
    done, _ = run(sandbox, item)
    assert done["downloaded"] == 1 and done["skipped"] == 2 and done["file_count"] == 3
    assert sandbox["calls"] == ["https://cdn/c3"]


def test_turbo_album_gone(sandbox, monkeypatch):
    os.makedirs(sandbox["arch"])
    monkeypatch.setattr(at.th, "make_session", lambda: object())

    def gone(s, ref):
        raise at.th.TurboGone(ref)
    monkeypatch.setattr(at.th, "album_files", gone)
    item = at.parse_album_url("https://turbo.cr/a/DEAD")
    done, errs = run(sandbox, item)
    assert done["alive"] is False and any("gone" in e["message"] for e in errs)
    st = at.read_status(at.album_archive_path(sandbox["arch"], "turbo", "DEAD", sandbox["dest"]))
    assert st["alive"] == "dead" and st["dead_since"]


def test_gofile_engine_uses_listing_dates(sandbox, monkeypatch):
    os.makedirs(sandbox["arch"])
    from backend import gofile_downloader as gf

    def fake_list(self, url, password=None):
        return {"token": "T", "name": "Folder", "files": [
            {"id": "f1", "rel_dir": "", "name": "one.jpg", "size": 5,
             "link": "https://store/f1", "create_time": 1735732800, "md5": None},
            {"id": "f2", "rel_dir": "sub", "name": "two.mp4", "size": 5,
             "link": "https://store/FAIL", "create_time": 1767268800, "md5": None}]}
    monkeypatch.setattr(gf.GofileRunner, "list_folder", fake_list)
    monkeypatch.setattr(gf, "make_session", lambda: object())
    item = at.parse_album_url("https://gofile.io/d/ABC")
    done, errs = run(sandbox, item)
    assert done["downloaded"] == 1 and done["errors"] == 1 and done["title"] == "Folder"
    assert os.path.isfile(os.path.join(sandbox["dest"], "Unsorted", "Images", "2025",
                                       "2025.01.01 - Gofile - one.jpg"))


def test_gofile_engine_gone(sandbox, monkeypatch):
    os.makedirs(sandbox["arch"])
    from backend import gofile_downloader as gf

    def gone(self, url, password=None):
        raise gf.GofileGone("error-notFound")
    monkeypatch.setattr(gf.GofileRunner, "list_folder", gone)
    done, _ = run(sandbox, at.parse_album_url("https://gofile.io/d/DEAD"))
    assert done["alive"] is False


class FakeCyberdrop:
    """Writes files into staging + a results.jsonl, like cyberdrop-dl does."""
    files = []
    scrape_error = None

    def __init__(self, platform="album"):
        pass

    def cancel(self):
        pass

    def run(self, url, download_dir, on_progress, on_complete, on_error, errors_path=None,
            state_dir=None, ignore_history=False, tuning=None, **kw):
        logs = os.path.join(state_dir, "logs")
        os.makedirs(logs, exist_ok=True)
        album = os.path.join(download_dir, "Album (Bunkr)")
        os.makedirs(album, exist_ok=True)
        with open(os.path.join(logs, "downloader.results.jsonl"), "w", encoding="utf-8") as f:
            for name, uploaded, furl in self.files:
                p = os.path.join(album, name)
                with open(p, "wb") as fh:
                    fh.write(b"y" * 7)
                f.write(json.dumps({"path": p, "url": furl, "uploaded_at": uploaded,
                                    "filename": name, "downloaded": True}) + "\n")
        on_complete({"downloaded": len(self.files), "skipped": 0, "errors": 0,
                     "scrape_error": self.scrape_error})


def test_cyberdrop_staging_files_to_dated_names(sandbox, monkeypatch, tmp_path):
    import backend.cyberdrop_dl_runner as cdr
    FakeCyberdrop.files = [("clip.mp4", 1761530000, "https://bunkr.cr/f/1"),
                           ("pic.jpg", None, "https://bunkr.cr/f/2")]
    FakeCyberdrop.scrape_error = None
    monkeypatch.setattr(cdr, "CyberdropDlRunner", FakeCyberdrop)
    os.makedirs(sandbox["arch"])
    item = at.parse_album_url("https://cyberdrop.cr/a/ALB")
    done, errs = run(sandbox, item, engine_state_dir=str(tmp_path / "eng"),
                     origin_date="2024-05-05T12:00:00+00:00")
    assert done["downloaded"] == 2 and done["title"] == "Album", errs
    root = os.path.join(sandbox["dest"], "Unsorted")
    assert os.path.isfile(os.path.join(root, "2025", "2025.10.27 - Cyberdrop - clip.mp4"))
    assert os.path.isfile(os.path.join(root, "Images", "2024", "2024.05.05 - Cyberdrop - pic.jpg"))  # post-date fallback
    assert os.listdir(os.path.join(root, ".incoming")) == []                        # staging emptied
    # the same files again (history overlap) are not filed twice
    done, _ = run(sandbox, item, engine_state_dir=str(tmp_path / "eng"))
    assert done["downloaded"] == 0 and done["file_count"] == 2
    assert sorted(os.listdir(os.path.join(root, "2025"))) == ["2025.10.27 - Cyberdrop - clip.mp4"]


def test_cyberdrop_404_scrape_is_gone(sandbox, monkeypatch, tmp_path):
    import backend.cyberdrop_dl_runner as cdr
    FakeCyberdrop.files = []
    FakeCyberdrop.scrape_error = "404 Not Found"
    monkeypatch.setattr(cdr, "CyberdropDlRunner", FakeCyberdrop)
    os.makedirs(sandbox["arch"])
    done, _ = run(sandbox, at.parse_album_url("https://cyberdrop.cr/a/DEAD"),
                  engine_state_dir=str(tmp_path / "eng"))
    assert done["alive"] is False


def test_cyberdrop_transient_scrape_is_unknown(sandbox, monkeypatch, tmp_path):
    import backend.cyberdrop_dl_runner as cdr
    FakeCyberdrop.files = []
    FakeCyberdrop.scrape_error = "504 Gateway Timeout"
    monkeypatch.setattr(cdr, "CyberdropDlRunner", FakeCyberdrop)
    os.makedirs(sandbox["arch"])
    done, _ = run(sandbox, at.parse_album_url("https://cyberdrop.cr/a/SLOW"),
                  engine_state_dir=str(tmp_path / "eng"))
    assert done["alive"] is None



# ── filester single files ───────────────────────────────────────────

class FakeFilester:
    """filester's public download API + CDN for one slug."""

    def __init__(self, exists=True):
        self.exists = exists
        self.headers = {}

    def post(self, url, json=None, headers=None, timeout=None):
        if not self.exists:
            return type("R", (), {"status_code": 404, "json": lambda s: {"success": False}})()
        return type("R", (), {"status_code": 200, "json": lambda s: {
            "success": True, "server": "https://fsc3.cdn.cr", "file": "uuid.mp4",
            "token": "T", "name": "clip_source.mp4"}})()

    def get(self, url, headers=None, timeout=None, **kw):
        return type("R", (), {"status_code": 206, "headers": {"Content-Range": "bytes 0-0/5"},
                              "close": lambda s: None})()


def test_filester_single_file(sandbox, monkeypatch):
    os.makedirs(sandbox["arch"])
    from backend import filester_downloader as fsd
    fake = FakeFilester()
    monkeypatch.setattr(fsd._rq, "Session", lambda **kw: fake)
    item = at.parse_album_url("https://filester.sh/d/Dp4Fiym")
    assert item == {"site": "filester", "kind": "file", "ref": "Dp4Fiym",
                    "url": "https://filester.sh/d/Dp4Fiym"}
    done, errs = run(sandbox, item, origin_date="2026-08-30T12:00:00+00:00")
    assert done["downloaded"] == 1 and done["errors"] == 0 and done["file_count"] == 1, errs
    assert sandbox["calls"][0].startswith("https://fsc3.cdn.cr/v2/uuid.mp4?token=T&download=true")
    assert os.path.isfile(os.path.join(sandbox["dest"], "Unsorted", "2026",
                                       "2026.08.30 - Filester - clip_source.mp4"))


def test_filester_single_file_gone(sandbox, monkeypatch):
    os.makedirs(sandbox["arch"])
    from backend import filester_downloader as fsd
    monkeypatch.setattr(fsd._rq, "Session", lambda **kw: FakeFilester(exists=False))
    done, _ = run(sandbox, at.parse_album_url("https://filester.me/d/GONE"))
    assert done["alive"] is False


def test_engine_that_fetches_nothing_is_an_error(sandbox, monkeypatch):
    """The original bug: the folder engine rejected the link without counting an
    error, so the run looked successful with 0 files."""
    os.makedirs(sandbox["arch"])
    import backend.filester_downloader as fsd

    class Quiet:
        def __init__(self, platform="album"):
            pass

        def cancel(self):
            pass

        def run(self, url, download_dir, on_progress, on_complete, on_error, password=None):
            on_complete({"downloaded": 0, "skipped": 0, "errors": 0, "subfolder": None})
    monkeypatch.setattr(fsd, "FilesterRunner", Quiet)
    done, errs = run(sandbox, at.parse_album_url("https://filester.gg/f/850ce01ea5967b08"))
    assert done["errors"] == 1 and any("nothing was downloaded" in e["message"] for e in errs)



# ── Unsorted layout: hand-moved files are recognised ───────────────

from backend import unsorted_layout as layout  # noqa: E402


def test_layout_locate(tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "2026"))
    os.makedirs(os.path.join(root, "Images", "2025"))
    open(os.path.join(root, "2026", "2026.09.28 - Bunkr - a.mp4"), "wb").close()
    open(os.path.join(root, "Images", "2025", "2025.01.01 - Gofile - b.jpg"), "wb").close()
    assert layout.locate(root, "2026/2026.09.28 - Bunkr - a.mp4") == "2026/2026.09.28 - Bunkr - a.mp4"
    # recorded under the old per-site layout / wrong kind folder → found where it is now
    assert layout.locate(root, "Bunkr/2026/2026.09.28 - Bunkr - a.mp4") == "2026/2026.09.28 - Bunkr - a.mp4"
    assert layout.locate(root, "2025/2025.01.01 - Gofile - b.jpg") == "Images/2025/2025.01.01 - Gofile - b.jpg"
    assert layout.locate(root, "2024/missing.mp4") is None
    assert layout.kind_of("x.JPG") == "image" and layout.kind_of("x.zip") == "video"


def test_album_file_moved_by_hand_is_not_refetched(sandbox, monkeypatch):
    """The first batch was filed under Simpcity/<Site>/<year>; the user moved them
    into Unsorted/<year>. The old record must follow the file, not re-download it."""
    os.makedirs(sandbox["arch"])
    monkeypatch.setattr(at.th, "make_session", lambda: object())
    monkeypatch.setattr(at.th, "album_files", lambda s, ref: ("A", ["a1"]))
    monkeypatch.setattr(at.th, "resolve", lambda s, fid: {
        "url": f"https://cdn/{fid}", "filename": f"{fid}.mp4", "size": 5, "md5": None,
        "upload_date": "2025-10-27"})
    item = at.parse_album_url("https://turbo.cr/a/ALB")
    dbp = at.album_archive_path(sandbox["arch"], "turbo", "ALB", sandbox["dest"])
    db = at.AlbumDB(dbp)
    db.record("a1", "2025.10.27 - Turbo - a1.mp4", "2025/2025.10.27 - Turbo - a1.mp4", 5)  # old relative key
    db.close()
    moved = os.path.join(sandbox["dest"], "Unsorted", "2025")
    os.makedirs(moved)
    with open(os.path.join(moved, "2025.10.27 - Turbo - a1.mp4"), "wb") as f:
        f.write(b"x" * 5)
    done, _ = run(sandbox, item)
    assert done["downloaded"] == 0 and done["skipped"] == 1 and sandbox["calls"] == []



def test_layout_legacy_present(tmp_path):
    dest = tmp_path / "Creator"
    (dest / "Simpcity" / "2024").mkdir(parents=True)
    (dest / "Simpcity" / "Bunkr" / "2026").mkdir(parents=True)
    (dest / "Simpcity" / "2024" / "a.mp4").write_bytes(b"x")
    (dest / "Simpcity" / "Bunkr" / "2026" / "b.mp4").write_bytes(b"x")
    root = layout.root_of(str(dest))
    assert layout.legacy_present(root, "2024/a.mp4")                # Simpcity/<year>
    assert layout.legacy_present(root, "2026/b.mp4")                # Simpcity/<Site>/<year>
    assert not layout.legacy_present(root, "2025/c.mp4")
    assert not layout.legacy_present(layout.root_of(str(tmp_path / "Other")), "2024/a.mp4")
