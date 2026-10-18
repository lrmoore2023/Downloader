"""Per-thread record for a tracked SimpCity thread.

One SQLite file per thread at `<archive_dir>/simpcity_<thread_id>.db` (or
`<dest>/.simpcity-archive.db` without an archive dir). Like every other archive
DB its path is derived from the thread id alone, so deleting and re-adding the
creator — or pointing a new record at the same folder — resolves to the same
file: nothing is re-downloaded and dismissed links stay dismissed.

  thread  key/value walk state (title, last_page, last_walk, …)
  posts   every post seen, with its page and date
  media   one row per embedded file (key = "<host>:<ref>"), whatever post(s) it
          appeared in: downloaded | failed | gone, plus the final filename
  links   every external link found (review list), with the user's decision
          (pending | dismissed | downloaded | tracked) and liveness
          (alive: live | dead | unknown)

A re-walk upserts posts/links but never resets a status the user (or a
finished download) set.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone

LINK_STATUSES = ("pending", "dismissed", "downloaded", "tracked")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ThreadDB:
    def __init__(self, path):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS thread (k TEXT PRIMARY KEY, v TEXT);
                CREATE TABLE IF NOT EXISTS posts (
                    post_id TEXT PRIMARY KEY, page INTEGER, date TEXT,
                    n_media INTEGER DEFAULT 0, n_links INTEGER DEFAULT 0, first_seen TEXT);
                CREATE TABLE IF NOT EXISTS media (
                    key TEXT PRIMARY KEY, host TEXT, ref TEXT, url TEXT, media TEXT,
                    post_id TEXT, post_date TEXT, page INTEGER,
                    status TEXT, filename TEXT, path_key TEXT, size INTEGER, md5 TEXT,
                    reason TEXT, attempts INTEGER DEFAULT 0, updated TEXT);
                CREATE TABLE IF NOT EXISTS links (
                    url TEXT PRIMARY KEY, host TEXT, kind TEXT, ref TEXT,
                    post_id TEXT, post_date TEXT, post_url TEXT, page INTEGER,
                    status TEXT DEFAULT 'pending', alive TEXT DEFAULT 'unknown',
                    last_checked TEXT, dead_since TEXT, title TEXT, file_count INTEGER,
                    first_seen TEXT, updated TEXT);
                CREATE INDEX IF NOT EXISTS media_status ON media(status);
                CREATE INDEX IF NOT EXISTS links_status ON links(status);
            """)
            self._db.commit()

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass

    # ── thread state ────────────────────────────────────────────────

    def get(self, k, default=None):
        with self._lock:
            row = self._db.execute("SELECT v FROM thread WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def set(self, **kv):
        with self._lock:
            for k, v in kv.items():
                self._db.execute("INSERT OR REPLACE INTO thread(k, v) VALUES(?, ?)",
                                 (k, None if v is None else str(v)))
            self._db.commit()

    def state(self):
        with self._lock:
            return {r["k"]: r["v"] for r in self._db.execute("SELECT k, v FROM thread")}

    # ── posts ───────────────────────────────────────────────────────

    def post_known(self, post_id):
        with self._lock:
            return self._db.execute("SELECT 1 FROM posts WHERE post_id=?",
                                    (str(post_id),)).fetchone() is not None

    def record_post(self, post_id, page, date, n_media, n_links):
        with self._lock:
            self._db.execute("""
                INSERT INTO posts(post_id, page, date, n_media, n_links, first_seen)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(post_id) DO UPDATE SET page=excluded.page, date=excluded.date,
                    n_media=excluded.n_media, n_links=excluded.n_links""",
                (str(post_id), page, date, n_media, n_links, _now()))
            self._db.commit()

    def post_count(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]

    # ── media ───────────────────────────────────────────────────────

    @staticmethod
    def media_key(host, ref):
        return f"{host}:{ref}"

    def media(self, key):
        with self._lock:
            row = self._db.execute("SELECT * FROM media WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def note_media(self, item, post_id, post_date, page):
        """Register an item seen in a post (first sighting wins the post/date)."""
        key = self.media_key(item["host"], item["ref"])
        with self._lock:
            self._db.execute("""
                INSERT INTO media(key, host, ref, url, media, post_id, post_date, page, status, updated)
                VALUES(?,?,?,?,?,?,?,?, 'new', ?)
                ON CONFLICT(key) DO UPDATE SET url=excluded.url""",
                (key, item["host"], item["ref"], item["url"], item.get("media"),
                 str(post_id), post_date, page, _now()))
            self._db.commit()
        return key

    def mark_downloaded(self, key, filename, path_key, size=None, md5=None):
        with self._lock:
            self._db.execute("""UPDATE media SET status='downloaded', filename=?, path_key=?,
                                size=?, md5=?, reason=NULL, updated=? WHERE key=?""",
                             (filename, path_key, size, md5, _now(), key))
            self._db.commit()

    def mark_failed(self, key, reason, gone=False):
        with self._lock:
            self._db.execute("""UPDATE media SET status=?, reason=?, attempts=attempts+1,
                                updated=? WHERE key=?""",
                             ("gone" if gone else "failed", str(reason)[:500], _now(), key))
            self._db.commit()

    def mark_skipped(self, key):
        """The user skipped this file mid-download: never fetch it again."""
        with self._lock:
            self._db.execute("UPDATE media SET status='skipped', updated=? WHERE key=?",
                             (_now(), key))
            self._db.commit()

    def reset_media(self, key):
        """Forget a recorded download (its file went missing) so it is fetched again."""
        with self._lock:
            self._db.execute("UPDATE media SET status='new', updated=? WHERE key=?", (_now(), key))
            self._db.commit()

    def media_by_status(self, *statuses):
        q = "SELECT * FROM media WHERE status IN (%s) ORDER BY post_date, key" % \
            ",".join("?" * len(statuses))
        with self._lock:
            return [dict(r) for r in self._db.execute(q, statuses)]

    def media_counts(self):
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) n FROM media GROUP BY status")
            return {r["status"]: r["n"] for r in rows}

    def filenames_taken(self):
        with self._lock:
            return {r[0] for r in self._db.execute(
                "SELECT path_key FROM media WHERE path_key IS NOT NULL")}

    # ── links (review list) ─────────────────────────────────────────

    def upsert_link(self, item, post_id, post_date, post_url, page):
        """Insert a newly found link; True if it is new. Never touches the
        status/liveness of a link already known."""
        now = _now()
        with self._lock:
            cur = self._db.execute("""
                INSERT OR IGNORE INTO links(url, host, kind, ref, post_id, post_date, post_url,
                                            page, first_seen, updated)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item["url"], item["host"], item["kind"], item["ref"], str(post_id),
                 post_date, post_url, page, now, now))
            self._db.commit()
            return cur.rowcount > 0

    def link(self, url):
        with self._lock:
            row = self._db.execute("SELECT * FROM links WHERE url=?", (url,)).fetchone()
        return dict(row) if row else None

    def set_link_status(self, url, status):
        if status not in LINK_STATUSES:
            raise ValueError(status)
        with self._lock:
            cur = self._db.execute("UPDATE links SET status=?, updated=? WHERE url=?",
                                   (status, _now(), url))
            self._db.commit()
            return cur.rowcount > 0

    def set_link_alive(self, url, alive, title=None, file_count=None):
        """alive: True / False / None. Keeps the first time it was seen dead."""
        now = _now()
        val = "live" if alive is True else "dead" if alive is False else "unknown"
        with self._lock:
            row = self._db.execute("SELECT dead_since FROM links WHERE url=?", (url,)).fetchone()
            if row is None:
                return False
            dead_since = (row["dead_since"] or now) if val == "dead" else \
                (None if val == "live" else row["dead_since"])
            self._db.execute("""UPDATE links SET alive=?, last_checked=?, dead_since=?,
                                title=COALESCE(?, title), file_count=COALESCE(?, file_count),
                                updated=? WHERE url=?""",
                             (val, now, dead_since, title, file_count, now, url))
            self._db.commit()
            return True

    def links(self, statuses=None):
        q, args = "SELECT * FROM links", ()
        if statuses:
            q += " WHERE status IN (%s)" % ",".join("?" * len(statuses))
            args = tuple(statuses)
        q += " ORDER BY post_date DESC, url"
        with self._lock:
            return [dict(r) for r in self._db.execute(q, args)]

    def link_counts(self):
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) n FROM links GROUP BY status")
            return {r["status"]: r["n"] for r in rows}


def open_readonly(path):
    """ThreadDB for reading, or None if it doesn't exist yet (never creates one)."""
    if not path or not os.path.isfile(path):
        return None
    return ThreadDB(path)
