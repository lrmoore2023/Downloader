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
          == "https://file.pawchive.st/data/22/a3/b.mp4?f=teaser.mp4")

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


def main():
    print("Running pawchive offline tests...")
    for t in (t_urls, t_parse_media, t_links_classify, t_filenames, t_manifest_merge):
        try:
            t()
        except Exception as e:
            check(t.__name__, False, detail=f"exception: {e}")
    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed.")
    sys.exit(0 if passed == len(_results) else 1)


if __name__ == "__main__":
    main()
