"""Offline unit checks for the Albums tab (no network, no real downloads).

Covers the site registry / engine dispatch, the gallery-dl album config, and the
AlbumRunner orchestration (per-site engine choice, cyberdrop-dl -> gallery-dl
fallback, unsupported-link handling, cancel forwarding) with the two sub-runners
mocked. Run directly:

    python tests/test_album_runner.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import album_sites
from backend import album_runner
from backend.album_runner import AlbumRunner
from backend.config_builder import build_album_config, cleanup_config


# ── site registry ───────────────────────────────────────────────────
def test_detect_site_maps_hosts_to_engines():
    cases = {
        "https://cyberdrop.cr/a/GmQ4oWTS": ("cyberdrop", "cyberdrop-dl"),
        "https://bunkr.cr/a/1j4nj3rg": ("bunkr", "cyberdrop-dl"),
        "https://bunkrr.su/a/xyz": ("bunkr", "cyberdrop-dl"),
        "https://bunkr.si/a/xyz": ("bunkr", "cyberdrop-dl"),
        "https://filester.gg/f/abc": ("filester", "filester"),
        "https://filester.me/f/abc": ("filester", "filester"),
    }
    for url, (key, engine) in cases.items():
        site = album_sites.detect_site(url)
        assert site is not None, url
        assert site["key"] == key, (url, site["key"])
        assert site["engine"] == engine, (url, site["engine"])


def test_detect_site_unknown_returns_none():
    for url in ("https://example.com/a/x", "https://youtube.com/watch?v=1", "", None):
        assert album_sites.detect_site(url) is None


def test_parse_entry_splits_optional_password():
    assert album_sites.parse_entry("https://filester.gg/f/x | secret") == ("https://filester.gg/f/x", "secret")
    assert album_sites.parse_entry("  https://bunkr.cr/a/x  ") == ("https://bunkr.cr/a/x", None)
    assert album_sites.parse_entry("https://f/x |   ") == ("https://f/x", None)
    assert album_sites.parse_entry("") == (None, None)
    assert album_sites.parse_entry("   ") == (None, None)


# ── gallery-dl album config ─────────────────────────────────────────
def test_build_album_config_shape():
    path = build_album_config("E:/Downloads/Grabs")
    try:
        cfg = json.load(open(path, encoding="utf-8"))
    finally:
        cleanup_config(path)
    ex = cfg["extractor"]
    assert ex["base-directory"] == "E:/Downloads/Grabs"
    assert ex["bunkr"]["directory"] == ["{album_name}"]
    assert ex["cyberdrop"]["directory"] == ["{album_name}"]
    assert ex["filester"]["directory"] == ["{folder_name} ({folder_id})"]
    assert cfg["downloader"]["retries"] >= 1


def test_build_album_config_cookies():
    path = build_album_config("E:/x", cookies_path="C:\\cookies.txt")
    try:
        cfg = json.load(open(path, encoding="utf-8"))
    finally:
        cleanup_config(path)
    assert cfg["extractor"]["bunkr"]["cookies"] == "C:/cookies.txt"


# ── AlbumRunner dispatch / fallback (mocked engines) ────────────────
class FakeRunner:
    """Stand-in for CyberdropDlRunner / GalleryDlRunner.

    Records the url it was asked to download and invokes on_complete with a
    caller-provided stats dict. `calls` is a shared list so a test can see the
    ordered sequence of (engine, url) across both engine classes.
    """
    def __init__(self, engine, calls, stats_for):
        self._engine = engine
        self._calls = calls
        self._stats_for = stats_for
        self.is_running = False

    def cancel(self):
        pass

    def run(self, url, *args, **kwargs):
        self._calls.append((self._engine, url))
        on_complete = kwargs.get("on_complete") or (args[3] if len(args) > 3 else None)
        stats = self._stats_for(self._engine, url)
        if on_complete:
            on_complete(stats)


def _install_fakes(monkeypatch_calls, stats_for):
    """Patch every engine class in album_runner; return the shared calls list."""
    calls = monkeypatch_calls
    album_runner.CyberdropDlRunner = lambda *a, **k: FakeRunner("cyberdrop-dl", calls, stats_for)
    album_runner.GalleryDlRunner = lambda *a, **k: FakeRunner("gallery-dl", calls, stats_for)
    album_runner.FilesterRunner = lambda *a, **k: FakeRunner("filester", calls, stats_for)
    return calls


def _run(links, stats_for):
    calls = []
    orig = (album_runner.CyberdropDlRunner, album_runner.GalleryDlRunner,
            album_runner.FilesterRunner)
    _install_fakes(calls, stats_for)
    events = {"complete": None, "progress": [], "error": []}
    try:
        with tempfile.TemporaryDirectory() as dest, tempfile.TemporaryDirectory() as app:
            r = AlbumRunner(app)
            r.run(
                destination=dest,
                links=links,
                on_progress=lambda d: events["progress"].append(d),
                on_complete=lambda d: events.__setitem__("complete", d),
                on_error=lambda d: events["error"].append(d),
            )
    finally:
        (album_runner.CyberdropDlRunner, album_runner.GalleryDlRunner,
         album_runner.FilesterRunner) = orig
    return calls, events


def test_dispatch_per_site():
    # bunkr/cyberdrop via cyberdrop-dl; filester via its native downloader.
    def stats_for(engine, url):
        return {"downloaded": 3, "skipped": 0, "errors": 0, "succeeded": True}
    calls, events = _run([
        "https://bunkr.cr/a/1",
        "https://filester.gg/f/2",
    ], stats_for)
    assert ("cyberdrop-dl", "https://bunkr.cr/a/1") in calls
    assert ("filester", "https://filester.gg/f/2") in calls
    # bunkr succeeded -> no gallery-dl fallback for it
    assert ("gallery-dl", "https://bunkr.cr/a/1") not in calls
    assert events["complete"]["downloaded"] == 6


def test_cyberdrop_empty_falls_back_to_gallery():
    # cyberdrop-dl resolves nothing -> same url retried via gallery-dl.
    def stats_for(engine, url):
        if engine == "cyberdrop-dl":
            return {"downloaded": 0, "skipped": 0, "errors": 1}
        return {"downloaded": 5, "skipped": 0, "errors": 0}
    calls, events = _run(["https://bunkr.cr/a/1"], stats_for)
    assert calls == [
        ("cyberdrop-dl", "https://bunkr.cr/a/1"),
        ("gallery-dl", "https://bunkr.cr/a/1"),
    ]
    # both counted: 0 (cdl) + 5 (gdl)
    assert events["complete"]["downloaded"] == 5


def test_scrape_error_skips_gallery_fallback():
    # A bunkr-side scrape error (502) must NOT fall through to gallery-dl.
    def stats_for(engine, url):
        if engine == "cyberdrop-dl":
            return {"downloaded": 0, "skipped": 0, "errors": 1, "scrape_error": "502 Bad Gateway"}
        return {"downloaded": 9, "skipped": 0, "errors": 0}
    calls, events = _run(["https://bunkr.cr/a/1"], stats_for)
    assert calls == [("cyberdrop-dl", "https://bunkr.cr/a/1")]   # no gallery-dl call
    assert events["complete"]["downloaded"] == 0


def test_scrape_failed_regex():
    from backend.cyberdrop_dl_runner import _SCRAPE_FAILED
    line = "[2026-07-18 13:00:00.000] ERROR    Scrape Failed: https://bunkr.cr/a/1j4nj3rg (502 Bad Gateway)"
    m = _SCRAPE_FAILED.search(line)
    assert m and m.group(1) == "502 Bad Gateway"


def test_cyberdrop_all_skipped_no_fallback():
    # Everything already present (skipped>0) is a success, not a fallback trigger.
    def stats_for(engine, url):
        return {"downloaded": 0, "skipped": 10, "errors": 0}
    calls, events = _run(["https://cyberdrop.cr/a/1"], stats_for)
    assert calls == [("cyberdrop-dl", "https://cyberdrop.cr/a/1")]
    assert events["complete"]["skipped"] == 10


def test_unsupported_link_reported_not_downloaded():
    def stats_for(engine, url):
        return {"downloaded": 1, "skipped": 0, "errors": 0}
    calls, events = _run(["https://example.com/a/1"], stats_for)
    assert calls == []
    assert events["complete"]["unsupported"] == ["https://example.com/a/1"]
    assert any("Unsupported" in (e.get("message") or "") for e in events["error"])


def test_force_urls_and_per_link_results():
    # Force only the bunkr link -> its cyberdrop-dl run gets ignore_history=True;
    # the unforced cyberdrop link does not.
    calls, captured = [], []
    orig = album_runner.CyberdropDlRunner

    class F:
        def cancel(self):
            pass

        def run(self, url, *a, **k):
            captured.append((url, k.get("ignore_history")))
            calls.append(("cdl", url))
            (k.get("on_complete") or (lambda s: None))({"downloaded": 2, "skipped": 0, "errors": 0})

    album_runner.CyberdropDlRunner = lambda *a, **k: F()
    summary = {}
    try:
        with tempfile.TemporaryDirectory() as dest, tempfile.TemporaryDirectory() as app:
            AlbumRunner(app).run(
                destination=dest,
                links=["https://bunkr.cr/a/1", "https://cyberdrop.cr/a/2"],
                force_urls=["https://bunkr.cr/a/1"],
                on_progress=lambda d: None,
                on_complete=lambda d: summary.update(d),
                on_error=lambda d: None,
            )
    finally:
        album_runner.CyberdropDlRunner = orig

    forced = dict(captured)
    assert forced["https://bunkr.cr/a/1"] is True         # forced -> ignore history
    assert forced["https://cyberdrop.cr/a/2"] is False    # not forced
    # per-link results present for the ledger
    res = {r["url"]: r for r in summary["results"]}
    assert set(res) == {"https://bunkr.cr/a/1", "https://cyberdrop.cr/a/2"}
    assert res["https://bunkr.cr/a/1"]["site"] == "bunkr"
    assert res["https://bunkr.cr/a/1"]["downloaded"] == 2


def _isolated_api():
    """An Api with app_state.json redirected to a temp file (never touches real state)."""
    import backend.api as apimod
    d = tempfile.mkdtemp()
    apimod.STATE_FILE = os.path.join(d, "state.json")
    apimod.STATE_BACKUP_DIR = os.path.join(d, "backups")
    return apimod.Api(), d


def test_album_creator_crud_and_dedup():
    api, d = _isolated_api()
    root = os.path.join(d, "Herkitty")
    os.makedirs(root, exist_ok=True)

    cid = api.save_album_creator({"name": "Herkitty", "root_dir": root})["id"]
    assert cid

    assert api.add_album_link(cid, "https://bunkr.cr/a/1")["site"] == "bunkr"
    assert "error" in api.add_album_link(cid, "https://example.com/a/1")   # unsupported
    assert "error" in api.add_album_link(cid, "https://bunkr.cr/a/1/")     # dup (norm)
    assert len(api.get_album_creator(cid)["links"]) == 1
    assert api.list_album_creators()[0]["link_count"] == 1

    # Not "known" for dedup until it's actually been downloaded.
    assert api.check_album_links(["https://bunkr.cr/a/1"])["results"][0]["known"] is False

    api._record_album_results(cid, [{
        "url": "https://bunkr.cr/a/1", "site": "bunkr", "downloaded": 5,
        "title": "Herkitty", "subfolder": "Herkitty (Bunkr)"}])
    chk = api.check_album_links(["https://bunkr.cr/a/1", "https://bunkr.cr/a/other"])["results"]
    assert chk[0]["known"] is True
    assert chk[0]["file_count"] == 5 and chk[0]["title"] == "Herkitty"
    assert chk[0]["root_dir"] == api._fwd(root)
    assert chk[1]["known"] is False

    api.remove_album_link(cid, "https://bunkr.cr/a/1")
    assert len(api.get_album_creator(cid)["links"]) == 0
    api.delete_album_creator(cid)
    assert api.get_album_creator(cid) is None


def test_album_creators_isolated_from_regular_creators():
    api, d = _isolated_api()
    root = os.path.join(d, "X")
    os.makedirs(root, exist_ok=True)
    api.save_album_creator({"name": "X", "root_dir": root})
    # Saving an album creator must never populate the regular downloader.
    assert api.load_state().get("creators") == {}


def test_clear_by_page_url_stops_compounding():
    from backend.download_errors import FailureStore
    d = tempfile.mkdtemp()
    s = FailureStore(os.path.join(d, "album_errors.db"))
    A, B = "https://bunkr.cr/a/AAA", "https://bunkr.cr/a/BBB"
    s.record_failure("album_1", platform="album", url="u1", page_url=A, reason="429")
    s.record_failure("album_2", platform="album", url="u2", page_url=A, reason="429")
    s.record_failure("album_3", platform="album", url="u3", page_url=B, reason="429")
    # A dismissed A-failure must survive the clear (dismissal is sticky).
    s.record_failure("album_4", platform="album", url="u4", page_url=A, reason="x")
    s.dismiss("album_4")

    s.clear_by_page_url(A)
    remaining = {r["entry"] for r in s.list_failures()}
    assert remaining == {"album_3", "album_4"}, remaining   # B kept, dismissed kept

    # Re-recording A's still-failing file doesn't stack onto the old ones.
    s.record_failure("album_1", platform="album", url="u1", page_url=A, reason="429")
    a_failed = [r for r in s.list_failures(state="failed") if r["page_url"] == A]
    assert len(a_failed) == 1
    s.close()


def test_cancel_forwards_to_current_runner():
    r = AlbumRunner(tempfile.gettempdir())
    cancelled = {"hit": False}

    class Cur:
        def cancel(self):
            cancelled["hit"] = True
    r._current = Cur()
    r.cancel()
    assert cancelled["hit"] is True
    assert r._cancel_event.is_set()


# ── runner ───────────────────────────────────────────────────────────
def _all_tests():
    return [v for k, v in sorted(globals().items())
            if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    failed = 0
    for fn in _all_tests():
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {fn.__name__} — {e}")
        except Exception as e:
            failed += 1
            print(f"  [ERROR] {fn.__name__} — {type(e).__name__}: {e}")
    print(f"\n{'ALL PASSED' if not failed else str(failed) + ' FAILED'}")
    sys.exit(1 if failed else 0)
