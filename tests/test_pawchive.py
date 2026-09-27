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
    # A manual host with a media-file path is MANUAL (share/landing page, not the raw
    # file) — must NOT be auto-grabbed as 'direct'. Host check beats the extension.
    check("dropbox .mp4 -> manual", ps._classify_link(
        "https://www.dropbox.com/scl/fi/x/Anim.mp4?rlkey=y&dl=0") == "manual")
    check("mega .zip -> manual", ps._classify_link("https://mega.nz/file/abc#k.zip") == "manual")
    check("catbox .mp4 -> direct", ps._classify_link("https://files.catbox.moe/x.mp4") == "direct")
    check("unknown host .mp4 -> direct", ps._classify_link("https://example.com/p/clip.mp4") == "direct")


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
    r._write_root = dest   # run() sets this; a 'full' run stages into the library itself
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


def t_extract_review():
    """Extract-into-library off: a pack unpacks into <dest>/_extracted/ laid out like
    the library (year / Images/year / year/<archive> leftovers), never touching the
    real year folders, and stays 'handled' once its files are sorted or deleted."""
    import zipfile
    from datetime import datetime
    from backend.pawchive_runner import PawchiveRunner, EXTRACTED_DIRNAME
    from backend.pawchive_archive import Archive, entry_key

    dest = tempfile.mkdtemp(prefix="pawxr_")
    # A same-named file already in the real year folder: the review copy must not
    # take a name that would clash when dragged into place.
    os.makedirs(os.path.join(dest, "2026"))
    open(os.path.join(dest, "2026", "2026.05.04 - Patreon - b.mp4"), "wb").close()

    def make(name, members):
        zp = os.path.join(dest, "2026", name)
        with zipfile.ZipFile(zp, "w") as z:
            for m, data in members.items():
                z.writestr(m, data)
        return zp

    r = PawchiveRunner(workers=1, extract=True, extract_into_library=False)
    r._destination = r._write_root = dest
    r._service = "patreon"
    r._archive = Archive(os.path.join(dest, "arc.db"))
    zpath = make("2026.05.04 - Patreon - Pack.zip",
                 {"b.mp4": b"vid", "2.png": b"img", "notes.txt": b"t"})
    entry = entry_key("p1", 1)
    r._archive.record(entry, "p1", os.path.basename(zpath), "archive", "2026")
    job = {"dt": datetime(2026, 5, 4), "media_kind": "archive", "post_id": "p1",
           "name": "Pack.zip", "entry": entry, "post_title": "Pack"}
    r._extract_archive(zpath, job, entry)

    def has(p):
        return os.path.isfile(os.path.join(dest, p))
    x = EXTRACTED_DIRNAME
    check("review: video -> _extracted/year (no clash with library copy)",
          has(f"{x}/2026/2026.05.04 - Patreon - b_1.mp4"))
    check("review: image -> _extracted/Images/year",
          has(f"{x}/Images/2026/2026.05.04 - Patreon - 2.png"))
    check("review: leftovers + archive under _extracted/year/<archive>",
          has(f"{x}/2026/2026.05.04 - Patreon - Pack/notes.txt")
          and has(f"{x}/2026/2026.05.04 - Patreon - Pack/2026.05.04 - Patreon - Pack.zip"))
    check("review: real year folders untouched",
          sorted(os.listdir(os.path.join(dest, "2026"))) == ["2026.05.04 - Patreon - b.mp4"]
          and not os.path.isdir(os.path.join(dest, "Images")))
    check("review: entry marked extracted", r._archive.is_extracted(entry))

    # Dragging the image into the real year folder still counts as present.
    os.makedirs(os.path.join(dest, "Images", "2026"))
    os.replace(os.path.join(dest, x, "Images", "2026", "2026.05.04 - Patreon - 2.png"),
               os.path.join(dest, "Images", "2026", "2026.05.04 - Patreon - 2.png"))
    check("review: moved-into-place output found",
          r._present_outputs([f"{x}/Images/2026/2026.05.04 - Patreon - 2.png"]))

    # Deleting the whole review folder is the user's call: nothing is re-fetched.
    import shutil
    shutil.rmtree(os.path.join(dest, x))
    fetched = []
    r._download_stream = lambda *a, **k: fetched.append(a) or True
    r.skipped_count = 0
    r._process_media(job)
    check("review: deleted contents never re-fetched",
          not fetched and r.skipped_count == 1 and r._job_present(job))

    # Into-library mode is unchanged: contents gone -> fetched again.
    r2 = PawchiveRunner(workers=1, extract=True)
    r2._destination = r2._write_root = dest
    r2._service = "patreon"
    r2._archive = r._archive
    z2 = make("2026.05.04 - Patreon - Lib.zip", {"only.png": b"i"})
    e2 = entry_key("p2", 1)
    r2._archive.record(e2, "p2", os.path.basename(z2), "archive", "2026")
    j2 = dict(job, post_id="p2", name="Lib.zip", entry=e2, post_title="Lib")
    r2._extract_archive(z2, j2, e2)
    check("library mode: image in real Images/year",
          has("Images/2026/2026.05.04 - Patreon - only.png"))
    os.remove(os.path.join(dest, "Images", "2026", "2026.05.04 - Patreon - only.png"))
    check("library mode: deleted contents -> not present", not r2._job_present(j2))

    # Password-protected: never unpacked or left in the year folders, whatever the
    # setting — moved whole to _extracted/Password Protected/ and marked handled.
    from backend.pawchive_runner import PASSWORD_DIRNAME
    for runner, pid in ((r2, "p3"), (r, "p4")):
        zp = os.path.join(dest, "2026", f"2026.05.04 - Patreon - Locked {pid}.zip")
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("secret.png", b"x")
        # zipfile won't write an encrypted member, so set the encryption flag (all
        # detection reads) in the local + central headers by hand.
        raw = bytearray(open(zp, "rb").read())
        for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            i = raw.find(sig)
            raw[i + off] |= 0x1
        open(zp, "wb").write(bytes(raw))
        ep = entry_key(pid, 1)
        runner._archive.record(ep, pid, os.path.basename(zp), "archive", "2026")
        jp = dict(job, post_id=pid, name="Locked.zip", entry=ep, post_title="Locked")
        runner._extract_archive(zp, jp, ep)
        mode = "library" if runner is r2 else "review"
        check(f"password ({mode}): archive moved to Password Protected",
              not os.path.exists(zp)
              and has(f"{x}/{PASSWORD_DIRNAME}/{os.path.basename(zp)}"))
        check(f"password ({mode}): nothing unpacked",
              not has("Images/2026/2026.05.04 - Patreon - secret.png")
              and not has(f"{x}/Images/2026/2026.05.04 - Patreon - secret.png"))
        check(f"password ({mode}): handled, not re-fetched",
              runner._archive.is_extracted(ep) and runner._job_present(jp))
    r._archive.close()

    from backend.creator_runner import extract_into_folders
    check("creator default: extract into artist folders",
          extract_into_folders({}) and extract_into_folders({"extract_into_folders": True}))
    check("creator opt-out: _extracted review folder",
          not extract_into_folders({"extract_into_folders": False}))


def t_password_detect():
    """Real encrypted archives are recognised: rar with encrypted files (-p) and
    encrypted headers (-hp) via UnRAR, zip via its member flag. The rar half needs
    WinRAR's Rar.exe to build the fixtures and is skipped without it."""
    import subprocess
    import zipfile
    from backend import pawchive_extract as px
    d = tempfile.mkdtemp(prefix="pawpw_")
    src = os.path.join(d, "a.txt")
    with open(src, "w") as f:
        f.write("hi")
    plain = os.path.join(d, "plain.zip")
    with zipfile.ZipFile(plain, "w") as z:
        z.write(src, "a.txt")
    check("pw: plain zip not protected", px.is_password_protected(plain) is False)
    check("pw: plain zip extracts", px.extract_to_temp(plain, tempfile.mkdtemp()) is True)
    rar = next((p for p in (r"C:\Program Files\WinRAR\Rar.exe",
                            r"C:\Program Files (x86)\WinRAR\Rar.exe") if os.path.isfile(p)), None)
    if not rar or not px.unrar_available():
        print("  (skip: WinRAR not installed — rar password checks)")
        return
    for flag, label in (("-psecret", "encrypted files"), ("-hpsecret", "encrypted headers"),
                        (None, "plain")):
        out = os.path.join(d, f"{label.replace(' ', '_')}.rar")
        subprocess.run([rar, "a", "-idq", "-ep"] + ([flag] if flag else []) + [out, src],
                       check=True, stdin=subprocess.DEVNULL)
        want = flag is not None
        check(f"pw: rar {label} detected={want}", px.is_password_protected(out) is want)
        res = px.extract_to_temp(out, tempfile.mkdtemp())
        check(f"pw: rar {label} extract -> {'PASSWORD' if want else 'True'}",
              res == (px.PASSWORD if want else True), res)
        if want:
            # The extraction path on its own (no pre-check) must also say PASSWORD,
            # promptly, instead of waiting on a prompt.
            check(f"pw: unrar -p- reports {label}",
                  px._extract_rar(out, tempfile.mkdtemp()) == px.PASSWORD)


def t_extract_restore():
    """An 'extracted' archive whose contents are gone from disk is fetched again
    (a re-added creator got every image back but none of its zips); contents
    still on disk — including a pack unpacked by hand — are never duplicated."""
    import io
    import tarfile
    import zipfile
    from datetime import datetime
    from backend.pawchive_runner import PawchiveRunner
    from backend.pawchive_archive import Archive, entry_key
    from backend import pawchive_extract as px

    dest = tempfile.mkdtemp(prefix="pawr_")
    r = PawchiveRunner(workers=1, extract=True)
    r._destination = r._write_root = dest
    r._service = "fanbox"
    r._archive = Archive(os.path.join(dest, "arc.db"))
    fetched = []
    r._download_stream = lambda url, path, entry, job, fn, is_video, bounded=False: \
        fetched.append((fn, bool(job.get("verify_extracted"))))
    dt = datetime(2023, 7, 28)

    def make_zip(name, members):
        p = os.path.join(dest, "2023", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with zipfile.ZipFile(p, "w") as z:
            for m in members:
                z.writestr(m, b"x")
        return p

    def job_for(pid, fname):
        e = entry_key(pid, 2)
        r._archive.record(e, pid, fname, "archive", "2023")
        return {"dt": dt, "media_kind": "archive", "post_id": pid, "name": fname,
                "entry": e, "url": f"https://file.example/{pid}.zip"}

    # 1. Recorded outputs: all gone → fetched again; one left → skipped.
    zname = "2023.07.28 - Fanbox - Req - 20230728.zip"
    j1 = job_for("p1", zname)
    r._extract_archive(make_zip(zname, ["20230728/a_1.jpg", "20230728/a_2.jpg"]), j1, j1["entry"])
    outs = r._archive.get_extracted_outputs(j1["entry"])
    check("extraction records its outputs", outs and len(outs) == 2, outs)
    for o in outs:
        os.remove(os.path.join(dest, o))
    r._process_media(j1)
    check("contents gone -> archive fetched again", fetched == [(zname, False)], fetched)
    check("reconcile sees it missing too", not r._job_present(j1))
    r._extract_archive(make_zip(zname, ["20230728/a_1.jpg", "20230728/a_2.jpg"]), j1, j1["entry"])
    os.remove(os.path.join(dest, r._archive.get_extracted_outputs(j1["entry"])[0]))
    fetched.clear(); r.skipped_count = 0
    r._process_media(j1)
    check("some contents left -> skipped (deliberate deletes stay deleted)",
          not fetched and r.skipped_count == 1)

    # 2. Legacy entry (extracted before outputs were tracked): listed remotely.
    real_list = px.list_zip_remote
    try:
        px.list_zip_remote = lambda sess, url, timeout=30: ["pack/b_1.jpg", "pack/b_2.jpg"]
        j2 = job_for("p2", "2023.07.28 - Fanbox - Other - pack.zip")
        r._archive.set_extracted(j2["entry"])
        fetched.clear()
        r._process_media(j2)
        check("legacy zip, contents absent -> fetched", len(fetched) == 1 and not fetched[0][1], fetched)
        # The same pack unpacked by hand, named without the folder part.
        os.makedirs(os.path.join(dest, "Images", "2023"), exist_ok=True)
        open(os.path.join(dest, "Images", "2023", "2023.07.28 - Fanbox - b_1.jpg"), "wb").close()
        r._archive.set_extracted(j2["entry"])
        fetched.clear(); r.skipped_count = 0
        r._process_media(j2)
        check("legacy zip unpacked by hand -> skipped", not fetched and r.skipped_count == 1)
        check("the match is recorded for next time",
              r._archive.get_extracted_outputs(j2["entry"])
              == [os.path.join("Images", "2023", "2023.07.28 - Fanbox - b_1.jpg")])
        px.list_zip_remote = lambda sess, url, timeout=30: None
        j3 = job_for("p3", "2023.07.28 - Fanbox - Unlistable - c.zip")
        r._archive.set_extracted(j3["entry"])
        fetched.clear()
        r._process_media(j3)
        check("unlistable -> fetched with verify flag", fetched and fetched[0][1], fetched)
        # The pack's video is also a separate attachment of the post, already
        # downloaded on its own: that alone must not count as "unpacked".
        px.list_zip_remote = lambda sess, url, timeout=30: ["m/m_1.jpg", "m/voice.mp4"]
        vid = "2023.07.28 - Fanbox - voice.mp4"
        r._archive.record(entry_key("p5", 1), "p5", vid, "video", "2023")
        open(os.path.join(dest, "2023", vid), "wb").close()
        j5 = job_for("p5", "2023.07.28 - Fanbox - Pack - m.zip")
        r._archive.set_extracted(j5["entry"])
        fetched.clear()
        r._process_media(j5)
        check("separately-downloaded attachment is no evidence -> fetched", len(fetched) == 1, fetched)
    finally:
        px.list_zip_remote = real_list

    # 3. Verify-on-extract: contents already there → nothing duplicated.
    open(os.path.join(dest, "Images", "2023", "2023.07.28 - Fanbox - d - d_1.jpg"), "wb").close()
    zv = make_zip("2023.07.28 - Fanbox - V - d.zip", ["d/d_1.jpg", "d/d_2.jpg"])
    jv = dict(job_for("p4", os.path.basename(zv)), verify_extracted=True)
    r._extract_archive(zv, jv, jv["entry"])
    imgs = sorted(os.listdir(os.path.join(dest, "Images", "2023")))
    check("verify: nothing re-placed", not any("d_2" in n for n in imgs), imgs)
    check("verify: archive discarded + marked", not os.path.exists(zv) and r._archive.is_extracted(jv["entry"]))
    r._archive.close()

    # 4. Remote listing over HTTP ranges (fake session serving a real zip's bytes).
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("dir/", b"")
        z.writestr("dir/v.mp4", b"0" * 5000)
    blob = buf.getvalue()

    class Resp:
        def __init__(self, code, content=b"", headers=None):
            self.status_code, self.content, self.headers = code, content, headers or {}

    class Sess:
        reads = 0
        def head(self, url, **k):
            return Resp(200, headers={"accept-ranges": "bytes", "content-length": str(len(blob))})
        def get(self, url, headers=None, **k):
            a, b = headers["Range"][6:].split("-")
            Sess.reads += 1
            return Resp(206, blob[int(a):int(b) + 1])
    check("list_zip_remote reads the directory", px.list_zip_remote(Sess(), "u") == ["dir/v.mp4"])
    check("list_zip_remote: no range support -> None",
          px.list_zip_remote(type("S", (), {"head": lambda s, u, **k: Resp(200, headers={})})(), "u") is None)

    # 5. bsdtar with a Japanese archive name (argv is ANSI on Windows).
    if __import__("shutil").which("tar"):
        tdir = tempfile.mkdtemp(prefix="pawt_")
        tpath = os.path.join(tdir, "2023.07.28 - Fanbox - リクエスト絵.tar")
        with tarfile.open(tpath, "w") as t:
            data = b"hello"
            ti = tarfile.TarInfo("sub/x.txt"); ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        out = os.path.join(tdir, "out"); os.makedirs(out)
        check("tar extracts a Japanese-named archive", px._extract_tar(tpath, out)
              and os.path.isfile(os.path.join(out, "sub", "x.txt")))


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
    r._write_root = dest
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


def t_resolved_ext_skipped():
    """A direct external link the user checked off in the URL tab must NOT be re-grabbed
    (same rule as a dismissed error); unchecked ones stay up for grabs, and skipping one
    doesn't shift the others' entry keys."""
    from backend import pawchive_runner as pr
    from backend.pawchive_runner import PawchiveRunner
    from backend.pawchive_links import PawchiveLinks, make_key

    d = tempfile.mkdtemp(prefix="pawext_")
    r = PawchiveRunner(workers=1, extract=False)
    r._service = "patreon"; r._user_id = "1"; r._mode = "full"
    r._archive = None; r._errors = None; r._session = None
    r._links = PawchiveLinks(os.path.join(d, "_pawchive_links.json"))
    r._links.autosave = False
    post = {"user": "1", "service": "patreon", "id": "pe", "title": "E",
            "published": "2026-01-01T00:00:00", "file": {}, "attachments": [],
            "detail_fetched": True, "has_full": True, "preview_state": "scraped",
            "content": ('<a href="https://files.catbox.moe/aaa.mp4">A</a>'
                        '<a href="https://files.catbox.moe/bbb.mp4">B</a>')}
    orig = pr.iter_posts
    pr.iter_posts = lambda *a, **k: iter([post])
    try:
        r._crawl(None)   # populate manifest
        r._links.mark_resolved(make_key("pe", "https://files.catbox.moe/aaa.mp4"))
        _mj, ext_jobs, _p = r._crawl(None)
    finally:
        pr.iter_posts = orig
    urls = {j["url"] for j in ext_jobs}
    check("resolved direct link skipped", "https://files.catbox.moe/aaa.mp4" not in urls)
    check("unchecked direct link kept", "https://files.catbox.moe/bbb.mp4" in urls)
    b = [j for j in ext_jobs if j["url"].endswith("bbb.mp4")]
    check("skipping a resolved link keeps others' entry keys stable",
          bool(b) and b[0]["entry"].endswith("_ext_2"))


def t_dismissed_not_refetched():
    """A checked-off (dismissed) error must NOT be re-fetched by a crawl, and must
    count as 'present' so reconcile leaves it alone — the user is done with it."""
    from datetime import datetime
    from backend.pawchive_runner import PawchiveRunner
    from backend.pawchive_archive import Archive, entry_key

    dest = tempfile.mkdtemp(prefix="pawdis_")
    r = PawchiveRunner(workers=2, extract=False)
    r._archive = Archive(os.path.join(dest, "d.db"))
    r._destination = dest
    r._write_root = dest
    r._service = "patreon"
    entry = entry_key("pd", 1)
    r._dismissed_ids = {entry}     # as run() would load from the FailureStore

    calls = []
    r._download_stream = lambda *a, **k: (calls.append(a) or True)
    job = {"jobkind": "media", "dt": datetime(2026, 1, 2), "media_kind": "video",
           "post_id": "pd", "name": "vid.mp4", "entry": entry,
           "url": "http://x/vid.mp4", "has_full": True}
    r._process_media(job)
    check("dismissed error not re-downloaded", len(calls) == 0)
    check("dismissed counted as skip", r.skipped_count == 1)
    check("dismissed job counts as present (no reconcile churn)", r._job_present(job))
    r._archive.close()


def t_preview_state():
    """preview_state (not has_full) is the availability signal. parse_post surfaces it;
    a 'scraped' post's files download even with has_full False; the crawl builds NO
    media jobs for a 'pending' post (its files 404) but still records the post."""
    from datetime import datetime
    from backend import pawchive_runner as pr
    from backend.pawchive_runner import PawchiveRunner
    from backend.pawchive_archive import Archive, entry_key
    from backend.download_errors import FailureStore

    # parse_post surfaces preview_state / origin (default "" when absent).
    p = ps.parse_post({**POST_VIDEO, "preview_state": "Scraped", "origin": "import"})
    check("parse preview_state lowercased", p["preview_state"] == "scraped")
    check("parse origin lowercased", p["origin"] == "import")
    check("parse preview_state default empty", ps.parse_post(POST_VIDEO)["preview_state"] == "")

    # A 'scraped' post with has_full False still gets downloaded (Buckethead case).
    dest = tempfile.mkdtemp(prefix="pawps_")
    r = PawchiveRunner(workers=2, extract=False)
    r._archive = Archive(os.path.join(dest, "ps.db"))
    r._errors = FailureStore(os.path.join(dest, "ps_err.db"))
    r._destination = dest; r._write_root = dest
    r._service = "patreon"; r._user_id = "1"
    calls = []
    r._download_stream = lambda *a, **k: (calls.append(a) or True)
    job = {"jobkind": "media", "dt": datetime(2026, 1, 2), "media_kind": "video",
           "post_id": "pf", "name": "vid.mp4", "entry": entry_key("pf", 1),
           "url": "http://x/vid.mp4?f=vid.mp4", "preview_state": "scraped"}
    r._process_media(job)
    check("scraped(has_full False) file is downloaded", len(calls) == 1)
    check("scraped file not pre-recorded as failure", r._errors.count() == 0)
    r._errors.close(); r._archive.close()

    # The crawl builds NO media jobs for 'pending' posts but LOGS each as a not_imported
    # error — except entries already known (shown) or checked off (dismissed).
    dest2 = tempfile.mkdtemp(prefix="pawcrawl_")
    r2 = PawchiveRunner(workers=1, extract=False)
    r2._service = "patreon"; r2._user_id = "1"; r2._mode = "full"
    r2._archive = None; r2._links = None; r2._session = None
    r2._destination = dest2; r2._write_root = dest2
    r2._errors = FailureStore(os.path.join(dest2, "e.db"))
    # p2's entry is already checked off — the crawl must NOT re-log it.
    r2._errors.record_failure("pawchive_p2_1", reason="not_imported")
    r2._errors.dismiss("pawchive_p2_1")
    r2._known_failure_entries = {"pawchive_p2_1"}
    r2._dismissed_ids = {"pawchive_p2_1"}
    base = {"user": "1", "service": "patreon", "published": "2026-01-01T00:00:00",
            "attachments": [], "detail_fetched": True, "has_full": False}
    scraped = {**base, "id": "s1", "title": "S", "file": {"name": "a.jpg", "path": "/aa/bb/x.jpg"},
               "preview_state": "scraped"}
    pending = {**base, "id": "p1", "title": "P", "file": {"name": "b.jpg", "path": "/cc/dd/y.jpg"},
               "preview_state": "pending"}
    pending2 = {**base, "id": "p2", "title": "P2", "file": {"name": "c.jpg", "path": "/ee/ff/z.jpg"},
                "preview_state": "pending"}
    orig = pr.iter_posts
    pr.iter_posts = lambda *a, **k: iter([scraped, pending, pending2])
    try:
        media_jobs, _ext, posts = r2._crawl(None)
    finally:
        pr.iter_posts = orig
    pids = {j["post_id"] for j in media_jobs}
    check("scraped post yields a media job", "s1" in pids)
    check("pending post yields NO media job", "p1" not in pids and "p2" not in pids)
    check("all posts recorded in posts", set(posts) == {"s1", "p1", "p2"})
    open_fails = r2._errors.list_failures(state="failed")
    entries = {f["entry"] for f in open_fails}
    check("new pending post logged as not_imported error", "pawchive_p1_1" in entries)
    check("checked-off pending post NOT re-logged (stays dismissed)",
          "pawchive_p2_1" not in entries)
    r2._errors.close()


def t_deferred_attachment():
    """A 'scraped' post can carry path-less ('deferred') attachments pawchive hasn't
    imported the bytes for yet (the SlipperyT case). parse_post surfaces them in
    deferred_media (NOT media); the crawl logs each as a 'deferred' error under the
    media index it WILL occupy once imported; 'latest' does not skip a seen post that
    still has one; and once the file lands it downloads under the SAME entry key."""
    from backend import pawchive_runner as pr
    from backend.pawchive_runner import PawchiveRunner
    from backend.pawchive_archive import Archive, entry_key
    from backend.download_errors import FailureStore

    # parse_post: a path-less attachment goes to deferred_media, never media.
    raw = {"id": "d1", "user": "1", "service": "patreon",
           "published": "2026-01-01T00:00:00", "file": {},
           "attachments": [{"name": "A.jpg", "path": "/aa/bb/x.jpg"},
                           {"name": "GIGACOLLAGE.jpg", "deferred": True}],
           "detail_fetched": True, "has_full": False, "preview_state": "scraped"}
    p = ps.parse_post(raw)
    check("deferred attachment kept OUT of media",
          [m["name"] for m in p["media"]] == ["A.jpg"])
    check("deferred attachment surfaced in deferred_media",
          [m["name"] for m in p["deferred_media"]] == ["GIGACOLLAGE.jpg"])

    dest = tempfile.mkdtemp(prefix="pawdef_")
    r = PawchiveRunner(workers=1, extract=False)
    r._service = "patreon"; r._user_id = "1"; r._mode = "full"
    r._archive = Archive(os.path.join(dest, "d.db")); r._links = None; r._session = None
    r._destination = dest; r._write_root = dest
    r._errors = FailureStore(os.path.join(dest, "e.db"))
    orig = pr.iter_posts
    pr.iter_posts = lambda *a, **k: iter([raw])
    try:
        media_jobs, _ext, _posts = r._crawl(None)
    finally:
        pr.iter_posts = orig
    job_entries = {j["entry"] for j in media_jobs}
    check("imported file yields a media job", entry_key("d1", 1) in job_entries)
    check("deferred file yields NO media job (nothing to fetch yet)",
          entry_key("d1", 2) not in job_entries)
    fails = {f["entry"]: f for f in r._errors.list_failures(state="failed")}
    check("deferred file LOGGED as error at trailing index len(media)+1",
          entry_key("d1", 2) in fails)
    check("deferred error carries reason 'deferred'",
          fails.get(entry_key("d1", 2), {}).get("reason") == "deferred")

    # 'latest' must NOT skip a partially-downloaded (seen) post that still has a
    # deferred attachment — else its file would never be picked up once it lands.
    r._mode = "latest"
    r._archive.record(entry_key("d1", 1), "d1", "A.jpg", "image", "2026")  # post now 'seen'
    r._known_failure_entries = {entry_key("d1", 2)}   # as run() would load from the store
    pr.iter_posts = lambda *a, **k: iter([raw])
    try:
        _mj2, _e2, posts2 = r._crawl(None)
    finally:
        pr.iter_posts = orig
    check("latest does NOT skip a seen post with a deferred file", "d1" in posts2)

    imported = {**raw, "attachments": [{"name": "A.jpg", "path": "/aa/bb/x.jpg"},
                                       {"name": "GIGACOLLAGE.jpg", "path": "/cc/dd/g.jpg"}]}
    # (a) Entry-key alignment: once a path appears, the crawl assigns the imported file
    #     the SAME entry key the deferred error was logged under (so the download clears
    #     it). Shown here via a 'full' re-crawl (no seen-post skip in the way).
    r._mode = "full"
    pr.iter_posts = lambda *a, **k: iter([imported])
    try:
        mj3, _e3, _p3 = r._crawl(None)
    finally:
        pr.iter_posts = orig
    check("imported deferred file gets the SAME entry key it was logged under",
          entry_key("d1", 2) in {j["entry"] for j in mj3})

    # (b) The 'latest' seen-post route: on the import run the crawl skips the (now
    #     fully-imported, seen) post, so the end-of-run recovery pass grabs it —
    #     _recover_one matches it by index and downloads it, NOT marking it 'gone'.
    r._read_post = lambda pid: imported
    calls = []
    r._download_stream = lambda *a, **k: (calls.append(a) or True)
    ok, gone = r._recover_one(fails[entry_key("d1", 2)])
    check("recovery downloads the now-imported deferred file (not marked gone)",
          ok and not gone and len(calls) == 1)
    r._errors.close(); r._archive.close()


def t_deferred_dismissed():
    """Checking off (dismissing) a deferred error is sticky, mirroring pending/normal
    errors: while the file is still missing the crawl won't re-log it, and if pawchive
    later re-imports it the runner won't re-download it — the user said 'I got it
    elsewhere, stop tracking it.'"""
    from backend import pawchive_runner as pr
    from backend.pawchive_runner import PawchiveRunner
    from backend.pawchive_archive import Archive, entry_key
    from backend.download_errors import FailureStore

    dest = tempfile.mkdtemp(prefix="pawdd_")
    defkey = entry_key("x1", 1)

    def fresh():
        r = PawchiveRunner(workers=1, extract=False)
        r._service = "patreon"; r._user_id = "1"; r._mode = "latest"
        r._archive = Archive(os.path.join(dest, "a.db")); r._links = None; r._session = None
        r._destination = dest; r._write_root = dest
        r._errors = FailureStore(os.path.join(dest, "e.db"))
        # as run() loads from the store: known (any state) + the dismissed subset.
        r._known_failure_entries = {defkey}
        r._dismissed_ids = {defkey}
        return r

    # Seed a deferred error the user has checked off.
    seed = FailureStore(os.path.join(dest, "e.db"))
    seed.record_failure(defkey, reason="deferred", post_id="x1")
    seed.dismiss(defkey)
    seed.close()

    # (a) File STILL deferred → crawl must not re-log / un-dismiss it.
    still = {"id": "x1", "user": "1", "service": "patreon",
             "published": "2026-01-01T00:00:00", "file": {},
             "attachments": [{"name": "g.jpg", "deferred": True}],
             "detail_fetched": True, "has_full": False, "preview_state": "scraped"}
    r = fresh()
    orig = pr.iter_posts
    pr.iter_posts = lambda *a, **k: iter([still])
    try:
        r._crawl(None)
    finally:
        pr.iter_posts = orig
    states = {f["entry"]: f["state"] for f in r._errors.list_failures()}
    check("dismissed deferred error stays dismissed (not re-logged)",
          states.get(defkey) == "dismissed")
    r._errors.close(); r._archive.close()

    # (b) File since IMPORTED → crawl builds a job, but _process_media skips it.
    imported = {**still, "file": {"name": "g.jpg", "path": "/a/b/g.jpg"},
                "attachments": [], "has_full": True}
    r = fresh()
    calls = []
    r._download_stream = lambda *a, **k: (calls.append(a) or True)
    pr.iter_posts = lambda *a, **k: iter([imported])
    try:
        media_jobs, _e, _p = r._crawl(None)
    finally:
        pr.iter_posts = orig
    for j in media_jobs:
        r._process_media(j)
    check("dismissed deferred file NOT re-downloaded after re-import", len(calls) == 0)
    r._errors.close(); r._archive.close()


def t_zip_encoding():
    """Legacy Japanese zips (CP932 names, UTF-8 flag unset) must extract to correct
    Japanese, not the CP437 mojibake zipfile produces by default; the repair helper
    reverses on-disk mojibake and leaves everything else alone."""
    import zipfile
    from backend import pawchive_extract as px

    real = "秋菜ちゃん【本編】"
    moji = real.encode("cp932").decode("cp437")   # simulate zipfile's CP437 decode
    check("repair reverses CP932->CP437 mojibake", px.repair_mojibake_name(moji) == real,
          px.repair_mojibake_name(moji))
    check("repair leaves ASCII alone", px.repair_mojibake_name("readme.txt") is None)
    check("repair leaves proper Japanese alone", px.repair_mojibake_name(real) is None)
    check("repair leaves Latin accents alone", px.repair_mojibake_name("café.txt") is None)
    check("repair idempotent (already-fixed name)",
          px.repair_mojibake_name(px.repair_mojibake_name(moji)) is None)

    # End-to-end: build a zip whose member is CP932 bytes with the UTF-8 flag CLEARED
    # (exactly the real-world condition), plus an ASCII member and a proper-UTF-8 one.
    class CP932Info(zipfile.ZipInfo):
        def _encodeFilenameFlags(self):
            return self.filename.encode("cp932"), self.flag_bits & ~0x800

    d = tempfile.mkdtemp(prefix="pawzenc_")
    zpath = os.path.join(d, "pack.zip")
    jp_name = "秋菜ちゃん【本編】_20260306.mp4"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr(CP932Info(jp_name), b"vid")
        z.writestr("readme_ascii.txt", b"hi")
        z.writestr(zipfile.ZipInfo("ゆかりん.txt"), b"u")   # proper UTF-8 (flag set)
    with zipfile.ZipFile(zpath) as z:
        check("zipfile alone mis-decodes the name (the bug)", jp_name not in z.namelist())

    out = os.path.join(d, "out")
    os.makedirs(out)
    px._extract_zip(zpath, out)
    landed = {f for _r, _dirs, fs in os.walk(out) for f in fs}
    check("extract recovers Japanese member name", jp_name in landed, sorted(landed))
    check("extract leaves ASCII member intact", "readme_ascii.txt" in landed)
    check("extract leaves proper-UTF-8 member intact", "ゆかりん.txt" in landed)


def main():
    print("Running pawchive offline tests...")
    for t in (t_urls, t_parse_media, t_links_classify, t_filenames, t_manifest_merge,
              t_extract, t_extract_review, t_password_detect, t_extract_restore, t_resolved_and_skip, t_preview_state, t_zip_encoding,
              t_resolved_ext_skipped, t_dismissed_not_refetched,
              t_deferred_attachment, t_deferred_dismissed):
        try:
            t()
        except Exception as e:
            check(t.__name__, False, detail=f"exception: {e}")
    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed.")
    sys.exit(0 if passed == len(_results) else 1)


if __name__ == "__main__":
    main()
