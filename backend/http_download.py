"""Shared resumable, integrity-checked file download.

One function, `download()`, used by the hosts that have no engine of their own
(turbo.cr videos, goonbox images, SimpCity attachments, turbo/goonbox albums).
It streams to `<dest>.part`, resumes with HTTP Range, and only renames to the
final name once the file is proven whole:

  * byte count == the size the server (or the host's API) reported;
  * MD5 == the host's published checksum, when there is one (turbo.cr prints it);
  * videos without a checksum must pass ffprobe.

A 206 must continue from exactly where the .part ends (a misaligned resume would
stitch a right-sized but corrupt file — the coomerfans lesson), and a checksum
mismatch after a resume is retried once from zero before it counts as a failure.

Signed URLs expire, so the caller may pass `refresh()` returning a fresh URL;
401/403/404/410 trigger it. A URL that stays 404/410 after refreshing — or a
`refresh()` that itself reports the file gone — is `gone` (genuinely absent),
which callers record differently from a transient `failed`.
"""

import hashlib
import os
import time

from backend.coomerfans_runner import content_range_start, expected_total, ffprobe_ok

_EXPIRED = (401, 403, 404, 410)
_GONE = (404, 410)


class Gone(Exception):
    """Raised by a `refresh()` callback when the host says the file no longer exists."""


class Result:
    __slots__ = ("status", "size", "http_status", "reason")

    def __init__(self, status, size=0, http_status=None, reason=""):
        self.status = status          # ok | gone | failed | cancelled | skipped
        self.size = size
        self.http_status = http_status
        self.reason = reason

    @property
    def ok(self):
        return self.status == "ok"

    def __repr__(self):
        return f"Result({self.status!r}, size={self.size}, http={self.http_status}, {self.reason!r})"


def md5_file(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def _discard(path):
    try:
        os.remove(path)
    except OSError:
        pass


def download(session, url, dest_path, *, expected_size=None, md5=None, is_video=False,
             refresh=None, headers=None, cancelled=None, skip=None, on_info=None,
             max_attempts=8, timeout=(30, 300), chunk_size=1 << 20, sleep=time.sleep):
    """Download `url` to `dest_path`. Returns a Result (never raises for HTTP/network
    trouble). The final file appears atomically; on any non-ok result only a
    `.part` may remain, which a later call resumes."""
    md5 = (md5 or "").strip().lower() or None
    info = on_info or (lambda m: None)
    name = os.path.basename(dest_path)
    part = dest_path + ".part"
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    fetch = _Fetch(session, url, part, expected_size, refresh, headers, cancelled, skip,
                   info, max_attempts, timeout, chunk_size, sleep)

    for pass_no in (1, 2):
        res = fetch.run()
        if not res.ok:
            return res
        size = os.path.getsize(part)
        if expected_size and size != expected_size:
            _discard(part)
            return Result("failed", size, res.http_status,
                          f"size {size} != expected {expected_size}")
        if md5:
            if md5_file(part) != md5:
                _discard(part)
                if pass_no == 1:
                    info(f"Checksum mismatch for {name}; downloading again from scratch")
                    continue
                return Result("failed", size, res.http_status, "checksum mismatch")
        elif is_video and not ffprobe_ok(part):
            _discard(part)
            return Result("failed", size, res.http_status, "video failed the ffprobe check")
        os.replace(part, dest_path)
        return Result("ok", size, res.http_status)
    return Result("failed", reason="checksum mismatch")


class _Fetch:
    """Stream a URL into a .part file with Range resume + retries."""

    def __init__(self, session, url, part, expected_size, refresh, headers, cancelled, skip,
                 info, max_attempts, timeout, chunk_size, sleep):
        self.session, self.url, self.part = session, url, part
        self.expected_size = expected_size
        self.refresh_cb, self.headers = refresh, headers
        self.cancelled = cancelled or (lambda: False)
        self.skip = skip or (lambda: False)
        self.info, self.max_attempts, self.timeout = info, max_attempts, timeout
        self.chunk_size, self.sleep = chunk_size, sleep
        self.refreshes = 0
        self.name = os.path.basename(part[:-5])

    def _wait(self, backoff):
        remaining = float(backoff)
        while remaining > 0 and not self.cancelled():
            step = min(0.5, remaining)
            self.sleep(step)
            remaining -= step

    def _refresh(self):
        """Swap in a fresh URL. True = retry, False = the file is gone / no refresher."""
        if self.refresh_cb is None or self.refreshes >= 3:
            return False
        self.refreshes += 1
        try:
            fresh = self.refresh_cb()
        except Gone:
            return False
        except Exception as e:
            self.info(f"Couldn't refresh the link for {self.name}: {e}")
            return True
        if fresh:
            self.url = fresh
        return True

    def run(self):
        part = self.part
        backoff = 3
        last_http, last_reason = None, ""
        for attempt in range(1, self.max_attempts + 1):
            if self.cancelled():
                return Result("cancelled", reason="cancelled")
            if self.skip():
                _discard(part)
                return Result("skipped", reason="skipped")
            resume = os.path.getsize(part) if os.path.exists(part) else 0
            if self.expected_size and resume > self.expected_size:
                _discard(part)
                resume = 0
            hdrs = dict(self.headers or {})
            if resume:
                hdrs["Range"] = f"bytes={resume}-"
            r = None
            try:
                r = self.session.get(self.url, stream=True, headers=hdrs, timeout=self.timeout)
                status = last_http = r.status_code
                if status in _EXPIRED:
                    if self._refresh():
                        continue
                    if status in _GONE:
                        return Result("gone", http_status=status, reason=f"HTTP {status}")
                    last_reason = f"HTTP {status}"
                    self._wait(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                if status == 416:
                    total = expected_total(r, resume) or self.expected_size
                    if resume and total and resume == total:
                        return Result("ok", resume, status)
                    _discard(part)
                    continue
                if status >= 400:
                    last_reason = f"HTTP {status}"
                    self._wait(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                if "text/html" in (r.headers.get("Content-Type") or "").lower():
                    # An error/challenge page where bytes were expected.
                    last_reason = "got a web page instead of the file"
                    if not self._refresh():
                        self._wait(backoff)
                        backoff = min(backoff * 2, 60)
                    continue
                total = expected_total(r, resume)
                if resume and status == 200:
                    resume = 0                  # server ignored Range → start over
                elif resume and status == 206 and content_range_start(r) != resume:
                    self.info(f"Resume misaligned for {self.name}; restarting clean")
                    _discard(part)
                    continue
                with open(part, "ab" if resume else "wb") as f:
                    for buf in r.iter_content(chunk_size=self.chunk_size):
                        if self.cancelled():
                            return Result("cancelled", reason="cancelled")
                        if self.skip():
                            f.close()
                            _discard(part)
                            return Result("skipped", reason="skipped")
                        if buf:
                            f.write(buf)
                size = os.path.getsize(part)
                want = total or self.expected_size
                if want and size < want:
                    self.info(f"Incomplete ({size}/{want} bytes) for {self.name}, resuming…")
                    continue
                if want and size > want:
                    _discard(part)
                    continue
                return Result("ok", size, status)
            except Exception as e:
                if self.cancelled():
                    return Result("cancelled", reason="cancelled")
                last_reason = f"{e.__class__.__name__}: {e}"
                self.info(f"Retry {attempt} for {self.name}: {last_reason}")
                self._wait(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                if r is not None:
                    try:
                        r.close()
                    except Exception:
                        pass
        return Result("failed", http_status=last_http, reason=last_reason or "too many attempts")
