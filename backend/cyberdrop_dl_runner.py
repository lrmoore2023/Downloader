"""cyberdrop-dl engine for the Albums tab (bunkr & cyberdrop albums).

Mirrors GalleryDlRunner's contract (is_running / cancel / counters / run) but
drives cyberdrop-dl, which is far more resilient than gallery-dl against bunkr's
flaky, domain-rotating CDN. cyberdrop-dl is shelled out headlessly and its
structured outputs (a per-file results .jsonl and a conditional download-errors
.csv) are the authoritative source of what succeeded and what failed.

Verified against cyberdrop-dl v10.2.0. Notes that shaped this runner:
  * `--ui disabled` turns off the Rich TUI so stdout is line-parseable.
  * Nested-value flags (`--hashing off`, `--max-children.album 2`, ...) crash the
    CLI's arg parser; only boolean-alias flags are safe. `--no-auto-dedupe` is
    passed so cyberdrop-dl never deletes the user's files by hash.
  * State-isolation flags (`--config-file/--cache-file/--database-file`) reject
    non-existent paths, so we pre-create them under a per-destination state dir.
    This also keeps the feature away from the user's global cyberdrop-dl install.
  * cyberdrop-dl names each album's subfolder itself (e.g. "<Album> (Bunkr)").
"""

import csv
import json
import os
import re
import subprocess
import threading

from backend.download_errors import FailureStore


# Final-summary counters printed once at the end of a run.
_STAT_DOWNLOADED = re.compile(r"Downloaded:\s+(\d+)\s+file", re.IGNORECASE)
_STAT_FAILED = re.compile(r"Failed:\s+(\d+)\s+file", re.IGNORECASE)
_STAT_SKIPPED = re.compile(r"Skipped[^:]*:\s+(\d+)\s+file", re.IGNORECASE)
# The album page itself couldn't be read (bunkr 502/503/timeout, etc.) — an
# album-level scrape error, distinct from a per-file download failure.
_SCRAPE_FAILED = re.compile(r"Scrape Failed:.*?\(([^)]+)\)")


class CyberdropDlRunner:
    def __init__(self, platform="album"):
        self._platform = platform
        self._process = None
        self._cancel_event = threading.Event()
        self.downloaded_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self._errors = None

    @property
    def is_running(self):
        return self._process is not None and self._process.poll() is None

    def run(self, url, download_dir, on_progress, on_complete, on_error,
            errors_path=None, attempts=10, state_dir=None, ignore_history=False,
            tuning=None, cookies=None):
        self.downloaded_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self._cancel_event.clear()
        self._errors = FailureStore(errors_path) if errors_path else None
        # Live-log state (set from the tail thread).
        self._scrape_error = None
        self._retry_notice = False      # one "throttling" notice per burst, not per file
        self._scraping_announced = False
        self._queue_warned = False

        state_dir = state_dir or os.path.join(download_dir, ".cyberdrop")
        logs_dir = os.path.join(state_dir, "logs")
        cfg = self._prepare_state(state_dir, logs_dir)
        results_jsonl = os.path.join(logs_dir, "downloader.results.jsonl")
        errors_csv = os.path.join(logs_dir, "download_errors.csv")
        log_path = os.path.join(logs_dir, "downloader.log")
        # Start clean so we read only THIS run's output, not a prior run's. The
        # main log appends across runs by default, so it must be reset too or the
        # live tail would replay old lines.
        for stale in (results_jsonl, errors_csv, log_path):
            try:
                if os.path.isfile(stale):
                    os.remove(stale)
            except OSError:
                pass

        exe = self._executable()
        cmd = exe + [
            "download", url,
            "-o", download_dir,
            "--ui", "disabled",
            "--no-auto-dedupe",
            "--attempts", str(attempts),
            "--config-file", cfg["config"],
            "--cache-file", cfg["cache"],
            "--database-file", cfg["db"],
            "--logs.folder", logs_dir,
            "--logs.console-level", "INFO",  # suppress the line-by-line DEBUG config dump
            # Use a real Chrome fingerprint for scrape requests so bunkr's
            # Cloudflare front is less likely to bounce us with an intermittent
            # 502/challenge (same rationale as curl_cffi elsewhere in the app).
            "--impersonate", "chrome",
            "--dump-json",
        ]
        # Per-site pacing (from album_sites tuning) — the dominant lever against
        # bunkr's 429-storms + .part pileup: cap simultaneous downloads-per-host and
        # connections-per-file so we never flood the CDN. Peak host connections ≈
        # per_domain × segments.
        t = tuning or {}
        cmd += ["--downloads.per-domain", str(t.get("per_domain", 2)),
                "--concurrent-segments", str(t.get("segments", 2))]
        if t.get("jitter"):
            cmd += ["--jitter", str(t["jitter"])]
        # Optional cookies (Netscape .txt), dormant by default. NOTE: for bunkr this
        # HURTS — a bunkr session cookie (bnState) throttles downloads to ~0.4x vs
        # anonymous (measured), so AlbumRunner intentionally does NOT pass cookies
        # here. Kept only for a future site that might genuinely need them.
        if cookies and os.path.isfile(cookies):
            cmd += ["--cookies", cookies]
        if ignore_history:
            # Re-download the whole album even if the history DB has it (used by
            # the "Redownload whole" choice on an already-downloaded album).
            cmd.append("--ignore-history")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            on_error({"type": "fatal", "message": "cyberdrop-dl not found. Is it installed in the venv?"})
            on_complete(self._stats(succeeded=False))
            return

        # cyberdrop-dl's stdout (with --ui disabled) carries only startup info and
        # the final stats block — NOT per-file progress. The per-file events live
        # in downloader.log ("Download finished:", "Skipping … already downloaded",
        # "Download Failed:"), so tail that in a thread for live green/red progress.
        tail_stop = threading.Event()
        tail = threading.Thread(target=self._tail_log,
                                args=(log_path, on_progress, tail_stop), daemon=True)
        tail.start()

        # Everything the user sees comes from the log tail, which reads the clean,
        # single-line log FILE. cyberdrop-dl's console (stdout) wraps long URLs
        # across several lines and repeats the same events — pure clutter — so we
        # read stdout only to harvest the final stats block, never surfacing it.
        stats_seen = {}
        try:
            for line in self._process.stdout:
                if self._cancel_event.is_set():
                    break
                line = line.rstrip("\n\r")
                if line and not self._is_box(line):
                    self._scan_stats(line, stats_seen)
        finally:
            self._process.wait()
            tail_stop.set()
            tail.join(timeout=3)

        return_code = self._process.returncode
        cancelled = self._cancel_event.is_set()
        if cancelled:
            on_progress({"type": "info", "message": "Download cancelled by user"})

        # An album-level scrape failure (bunkr 502/503/timeout) means nothing could
        # be read — surface it as a clear, honest error instead of a silent 0-file run.
        if self._scrape_error and not cancelled:
            self.error_count += 1
            on_error({"type": "error", "message":
                      f"Couldn't read this album — bunkr returned “{self._scrape_error}”. "
                      f"That's usually a temporary bunkr server issue; try downloading again shortly."})

        # Failures for the panel come from the structured CSV (authoritative, only
        # what STILL failed). Live red lines already came from the log tail, so
        # don't re-emit per row here. On CANCEL we skip this entirely: terminating
        # mid-transfer makes cyberdrop-dl log a burst of 429/curl errors for the
        # interrupted in-flight downloads, but those are NOT real failures.
        if not cancelled:
            self._record_errors(errors_csv, url)

        # The final stats block (full runs only) is authoritative; on cancel there
        # is none, so keep the live counts the log tail accumulated.
        if "downloaded" in stats_seen:
            self.downloaded_count = stats_seen["downloaded"]
        if "skipped" in stats_seen:
            self.skipped_count = stats_seen["skipped"]
        if "failed" in stats_seen:
            self.error_count = max(self.error_count, stats_seen["failed"])

        if self._errors:
            try:
                self._errors.close()
            except Exception:
                pass

        stats = self._stats(succeeded=(return_code == 0 and not cancelled))
        stats["return_code"] = return_code
        stats["cancelled"] = cancelled
        stats["scrape_error"] = self._scrape_error
        stats["subfolder"] = self._subfolder_from_results(results_jsonl)
        on_complete(stats)

    # ── helpers ─────────────────────────────────────────────────────────
    def _executable(self):
        """Prefer the console entrypoint; fall back to `python -m cyberdrop_dl`."""
        import sys
        exe = os.path.join(os.path.dirname(sys.executable), "cyberdrop-dl.exe")
        if os.path.isfile(exe):
            return [exe]
        exe_nix = os.path.join(os.path.dirname(sys.executable), "cyberdrop-dl")
        if os.path.isfile(exe_nix):
            return [exe_nix]
        return [sys.executable, "-m", "cyberdrop_dl"]

    def _prepare_state(self, state_dir, logs_dir):
        """Pre-create the isolated state files cyberdrop-dl refuses to auto-create."""
        os.makedirs(logs_dir, exist_ok=True)
        config = os.path.join(state_dir, "config.yaml")
        cache = os.path.join(state_dir, "cache.json")
        db = os.path.join(state_dir, "cdl.db")
        if not os.path.isfile(config):
            with open(config, "w", encoding="utf-8") as f:
                f.write("---\n")
        if not os.path.isfile(cache):
            with open(cache, "w", encoding="utf-8") as f:
                f.write("{}")
        if not os.path.isfile(db):
            open(db, "a").close()
        return {"config": config, "cache": cache, "db": db}

    _LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

    @classmethod
    def _loggable(cls, line):
        """Return display text for a worth-showing line, or None to drop it.

        Drops DEBUG lines and the prefix-less continuation lines of cyberdrop-dl's
        DEBUG config dump; keeps INFO/WARNING/ERROR and our own messages.
        """
        s = line.strip()
        if not s:
            return None
        head = s.split(None, 1)
        level = head[0] if head else ""
        if level in cls._LEVELS:
            if level == "DEBUG":
                return None
            return s
        # No recognised level prefix. If it looks like part of the dumped JSON
        # config (braces / quoted keys), drop it; otherwise it's one of our own
        # messages (e.g. "Starting <url>") — keep it.
        if s[0] in '{}[]"' or s.endswith("{") or s.endswith("},") or s.endswith("],"):
            return None
        return s

    @staticmethod
    def _is_box(line):
        s = line.lstrip()
        return s.startswith("|") or s.startswith("+-") or s.startswith("│") or s.startswith("╭") or s.startswith("╰")

    @staticmethod
    def _scan_stats(line, out):
        m = _STAT_DOWNLOADED.search(line)
        if m:
            out["downloaded"] = int(m.group(1))
            return
        m = _STAT_FAILED.search(line)
        if m:
            out["failed"] = int(m.group(1))
            return
        m = _STAT_SKIPPED.search(line)
        if m:
            out["skipped"] = out.get("skipped", 0) + int(m.group(1))

    # ── live progress via the main log ──────────────────────────────────
    _RE_FINISHED = re.compile(r"Download finished:\s*(\S+)")
    _RE_SKIPPED = re.compile(r"Skipping\s+(\S+)\s+as it has already been downloaded")
    _RE_FAILED = re.compile(r"Download [Ff]ailed:\s*(.*)")
    _RE_PREFIX = re.compile(r"^\[[^\]]*\]\s*\w+\s+")   # "[timestamp] LEVEL "

    def _tail_log(self, log_path, on_progress, stop_event):
        """Follow downloader.log and emit live per-file events until stopped."""
        pos = 0
        while True:
            pos = self._drain_log(log_path, pos, on_progress)
            if stop_event.is_set():
                self._drain_log(log_path, pos, on_progress)   # final catch-up
                return
            stop_event.wait(0.4)

    def _drain_log(self, log_path, pos, on_progress):
        """Process complete new lines since byte offset `pos`; return the new offset."""
        try:
            if os.path.getsize(log_path) <= pos:
                return pos
            with open(log_path, "rb") as f:
                f.seek(pos)
                data = f.read()
        except OSError:
            return pos
        if b"\n" not in data:
            return pos
        text, _, _rest = data.rpartition(b"\n")
        for raw in text.split(b"\n"):
            self._handle_log_line(raw.decode("utf-8", "replace"), on_progress)
        return pos + len(text) + 1

    def _handle_log_line(self, line, on_progress):
        # Once cancelling, stop surfacing anything — the teardown logs a flurry of
        # 429/curl errors for interrupted downloads that would spam the log.
        if self._cancel_event.is_set():
            return
        m = self._RE_FINISHED.search(line)
        if m:
            self.downloaded_count += 1
            self._retry_notice = False   # a success ends the current throttle burst
            on_progress({"type": "download", "message": self._name_from_url(m.group(1)) or m.group(1)})
            return
        m = self._RE_SKIPPED.search(line)
        if m:
            self.skipped_count += 1
            on_progress({"type": "skip", "message": self._name_from_url(m.group(1)) or m.group(1)})
            return
        m = _SCRAPE_FAILED.search(line)
        if m:
            self._scrape_error = m.group(1)
            return
        if self._RE_FAILED.search(line):
            # cyberdrop-dl logs "Download Failed" on EVERY failed attempt then retries,
            # so a file that ultimately succeeds logs several. Showing each is pure
            # clutter — instead show ONE muted "throttling" notice per burst (reset on
            # the next success). The genuinely-failed files are in the errors panel.
            if not self._retry_notice:
                self._retry_notice = True
                on_progress({"type": "info",
                             "message": "⟳ bunkr is throttling / dropping connections — retrying automatically…"})
            return
        if "] Scraping " in line and not self._scraping_announced:
            self._scraping_announced = True
            on_progress({"type": "info", "message": "Reading the album's file list…"})
            return
        if "Too many downloads queued" in line and not self._queue_warned:
            self._queue_warned = True
            on_progress({"type": "info", "message": "Pacing bunkr downloads to avoid rate limits…"})

    def _subfolder_from_results(self, results_jsonl):
        """Album subfolder name (basename of the download_folder) from the results."""
        if not os.path.isfile(results_jsonl):
            return None
        try:
            with open(results_jsonl, encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    folder = rec.get("download_folder")
                    if folder:
                        return os.path.basename(folder.rstrip("\\/"))
        except OSError:
            pass
        return None

    def _record_errors(self, errors_csv, source_url):
        """Feed cyberdrop-dl's download-errors CSV into the FailureStore (only what
        still failed after retries). Counts errors; live red lines came from the tail."""
        if not os.path.isfile(errors_csv):
            return
        try:
            with open(errors_csv, encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    lower = {(k or "").strip().lower(): (v or "").strip()
                             for k, v in row.items()}
                    media_url = (lower.get("url") or lower.get("download_url")
                                 or lower.get("link") or "")
                    reason = (lower.get("error") or lower.get("reason")
                              or lower.get("message") or "cyberdrop-dl error")
                    filename = (lower.get("filename") or lower.get("name")
                                or self._name_from_url(media_url))
                    self.error_count += 1
                    if self._errors and media_url:
                        try:
                            self._errors.record_failure(
                                f"{self._platform}_{media_url}", platform=self._platform,
                                url=media_url, page_url=source_url,
                                filename=filename, reason=reason[:300])
                        except Exception:
                            pass
        except OSError:
            return

    @staticmethod
    def _name_from_url(url):
        """bunkr/cyberdrop CDN URLs carry the real filename in an `n=` query param."""
        if not url:
            return None
        from urllib.parse import parse_qs, urlparse
        try:
            n = parse_qs(urlparse(url).query).get("n")
            if n and n[0]:
                return n[0]
        except Exception:
            pass
        base = os.path.basename(urlparse(url).path)
        return base or None

    def cancel(self):
        self._cancel_event.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def _stats(self, succeeded=True):
        return {
            "downloaded": self.downloaded_count,
            "skipped": self.skipped_count,
            "errors": self.error_count,
            "succeeded": succeeded,
        }
