"""Tests for the Duplicates feature: perceptual hashing (czkawka parity),
the signature cache, and scan -> apply -> revert across the three workflows."""
import os
import random
import sqlite3
import subprocess

import numpy as np
import pytest
from PIL import Image, ImageDraw

from backend import dedupe, dedupe_apply, media_hash as mh
from backend.media_sig_cache import SigCache
from backend.coomerfans_archive import Archive, entry_key


def _photo(seed, size=(640, 480)):
    rnd = random.Random(seed)
    im = Image.new("RGB", (640, 480))
    d = ImageDraw.Draw(im)
    for _ in range(60):
        x, y = rnd.randint(0, 600), rnd.randint(0, 440)
        d.rectangle([x, y, x + rnd.randint(20, 120), y + rnd.randint(20, 120)],
                    fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
    return im.resize(size)


def _detailed(seed, size):
    """High-entropy image so byte size scales with resolution (like a real photo)."""
    rnd = np.random.default_rng(seed)
    im = Image.new("RGB", (640, 480))
    d = ImageDraw.Draw(im)
    for _ in range(60):
        x, y = int(rnd.integers(0, 600)), int(rnd.integers(0, 440))
        d.rectangle([x, y, x + int(rnd.integers(20, 120)), y + int(rnd.integers(20, 120))],
                    fill=tuple(int(v) for v in rnd.integers(0, 255, 3)))
    arr = np.asarray(im).astype(np.int16) + rnd.integers(-30, 30, (480, 640, 3))
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8")).resize(size)


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


# ── perceptual hashing (czkawka Mean@16) ──────────────────────────────
def test_image_distances_match_czkawka_bands(tmp_path):
    orig = tmp_path / "o.png"
    im = _photo(1)
    im.save(orig)
    resized = tmp_path / "r.jpg"
    im.resize((320, 240)).save(resized, quality=80)
    unrelated = tmp_path / "u.png"
    _photo(999).save(unrelated)

    so = mh.image_signature(str(orig))
    assert len(so["ahash"]) == 64 and len(so["phash"]) == 64      # 256-bit
    assert mh.image_distance(so, mh.image_signature(str(resized))) <= 15   # <= Medium
    assert mh.image_distance(so, mh.image_signature(str(unrelated))) > 30  # unrelated


def test_orientation_matching(tmp_path):
    p = tmp_path / "o.png"
    im = _photo(2)
    im.save(p)
    rot = tmp_path / "r.png"
    im.rotate(90, expand=True).save(rot)
    plain = mh.image_signature(str(p))
    withori = mh.image_signature(str(p), orientations=True)
    srot = mh.image_signature(str(rot))
    assert mh.image_distance(plain, srot) > 15            # no match without orientations
    assert mh.image_distance(withori, srot) <= 5          # matches with orientations


def test_sig_cache_hit_miss(tmp_path):
    p = tmp_path / "o.png"
    _photo(3).save(p)
    cache = SigCache(str(tmp_path / "c.db"))
    sig = mh.image_signature(str(p))
    st = os.stat(p)
    cache.put(str(p), st.st_mtime_ns, st.st_size, "image", sig, sha256="abc")
    assert cache.get(str(p), st.st_mtime_ns, st.st_size)["ahash"] == sig["ahash"]
    assert cache.get(str(p), st.st_mtime_ns, st.st_size + 1) is None   # size change
    assert cache.get(str(p), st.st_mtime_ns + 1, st.st_size) is None   # mtime change
    assert cache.find_by_content("abc", st.st_size)["ahash"] == sig["ahash"]
    cache.close()


# ── within-creator scan -> apply -> revert ────────────────────────────
def test_within_creator_scan_apply_revert(tmp_path):
    dest = tmp_path / "creator"
    img = dest / "Images" / "2026"
    img.mkdir(parents=True)
    base = _photo(4)
    base.save(img / "2026.01.01 - OF - 100_1.png")
    base.save(img / "2026.01.02 - OF - 101_1.png")            # exact dup
    _photo(5).save(img / "2026.01.03 - OF - 102_1.png")       # unique
    cache = SigCache(str(tmp_path / "c.db"))

    res = dedupe.scan_within(str(dest), cache, None, {"image_distance": 10})
    assert res["reachable"] and len(res["groups"]) == 1
    g = res["groups"][0]
    assert len(g["items"]) == 2

    journal = str(tmp_path / "j.jsonl")
    out = dedupe_apply.apply(
        [{"group_id": g["id"], "action": "keep_one", "keep_path": g["best_path"]}],
        {g["id"]: g}, {"mode": "within", "dest": str(dest)}, journal)
    assert out["deleted"] == 1
    assert len(os.listdir(img)) == 2
    dedupe_apply.revert(journal)
    assert len(os.listdir(img)) == 3
    cache.close()


def test_keep_many_and_delete_all(tmp_path):
    dest = tmp_path / "creator"
    img = dest / "Images" / "2026"
    img.mkdir(parents=True)
    base = _photo(4)
    for i in range(3):                                    # 3 identical copies
        base.save(img / f"2026.01.0{i + 1} - OF - 10{i}_1.png")
    cache = SigCache(str(tmp_path / "c.db"))
    res = dedupe.scan_within(str(dest), cache, None, {"image_distance": 10})
    g = res["groups"][0]
    paths = [it["path"] for it in g["items"]]

    # keep_many: keep two of the three -> one retired
    j1 = str(tmp_path / "j1.jsonl")
    out = dedupe_apply.apply(
        [{"group_id": g["id"], "action": "keep_many", "keep_paths": paths[:2]}],
        {g["id"]: g}, {"mode": "within", "dest": str(dest)}, j1)
    assert out["deleted"] == 1 and len(os.listdir(img)) == 2
    dedupe_apply.revert(j1)
    assert len(os.listdir(img)) == 3

    # delete_all: keep none -> all three retired
    j2 = str(tmp_path / "j2.jsonl")
    out2 = dedupe_apply.apply(
        [{"group_id": g["id"], "action": "keep_many", "keep_paths": []}],
        {g["id"]: g}, {"mode": "within", "dest": str(dest)}, j2)
    assert out2["deleted"] == 3 and len(os.listdir(img)) == 0
    dedupe_apply.revert(j2)
    assert len(os.listdir(img)) == 3
    cache.close()


# ── _new ingest: adopt + archive expected_size lockstep + revert ──────
def test_new_ingest_adopt_lockstep(tmp_path):
    dest = tmp_path / "creator"
    img = dest / "Images" / "2026"
    img.mkdir(parents=True)
    name = "2026.03.03 - OF - 200_1.jpg"
    existing = img / name
    _detailed(5, (320, 240)).save(existing, quality=88)
    existing_size = existing.stat().st_size

    db = str(tmp_path / "coomerfans_onlyfans_200.db")
    a = Archive(db)
    ek = entry_key("200", 1)
    a.record(ek, "200", name, "image", "2026", expected_size=existing_size)
    a.close()

    newdir = dest / "_new"
    newdir.mkdir()
    _detailed(5, (640, 480)).save(newdir / "dump.jpg", quality=88)   # higher-res upgrade
    _photo(9).save(newdir / "brand_new.png")                         # unmatched

    def resolver(filename, kind, year):
        row = sqlite3.connect(db).execute(
            "SELECT entry FROM archive WHERE filename=?", (filename,)).fetchone()
        return (db, row[0]) if row else None

    cache = SigCache(str(tmp_path / "c.db"))
    res = dedupe.scan_new_ingest(str(dest), cache, None, resolve_entry=resolver,
                                 opts={"image_distance": 10})
    adopt = [g for g in res["groups"] if g["suggested"]["action"] == "adopt"]
    assert len(adopt) == 1
    assert any(u["filename"] == "brand_new.png" for u in res["unmatched_new"])

    ga = adopt[0]
    s = ga["suggested"]
    journal = str(tmp_path / "j.jsonl")
    out = dedupe_apply.apply(
        [{"group_id": ga["id"], "action": "adopt", "new_path": s["new_path"],
          "target_path": s["target_path"], "db_path": s["db_path"], "entry": s["entry"]}],
        {ga["id"]: ga}, {"mode": "new", "dest": str(dest)}, journal)
    assert out["adopted"] == 1
    # identity kept, bytes upgraded, archive expected_size updated in lockstep
    new_size = existing.stat().st_size
    assert new_size > existing_size
    row = sqlite3.connect(db).execute(
        "SELECT expected_size FROM archive WHERE entry=?", (ek,)).fetchone()
    assert row[0] == new_size

    dedupe_apply.revert(journal)
    assert existing.stat().st_size == existing_size
    row2 = sqlite3.connect(db).execute(
        "SELECT expected_size FROM archive WHERE entry=?", (ek,)).fetchone()
    assert row2[0] == existing_size
    assert (newdir / "dump.jpg").is_file()
    cache.close()


# ── reference vs check: rename to reference name + revert ─────────────
def test_ref_check_rename(tmp_path):
    ref = tmp_path / "ref"
    chk = tmp_path / "check"
    ref.mkdir()
    chk.mkdir()
    _photo(3).save(ref / "Good_Name.png")
    _photo(3).save(chk / "IMG_random.png")
    _photo(4).save(chk / "unrelated.png")
    cache = SigCache(str(tmp_path / "c.db"))

    res = dedupe.scan_ref_check(str(ref), str(chk), cache, None, {"image_distance": 10})
    assert len(res["groups"]) == 1
    g = res["groups"][0]
    assert g["suggested"]["action"] == "rename_to_ref"

    journal = str(tmp_path / "j.jsonl")
    out = dedupe_apply.apply(
        [{"group_id": g["id"], "action": "rename_to_ref",
          "reference_name": g["suggested"]["reference_name"]}],
        {g["id"]: g}, {"mode": "ref_check", "check_dir": str(chk)}, journal)
    assert out["renamed"] == 1
    assert (chk / "Good_Name.png").is_file()
    dedupe_apply.revert(journal)
    assert (chk / "IMG_random.png").is_file()
    cache.close()


@pytest.mark.skipif(_ffmpeg() is None, reason="ffmpeg not available")
def test_video_near_dupe_grouping(tmp_path):
    ff = _ffmpeg()
    dest = tmp_path / "creator"
    vy = dest / "2026"
    vy.mkdir(parents=True)

    def gen(path, src):
        subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", src, "-t", "12", "-pix_fmt", "yuv420p", str(path)], check=True)

    gen(vy / "2026.06.01 - OF - 400_1.mp4", "testsrc=size=640x480:rate=24")
    subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(vy / "2026.06.01 - OF - 400_1.mp4"),
                    "-vf", "scale=320:240", "-crf", "35",
                    str(vy / "2026.06.02 - OF - 401_1.mp4")], check=True)
    gen(vy / "2026.06.03 - OF - 402_1.mp4", "mandelbrot=size=640x480:rate=24")

    cache = SigCache(str(tmp_path / "c.db"))
    res = dedupe.scan_within(str(dest), cache, ff, {"image_distance": 10})
    vgroups = [g for g in res["groups"] if g["kind"] == "video"]
    assert len(vgroups) == 1 and len(vgroups[0]["items"]) == 2
    cache.close()
