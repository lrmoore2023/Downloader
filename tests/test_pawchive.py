"""Offline unit checks for the pawchive engine (no network).

Covers the pure parsing/naming layer and the stateful external-links manifest —
the parts whose correctness the download flow depends on. Run directly:

    python tests/test_pawchive.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import pawchive_scraper as ps
from backend.pawchive_links import PawchiveLinks

_results = []


def check(name, ok, detail=""):
    _results.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# Fixtures mirror real captured API payloads (see scratchpad api_*.json).
POST_LINKS = {
    "id": "160106941", "user": "2793377", "service": "patreon",
    "title": "June 2026 Daily Animation Challenge - DAY 3",
    "content": ('<p><a href="https://files.catbox.moe/knpcy0.mp4">MP4</a></p>'
                '<p><a href="https://porn3dx.com/post/88027/makeup-sex">P3DX</a></p>'
                '<p><a href="https://x.com/doublenyl/status/206">TWEET</a></p><p>body</p>'),
    "embed": {}, "published": "2026-06-04T06:55:27", "added": "2026-06-04T06:55:27",
    "file": {}, "attachments": [{"name": "Makeup Sex.gif", "path": "/09/fb/abc.gif"}],
    "detail_fetched": True, "has_full": True,
}
POST_VIDEO = {
    "id": "158046668", "user": "2648863", "service": "patreon",
    "title": "Label's Secret", "content": "<p>Full Mp4</p>", "embed": {},
    "published": "2026-05-12T09:41:25", "file": {},
    "attachments": [
        {"name": "label stream 2.jpg", "path": "/84/ad/a.jpg"},
        {"name": "teaser.mp4", "path": "/22/a3/b.mp4"},
        {"name": "fullHD.mp4", "path": "/73/73/c.mp4"},
    ], "detail_fetched": True, "has_full": True,
}
POST_MEGA = {
    "id": "152485241", "user": "86788386", "service": "patreon",
    "title": "MEGA Archive Febraury", "content": "",
    "embed": {"url": "https://mega.nz/folder/WId2#k", "subject": "100 MB folder on MEGA"},
    "published": "2026-03-07T20:50:55", "file": {"name": "p.png", "path": "/04/c9/d.png"},
    "attachments": [], "detail_fetched": True, "has_full": False,
}


def t_urls():
    check("parse_creator_url", ps.parse_creator_url("https://pawchive.st/patreon/user/2793377")
          == {"service": "patreon", "user_id": "2793377"})
    check("creator_url rejects post", ps.parse_creator_url(
        "https://pawchive.st/patreon/user/2793377/post/160106941") is None)
    check("parse_post_url", ps.parse_post_url(
        "https://pawchive.st/fanbox/user/5/post/9") == {"service": "fanbox", "user_id": "5", "post_id": "9"})


def t_parse_media():
    p = ps.parse_post(POST_VIDEO)
    check("video post: 3 media", len(p["media"]) == 3)
    kinds = [m["kind"] for m in p["media"]]
    check("kinds image+video+video", kinds == ["image", "video", "video"])
    check("dt parsed", p["dt"] and p["dt"].year == 2026)
    check("media url built", p["media"][1]["url"]
          == "https://file.pawchive.pw/data/22/a3/b.mp4?f=teaser.mp4")

    m = ps.parse_post(POST_MEGA)
    check("mega: file counts as media", len(m["media"]) == 1)
    check("mega: embed link extracted", any(l["host"] == "mega.nz" for l in m["external_links"]))
    check("mega: classified manual",
          [l for l in m["external_links"] if l["host"] == "mega.nz"][0]["kind"] == "manual")


def t_links_classify():
    p = ps.parse_post(POST_LINKS)
    by = {l["host"]: l["kind"] for l in p["external_links"]}
    check("catbox -> direct", by.get("files.catbox.moe") == "direct")
    check("porn3dx -> reference", by.get("porn3dx.com") == "reference")
    check("x.com -> reference", by.get("x.com") == "reference")
    check("internal pawchive links skipped",
          not any("pawchive" in l["host"] for l in p["external_links"]))


def t_filenames():
    p = ps.parse_post(POST_VIDEO)
    fn = ps.build_filename(p["dt"], "patreon", "teaser.mp4")
    check("build_filename", fn == "2026.05.12 - Patreon - teaser.mp4", fn)
    check("filename_prefix", ps.filename_prefix(p["dt"], "patreon") == "2026.05.12 - Patreon - ")
    check("site_code title case", ps.site_code("fanbox") == "Fanbox")
    check("sanitize strips illegal", ps.sanitize_filename('a:b?<c>.mp4') == "abc.mp4",
          ps.sanitize_filename('a:b?<c>.mp4'))
    check("add_index_suffix before ext", ps.add_index_suffix("x - y.mp4", 1) == "x - y_1.mp4")
    # archive attachments (zip/rar/...) -> 'archive' kind, routed to the year folder
    check("zip -> archive kind", ps._kind_and_ext("pack.zip") == ("archive", "zip"))
    check("rar -> archive kind", ps._kind_and_ext("pack.rar") == ("archive", "rar"))
    ap = ps.target_path("D", "archive", p["dt"], "2026.05.12 - Patreon - pack.zip").replace("\\", "/")
    check("archive -> year folder", ap == "D/2026/2026.05.12 - Patreon - pack.zip", ap)
    ip = ps.target_path("D", "image", p["dt"], "x.png").replace("\\", "/")
    check("image -> images/year folder", ip == "D/Images/2026/x.png", ip)
    # page-order decoration (images only): ordinal + optional time token
    check("build_filename ordinal",
          ps.build_filename(p["dt"], "patreon", "a.png", ordinal=2, width=2)
          == "2026.05.12 - Patreon - 02 - a.png")
    check("build_filename time+ordinal",
          ps.build_filename(p["dt"], "patreon", "a.png", ordinal=1, width=2, include_time=True)
          == f"2026.05.12 {p['dt']:%H.%M} - Patreon - 01 - a.png")


def t_manifest_merge():
    d = tempfile.mkdtemp(prefix="pawmanifest_")
    pl = PawchiveLinks(os.path.join(d, "_pawchive_links.json"))
    post = ps.parse_post(POST_LINKS)

    # catbox grabbed by the runner -> hidden; references pending
    pl.upsert_post(post, {"https://files.catbox.moe/knpcy0.mp4": {"status": "grabbed"}})
    hosts = sorted(i["host"] for i in pl.pending())
    check("grabbed direct hidden; refs pending", hosts == ["porn3dx.com", "x.com"], hosts)

    # resolve one reference, then a re-scan with no states must preserve it
    key = [i["key"] for i in pl.pending() if i["host"] == "porn3dx.com"][0]
    pl.mark_resolved(key, True)
    pl.upsert_post(post, {})   # re-scan
    hosts2 = [i["host"] for i in pl.pending()]
    check("resolved survives re-scan", hosts2 == ["x.com"], hosts2)
    check("grabbed survives re-scan (no state)",
          all(i["host"] != "files.catbox.moe" for i in pl.pending()))

    # a failed direct file must be surfaced (completeness)
    pl.upsert_post(post, {"https://files.catbox.moe/knpcy0.mp4":
                          {"status": "failed", "note": "404"}})
    check("failed direct surfaced",
          any(i["host"] == "files.catbox.moe" and i["status"] == "failed" for i in pl.pending()))
    check("markdown written", os.path.isfile(pl.md_path))


def t_extract():
    """Auto-extract: media pulled into the library (subfolder folded into the name),
    non-media kept together under an archive-named folder, original deleted, and the
    whole thing idempotent."""
    import zipfile
    from datetime import datetime
    from backend.pawchive_runner import PawchiveRunner
    from backend.pawchive_archive import Archive, entry_key
    from backend import pawchive_extract as px

    # classify: only known image/video exts are media; unknown is NOT (unlike _kind_and_ext)
    check("classify image", px.classify("2.png") == "image")
    check("classify video", px.classify("a.MP4") == "video")
    check("classify txt -> None", px.classify("read me.txt") is None)
    check("classify unknown -> None", px.classify("model.obj") is None)

    dest = tempfile.mkdtemp(prefix="pawx_")
    # A pack: root media + root non-media + a subfolder with a same-named image + a
    # nested asset tree.
    zpath = os.path.join(dest, "2026", "2026.05.04 - Patreon - MyPack.zip")
    os.makedirs(os.path.dirname(zpath), exist_ok=True)
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("b.mp4", b"vid")
        z.writestr("2.png", b"img-root")
        z.writestr("notes.txt", b"hello")
        z.writestr("textless/2.png", b"img-alt")     # same name, different folder
        z.writestr("textless/c.webm", b"vid2")
        z.writestr("assets/model.obj", b"obj")
        z.writestr("assets/sub/data.bin", b"bin")

    r = PawchiveRunner(workers=2, extract=True)
    r._destination = dest
    r._service = "patreon"
    r._archive = Archive(os.path.join(dest, "arc.db"))
    entry = entry_key("p1", 1)
    r._archive.record(entry, "p1", os.path.basename(zpath), "archive", "2026")
    job = {"dt": datetime(2026, 5, 4), "media_kind": "archive", "post_id": "p1",
           "name": "MyPack.zip", "entry": entry, "post_title": "My Pack"}

    r._extract_archive(zpath, job, entry)

    def has(p):
        return os.path.isfile(os.path.join(dest, p))
    check("root video -> year", has("2026/2026.05.04 - Patreon - b.mp4"))
    check("root image -> Images/year", has("Images/2026/2026.05.04 - Patreon - 2.png"))
    check("subfolder video foldered name", has("2026/2026.05.04 - Patreon - textless - c.webm"))
    check("subfolder image foldered name",
          has("Images/2026/2026.05.04 - Patreon - textless - 2.png"))
    stem = "2026/2026.05.04 - Patreon - MyPack"
    check("leftover txt in archive folder", has(f"{stem}/notes.txt"))
    check("leftover asset structure preserved", has(f"{stem}/assets/sub/data.bin"))
    check("leftover model preserved", has(f"{stem}/assets/model.obj"))
    check("no media left in archive folder", not has(f"{stem}/b.mp4")
          and not os.path.isdir(os.path.join(dest, stem, "textless")))
    # This pack had leftovers → the original archive is MOVED into the leftover folder
    # (not deleted), so the project stays complete.
    check("archive moved into leftover folder (not deleted)",
          not os.path.isfile(zpath) and has(f"{stem}/2026.05.04 - Patreon - MyPack.zip"))
    check("entry marked extracted", r._archive.is_extracted(entry))
    check("job_present true after extract (zip moved)", r._job_present(job))

    # Idempotent: extracting again is a no-op, _process_media skips.
    r._extract_archive(zpath, job, entry)
    r.skipped_count = 0
    r._process_media(job)
    check("re-run skips extracted archive", r.skipped_count == 1)

    # Pure-media pack: no leftovers → the redundant archive IS deleted.
    z2 = os.path.join(dest, "2026", "2026.05.04 - Patreon - MediaOnly.zip")
    with zipfile.ZipFile(z2, "w") as z:
        z.writestr("only.png", b"img")
        z.writestr("clip.mp4", b"vid")
    e2 = entry_key("p2", 1)
    r._archive.record(e2, "p2", os.path.basename(z2), "archive", "2026")
    job2 = dict(job, post_id="p2", name="MediaOnly.zip", entry=e2, post_title="Media Only")
    r._extract_archive(z2, job2, e2)
    check("pure-media archive deleted", not os.path.isfile(z2))
    check("pure-media has no leftover folder",
          not os.path.isdir(os.path.join(dest, "2026", "2026.05.04 - Patreon - MediaOnly")))
    r._archive.close()


def t_resolved_and_skip():
    """Item 2: runner saves don't clobber user 'resolved' flags. Item 6: skip &
    remember marks a file so it's never re-fetched."""
    from datetime import datetime
    from backend.pawchive_runner import PawchiveRunner
    from backend.pawchive_archive import Archive, entry_key
    from backend.pawchive_links import PawchiveLinks, make_key

    # ── resolved survives a stale runner-side save ──
    dest = tempfile.mkdtemp(prefix="pawres_")
    jpath = os.path.join(dest, "_pawchive_links.json")
    url = "https://mega.nz/x"
    post = {"post_id": "p1", "title": "T", "dt": None, "service": "patreon",
            "external_links": [{"url": url, "host": "mega.nz", "kind": "manual"}]}
    runner_links = PawchiveLinks(jpath)          # long-lived (like the runner holds)
    runner_links.autosave = False
    runner_links.upsert_post(post, {})
    runner_links.save()
    # user checks it off via a separate instance (like api.py)
    api_links = PawchiveLinks(jpath)
    key = make_key("p1", url)
    api_links.mark_resolved(key)
    # stale runner save WITHOUT the merge would wipe it; with reload it survives
    runner_links.reload_resolved_from_disk()
    runner_links.save()
    reloaded = PawchiveLinks(jpath)
    survived = reloaded.data["posts"]["p1"]["links"][url].get("resolved") is True
    check("resolved flag survives runner save", survived)

    # ── skip & remember ──
    r = PawchiveRunner(workers=2)
    r._archive = Archive(os.path.join(dest, "sk.db"))
    r._destination = dest
    entry = entry_key("p9", 3)
    job = {"dt": datetime(2026, 1, 2), "media_kind": "video", "post_id": "p9",
           "name": "big.mp4", "entry": entry, "url": "http://x/big.mp4"}
    r._mark_skipped(job, entry, "big.mp4")
    check("is_skipped after mark", r._archive.is_skipped(entry))
    check("skipped job counts present", r._job_present(job))
    r.skipped_count = 0
    r._process_media(job)
    check("skipped file not re-downloaded", r.skipped_count == 1)
    # request_skip registers the entry for the live loop
    r.request_skip("some_entry")
    check("request_skip registers", r._skip_requested("some_entry"))
    r._archive.close()


def main():
    print("Running pawchive offline tests...")
    for t in (t_urls, t_parse_media, t_links_classify, t_filenames, t_manifest_merge,
              t_extract, t_resolved_and_skip):
        try:
            t()
        except Exception as e:
            check(t.__name__, False, detail=f"exception: {e}")
    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed.")
    sys.exit(0 if passed == len(_results) else 1)


if __name__ == "__main__":
    main()
