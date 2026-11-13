"""Child-process helper: print one X/Twitter account's profile as a JSON line.

Run as a script (never imported by the app), like twitter_media_dump.py, so
gallery-dl's global config never touches the app process:

    python twitter_profile_dump.py <config.json> <username>

Uses gallery-dl's `x.com/<user>/info` extractor — a single UserByScreenName
request, no timeline walk. Prints {"t": "user", "name", "nick", "profile_image"}
(profile_image = the full-size pbs.twimg.com URL) or {"t": "error", "msg"}.
"""

import json
import logging
import sys

from gallery_dl import config, exception, job


class _Errors(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


class InfoJob(job.Job):
    def __init__(self, url):
        job.Job.__init__(self, url)
        self.user = None

    def handle_directory(self, kw):
        if self.user is None:
            self.user = kw.get("user") or kw

    def handle_url(self, url, kw):
        pass

    def handle_queue(self, url, kw):
        pass


def main(argv):
    cfg_path, username = argv[1], argv[2]
    errors = _Errors()
    root = logging.getLogger()
    root.handlers[:] = [errors]
    root.setLevel(logging.WARNING)
    config.load((cfg_path,), strict=True)
    try:
        j = InfoJob(f"https://x.com/{username}/info")
        j.run()
    except exception.GalleryDLException as e:
        errors.msgs.append(f"{type(e).__name__}: {e}")
        j = None
    u = (j.user if j else None) or {}
    if u.get("profile_image"):
        out = {"t": "user", "name": u.get("name") or "", "nick": u.get("nick") or "",
               "profile_image": u["profile_image"]}
    else:
        out = {"t": "error", "msg": "; ".join(errors.msgs)[:500] or "no profile returned"}
    sys.stdout.write(json.dumps(out, ensure_ascii=True) + "\n")
    return 0 if "profile_image" in out else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
