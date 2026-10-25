"""Child-process driver for twitter_pmv: walk an X/Twitter /media timeline with
gallery-dl and print one JSON object per line on stdout. Metadata only.

Run as a script (never imported by the app), so the parent can kill it the
moment a check is cancelled or x.com stops answering:

    python twitter_media_dump.py <config.json> <url> [stop_below] [stop_after]

`stop_below` (a tweet id) makes the walk incremental: the /media timeline is
newest-first by tweet id, so once `stop_after` consecutive tweets are at or
below the newest one already recorded, everything further down is known too.

Lines: {"t": "user", …} once, {"t": "tweet", …} per tweet with media,
{"t": "file", …} per media file, {"t": "log", "level", "msg"} for gallery-dl
warnings/errors, and finally {"t": "done", "status", "stopped"}.
"""

import json
import logging
import sys
import threading
from datetime import datetime, timezone

from gallery_dl import config, exception, job

_out_lock = threading.Lock()


def emit(obj):
    line = json.dumps(obj, ensure_ascii=True, default=str)
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def iso(d):
    if not isinstance(d, datetime):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)          # gallery-dl dates are naive UTC
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


class _LogToJson(logging.Handler):
    def emit(self, record):
        try:
            emit({"t": "log", "level": record.levelname.lower(), "msg": record.getMessage()})
        except Exception:
            pass


class MediaJob(job.Job):
    def __init__(self, url, stop_below=0, stop_after=20):
        job.Job.__init__(self, url)
        self.stop_below = stop_below
        self.stop_after = stop_after
        self.old_run = 0
        self.user_sent = False
        self.stopped = False

    def handle_directory(self, kw):
        user = kw.get("user") or kw.get("author") or {}
        if not self.user_sent:
            self.user_sent = True
            emit({"t": "user", "id": str(user.get("id") or ""), "name": user.get("name") or "",
                  "nick": user.get("nick") or "", "media_count": user.get("media_count"),
                  "statuses_count": user.get("statuses_count")})
        tid = int(kw.get("tweet_id") or 0)
        author = kw.get("author") or {}
        emit({"t": "tweet", "id": str(tid), "date": iso(kw.get("date")),
              "content": kw.get("content") or "", "author": author.get("name") or "",
              "reply_id": str(kw.get("reply_id") or ""), "count": kw.get("count") or 0})
        if self.stop_below:
            if tid <= self.stop_below:
                self.old_run += 1
                if self.old_run >= self.stop_after:
                    self.stopped = True
                    raise exception.StopExtraction()
            else:
                self.old_run = 0

    def handle_url(self, url, kw):
        emit({"t": "file", "tweet_id": str(kw.get("tweet_id") or ""), "num": kw.get("num"),
              "type": kw.get("type") or "", "extension": kw.get("extension") or "",
              "width": kw.get("width") or 0, "height": kw.get("height") or 0,
              "duration": kw.get("duration") or 0})

    def handle_queue(self, url, kw):
        pass


def main(argv):
    cfg_path, url = argv[1], argv[2]
    stop_below = int(argv[3]) if len(argv) > 3 and argv[3].isdigit() else 0
    stop_after = int(argv[4]) if len(argv) > 4 and argv[4].isdigit() else 20
    root = logging.getLogger()
    root.handlers[:] = [_LogToJson()]
    root.setLevel(logging.WARNING)
    config.load((cfg_path,), strict=True)
    try:
        j = MediaJob(url, stop_below, stop_after)
    except exception.NoExtractorError:
        emit({"t": "log", "level": "error", "msg": f"not an X/Twitter URL: {url}"})
        emit({"t": "done", "status": 64, "stopped": False})
        return 64
    status = j.run()
    emit({"t": "done", "status": status, "stopped": j.stopped})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
