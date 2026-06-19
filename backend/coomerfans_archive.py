"""Per-creator SQLite download archive for the coomerfans tab.

Mirrors the spirit of archive_sync.py (Twitter tab) but is keyed per media
item. Because filenames use the post's title slug (not the post id), the
post id is NOT recoverable from disk, so this archive is the authoritative
record of "what has been downloaded" — it stores the chosen filename for each
item so re-runs are idempotent and distinct items never overwrite each other.

Entry key: "coomerfans_{post_id}_{index}".

Thread-safe: the runner downloads with a worker pool, so every DB access is
guarded by a lock and the connection is opened with check_same_thread=False.
"""

import os
import sqlite3
import threading


def entry_key(post_id, index):
    return f"coomerfans_{post_id}_{index}"


class Archive:
    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS archive (
                entry         TEXT PRIMARY KEY,
                post_id       TEXT,
                filename      TEXT,
                kind          TEXT,
                year          TEXT,
                expected_size INTEGER,
                path_key      TEXT
            )
            """
        )
        # Migrate older DBs that predate expected_size/path_key.
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(archive)")}
        for col, decl in (("expected_size", "INTEGER"), ("path_key", "TEXT")):
            if col not in existing:
                self._conn.execute(f"ALTER TABLE archive ADD COLUMN {col} {decl}")
        self._conn.commit()

    # ── lookups ──────────────────────────────────────────────
    def has(self, entry):
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM archive WHERE entry = ?", (entry,)
            )
            return cur.fetchone() is not None

    def get_filename(self, entry):
        with self._lock:
            cur = self._conn.execute(
                "SELECT filename FROM archive WHERE entry = ?", (entry,)
            )
            row = cur.fetchone()
            return row[0] if row else None

    def post_seen(self, post_id):
        """True if any media for this post has been recorded (used by the
        'fetch latest' mode to skip whole posts already downloaded)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM archive WHERE post_id = ? LIMIT 1", (post_id,)
            )
            return cur.fetchone() is not None

    def filenames_in_year(self, year):
        with self._lock:
            cur = self._conn.execute(
                "SELECT filename FROM archive WHERE year = ?", (str(year),)
            )
            return {row[0] for row in cur.fetchall() if row[0]}

    # ── writes ───────────────────────────────────────────────
    def record(self, entry, post_id, filename, kind, year, expected_size=None, path_key=None):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO archive "
                "(entry, post_id, filename, kind, year, expected_size, path_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entry, str(post_id), filename, kind, str(year), expected_size, path_key),
            )
            self._conn.commit()

    def rows(self):
        """All archive rows as dicts (used by Verify & Repair)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT entry, post_id, filename, kind, year, expected_size, path_key FROM archive"
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def remove_post(self, post_id):
        with self._lock:
            self._conn.execute(
                "DELETE FROM archive WHERE post_id = ?", (str(post_id),)
            )
            self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
