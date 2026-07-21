"""Shared, persistent cache of perceptual signatures, keyed by normalised path.

One DB for every creator, living beside the download archives in ``archive_dir``
(``P:\\Utility\\Database\\Downloader\\media_sig_cache.db``). A row is only valid
while the file's (mtime, size) are unchanged, so an in-place content swap or any
edit forces a rehash automatically.

Class shape (lock + ``check_same_thread=False`` + PRAGMA-based column migration)
mirrors coomerfans_archive.Archive so the codebase has one storage idiom.
"""

import json
import os
import sqlite3
import threading

from backend.media_hash import norm_path

CACHE_FILENAME = "media_sig_cache.db"


def cache_path(archive_dir):
    """Preferred cache location: the shared archive dir; falls back to None when
    it is unset/unreachable (the feature then runs without a persistent cache)."""
    ad = (archive_dir or "").strip()
    if ad and os.path.isdir(ad):
        return os.path.join(ad, CACHE_FILENAME)
    return None


class SigCache:
    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sig (
                path_key TEXT PRIMARY KEY,
                mtime    INTEGER,
                size     INTEGER,
                sha256   TEXT,
                kind     TEXT,
                ahash    TEXT,
                phash    TEXT,
                orient   TEXT,
                width    INTEGER,
                height   INTEGER,
                duration REAL,
                windows  TEXT
            )
            """
        )
        # Forward-compatible migration for older cache DBs.
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(sig)")}
        for col, decl in (
            ("sha256", "TEXT"), ("kind", "TEXT"), ("ahash", "TEXT"),
            ("phash", "TEXT"), ("orient", "TEXT"), ("width", "INTEGER"),
            ("height", "INTEGER"), ("duration", "REAL"), ("windows", "TEXT"),
        ):
            if col not in existing:
                self._conn.execute(f"ALTER TABLE sig ADD COLUMN {col} {decl}")
        self._conn.commit()

    # ── lookups ──────────────────────────────────────────────
    def get(self, path, mtime, size):
        """Return the cached signature dict for ``path`` iff (mtime, size) match,
        else None. Shape matches media_hash.compute_signature plus sha256/kind."""
        key = norm_path(path)
        with self._lock:
            cur = self._conn.execute(
                "SELECT mtime, size, sha256, kind, ahash, phash, orient, "
                "width, height, duration, windows FROM sig WHERE path_key = ?",
                (key,),
            )
            row = cur.fetchone()
        if not row:
            return None
        if int(row[0] or -1) != int(mtime) or int(row[1] or -1) != int(size):
            return None                       # stale -> caller rehashes
        return {
            "sha256": row[2],
            "kind": row[3],
            "ahash": row[4],
            "phash": row[5],
            "orient": json.loads(row[6]) if row[6] else None,
            "width": row[7],
            "height": row[8],
            "duration": row[9],
            "windows": json.loads(row[10]) if row[10] else None,
        }

    # ── writes ───────────────────────────────────────────────
    def put(self, path, mtime, size, kind, sig, sha256=None):
        """Upsert a signature. ``sig`` is a media_hash signature dict (or {} for a
        file that produced no perceptual hash — the row still records mtime/size so
        we don't retry a broken file every scan)."""
        key = norm_path(path)
        sig = sig or {}
        orient = sig.get("orient")
        windows = sig.get("windows")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sig (path_key, mtime, size, sha256, kind, ahash, phash,
                                 orient, width, height, duration, windows)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path_key) DO UPDATE SET
                    mtime=excluded.mtime, size=excluded.size, sha256=excluded.sha256,
                    kind=excluded.kind, ahash=excluded.ahash, phash=excluded.phash,
                    orient=excluded.orient, width=excluded.width,
                    height=excluded.height, duration=excluded.duration,
                    windows=excluded.windows
                """,
                (
                    key, int(mtime), int(size), sha256, kind,
                    sig.get("ahash"), sig.get("phash"),
                    json.dumps(orient) if orient else None,
                    sig.get("width"), sig.get("height"), sig.get("duration"),
                    json.dumps(windows) if windows else None,
                ),
            )
            self._conn.commit()

    def find_by_content(self, sha256, size):
        """Reuse perceptual hashes from any row with the same (sha256, size) —
        lets a file that moved to a new path skip rehashing. Returns a sig dict or
        None."""
        if not sha256:
            return None
        with self._lock:
            cur = self._conn.execute(
                "SELECT kind, ahash, phash, orient, width, height, duration, windows "
                "FROM sig WHERE sha256 = ? AND size = ? LIMIT 1",
                (sha256, int(size)),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "sha256": sha256, "kind": row[0], "ahash": row[1], "phash": row[2],
            "orient": json.loads(row[3]) if row[3] else None,
            "width": row[4], "height": row[5], "duration": row[6],
            "windows": json.loads(row[7]) if row[7] else None,
        }

    def prune_missing(self, limit=5000):
        """Drop rows whose path no longer exists on disk (bounded per call)."""
        with self._lock:
            cur = self._conn.execute("SELECT path_key FROM sig LIMIT ?", (limit,))
            keys = [r[0] for r in cur.fetchall()]
        gone = [k for k in keys if not os.path.exists(k)]
        if gone:
            with self._lock:
                self._conn.executemany(
                    "DELETE FROM sig WHERE path_key = ?", [(k,) for k in gone])
                self._conn.commit()
        return len(gone)

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
