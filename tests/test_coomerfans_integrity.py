"""Offline integrity tests for the coomerfans downloader.

Runs a local HTTP server whose responses we script, so the truncation / 416 /
410 / error-page failure modes reproduce deterministically without touching the
live site. Run directly:  python tests/test_coomerfans_integrity.py
(No pytest dependency — plain asserts, prints PASS/FAIL, exits non-zero on failure.)
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import coomerfans_runner as cr          # noqa: E402
from backend import coomerfans_verify as cv           # noqa: E402
from backend.coomerfans_archive import Archive        # noqa: E402

CONTENT = os.urandom(2_000_000)          # 2 MB pseudo-file
FULL_SHA = hashlib.sha256(CONTENT).hexdigest()
STATE = {"trunc_full_gets": 0, "misalign_full_gets": 0, "videobreak_full_gets": 0,
         "chunkdrop_hits": {}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _range_start(self):
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d+)-", rng)
            if m:
                return int(m.group(1))
        return None

    def _safe_write(self, data):
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        n = len(CONTENT)
        if path == "/full":
            rng = self.headers.get("Range")
            if rng:
                m = re.match(r"bytes=(\d+)-(\d*)", rng)
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else n - 1   # open-ended -> EOF
                end = min(end, n - 1)
                if start >= n:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{n}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{n}")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                self._safe_write(CONTENT[start:end + 1])
                return
            self.send_response(200)
            self.send_header("Content-Length", str(n))
            self.end_headers()
            self._safe_write(CONTENT)
            return

        if path == "/trunc416":
            start = self._range_start()
            if start is not None:
                # refuse to resume -> 416 (the exact bug trigger)
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{n}")
                self.end_headers()
                return
            if STATE["trunc_full_gets"] == 0:
                STATE["trunc_full_gets"] += 1
                # declare full length but send half then drop -> IncompleteRead
                self.send_response(200)
                self.send_header("Content-Length", str(n))
                self.end_headers()
                self._safe_write(CONTENT[: n // 2])
                self.close_connection = True
                return
            self.send_response(200)
            self.send_header("Content-Length", str(n))
            self.end_headers()
            self._safe_write(CONTENT)
            return

        if path == "/misalign":
            start = self._range_start()
            if start is None:
                # First full GET: send half then drop -> a partial .part to resume.
                if STATE["misalign_full_gets"] == 0:
                    STATE["misalign_full_gets"] += 1
                    self.send_response(200)
                    self.send_header("Content-Length", str(n))
                    self.end_headers()
                    self._safe_write(CONTENT[: n // 2])
                    self.close_connection = True
                    return
                # Later full GET (after our misaligned resume is rejected): serve full.
                self.send_response(200)
                self.send_header("Content-Length", str(n))
                self.end_headers()
                self._safe_write(CONTENT)
                return
            # Resume request: MISALIGNED 206 — claims to resume from 0 and resends the
            # first half, which would append into a right-length but corrupt file.
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 0-{n // 2 - 1}/{n}")
            self.send_header("Content-Length", str(n // 2))
            self.end_headers()
            self._safe_write(CONTENT[: n // 2])
            return

        if path == "/videobreak":
            start = self._range_start()
            if start is not None:
                # A fixed runner must NEVER resume a video. If it does, we serve
                # GARBAGE for the tail so the resulting file is corrupt and the test
                # fails loudly (proving the no-resume guard works).
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{n-1}/{n}")
                self.send_header("Content-Length", str(n - start))
                self.end_headers()
                self._safe_write(b"\x00" * (n - start))
                return
            # Full GET: drop mid-stream the first time (partial .part), then serve full.
            if STATE["videobreak_full_gets"] == 0:
                STATE["videobreak_full_gets"] += 1
                self.send_response(200)
                self.send_header("Content-Length", str(n))
                self.end_headers()
                self._safe_write(CONTENT[: n // 2])
                self.close_connection = True
                return
            self.send_response(200)
            self.send_header("Content-Length", str(n))
            self.end_headers()
            self._safe_write(CONTENT)
            return

        if path == "/chunkdrop":
            # Byte-accurate ranges (for segmented download), but the FIRST request
            # for the chunk at offset 524288 drops mid-body -> segmented must retry it.
            rng = self.headers.get("Range", "")
            m = re.match(r"bytes=(\d+)-(\d+)", rng)
            if not m:                       # no range -> serve full (unused path)
                self.send_response(200)
                self.send_header("Content-Length", str(n)); self.end_headers()
                self._safe_write(CONTENT); return
            start, end = int(m.group(1)), min(int(m.group(2)), n - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{n}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            if start == 524288 and not STATE["chunkdrop_hits"].get(start):
                STATE["chunkdrop_hits"][start] = True
                self._safe_write(CONTENT[start:start + 100])   # partial then drop
                self.close_connection = True
                return
            self._safe_write(CONTENT[start:end + 1])
            return

        if path == "/gone":
            self.send_response(410)
            self.end_headers()
            self._safe_write(b"gone")
            return

        if path == "/html":
            body = b"<html>not media</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._safe_write(body)
            return

        self.send_response(404)
        self.end_headers()


# ── harness ──────────────────────────────────────────────────────────
_srv = HTTPServer(("127.0.0.1", 0), Handler)
BASE = f"http://127.0.0.1:{_srv.server_address[1]}"
threading.Thread(target=_srv.serve_forever, daemon=True).start()

_tmp = tempfile.mkdtemp(prefix="cf_itest_")
_results = []


def mk_runner(archive=None, prog=None):
    r = cr.CoomerfansRunner(workers=1, connect_timeout=5, read_timeout=15)
    r._session = requests.Session()
    r._destination = _tmp
    r._service = "onlyfans"
    r._mode = "full"
    r._archive = archive
    r._on_progress = prog or (lambda d: None)
    r._sleep_cancellable = lambda *a: None      # no real waiting in tests
    return r


def mk_job(url, kind="image", ext="jpg", path_key="/pk", post_id="100", index=1):
    return {"post_url": BASE + "/post", "post_id": post_id, "index": index,
            "kind": kind, "ext": ext, "url": url, "path_key": path_key,
            "dt": datetime(2025, 1, 2, tzinfo=timezone.utc), "slug": "s"}


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def check(name, cond, detail=""):
    _results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not cond else ""))


def fresh_path(name):
    p = os.path.join(_tmp, name)
    for x in (p, p + ".part"):
        if os.path.exists(x):
            os.remove(x)
    return p


# ── tests ────────────────────────────────────────────────────────────

def t_resume_correctness():
    final = fresh_path("resume.jpg")
    with open(final + ".part", "wb") as f:
        f.write(CONTENT[:800_000])           # 40% already there
    ok = mk_runner()._download_stream(mk_job(BASE + "/full"), final, "e", "resume.jpg")
    check("resume: byte-identical", ok and os.path.isfile(final) and sha(final) == FULL_SHA)
    check("resume: no leftover .part", not os.path.exists(final + ".part"))


def t_misaligned_resume():
    # A connection drops mid-download (partial .part), then the resume comes back
    # from the WRONG offset (206 starting at 0). The old code appended blindly,
    # producing a right-length but internally-corrupt file. Now it must detect the
    # misalignment, discard, and restart clean -> byte-identical result.
    STATE["misalign_full_gets"] = 0
    final = fresh_path("misalign.mp4")
    ok = mk_runner()._download_stream(
        mk_job(BASE + "/misalign", kind="image"), final, "e", "misalign.mp4")
    good = ok and os.path.isfile(final) and sha(final) == FULL_SHA
    check("misaligned-resume: rejected + recovers byte-correct file", good,
          f"sha match={os.path.isfile(final) and sha(final) == FULL_SHA}")
    check("misaligned-resume: no leftover .part", not os.path.exists(final + ".part"))


def t_video_segmented_ok():
    # Videos download via verified byte-range chunks; multi-chunk assembly must be
    # byte-identical to a plain download.
    final = fresh_path("seg.mp4")
    orig = cr.ffprobe_ok
    cr.ffprobe_ok = lambda p: True          # CONTENT is random bytes, not a real video
    try:
        res = mk_runner()._download_segmented(
            mk_job(BASE + "/full", kind="video", ext="mp4"), final, "e", "seg.mp4",
            chunk=512 * 1024)               # 4 chunks over the 2 MB file
    finally:
        cr.ffprobe_ok = orig
    good = res is True and os.path.isfile(final) and sha(final) == FULL_SHA
    check("segmented: multi-chunk assembles byte-correct", good,
          f"res={res} sha_ok={os.path.isfile(final) and sha(final) == FULL_SHA}")
    check("segmented: no leftover .part", not os.path.exists(final + ".part"))


def t_video_segmented_chunk_retry():
    # A chunk whose connection drops mid-body must be retried and recovered — the
    # whole file still assembles byte-correct (guarantees flaky files complete).
    STATE["chunkdrop_hits"] = {}
    final = fresh_path("segretry.mp4")
    orig = cr.ffprobe_ok
    cr.ffprobe_ok = lambda p: True
    try:
        res = mk_runner()._download_segmented(
            mk_job(BASE + "/chunkdrop", kind="video", ext="mp4"), final, "e", "segretry.mp4",
            chunk=512 * 1024)
    finally:
        cr.ffprobe_ok = orig
    good = res is True and os.path.isfile(final) and sha(final) == FULL_SHA
    check("segmented: dropped chunk retried -> byte-correct", good,
          f"res={res} retried={STATE['chunkdrop_hits']}")


def t_truncation_416():
    STATE["trunc_full_gets"] = 0
    final = fresh_path("trunc.jpg")
    ok = mk_runner()._download_stream(mk_job(BASE + "/trunc416"), final, "e", "trunc.jpg")
    good = ok and os.path.isfile(final) and os.path.getsize(final) == len(CONTENT) and sha(final) == FULL_SHA
    check("truncation+416: recovers full byte-correct file (not the truncated one)", good,
          detail=f"size={os.path.getsize(final) if os.path.isfile(final) else 'missing'}")


def t_416_when_complete():
    final = fresh_path("complete.jpg")
    with open(final + ".part", "wb") as f:
        f.write(CONTENT)                      # already full
    ok = mk_runner()._download_stream(mk_job(BASE + "/full"), final, "e", "complete.jpg")
    check("416-when-complete: accepted", ok and os.path.isfile(final)
          and os.path.getsize(final) == len(CONTENT))


def t_410_refresh_recovers():
    final = fresh_path("gone_ok.jpg")
    orig = cr.refresh_media_url
    cr.refresh_media_url = lambda *a, **k: BASE + "/full"
    try:
        ok = mk_runner()._download_stream(mk_job(BASE + "/gone"), final, "e", "gone_ok.jpg")
    finally:
        cr.refresh_media_url = orig
    check("410 refresh: recovers via fresh URL", ok and os.path.isfile(final)
          and os.path.getsize(final) == len(CONTENT))


def t_410_refresh_resistant():
    final = fresh_path("gone_bad.jpg")
    orig = cr.refresh_media_url
    cr.refresh_media_url = lambda *a, **k: None       # can't recover
    r = mk_runner()
    try:
        ok = r._download_stream(mk_job(BASE + "/gone"), final, "e", "gone_bad.jpg")
    finally:
        cr.refresh_media_url = orig
    check("refresh-resistant gone: gives up + flags error (no infinite loop)",
          ok is False and r.error_count >= 1 and not os.path.exists(final))


def t_error_page_guard():
    final = fresh_path("html.jpg")
    orig = cr.refresh_media_url
    cr.refresh_media_url = lambda *a, **k: BASE + "/full"
    try:
        ok = mk_runner()._download_stream(mk_job(BASE + "/html"), final, "e", "html.jpg")
    finally:
        cr.refresh_media_url = orig
    good = ok and os.path.isfile(final) and sha(final) == FULL_SHA
    check("error-page guard: HTML body refused, refreshed to real media", good)


def t_expected_size_recorded():
    final = fresh_path("sized.jpg")
    db = os.path.join(_tmp, "arch.db")
    if os.path.exists(db):
        os.remove(db)
    arch = Archive(db)
    ok = mk_runner(archive=arch)._download_stream(
        mk_job(BASE + "/full", post_id="200", index=1), final, "coomerfans_200_1", "sized.jpg")
    rows = {r["entry"]: r for r in arch.rows()}
    arch.close()
    row = rows.get("coomerfans_200_1", {})
    check("expected_size + path_key recorded", ok and row.get("expected_size") == len(CONTENT)
          and row.get("path_key") == "/pk", detail=str(row))


def t_ffprobe_validation():
    ff = shutil.which("ffmpeg")
    if not ff or not shutil.which("ffprobe"):
        print("  [SKIP] ffprobe validation (ffmpeg/ffprobe not installed)")
        return
    vid = os.path.join(_tmp, "real.mp4")
    subprocess.run([ff, "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=128x128:rate=10",
                    "-pix_fmt", "yuv420p", vid], capture_output=True)
    check("ffprobe: valid mp4 passes", cr.ffprobe_ok(vid))
    trunc = os.path.join(_tmp, "trunc.mp4")
    with open(vid, "rb") as s, open(trunc, "wb") as d:
        d.write(s.read()[: os.path.getsize(vid) // 3])
    check("ffprobe: truncated mp4 flagged", not cr.ffprobe_ok(trunc))


def t_repair_present_and_deleted():
    # fresh repair sandbox with its own archive + planted files
    repdir = os.path.join(_tmp, "rep")
    shutil.rmtree(repdir, ignore_errors=True)
    img = os.path.join(repdir, "images", "2025")
    os.makedirs(img, exist_ok=True)
    broken_file = os.path.join(img, "b.jpg")
    with open(broken_file, "wb") as f:
        f.write(CONTENT[:1000])                 # present but truncated

    db = os.path.join(repdir, "a.db")
    arch = Archive(db)
    arch.record("coomerfans_500_1", "500", "b.jpg", "image", "2025", len(CONTENT), "/pk")
    arch.record("coomerfans_501_1", "501", "gone.jpg", "image", "2025", len(CONTENT), "/pk")  # file absent
    arch.close()

    sess = requests.Session()
    res = cv.find_broken(db, repdir, "onlyfans", "999", sess)
    names = {b["filename"] for b in res["broken"]}
    check("verify: present-but-broken flagged, deleted (missing) ignored",
          names == {"b.jpg"} and res["present"] == 1 and res["checked"] == 2, detail=str(names))

    # repair: monkeypatch parse_post to point media at the local server
    orig = cv.parse_post
    cv.parse_post = lambda s, url: {
        "url": url, "dt": datetime(2025, 1, 2, tzinfo=timezone.utc),
        "media": [{"url": BASE + "/full", "path_key": "/pk", "kind": "image", "ext": "jpg"}],
    }
    runner = mk_runner(archive=Archive(db))
    try:
        rep = cv.repair_broken(res["broken"], repdir, "onlyfans", "999", runner, sess)
    finally:
        cv.parse_post = orig
        runner._archive.close()

    check("repair: broken file re-downloaded to full size",
          rep["repaired"] == 1 and rep["still_bad"] == 0
          and os.path.getsize(broken_file) == len(CONTENT) and sha(broken_file) == FULL_SHA)
    check("repair: deleted file NOT resurrected",
          not os.path.exists(os.path.join(img, "gone.jpg")))


def main():
    print("Running coomerfans integrity tests...")
    for t in (t_resume_correctness, t_misaligned_resume,
              t_video_segmented_ok, t_video_segmented_chunk_retry,
              t_truncation_416, t_416_when_complete,
              t_410_refresh_recovers, t_410_refresh_resistant, t_error_page_guard,
              t_expected_size_recorded, t_ffprobe_validation, t_repair_present_and_deleted):
        try:
            t()
        except Exception as e:
            check(t.__name__, False, detail=f"exception: {e}")
    _srv.shutdown()
    shutil.rmtree(_tmp, ignore_errors=True)
    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed.")
    sys.exit(0 if passed == len(_results) else 1)


if __name__ == "__main__":
    main()
