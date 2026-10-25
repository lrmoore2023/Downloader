"""Per-run site health for PMV checks: stop hammering a site that is down.

A PMV check visits many links per site. When a site is down every request
burns its full retry budget (4 attempts with growing back-off and 15 s connect
timeouts), so 30 links on a dead site used to cost the better part of an hour
before the other sites got their turn. `SiteHealth` is shared by the feed phase
and PmvRunner for one run: once a site looks down, every remaining link on it is
skipped at once with a recorded error, and the run moves on to the other sites.

Nothing is lost by skipping: a skipped link is recorded as failed exactly like
one that was tried, so the check fetches it again next time (pending feed
videos stay pending, pawchive signatures aren't advanced, manifests keep their
old last_fetch).

What counts as down: the scrapers' retry loops end with "… after N attempts"
(timeouts, connection errors, 5xx, 429) or name a requests exception. A 404, a
login problem or a parse error is about one link, never the site. A connection
failure trips the site at once; HTTP-level failures (5xx/429 after retries) need
TRIP_AFTER in a row, so one broken page doesn't take a healthy site out.
"""

import re
import threading

TRIP_AFTER = 2

_CONNECTION_RE = re.compile(
    r"\b(?:ConnectionError|ConnectTimeout|ReadTimeout|Timeout|SSLError|ProxyError|"
    r"ChunkedEncodingError|NewConnectionError|NameResolutionError|unreachable|not responding)\b", re.I)
_HTTP_RE = re.compile(r"after \d+ attempts|\bHTTP (?:5\d\d|429)\b", re.I)


def outage_kind(message):
    """'connection' / 'http' when an error message says the site itself is
    failing, else None."""
    msg = str(message or "")
    if _CONNECTION_RE.search(msg):
        return "connection"
    if _HTTP_RE.search(msg):
        return "http"
    return None


class SiteHealth:
    def __init__(self, trip_after=TRIP_AFTER):
        self.trip_after = trip_after
        self._lock = threading.Lock()
        self._streak = {}
        self._down = {}            # platform → first message that tripped it
        self.skipped = {}          # platform → links skipped because it was down

    def is_down(self, platform):
        with self._lock:
            return platform in self._down

    def reason(self, platform):
        with self._lock:
            return self._down.get(platform, "")

    def ok(self, platform):
        with self._lock:
            self._streak[platform] = 0

    def failed(self, platform, message, weak=False):
        """Record a failure; True when the site is (now) considered down.
        `weak` (a feed: a different endpoint from the links themselves) only
        ever counts as one strike."""
        kind = outage_kind(message)
        with self._lock:
            if platform in self._down:
                return True
            if kind is None:
                self._streak[platform] = 0
                return False
            n = self._streak.get(platform, 0) + 1
            self._streak[platform] = n
            if (kind == "connection" and not weak) or n >= self.trip_after:
                self._down[platform] = str(message)[:300]
                return True
            return False

    def note_skip(self, platform):
        with self._lock:
            self.skipped[platform] = self.skipped.get(platform, 0) + 1

    def down(self):
        """{platform: {"message", "skipped"}} for every site that tripped."""
        with self._lock:
            return {p: {"message": m, "skipped": self.skipped.get(p, 0)} for p, m in self._down.items()}
