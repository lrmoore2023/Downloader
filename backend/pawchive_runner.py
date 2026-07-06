"""Download engine for pawchive.st.

Mirrors CoomerfansRunner's public contract (run/cancel/is_running + the
downloaded/skipped/error counters) so backend.creator_runner can drive it the
same way. Differences from coomerfans:

  * pawchive has a JSON API, so the crawl paginates the listing endpoint (which
    already carries full post detail) instead of scraping HTML — one request per
    50 posts, with a per-post detail fallback when the listing omits it.
  * media URLs are content-addressed (no signing / no expiry), so there is no
    URL-refresh dance — downloads are a straight resumable stream.
  * on-site media are named after the file's OWN name, e.g.
    "2026.05.12 - Patreon - Label stream night X teaser.mp4".
  * external links found in a post body/embed are handled: single *direct* files
    (catbox etc.) are auto-downloaded and verified; everything else (mega, gdrive,
    gofile, reference pages) plus any direct file that fails is written to the
    per-creator manifest (PawchiveLinks) for manual download — nothing is dropped.

Curation rule (see memory curation-deletion-workflow): 'latest' skips whole
posts already in the archive (never resurrects deletions); 'full' re-downloads
missing files by design; 'redownload_year' ("Download Year") downloads a single
year, skipping anything already present (it never re-downloads or overwrites).
"""

import json
import os
import shutil
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from urllib.parse import unquote, urlsplit

from backend import pawchive_extract

from backend.pawchive_scraper import (
    make_session, parse_creator_url, iter_posts, fetch_json,
    parse_post, fetch_creator_name, build_filename, add_index_suffix,
    target_path, site_code, _kind_and_ext, to_mirror, API, MIRROR_BASE,
    # HTTP-backend-agnostic exception classes (curl_cffi or requests) — the runner
    # MUST catch these, not requests.*, or every try/except silently stops catching.
    HTTPError, ConnectionError, Timeout, RequestException,
)
from backend.pawchive_archive import Archive, entry_key, ext_entry_key
from backend.pawchive_links import PawchiveLinks
# Reuse the proven low-level helpers verbatim.
from backend.coomerfans_runner import (
    expected_total, ffprobe_ok, _TRANSIENT_READ_STATUSES,
)


def _url_basename(url):
    path = urlsplit(url).path
    return unquote(path.rsplit("/", 1)[-1]) or "file"


# Keep the whole destination path under Windows' classic MAX_PATH (260). We budget
# a bit less and reserve headroom for the '.part' suffix and any '_n' collision
# suffix, so archive names (which append the post title) get truncated in time.
_WIN_PATH_LIMIT = 255
_NAME_RESERVE = 12
# Video/archive files are the heavy transfers; capping how many run at once keeps
# the CDN from 429'ing while images keep flowing at full concurrency.
_LARGE_KINDS = ("video", "archive")
# Flush the external-links manifest to disk every N crawled posts so the UI panel
# fills in during the crawl rather than only when it finishes.
_MANIFEST_FLUSH_EVERY = 20


class _AdaptiveThrottle:
    """Self-tuning request pacer shared by all workers (AIMD).

    Each caller reserves the next time slot, so N workers issue at most one
    request per `interval` overall. The interval adapts: it eases *down* toward
    `floor` on every clean response (probing for the fastest safe rate) and jumps
    *up* toward `ceil` on a 429 (respecting Retry-After), so the whole pool backs
    off together instead of each worker hammering the same wall. Used with a tight
    profile for the rate-limit-sensitive API and a loose one for the file CDN."""

    def __init__(self, interval, floor, ceil, recover=0.9):
        self.interval = interval
        self.floor = floor
        self.ceil = ceil
        self.recover = recover   # per-success multiplier easing interval toward floor
        self._lock = threading.Lock()
        self._next = 0.0   # monotonic time of the next allowed request

    def wait(self, is_cancelled):
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        while True:
            remaining = start - time.monotonic()
            if remaining <= 0 or is_cancelled():
                return
            time.sleep(min(0.2, remaining))

    def on_success(self):
        # Additive-ish speed-up: ease the interval down toward the floor.
        with self._lock:
            if self.interval > self.floor:
                self.interval = max(self.floor, self.interval * self.recover)

    def on_throttled(self, retry_after):
        # Multiplicative back-off + honor the server's Retry-After for the next slot.
        with self._lock:
            self.interval = min(self.ceil, max(self.interval * 2, 0.5))
            self._next = max(self._next, time.monotonic() + retry_after)


# A subset of response headers worth capturing when diagnosing rate limits.
_RL_HEADERS = ("retry-after", "x-ratelimit-limit", "x-ratelimit-remaining",
               "x-ratelimit-reset", "cf-ray", "server", "content-type",
               "content-length")


class _Diag:
    """Append-only JSONL recorder for diagnosing throughput / 429 rate limiting.
    One file per run (truncated at start); each event is a JSON line with a
    seconds-since-start timestamp. Cheap and thread-safe; a no-op if disabled."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._fh = None
        self.start = time.time()
        self.counts = Counter()
        if path:
            try:
                self._fh = open(path, "w", encoding="utf-8")
            except OSError:
                self._fh = None

    @property
    def enabled(self):
        return self._fh is not None

    def bump(self, event):
        """Increment a counter without writing a line (for high-frequency events)."""
        self.counts[event] += 1

    def log(self, event, **fields):
        self.counts[event] += 1
        if not self._fh:
            return
        rec = {"t": round(time.time() - self.start, 3), "event": event}
        rec.update(fields)
        try:
            line = json.dumps(rec, ensure_ascii=False, default=str)
        except Exception:
            return
        with self._lock:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except Exception:
                pass

    @staticmethod
    def rl_headers(resp):
        try:
            h = resp.headers if resp is not None else {}
            return {k: h.get(k) for k in _RL_HEADERS if h.get(k) is not None}
        except Exception:
            return {}

    def close(self):
        with self._lock:
            if self._fh:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None


# Diagnostics land next to the app (local disk, not the NAS), overwritten per run.
_DIAG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pawchive_diag.jsonl")


class PawchiveRunner:
    def __init__(self, workers=6, connect_timeout=30, read_timeout=300,
                 chunk_size=1 << 20, max_download_attempts=10, large_workers=1,
                 extract=True):
        # pawchive's file CDN bandwidth-caps EACH connection to ~1 MB/s, so aggregate
        # throughput scales almost linearly with parallel connections (measured: K=1
        # 1.2 MB/s, K=6 10 MB/s, K=10 14 MB/s). The catch: those connections must be
        # INDEPENDENT — each download worker gets its OWN session (its own curl handle
        # + connection), just like separate browser tabs. Sharing one session across
        # threads is what triggered the DDoS-Guard 403 blocks; independent sessions
        # scale cleanly with ~0 blocks. See _dl_session / _download_all.
        self.workers = max(1, min(10, int(workers)))
        # Kept for API compatibility; the download pool is now unified (all files run
        # in one pool of `workers`, each worker with its own session), so big files
        # get the full concurrency benefit instead of a tiny separate pool.
        self.large_workers = max(1, int(large_workers))
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.chunk_size = chunk_size
        self.max_download_attempts = max_download_attempts
        # Per-worker download sessions (thread-local): one independent connection per
        # worker thread. self._session is used only for the single-threaded API crawl.
        self._tls = threading.local()
        self._dl_sessions = []            # every worker session, for cleanup
        self._dl_sessions_lock = threading.Lock()
        self._cookies_path = None
        # API pacer only: the crawl is ~1 request/page and rate-limits gently. File
        # downloads no longer use a global throttle/cooldown — each worker backs off
        # and RESUMES on its own (the CDN is flaky and breaks mid-transfer even in a
        # browser), so one stalled connection never freezes the others.
        self._api_throttle = _AdaptiveThrottle(0.3, floor=0.25, ceil=5.0)
        self._file_throttle = _AdaptiveThrottle(0.05, floor=0.02, ceil=2.0,
                                                recover=0.9)

        # Global DDoS-Guard cooldown gate, shared by the API and file hosts (the
        # block is per-IP across the whole domain). On a 429 the whole pool goes
        # silent, with the quiet period escalating on repeated strikes so the
        # block actually clears instead of being reset by continued retries.
        self._cd_lock = threading.Lock()
        self._cooldown_until = 0.0   # monotonic; no requests may go out before this
        self._dg_strikes = 0         # consecutive throttle events (escalation level)
        self._dg_last = 0.0          # monotonic time of the last strike
        self._succ_since = 0         # clean responses since the last strike

        self.downloaded_count = 0
        self.skipped_count = 0
        self.error_count = 0

        self._running = False
        # Mirror failover: default to .st; flip to .pw only if .st proves entirely
        # unreachable (connection-level failures, not 429s), probing the mirror once.
        self._use_mirror = False
        self._mirror_checked = False
        self._cancel_event = threading.Event()
        self._count_lock = threading.Lock()
        self._fname_lock = threading.Lock()
        self._ext_lock = threading.Lock()
        self._claimed = set()
        self._ext_results = {}      # post_id -> {url: state dict}
        # Diagnostics (throughput + 429 rate limiting).
        self._diag = _Diag(None)
        self._active = 0            # concurrent file downloads in flight
        self._active_max = 0
        self._active_lock = threading.Lock()

        # Auto-extract downloaded archives (zip/rar/…) into the library, one at a
        # time in a dedicated single worker so it never blocks the download pool.
        self._extract_enabled = bool(extract)
        self._extract_pool = None
        self._extract_futures = []
        self._extract_lock = threading.Lock()
        self._extracted_count = 0

        # Per-file skip: entry keys the user asked to abandon mid-run (see request_skip).
        self._skip_ids = set()
        self._skip_lock = threading.Lock()

        self._on_progress = None
        self._archive = None
        self._links = None
        self._session = None
        self._destination = None
        self._service = None
        self._mode = "full"

    # ── public contract ──────────────────────────────────────
    @property
    def is_running(self):
        return self._running

    def cancel(self):
        self._cancel_event.set()

    def _cancelled(self):
        return self._cancel_event.is_set()

    def request_skip(self, entry):
        """Abandon a single in-flight download (by its archive entry key) without
        cancelling the run. The download loop polls this at the same points it polls
        cancel, so the worker aborts promptly and frees up for the next file."""
        if not entry:
            return
        with self._skip_lock:
            self._skip_ids.add(entry)

    def _skip_requested(self, entry):
        with self._skip_lock:
            return entry in self._skip_ids

    # ── DDoS-Guard cooldown gate ─────────────────────────────
    # Strikes within this window escalate the quiet period; a single cooldown
    # never exceeds the cap. Escalation matters because DDoS-Guard ignores its own
    # 10s retry-after once an IP is flagged — only a long enough silence clears it.
    _COOLDOWN_WINDOW = 90       # seconds; strikes within this window compound
    _COOLDOWN_MAX = 90          # seconds; cap on a single quiet period (a browser-
                                # class client that honors retry-after:10 shouldn't
                                # need the old 180s bot-penalty silences)
    _SUCC_TO_DECAY = 4          # clean responses that step the escalation back down

    def _await_cooldown(self):
        """Block until any active global DDoS-Guard cooldown expires. Called by
        every worker (API and file) right before a request, so the whole pool goes
        silent together — that silence is what lets the block clear."""
        while not self._cancelled():
            with self._cd_lock:
                remaining = self._cooldown_until - time.monotonic()
            if remaining <= 0:
                return
            self._sleep_cancellable(min(remaining, 1.0))

    def _register_throttle(self, retry_after):
        """Record a DDoS-Guard 429 and open/extend the global quiet period. The
        quiet escalates with consecutive strikes (10 → 20 → 40 → 80 → cap) so a
        persistent block eventually gets a long enough silence to reset. Returns
        (quiet_seconds, strike_count) for logging."""
        with self._cd_lock:
            now = time.monotonic()
            if now - self._dg_last <= self._COOLDOWN_WINDOW:
                self._dg_strikes += 1
            else:
                self._dg_strikes = 1
            self._dg_last = now
            self._succ_since = 0
            base = max(int(retry_after or 0), 5)
            quiet = min(self._COOLDOWN_MAX, base * (2 ** (self._dg_strikes - 1)))
            self._cooldown_until = max(self._cooldown_until, now + quiet)
            return quiet, self._dg_strikes

    def _register_success(self):
        """A clean response — decay the escalation so we don't stay backed off
        forever once the block has cleared."""
        with self._cd_lock:
            if not self._dg_strikes:
                return
            self._succ_since += 1
            if self._succ_since >= self._SUCC_TO_DECAY:
                self._dg_strikes -= 1
                self._succ_since = 0

    def run(self, creator_url, destination, mode, archive_path, links_path,
            on_progress, on_complete, on_error, cookies_path=None, year=None):
        self.downloaded_count = self.skipped_count = self.error_count = 0
        self._cancel_event.clear()
        with self._skip_lock:
            self._skip_ids.clear()
        self._claimed.clear()
        self._ext_results = {}
        self._use_mirror = False
        self._mirror_checked = False
        self._cooldown_until = 0.0
        self._dg_strikes = 0
        self._dg_last = 0.0
        self._succ_since = 0
        self._active = 0
        self._active_max = 0
        self._cookies_path = cookies_path
        self._diag = _Diag(_DIAG_PATH)
        self._running = True
        self._on_progress = on_progress
        self._destination = destination
        self._mode = mode
        if self._diag.enabled:
            self._info(f"diagnostics → {_DIAG_PATH}")

        try:
            creator = parse_creator_url(creator_url)
            if not creator:
                on_error({"type": "error", "message": "Invalid pawchive creator URL"})
                on_complete(self._stats(cancelled=False))
                return
            self._service = creator["service"]
            self._user_id = creator["user_id"]
            self._session = make_session(cookies_path=cookies_path)
            # Pick a working host up front so a down/hung pawchive.st doesn't make
            # every request eat a timeout before failing over.
            self._check_primary_or_failover()
            self._archive = Archive(archive_path) if archive_path else None
            self._links = PawchiveLinks(links_path) if links_path else None
            if self._links:
                # Batch manifest writes: mutate in memory during the crawl and
                # flush once, instead of hammering the NAS once per post.
                self._links.autosave = False

            # Name lookup via the failover-aware path (short timeout + mirror
            # rewrite) so it can't hang the run's start on a dead primary.
            prof = self._request_with_retry(
                lambda u: fetch_json(self._session, u, timeout=self._API_TIMEOUT),
                f"{API}/{self._service}/user/{self._user_id}/profile",
                "profile", attempts=3) or {}
            name = prof.get("name") if isinstance(prof, dict) else ""
            if name:
                self._info(f"Creator: {name} ({site_code(self._service)})")

            if self._extract_enabled:
                self._extract_pool = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="pawextract")

            media_jobs, ext_jobs, posts = self._crawl(year)
            if self._cancelled():
                self._info("Cancelled during crawl.")
                self._finish(on_complete)
                return

            self._info(f"{len(media_jobs)} media item(s) + {len(ext_jobs)} direct "
                       f"external file(s) to fetch ({self.skipped_count} already present).")
            self._download_all(media_jobs + ext_jobs)
            # Let any queued extractions finish before reconciling (reconcile checks
            # files on disk, and extraction moves/deletes them).
            self._drain_extractions()
            if not self._cancelled():
                self._reconcile(media_jobs)
            self._finalize_manifest(posts)

            self._finish(on_complete)
        except Exception as e:
            on_error({"type": "error", "message": f"Fatal: {e}"})
            self._finish(on_complete)
        finally:
            self._shutdown_extract_pool()
            self._close_dl_sessions()
            self._emit_diag_summary()
            self._diag.close()
            if self._archive:
                self._archive.close()
            self._running = False

    def _emit_diag_summary(self):
        """Roll the run's diagnostics into one summary line (also to the UI log)."""
        c = self._diag.counts
        elapsed = round(time.time() - self._diag.start, 1)
        summary = {
            "elapsed_s": elapsed,
            "api_reqs": c.get("api_req", 0), "api_429": c.get("api_429", 0),
            "api_conn_err": c.get("api_conn_err", 0),
            "file_reqs": c.get("file_req", 0), "file_blocks": c.get("file_block", 0),
            "downloads_done": c.get("dl_end_ok", 0),
            "downloads_failed": c.get("dl_end_fail", 0),
            "max_concurrency": self._active_max, "used_mirror": self._use_mirror,
        }
        self._diag.log("summary", **summary)
        self._info(
            "diag: "
            f"api {summary['api_reqs']} reqs / {summary['api_429']} 429s, "
            f"file {summary['file_reqs']} reqs / {summary['file_blocks']} blocks, "
            f"{summary['downloads_done']} ok / {summary['downloads_failed']} failed, "
            f"peak {summary['max_concurrency']} concurrent, {elapsed}s")

    # ── crawl + build jobs ───────────────────────────────────
    def _crawl(self, year):
        self._info("Crawling creator posts...")

        def on_page(page, n):
            self._info(f"Page {page}: {n} post(s)")

        media_jobs, ext_jobs, posts = [], [], {}
        yr = int(year) if year else None
        posts_since_flush = 0

        for raw in iter_posts(self._session, self._service, self._user_id,
                              on_page=on_page, should_cancel=self._cancelled,
                              fetch=self._fetch_json):
            if self._cancelled():
                break
            post = parse_post(raw)
            # Year-scoped download ('Download Year'): the listing is strictly
            # newest->oldest by published date, so once we drop below the target
            # year we can STOP paginating — older pages can't contain it. Checked
            # before any detail fetch so out-of-year posts cost nothing.
            if yr is not None:
                pdt = post["dt"]
                if pdt and pdt.year < yr:
                    break                       # past the year; no older pages needed
                if not pdt or pdt.year != yr:
                    continue                    # newer year (page toward target) / no date
            # Un-imported listing entries (detail_fetched=False) usually still
            # carry their media inline, so only spend an extra per-post API request
            # when the listing gave us NO media for such a post. This cuts the crawl
            # from ~1 request/post to ~1 request/page for imported creators — the
            # ~1300-request crawls were what tripped DDoS-Guard. Trade-off: posts
            # skipped here don't get their body scanned for external links.
            if not post["detail_fetched"] and not post["media"]:
                full = self._read_post(post["post_id"])
                if full is not None:
                    post = parse_post(full)
            if (self._mode == "latest" and self._archive
                    and self._archive.post_seen(post["post_id"])):
                continue

            posts[post["post_id"]] = post
            # Images get a page-order ordinal ('01 - ') only when a post has >1 of
            # them; videos always use standard naming (they carry real names and
            # live in a separate folder). image_no counts images as we go so the
            # ordinal reflects position among the post's images, in page order.
            image_count = sum(1 for m in post["media"] if m["kind"] == "image")
            ord_width = max(2, len(str(image_count)))
            image_no = 0
            for idx, m in enumerate(post["media"], 1):
                ordinal = None
                if m["kind"] == "image":
                    image_no += 1
                    if image_count > 1:
                        ordinal = image_no
                media_jobs.append({
                    "jobkind": "media",
                    "post_id": post["post_id"],
                    "index": idx,
                    "entry": entry_key(post["post_id"], idx),
                    "media_kind": m["kind"],
                    "url": m["url"],
                    "name": m["name"],
                    "dt": post["dt"],
                    "ordinal": ordinal,
                    "ord_width": ord_width,
                    "include_time": False,   # set below once all posts are known
                    "post_title": post.get("title", ""),   # used for archive names
                })
            ei = 0
            for l in post["external_links"]:
                if l["kind"] == "direct":
                    ei += 1
                    ext_jobs.append({
                        "jobkind": "ext",
                        "post_id": post["post_id"],
                        "index": ei,
                        "entry": ext_entry_key(post["post_id"], ei),
                        "url": l["url"],
                        "dt": post["dt"],
                    })
            # Record the post's text + all links now (direct ones start pending;
            # _finalize_manifest upgrades them to grabbed/failed after downloads).
            # Flush every _MANIFEST_FLUSH_EVERY posts so the "Links needing
            # attention" panel fills in progressively during a long crawl instead
            # of only appearing once the whole crawl finishes.
            if self._links:
                self._links.upsert_post(post, {})
                posts_since_flush += 1
                if posts_since_flush >= _MANIFEST_FLUSH_EVERY:
                    posts_since_flush = 0
                    self._save_links()

        # Add the ' HH.MM' token to an image's name only when its day holds >1
        # post containing images, so same-day sets stay grouped in the flat
        # images/<year>/ folder instead of interleaving. Counted across this crawl.
        img_posts_per_day = Counter(
            post["dt"].date()
            for post in posts.values()
            if post["dt"] and any(m["kind"] == "image" for m in post["media"])
        )
        for job in media_jobs:
            if (job["media_kind"] == "image" and job["dt"]
                    and img_posts_per_day[job["dt"].date()] > 1):
                job["include_time"] = True

        # One NAS write for the whole crawl (autosave is off during the loop).
        self._save_links()
        return media_jobs, ext_jobs, posts

    def _url(self, url):
        """The request URL to actually use. pawchive.st and .pw are equal mirrors of
        the same backend (.st redirects to .pw), so there is no host to fail over to
        — we always use the URL as-is (base already targets .pw)."""
        return url

    def _check_primary_or_failover(self):
        """Fast startup reachability probe. Just a courtesy log if the host doesn't
        answer — there's no separate mirror to switch to (see _url)."""
        try:
            self._session.get(f"{API}/", timeout=(6, 8))
        except RequestException:
            self._info("pawchive isn't responding yet — will retry per request")

    def _failover_to_mirror(self):
        """No-op. Retained so existing call sites need no change. .st and .pw are the
        same backend now, so 'failing over' just meant hammering the same host under
        a worse pace — a connection blip is handled by normal retry/backoff instead.
        Always returns False so callers fall through to their backoff path."""
        return False

    # JSON API calls should answer fast; a short read timeout means a dead/hung
    # pawchive.st is detected in ~20s (then we fail over to .pw) instead of stalling
    # on the default 60s per attempt.
    _API_TIMEOUT = (12, 20)

    def _fetch_json(self, session, url):
        """Resilient listing fetch: transient server errors back off & retry so a
        blip never truncates pagination (which would silently lose later pages)."""
        return self._request_with_retry(
            lambda u: fetch_json(session, u, timeout=self._API_TIMEOUT),
            url, f"listing {url}", attempts=10)

    def _read_post(self, post_id):
        url = f"{API}/{self._service}/user/{self._user_id}/post/{post_id}"
        return self._request_with_retry(
            lambda u: fetch_json(self._session, u, timeout=self._API_TIMEOUT),
            url, f"post {post_id}", attempts=6)

    @staticmethod
    def _retry_after(resp, default):
        """Seconds to wait from a Retry-After header, else `default` (clamped)."""
        try:
            ra = (resp.headers.get("Retry-After") or "").strip() if resp is not None else ""
            if ra.isdigit():
                return max(1, min(int(ra), 300))
        except Exception:
            pass
        return default

    @staticmethod
    def _is_ddos_guard(resp):
        """True if the response was served by DDoS-Guard (its block/limit pages)."""
        try:
            return (resp.headers.get("server") or "").lower() == "ddos-guard"
        except Exception:
            return False

    def _is_rate_block(self, resp):
        """A rate-limit / block response that must be RETRIED, not treated as a
        permanent 'gone': an explicit 429, a DDoS-Guard block page served as 403/503,
        or any 5xx server overload (all transient under sustained load)."""
        sc = resp.status_code
        if sc == 429 or sc >= 500:
            return True
        if sc == 403 and self._is_ddos_guard(resp):
            return True
        return False

    def _request_with_retry(self, call, url, what, attempts, backoff=3):
        b = backoff
        for attempt in range(1, attempts + 1):
            if self._cancelled():
                return None
            self._api_throttle.wait(self._cancelled)
            self._await_cooldown()   # respect a global DDoS-Guard quiet period
            if self._cancelled():
                return None
            target = self._url(url)
            self._diag.log("api_req", host=urlsplit(target).netloc, what=what,
                           attempt=attempt, interval=round(self._api_throttle.interval, 3))
            try:
                result = call(target)
                self._api_throttle.on_success()   # clean → probe a bit faster
                self._register_success()
                return result
            except HTTPError as e:
                resp = getattr(e, "response", None)
                status = getattr(resp, "status_code", None)
                # A DDoS-Guard block page (403/503) is transient — retry it like a
                # rate limit rather than giving up as 'not retriable'.
                dg_block = status in (403, 503) and self._is_ddos_guard(resp)
                if status not in _TRANSIENT_READ_STATUSES and not dg_block:
                    self._error(f"{what}: HTTP {status} (not retriable)")
                    return None
                wait_s = self._retry_after(resp, b)
                if status == 429 or dg_block:
                    self._api_throttle.on_throttled(wait_s)   # back the whole pool off
                    quiet, strikes = self._register_throttle(wait_s)
                    self._diag.log("api_429", host=urlsplit(target).netloc, what=what,
                                   attempt=attempt, retry_after=wait_s, quiet=quiet,
                                   strikes=strikes,
                                   interval=round(self._api_throttle.interval, 3),
                                   mirror=self._use_mirror,
                                   headers=self._diag.rl_headers(resp))
                err = f"HTTP {status}"
            except (ConnectionError, Timeout) as e:
                self._diag.log("api_conn_err", host=urlsplit(target).netloc,
                               what=what, attempt=attempt, err=e.__class__.__name__)
                # Host unreachable or not responding — .st and .pw are the same
                # backend now, so there's nothing to fail over to; back off and retry
                # the same host. (_failover_to_mirror is a no-op returning False.)
                if self._failover_to_mirror():
                    continue
                err = e.__class__.__name__
                wait_s = b
            except RequestException as e:
                err = e.__class__.__name__
                wait_s = b
            if attempt < attempts:
                self._info(f"{what}: {err}; retry {attempt}/{attempts} in {wait_s}s")
                self._sleep_cancellable(wait_s)
                b = min(b * 2, 120)
            else:
                self._info(f"{what}: {err}; gave up after {attempts} attempt(s)")
        return None

    # ── download ─────────────────────────────────────────────
    def _job_is_large(self, job):
        """Heavy transfer (video/archive) that belongs in the capped pool. Media
        jobs carry media_kind; ext jobs are classified from their URL."""
        mk = job.get("media_kind")
        if mk:
            return mk in _LARGE_KINDS
        kind, _ = _kind_and_ext(_url_basename(job["url"]))
        return kind in _LARGE_KINDS

    def _download_all(self, jobs):
        """One unified pool: every worker handles any file (image/video/archive) using
        its OWN session, so the CDN's per-connection bandwidth cap is beaten by running
        `workers` independent connections in parallel.

        Jobs are ordered small-first (images before videos/archives) so quick files
        never wait in the queue behind a burst of multi-GB packs. (A single very large
        file that lands last is handled by the per-file skip, not by ordering.)"""
        ordered = sorted(jobs, key=self._job_is_large)   # False(small) sorts before True
        with ThreadPoolExecutor(max_workers=self.workers,
                                thread_name_prefix="pawdl") as ex:
            futures = [ex.submit(self._process_job, j) for j in ordered]
            for _ in as_completed(futures):
                pass

    def _dl_session(self):
        """The calling worker thread's own download session (lazily created). Each
        worker gets an INDEPENDENT connection — the key to scaling past the CDN's
        per-connection bandwidth cap without tripping DDoS-Guard's shared-session
        blocks. The single-threaded API crawl keeps using self._session."""
        s = getattr(self._tls, "session", None)
        if s is None:
            s = make_session(cookies_path=self._cookies_path,
                             proxies=self._proxy_dict())
            self._tls.session = s
            with self._dl_sessions_lock:
                self._dl_sessions.append(s)
        return s

    def _reset_dl_session(self):
        """Drop the calling worker's session after a connection error so its next
        attempt opens a fresh connection instead of reusing a poisoned one."""
        s = getattr(self._tls, "session", None)
        if s is None:
            return
        self._tls.session = None
        with self._dl_sessions_lock:
            if s in self._dl_sessions:
                self._dl_sessions.remove(s)
        try:
            s.close()
        except Exception:
            pass

    def _proxy_dict(self):
        """Proxy config for sessions (Phase 2 hook; None until wired)."""
        proxy = getattr(self, "_proxy", None)
        return {"http": proxy, "https": proxy} if proxy else None

    def _close_dl_sessions(self):
        with self._dl_sessions_lock:
            sessions, self._dl_sessions = self._dl_sessions, []
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass

    def _process_job(self, job):
        if self._cancelled():
            return
        try:
            if job["jobkind"] == "media":
                self._process_media(job)
            else:
                self._process_ext(job)
        except Exception as e:
            self._bump("error")
            self._error(f"job failed ({job.get('url')}): {e}")

    def _process_media(self, job):
        entry = job["entry"]
        is_archive = job["media_kind"] == "archive"
        # A file the user skipped on a prior run — never re-download it.
        if self._archive and self._archive.is_skipped(entry):
            self._bump("skip")
            return
        # An archive we already unpacked (its .zip/.rar was deleted on success) —
        # nothing to re-download or re-extract.
        if is_archive and self._archive and self._archive.is_extracted(entry):
            self._bump("skip")
            return
        recorded = self._archive.get_filename(entry) if self._archive else None
        if recorded:
            prev = target_path(self._destination, job["media_kind"], job["dt"], recorded)
            if os.path.isfile(prev):
                # Archive on disk but not yet extracted (feature enabled after a prior
                # download, or a previous extraction failed) → extract it now.
                if is_archive and self._extract_enabled:
                    self._enqueue_extraction(prev, job, entry)
                self._bump("skip")   # already downloaded — never re-fetch
                return
            # In the archive but gone from disk -> re-fetch to the same name.
            self._download_stream(job["url"], prev, entry, job, recorded,
                                  job["media_kind"] == "video")
            return
        # Archives carry the post title so a zip/rar reveals which post it's from.
        post_title = job.get("post_title") if job["media_kind"] == "archive" else None
        # If the correctly-named file is already on disk (e.g. a prior download
        # whose archive was cleared, or files copied in) adopt it: record it and
        # skip, instead of downloading a duplicate. Claim it so a same-named item
        # in this same run still gets a '_n' suffix (real collision).
        natural = self._natural_name(
            job["dt"], job["media_kind"], job["name"],
            ordinal=job.get("ordinal"), ord_width=job.get("ord_width", 2),
            include_time=job.get("include_time", False), post_title=post_title)
        natural_path = target_path(self._destination, job["media_kind"], job["dt"], natural)
        with self._fname_lock:
            if natural_path not in self._claimed and os.path.isfile(natural_path):
                self._claimed.add(natural_path)
                self._bump("skip")
                self._record(entry, job, natural, job["media_kind"])
                return
        dest_path, filename = self._assign_path(
            job["dt"], job["media_kind"], job["name"],
            ordinal=job.get("ordinal"), ord_width=job.get("ord_width", 2),
            include_time=job.get("include_time", False), post_title=post_title)
        self._download_stream(job["url"], dest_path, entry, job, filename,
                              job["media_kind"] == "video")

    def _process_ext(self, job):
        """Auto-grab a single direct external file; record its outcome so the
        manifest can surface any failure for manual download."""
        entry = job["entry"]
        if self._archive and self._archive.is_skipped(entry):
            self._bump("skip")
            return
        name = _url_basename(job["url"])
        kind, _ext = _kind_and_ext(name)

        recorded = self._archive.get_filename(entry) if self._archive else None
        if recorded:
            prev = target_path(self._destination, kind, job["dt"], recorded)
            if os.path.isfile(prev):
                self._bump("skip")
                self._set_ext_state(job, "grabbed", filename=recorded)
                return

        dest_path, filename = self._assign_path(job["dt"], kind, name)
        ok = self._download_stream(job["url"], dest_path, entry, job, filename,
                                   kind == "video", bounded=True)
        if ok:
            self._set_ext_state(job, "grabbed", filename=filename)
        elif not self._cancelled():
            self._set_ext_state(job, "failed",
                                note=f"auto-download failed; intended name: {filename}")

    def _set_ext_state(self, job, status, filename=None, note=None):
        st = {"status": status}
        if filename:
            st["filename"] = filename
        if note:
            st["note"] = note
        with self._ext_lock:
            self._ext_results.setdefault(job["post_id"], {})[job["url"]] = st

    def _natural_name(self, dt, media_kind, original_name, ordinal=None,
                      ord_width=2, include_time=False, post_title=None):
        """The un-suffixed '<date> - SITE - <name>' filename a media item would get
        (before any '_n' collision suffix). Used both to place new files and to
        recognise an already-downloaded file on disk."""
        max_len = None
        if post_title:
            folder = os.path.dirname(
                target_path(self._destination, media_kind, dt, "_"))
            max_len = max(40, _WIN_PATH_LIMIT - len(folder) - 1 - _NAME_RESERVE)
        return build_filename(dt, self._service, original_name, ordinal=ordinal,
                              width=ord_width, include_time=include_time,
                              post_title=post_title, max_len=max_len)

    def _assign_path(self, dt, media_kind, original_name, ordinal=None,
                     ord_width=2, include_time=False, post_title=None):
        """Collision-free '<date> - SITE - <name>' path; '_n' before the ext on
        clash so nothing is ever overwritten. ordinal/include_time add the
        page-order decoration for multi-image posts, and post_title adds the post
        name to archives — truncated (title only) to keep the full path within the
        Windows limit (see build_filename)."""
        base = self._natural_name(dt, media_kind, original_name, ordinal=ordinal,
                                  ord_width=ord_width, include_time=include_time,
                                  post_title=post_title)
        with self._fname_lock:
            path = target_path(self._destination, media_kind, dt, base)
            if path not in self._claimed and not os.path.isfile(path):
                self._claimed.add(path)
                return path, base
            n = 1
            while True:
                cand = add_index_suffix(base, n)
                cand_path = target_path(self._destination, media_kind, dt, cand)
                if cand_path not in self._claimed and not os.path.isfile(cand_path):
                    self._claimed.add(cand_path)
                    return cand_path, cand
                n += 1

    def _active_inc(self):
        with self._active_lock:
            self._active += 1
            if self._active > self._active_max:
                self._active_max = self._active

    def _active_dec(self):
        with self._active_lock:
            self._active -= 1

    def _download_stream(self, url, dest_path, entry, job, filename, is_video,
                         bounded=False):
        """Diagnostics wrapper around the streaming loop: tracks concurrency and
        (on success, via _finalize) per-file throughput."""
        kind = job.get("media_kind") or ("video" if is_video else "image")
        job["_dl_t0"] = time.monotonic()
        self._active_inc()
        self._diag.log("dl_start", file=filename, kind=kind,
                       active=self._active, mirror=self._use_mirror)
        self._dl_event("dl_start", entry, filename)   # UI active-downloads panel
        ok = False
        try:
            ok = self._run_download(url, dest_path, entry, job, filename,
                                    is_video, bounded)
            return ok
        finally:
            self._active_dec()
            if not ok:
                fail = job.get("_dl_fail") or {}
                # A user "skip": abandon the partial and remember it so future runs
                # never re-fetch it. (Done here, after the stream/file handles closed.)
                if fail.get("reason") == "skipped":
                    self._discard(dest_path + ".part")
                    self._mark_skipped(job, entry, filename)
                self._diag.log("dl_end_fail", file=filename, kind=kind,
                               secs=round(time.monotonic() - job["_dl_t0"], 2),
                               reason=fail.get("reason"), status=fail.get("status"),
                               attempt=fail.get("attempt"), mirror=self._use_mirror)
            self._dl_event("dl_stop", entry)   # remove the row from the UI panel

    def _dl_event(self, kind, entry, filename=None):
        """Push an active-download start/stop event to the UI (keyed by entry id) so
        the frontend can show a live list with a per-file Skip button."""
        if not self._on_progress:
            return
        msg = {"type": kind, "id": entry}
        if filename is not None:
            msg["message"] = filename
        self._on_progress(msg)

    def _mark_skipped(self, job, entry, filename):
        """Record a user-skipped download so future runs never re-fetch it."""
        if self._archive:
            year = f"{job['dt']:%Y}" if job.get("dt") else "unknown"
            kind = job.get("media_kind") or "file"
            try:
                self._archive.set_skipped(entry, job.get("post_id"), filename, kind, year)
            except Exception:
                pass
        self._bump("skip")
        self._info(f"Skipped (won't re-download): {filename}")

    def _fail(self, job, reason, status=None, attempt=None):
        """Record *why* a download gave up so dl_end_fail logs it instead of failing
        blind. Always returns False, for use as `return self._fail(...)`."""
        job["_dl_fail"] = {"reason": reason, "status": status, "attempt": attempt}
        return False

    def _run_download(self, url, dest_path, entry, job, filename, is_video,
                      bounded=False):
        """Stream to <dest>.part with HTTP Range resume + size/ffprobe verify,
        then atomically finalize. Retries transient/network errors with backoff;
        `bounded` caps attempts (used for external files, which shouldn't hang a
        run) whereas on-site media retries up to max_download_attempts. Returns
        True on success, False on give-up/cancel."""
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        part = dest_path + ".part"
        attempt = 0
        # Short initial backoff: the CDN's connection blips (curl 18/28/35) clear
        # almost instantly and a resume from the .part costs nothing, so retry fast
        # rather than sitting idle. Still doubles toward the cap if a host stays down.
        backoff = 2
        cap = self.max_download_attempts
        job.pop("_dl_fail", None)   # clear any stale reason from a prior attempt

        while not self._cancelled():
            attempt += 1
            if self._skip_requested(entry):
                return self._fail(job, "skipped", attempt=attempt)
            if bounded and attempt > cap:
                # external files shouldn't hang a run indefinitely
                return self._fail(job, "bounded_cap", attempt=attempt)
            try:
                resume_pos = os.path.getsize(part) if os.path.exists(part) else 0
                headers = {}
                open_mode = "wb"
                if resume_pos:
                    headers["Range"] = f"bytes={resume_pos}-"
                    open_mode = "ab"

                self._file_throttle.wait(self._cancelled)
                if self._cancelled():
                    return self._fail(job, "cancelled", attempt=attempt)
                self._diag.bump("file_req")
                target = self._url(url)
                # Each worker uses its OWN session (independent connection) — the key
                # to scaling past the CDN's per-connection bandwidth cap. closing()
                # not `with ... as r`: curl_cffi's Response has .close() but is NOT a
                # context manager (requests' is); closing() works for both.
                sess = self._dl_session()
                with closing(sess.get(
                    target, stream=True, headers=headers,
                    timeout=(self.connect_timeout, self.read_timeout),
                )) as r:
                    total = expected_total(r, resume_pos)

                    if self._is_rate_block(r):
                        # DDoS-Guard rate-limit/block (429, a 403/503 block page, or a
                        # 5xx overload) — NEVER a permanent 'gone'. Back THIS worker off
                        # briefly and retry (with resume); the other workers keep
                        # flowing. Independent per-worker sessions make blocks rare, so
                        # a per-connection backoff beats freezing the whole pool.
                        wait_s = min(self._retry_after(r, backoff), 30)
                        self._diag.log("file_block", file=filename, kind=job.get("media_kind"),
                                       host=urlsplit(target).netloc, code=r.status_code,
                                       retry_after=wait_s, active=self._active,
                                       size=total, attempt=attempt,
                                       headers=self._diag.rl_headers(r))
                        self._sleep_cancellable(wait_s)
                        backoff = min(backoff * 2, 60)
                        continue

                    if r.status_code in (401, 403, 404, 410):
                        # A genuine forbidden/missing file (e.g. a removed catbox link);
                        # DDoS-Guard 403/503 blocks were already handled above.
                        self._error(f"file gone (HTTP {r.status_code}): {url}")
                        return self._fail(job, "http_gone", status=r.status_code,
                                          attempt=attempt)

                    if r.status_code == 416:
                        if (resume_pos and total is not None and resume_pos == total
                                and os.path.exists(part)):
                            if self._finalize(part, dest_path, entry, job, filename,
                                              total, is_video):
                                return True
                            continue
                        self._discard(part)
                        continue

                    if resume_pos and r.status_code == 200:
                        resume_pos = 0
                        open_mode = "wb"

                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if r.status_code == 200 and "text/html" in ctype:
                        # a landing/error page where a file was expected
                        if bounded and attempt >= cap:
                            self._error(f"expected a file, got HTML: {url}")
                            return self._fail(job, "expected_html", status=200,
                                              attempt=attempt)
                        self._sleep_cancellable(backoff)
                        backoff = min(backoff * 2, 120)
                        continue

                    r.raise_for_status()
                    with open(part, open_mode) as f:
                        for chunk in r.iter_content(chunk_size=self.chunk_size):
                            if self._cancelled():
                                return self._fail(job, "cancelled", attempt=attempt)
                            if self._skip_requested(entry):
                                # abandon this file mid-stream; .part cleaned up + the
                                # skip remembered in _download_stream's finally.
                                return self._fail(job, "skipped", attempt=attempt)
                            if chunk:
                                f.write(chunk)

                size = os.path.getsize(part) if os.path.exists(part) else 0
                if total is not None and size < total:
                    self._info(f"Incomplete ({size}/{total}) for {filename}, resuming...")
                    self._sleep_cancellable(2)
                    continue
                if total is not None and size > total:
                    self._discard(part)
                    continue

                self._file_throttle.on_success()   # clean transfer → ease back up
                self._register_success()           # decay any DDoS-Guard escalation
                if self._finalize(part, dest_path, entry, job, filename,
                                  total if total is not None else size, is_video):
                    return True
                continue
            except (ConnectionError, Timeout) as e:
                if self._cancelled():
                    return self._fail(job, "cancelled", attempt=attempt)
                # Connection reset / read timeout mid-download — the CDN is flaky and
                # breaks streams even in a browser. Keep the .part and RESUME on the
                # next attempt (Range from resume_pos). Back off THIS worker only; the
                # others keep flowing. Drop this worker's session so the next attempt
                # gets a fresh connection instead of a possibly-poisoned one.
                self._fail(job, e.__class__.__name__, attempt=attempt)
                self._reset_dl_session()
                self._diag.log("file_neterr", file=filename, err=e.__class__.__name__,
                               attempt=attempt)
                self._info(f"Retry {attempt} for {filename}: {e.__class__.__name__} (resuming)")
                self._sleep_cancellable(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                if self._cancelled():
                    return self._fail(job, "cancelled", attempt=attempt)
                self._fail(job, e.__class__.__name__, attempt=attempt)
                self._reset_dl_session()
                self._diag.log("file_neterr", file=filename, err=e.__class__.__name__,
                               attempt=attempt)
                self._info(f"Retry {attempt} for {filename}: {e} (resuming)")
                self._sleep_cancellable(backoff)
                backoff = min(backoff * 2, 60)

            if bounded and attempt >= cap:
                return self._fail(job, "bounded_cap", attempt=attempt)
        return self._fail(job, "cancelled", attempt=attempt)

    def _finalize(self, part, dest_path, entry, job, filename, expected_size, is_video):
        if is_video and not ffprobe_ok(part):
            self._discard(part)
            self._info(f"ffprobe validation failed for {filename}, re-downloading")
            return False
        os.replace(part, dest_path)
        self._bump("download")
        media_kind = job.get("media_kind") or ("video" if is_video else "image")
        self._record(entry, job, filename, media_kind, expected_size)
        # Per-file throughput — the key signal for "is the site slow or is it us".
        dur = round(time.monotonic() - job.get("_dl_t0", time.monotonic()), 2)
        self._diag.log("dl_end_ok", file=filename, kind=media_kind,
                       bytes=expected_size, secs=dur, active=self._active,
                       mbps=(round((expected_size or 0) / 1e6 / dur, 2) if dur > 0 else None),
                       mirror=self._use_mirror)
        self._progress_download(filename)
        # A freshly-downloaded archive: hand it to the background extractor (one at a
        # time) so its media is sorted into the library and the pack is unpacked.
        if media_kind == "archive":
            self._enqueue_extraction(dest_path, job, entry)
        return True

    # ── archive extraction ───────────────────────────────────
    def _enqueue_extraction(self, archive_path, job, entry):
        """Queue an archive for background extraction (single worker, one at a time).
        No-op if disabled or the pool isn't running."""
        if not self._extract_enabled or self._extract_pool is None:
            return
        try:
            fut = self._extract_pool.submit(self._extract_archive, archive_path, job, entry)
        except RuntimeError:
            return  # pool already shutting down
        with self._extract_lock:
            self._extract_futures.append(fut)

    def _drain_extractions(self):
        """Wait for all queued extractions to finish (called before reconcile)."""
        with self._extract_lock:
            futures = list(self._extract_futures)
        for fut in futures:
            try:
                fut.result()
            except Exception as e:
                self._info(f"extract worker error: {e}")

    def _shutdown_extract_pool(self):
        if self._extract_pool is not None:
            try:
                self._extract_pool.shutdown(wait=True)
            except Exception:
                pass
            self._extract_pool = None

    def _extract_archive(self, archive_path, job, entry):
        """Unpack a downloaded archive into the library: images → Images/<year>/ and
        videos → <year>/ (named '<date> - SITE - [subfolder - …] - name'), everything
        else preserved under <year>/<archive-stem>/. On full success the original
        archive is deleted and the entry marked extracted (idempotent re-runs)."""
        if self._cancelled() or not os.path.isfile(archive_path):
            return
        stem = os.path.splitext(os.path.basename(archive_path))[0]
        tmp = os.path.join(self._destination, ".pawchive_extract_tmp", uuid.uuid4().hex)
        leftover_root = target_path(self._destination, "archive", job["dt"], stem)
        media_n = other_n = 0
        try:
            os.makedirs(tmp, exist_ok=True)
            if not pawchive_extract.extract_to_temp(archive_path, tmp):
                self._info(f"Could not extract {os.path.basename(archive_path)} "
                           f"(unsupported/failed) — left in place")
                return
            for root, _dirs, files in os.walk(tmp):
                for fn in files:
                    if self._cancelled():
                        return
                    src = os.path.join(root, fn)
                    rel = os.path.relpath(src, tmp)
                    parts = rel.replace("\\", "/").split("/")
                    folder_parts, name = parts[:-1], parts[-1]
                    kind = pawchive_extract.classify(name)
                    if kind in ("image", "video"):
                        display = " - ".join(
                            [self._sanitize_part(p) for p in folder_parts] + [name])
                        dest, _fname = self._assign_path(job["dt"], kind, display,
                                                         post_title=None)
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        shutil.move(src, dest)
                        media_n += 1
                    else:
                        dest = os.path.join(leftover_root, *[self._sanitize_part(p)
                                                             for p in folder_parts], name)
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        if not os.path.exists(dest):
                            shutil.move(src, dest)
                        other_n += 1
            # Full success → mark it done. If the pack had non-media "leftovers" a
            # <stem>/ folder was created for them; keep the original archive by moving
            # it INTO that folder (a game/asset project stays complete + re-extractable).
            # A pure-media pack has no such folder, so the redundant archive is deleted.
            if other_n > 0:
                try:
                    os.makedirs(leftover_root, exist_ok=True)
                    shutil.move(archive_path,
                                os.path.join(leftover_root, os.path.basename(archive_path)))
                except Exception:
                    self._discard(archive_path)
            else:
                self._discard(archive_path)
            if self._archive:
                self._archive.set_extracted(entry)
            with self._count_lock:
                self._extracted_count += media_n
            self._info(f"Extracted {media_n} media + {other_n} other file(s) from "
                       f"{os.path.basename(archive_path)}")
        except Exception as e:
            # Keep the archive (don't mark extracted) so a later run retries.
            self._info(f"extract failed for {os.path.basename(archive_path)}: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _sanitize_part(part):
        """Make one path component safe (used for subfolder names folded into a
        filename and for leftover subfolders)."""
        from backend.pawchive_scraper import _ILLEGAL_FS, _WS
        cleaned = _WS.sub(" ", _ILLEGAL_FS.sub("", part or "")).strip().strip(".").strip()
        return cleaned or "_"

    @staticmethod
    def _discard(part):
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass

    def _sleep_cancellable(self, seconds):
        slept = 0.0
        while slept < seconds and not self._cancelled():
            time.sleep(0.5)
            slept += 0.5

    # ── reconciliation ───────────────────────────────────────
    def _reconcile(self, media_jobs):
        missing = [j for j in media_jobs if not self._job_present(j)]
        total = len(media_jobs)
        if not missing:
            self._info(f"Verified {total}/{total} media item(s) present on disk.")
            return
        self._info(f"Reconciling {len(missing)} missing item(s)...")
        for job in missing:
            if self._cancelled():
                break
            self._process_media(job)
        present = sum(1 for j in media_jobs if self._job_present(j))
        self._info(f"Verified {present}/{total} media item(s) present on disk.")

    def _job_present(self, job):
        # A skipped or extracted entry counts as present (its file may be gone), so
        # reconcile never re-downloads it.
        if self._archive and self._archive.is_skipped(job["entry"]):
            return True
        if (job.get("media_kind") == "archive" and self._archive
                and self._archive.is_extracted(job["entry"])):
            return True
        fn = self._archive.get_filename(job["entry"]) if self._archive else None
        if not fn:
            return False
        return os.path.isfile(target_path(self._destination, job["media_kind"],
                                          job["dt"], fn))

    # ── manifest finalize ────────────────────────────────────
    def _save_links(self):
        """Persist the manifest, first pulling in any 'resolved' flags the user ticked
        on disk while we held it open — otherwise our long-lived in-memory copy would
        clobber their check-offs when the run finishes."""
        if not self._links:
            return
        try:
            self._links.reload_resolved_from_disk()
            self._links.save()
        except OSError as e:
            self._info(f"manifest write note: {e}")

    def _finalize_manifest(self, posts):
        """Re-record posts whose direct links were attempted, so grabbed/failed
        statuses land in the manifest (upsert preserves user 'resolved' flags)."""
        if not self._links:
            return
        with self._ext_lock:
            results = dict(self._ext_results)
        for pid, states in results.items():
            post = posts.get(pid)
            if post is None:
                continue
            self._links.upsert_post(post, states)
        self._save_links()

    # ── helpers ──────────────────────────────────────────────
    def _record(self, entry, job, filename, media_kind, expected_size=None):
        if self._archive:
            year = f"{job['dt']:%Y}" if job["dt"] else "unknown"
            self._archive.record(entry, job["post_id"], filename, media_kind, year,
                                 expected_size, None)

    def _bump(self, kind):
        with self._count_lock:
            if kind == "download":
                self.downloaded_count += 1
            elif kind == "skip":
                self.skipped_count += 1
            elif kind == "error":
                self.error_count += 1

    def _progress_download(self, filename):
        if self._on_progress:
            self._on_progress({"type": "download", "message": filename})

    def _info(self, message):
        if self._on_progress:
            self._on_progress({"type": "info", "message": message})

    def _error(self, message):
        if self._on_progress:
            self._on_progress({"type": "error", "message": message})

    def _stats(self, cancelled):
        return {"downloaded": self.downloaded_count, "skipped": self.skipped_count,
                "errors": self.error_count, "cancelled": cancelled}

    def _finish(self, on_complete):
        on_complete(self._stats(cancelled=self._cancelled()))
