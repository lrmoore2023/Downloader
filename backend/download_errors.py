"""Per-creator-link persistent record of download failures.

The download engines (coomerfans/pawchive/twitter) log give-ups only to the
in-memory job dict + the diagnostics file, which is overwritten every run — so
after a run finishes nothing durably knows *which* files failed or *where* they
came from. This store fixes that: every non-transient give-up is recorded here
with enough to (a) surface a per-creator "N errors" affordance across app
restarts, (b) re-attempt the file later ("Redownload Errors"), and (c) hand the
user the direct URL + post page for anything genuinely gone upstream.

One SQLite DB per creator-link (sibling of that link's archive DB, see
creator_runner.errors_db_path). A successful (re)download clears the row; a
failure that survives a recovery attempt is flipped to state='gone'.

Thread-safe (the download pool writes concurrently): every access is guarded by
a lock and the connection is check_same_thread=False.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


# state values
FAILED = "failed"        # failed at least once; eligible for a redownload attempt
GONE = "gone"            # a recovery attempt confirmed it's gone upstream (manual only)
DISMISSED = "dismissed"  # user checked it off — hidden and never resurrected by a re-failure


class FailureStore:
    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS failures (
                entry        TEXT PRIMARY KEY,
                platform     TEXT,
                service      TEXT,
                user_id      TEXT,
                post_id      TEXT,
                filename     TEXT,
                url          TEXT,
                page_url     TEXT,
                media_kind   TEXT,
                status       INTEGER,
                reason       TEXT,
                attempts     INTEGER DEFAULT 0,
                first_seen   TEXT,
                last_attempt TEXT,
                state        TEXT DEFAULT 'failed'
            )
            """
        )
        self._conn.commit()

    # ── writes ───────────────────────────────────────────────
    def record_failure(self, entry, *, platform=None, service=None, user_id=None,
                       post_id=None, filename=None, url=None, page_url=None,
                       media_kind=None, status=None, reason=None, state=FAILED):
        """Upsert a failure. On re-failure of the same entry, bump `attempts` and
        refresh the mutable fields (url/status/reason/…) while preserving
        `first_seen`. A new failure always resets state to `failed` (a file that
        was 'gone' but failed again is worth another look)."""
        if not entry:
            return
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts, first_seen, state FROM failures WHERE entry = ?",
                (entry,)).fetchone()
            attempts = (row[0] or 0) + 1 if row else 1
            first_seen = row[1] if row and row[1] else now
            # A user-dismissed entry stays dismissed even if it fails again — the
            # check-off is sticky (mirrors the pending-links 'resolved' behaviour).
            if row and row[2] == DISMISSED:
                state = DISMISSED
            self._conn.execute(
                """
                INSERT OR REPLACE INTO failures
                (entry, platform, service, user_id, post_id, filename, url,
                 page_url, media_kind, status, reason, attempts, first_seen,
                 last_attempt, state)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (entry, platform, service, user_id, post_id, filename, url,
                 page_url, media_kind, status, reason, attempts, first_seen,
                 now, state),
            )
            self._conn.commit()

    def clear_failure(self, entry):
        """Remove a failure — call on a successful (re)download."""
        if not entry:
            return
        with self._lock:
            self._conn.execute("DELETE FROM failures WHERE entry = ?", (entry,))
            self._conn.commit()

    def clear_by_page_url(self, page_url):
        """Drop non-dismissed failures for one source page (e.g. an album URL).

        Album engines re-record failures every run but can't map a later success
        back to a specific entry, so a re-run of an album clears its prior
        failures first and then records only what still fails — otherwise old,
        already-resolved failures compound across runs. Dismissed entries are
        left alone so a user's dismissal stays sticky."""
        if not page_url:
            return
        with self._lock:
            self._conn.execute(
                "DELETE FROM failures WHERE page_url = ? AND state != 'dismissed'",
                (page_url,))
            self._conn.commit()

    def clear_all(self, platform=None):
        """Wipe the store (optionally for one platform). Used by platforms that
        can't map a success back to a specific entry (twitter/gallery-dl): the
        recovery run clears first, then re-records only what still fails."""
        with self._lock:
            if platform:
                self._conn.execute(
                    "DELETE FROM failures WHERE platform = ?", (platform,))
            else:
                self._conn.execute("DELETE FROM failures")
            self._conn.commit()

    def mark_gone(self, entry, page_url=None, status=None, url=None):
        """Flip a failure to 'gone' after a recovery attempt confirmed it can't be
        fetched from the source. Optionally refresh the URL/page_url/status so the
        UI shows the freshest link for a manual grab."""
        sets = ["state = ?", "last_attempt = ?"]
        vals = [GONE, _now()]
        if page_url is not None:
            sets.append("page_url = ?"); vals.append(page_url)
        if url is not None:
            sets.append("url = ?"); vals.append(url)
        if status is not None:
            sets.append("status = ?"); vals.append(status)
        vals.append(entry)
        with self._lock:
            self._conn.execute(
                f"UPDATE failures SET {', '.join(sets)} WHERE entry = ?", vals)
            self._conn.commit()

    def dismiss(self, entry):
        """Hide an entry from the UI for good (the user checked it off). Kept as a
        row — not deleted — so record_failure won't resurrect it on a later run."""
        if not entry:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE failures SET state = ?, last_attempt = ? WHERE entry = ?",
                (DISMISSED, _now(), entry))
            self._conn.commit()
            return cur.rowcount > 0

    def restore(self, entry):
        """Un-dismiss an entry back to 'failed' (the undo of dismiss). Only acts on a
        dismissed row so it can't override a 'gone' verdict."""
        if not entry:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE failures SET state = ?, last_attempt = ? "
                "WHERE entry = ? AND state = ?",
                (FAILED, _now(), entry, DISMISSED))
            self._conn.commit()
            return cur.rowcount > 0

    # ── reads ────────────────────────────────────────────────
    def list_failures(self, state=None):
        """Rows in one `state`, or several if `state` is an iterable of states, or
        all rows when None."""
        with self._lock:
            if state is None:
                cur = self._conn.execute("SELECT * FROM failures")
            elif isinstance(state, (list, tuple, set)):
                states = list(state)
                marks = ",".join("?" * len(states))
                cur = self._conn.execute(
                    f"SELECT * FROM failures WHERE state IN ({marks})", states)
            else:
                cur = self._conn.execute(
                    "SELECT * FROM failures WHERE state = ?", (state,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def count(self, state=None):
        with self._lock:
            if state:
                cur = self._conn.execute(
                    "SELECT COUNT(*) FROM failures WHERE state = ?", (state,))
            else:
                cur = self._conn.execute("SELECT COUNT(*) FROM failures")
            return cur.fetchone()[0]

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


def open_readonly(db_path):
    """Open an existing store read-only (for status queries) without creating the
    file. Returns a FailureStore-like reader, or None if the DB doesn't exist."""
    if not db_path or not os.path.isfile(db_path):
        return None
    return FailureStore(db_path)
