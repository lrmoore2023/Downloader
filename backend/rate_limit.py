"""Shared request pacing for the scraping backends.

Extracted from pawchive_runner so coomerfans can use the same AIMD pacer
(pawchive_runner keeps importing it from here). The coomerfans bot-guard adds
one extra input the DDoS-Guard path never had: the server reports how close we
are to tripping a challenge *before* it trips, via `X-Bg-Score`. `on_score`
turns that into pre-emptive backoff — see AdaptiveThrottle.on_score.
"""

import threading
import time


class AdaptiveThrottle:
    """Self-tuning request pacer shared by all workers (AIMD).

    Each caller reserves the next time slot, so N workers issue at most one
    request per `interval` overall. The interval adapts: it eases *down* toward
    `floor` on every clean response (probing for the fastest safe rate) and jumps
    *up* toward `ceil` when throttled (respecting Retry-After), so the whole pool
    backs off together instead of each worker hammering the same wall. Used with a
    tight profile for the rate-limit-sensitive API and a loose one for the file CDN."""

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

    def on_score(self, score, warn_at, trip_at, cooloff=0.0):
        """React to a server-reported "how suspicious are you" score.

        coomerfans returns `X-Bg-Score` on *every* response and serves a 503
        challenge once it crosses ~1.0. The score is a slowly-decaying *budget*,
        not an instantaneous rate gate: a strictly serial crawl still walks it
        from 0.6 to 1.3 over ~160 requests. So there is no "safe rate" to settle
        at — the only way to finish a long crawl without tripping is to widen the
        interval as the score climbs, and idle outright near the top so the
        budget can decay. Tripping costs the whole pool ~15s of hard 503s, so
        `cooloff` seconds of voluntary idle above 90% of the trip line is cheap.
        Below `warn_at` this is a no-op and normal on_success recovery applies.
        """
        if score is None or score < warn_at:
            return
        span = max(trip_at - warn_at, 1e-6)
        ratio = min((score - warn_at) / span, 1.0)
        # 1x at the warn line rising to 6x as the score approaches the trip line.
        factor = 1.0 + 5.0 * ratio
        with self._lock:
            self.interval = min(self.ceil, max(self.interval, self.floor * factor))
            if cooloff and score >= trip_at * 0.9:
                self._next = max(self._next, time.monotonic() + cooloff)
