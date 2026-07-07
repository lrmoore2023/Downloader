import os
import re
import subprocess
import sys
import threading

from backend.download_errors import FailureStore


class GalleryDlRunner:
    def __init__(self):
        self._process = None
        self._cancel_event = threading.Event()
        self.downloaded_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self._errors = None

    @property
    def is_running(self):
        return self._process is not None and self._process.poll() is None

    def run(self, url, config_path, archive_path, on_progress, on_complete, on_error,
            errors_path=None, reset_errors=False):
        """Run gallery-dl as a subprocess with real-time output parsing.

        Failure capture is best-effort: gallery-dl owns its own retries/archive and
        its per-item errors don't cleanly map to a source tweet, so we record any
        non-transient `[error]` line together with the media URL it was last working
        on. `reset_errors=True` (used by the redownload-errors recovery run, which
        re-attempts everything) wipes the store first so it reflects only what still
        fails after the run."""
        self.downloaded_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self._cancel_event.clear()

        self._errors = FailureStore(errors_path) if errors_path else None
        if self._errors and reset_errors:
            try:
                self._errors.clear_all(platform="twitter")
            except Exception:
                pass
        current_url = None   # the media URL from the most recent "# <url>" line

        cmd = [
            sys.executable, "-m", "gallery_dl",
            "--config-ignore",
            "-c", config_path,
        ]
        if archive_path:
            cmd += ["--download-archive", archive_path]
        cmd.append(url)

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
            on_error({"type": "fatal", "message": "gallery-dl not found. Is Python/gallery-dl installed?"})
            on_complete(self._stats())
            return

        for line in self._process.stdout:
            if self._cancel_event.is_set():
                break

            line = line.rstrip("\n\r")
            if not line:
                continue

            parsed = self._parse_line(line)
            if parsed["type"] == "download":
                self.downloaded_count += 1
                current_url = None   # this item succeeded; drop its error context
                on_progress(parsed)
            elif parsed["type"] == "skip":
                self.skipped_count += 1
                on_progress(parsed)
            elif parsed["type"] == "url":
                current_url = parsed.get("message") or current_url
                on_progress(parsed)
            elif parsed["type"] == "error":
                self.error_count += 1
                # Transient categories (auth/cookies, network, rate limit) aren't a
                # per-file "gone" — don't persist those. Everything else with a
                # known media URL is a durable failure worth surfacing.
                if parsed.get("subtype") not in ("auth", "network") and current_url:
                    self._record_failure(current_url, url, parsed.get("message"))
                on_error(parsed)
            else:
                on_progress(parsed)

        self._process.wait()
        return_code = self._process.returncode

        if self._cancel_event.is_set():
            on_progress({"type": "info", "message": "Download cancelled by user"})

        if self._errors:
            try:
                self._errors.close()
            except Exception:
                pass

        stats = self._stats()
        stats["return_code"] = return_code
        stats["cancelled"] = self._cancel_event.is_set()
        on_complete(stats)

    def _record_failure(self, media_url, source_url, reason):
        if not self._errors:
            return
        try:
            self._errors.record_failure(
                f"twitter_{media_url}", platform="twitter", url=media_url,
                page_url=source_url, filename=os.path.basename(
                    media_url.split("?", 1)[0]) or None,
                reason=(reason or "gallery-dl error")[:300])
        except Exception:
            pass

    def cancel(self):
        """Cancel the running download."""
        self._cancel_event.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def _stats(self):
        return {
            "downloaded": self.downloaded_count,
            "skipped": self.skipped_count,
            "errors": self.error_count,
        }

    def _parse_line(self, line):
        """Parse a gallery-dl output line into a structured event."""
        # URL being processed: "# https://pbs.twimg.com/..."
        if line.startswith("# "):
            return {"type": "url", "message": line[2:]}

        # File path (successful download) - looks like an absolute path
        # Check this EARLY to avoid false matches on paths containing error-like words
        stripped = line.strip()
        if os.path.isabs(stripped) or re.match(r"^[A-Z]:\\", stripped):
            filename = os.path.basename(stripped)
            return {"type": "download", "message": filename, "path": stripped}

        # Skip/archive hit - only match gallery-dl's actual skip format
        if re.search(r"\[download\].*skipp(ing|ed)", line, re.IGNORECASE):
            return {"type": "skip", "message": line}

        # Auth errors - only match gallery-dl's error format: [category][error] HttpError: 401
        if re.search(r"\[error\].*\b(401|403)\b", line) or \
           re.search(r"\[error\].*(Unauthorized|Forbidden)", line, re.IGNORECASE):
            return {
                "type": "error",
                "subtype": "auth",
                "message": "Authentication failed. Cookies may have expired.",
                "raw": line,
            }

        # Rate limiting - only match gallery-dl's actual rate limit messages
        if re.search(r"\[error\].*\b429\b", line) or \
           re.search(r"(?:Sleeping|Waiting).*(?:rate|limit|seconds)", line, re.IGNORECASE):
            return {
                "type": "info",
                "subtype": "rate_limit",
                "message": "Rate limited - gallery-dl is waiting to retry...",
                "raw": line,
            }

        # Network errors - only match gallery-dl's error-tagged lines
        if re.search(r"\[error\].*(ConnectionError|Timeout|SocketError|SSLError)", line, re.IGNORECASE):
            return {
                "type": "error",
                "subtype": "network",
                "message": line,
            }

        # General gallery-dl errors (tagged with [error])
        if re.search(r"\[(error|warning)\]", line, re.IGNORECASE):
            return {"type": "error", "message": line}

        # Python traceback
        if line.startswith("Traceback "):
            return {"type": "error", "message": line}

        # Default: informational
        return {"type": "info", "message": line}
