"""X/Twitter for the PMV tracker — every tweet on a user's /media timeline that
carries a video. Metadata only; nothing is downloaded.

The timeline (`x.com/<user>/media`, GraphQL `UserMedia`) is members-only:
anonymously it is a 404 that gallery-dl reports as "authenticated cookies
needed" (measured 2026-10-25). So this reuses the app's Twitter sign-in —
Settings ▸ Twitter (`cookies_path`, else `cookies_browser`) — and gallery-dl's
own extractor, which keeps up with X's rotating query ids and headers.

gallery-dl runs in a child process (twitter_media_dump.py) streaming JSON
lines, so a cancelled check or an x.com that stops answering is a kill, never a
hang: no output for IDLE_TIMEOUT seconds ends the walk as "not responding".
X rate limits are set to abort (gallery-dl would otherwise sleep up to 15
minutes inside a check); the link is simply fetched again next time.

Only `video` files count. Photos, GIFs (X serves them as mp4 but they are
`animated_gif`) and text tweets are left out, as the user asked.
"""

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

from backend.cookie_manager import validate_cookies_file

SITE = "https://x.com"
IDLE_TIMEOUT = 180          # seconds without a line from gallery-dl → x.com not responding
STOP_AFTER = 20             # incremental: consecutive already-known tweets before stopping
SLEEP_REQUEST = 1.0         # gallery-dl pacing between timeline pages

# Paths that look like x.com/<name> but are not accounts.
_RESERVED = {"i", "home", "explore", "search", "notifications", "messages", "settings",
             "compose", "intent", "share", "hashtag", "login", "signup", "tos", "privacy",
             "account", "jobs", "lists", "communities", "premium"}
_USER_RE = re.compile(r"(?:^|[/.])(?:x|twitter)\.com/@?([A-Za-z0-9_]{1,15})(?=[/?#]|$)", re.I)

_DUMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "twitter_media_dump.py")


class SiteError(Exception):
    pass


class AuthError(SiteError):
    """The timeline needs a signed-in X session and the saved one is missing or
    rejected — not an outage."""


# ── URLs ────────────────────────────────────────────────────────────

def parse_profile_url(url):
    """'https://x.com/jjadesyx/media' (or twitter.com, a status link, '@name')
    → {"username"} or None."""
    m = _USER_RE.search((url or "").strip())
    if not m or m.group(1).lower() in _RESERVED:
        return None
    return {"username": m.group(1)}


def media_url(username):
    return f"{SITE}/{username}/media"


def status_url(username, tweet_id):
    return f"{SITE}/{username or 'i'}/status/{tweet_id}"


# ── cookies ─────────────────────────────────────────────────────────

def auth_options(state):
    """(cookies_path, cookies_browser, problem) from the Settings ▸ Twitter
    choice. `problem` is a message when neither is usable."""
    path = (state.get("cookies_path") or "").strip()
    browser = (state.get("cookies_browser") or "").strip()
    if path:
        res = validate_cookies_file(path)
        if res.get("valid"):
            return path, "", ""
        if not browser:
            return "", "", f"Twitter cookies unusable ({res.get('message')}) — set them in Settings ▸ Twitter"
    if browser:
        return "", browser, ""
    return "", "", "X needs a signed-in session for /media — add cookies in Settings ▸ Twitter"


def build_config(cookies_path="", cookies_browser=""):
    tw = {
        "retweets": False,
        "quoted": False,
        "replies": True,          # a self-reply in a thread can carry a video
        "videos": True,
        "cards": False,
        "unavailable": True,      # withheld/geo-blocked media is still a post
        "ratelimit": "abort",
        "sleep-request": SLEEP_REQUEST,
        "retries": 3,
        "timeout": 30.0,
    }
    if cookies_path:
        tw["cookies"] = cookies_path.replace("\\", "/")
    elif cookies_browser:
        tw["cookies-from-browser"] = cookies_browser
    return {"extractor": {"twitter": tw}, "output": {"mode": "null", "progress": False}}


# ── the walk ────────────────────────────────────────────────────────

def _classify(errors, status):
    """Turn gallery-dl's error lines into one exception."""
    text = "; ".join(errors) or f"gallery-dl exit status {status}"
    low = text.lower()
    if "authrequired" in low or "authenticated cookies" in low or "could not authenticate" in low \
            or "authorizationerror" in low or "account temporarily locked" in low:
        return AuthError(f"X rejected the session ({text[:200]}) — refresh cookies in Settings ▸ Twitter")
    if "rate limit" in low:
        return SiteError("rate limited by X (HTTP 429) — will retry next check")
    if "notfounderror" in low or "user not found" in low or "suspended" in low:
        return SiteError(f"account not found or suspended ({text[:200]})")
    if status & 128 or re.search(r"connection|timeout|timed out|unable to download data", low):
        return SiteError(f"ConnectionError: x.com unreachable ({text[:200]})")
    m = re.search(r"\b(5\d\d)\b", text)
    if m:
        return SiteError(f"HTTP {m.group(1)} from x.com ({text[:200]})")
    return SiteError(text[:300])


def _popen(cmd):
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                            errors="replace", bufsize=1, **kw)


def walk_media(username, *, cookies_path="", cookies_browser="", stop_below=0,
               stop_after=STOP_AFTER, should_cancel=None, on_event=None,
               idle_timeout=IDLE_TIMEOUT, popen=_popen):
    """Walk one /media timeline. Returns {"tweets": [{id, date, content, author,
    reply_id, files: [{type, extension, width, height, duration, num}]}],
    "user": {...}, "stopped": bool, "cancelled": bool}. Raises AuthError /
    SiteError when the walk did not finish cleanly — a partial walk is never
    returned as complete."""
    cancelled = should_cancel or (lambda: False)
    on_event = on_event or (lambda kind, data: None)
    fd, cfg = tempfile.mkstemp(prefix="pmv-twitter-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(build_config(cookies_path, cookies_browser), fh)
    cmd = [sys.executable, "-I", _DUMP, cfg, media_url(username),
           str(int(stop_below or 0)), str(int(stop_after))]
    try:
        try:
            proc = popen(cmd)
        except OSError as e:
            raise SiteError(f"couldn't start gallery-dl ({e})")
        lines = queue.Queue()

        def pump(stream, tag):
            try:
                for ln in stream:
                    lines.put((tag, ln))
            finally:
                lines.put((tag, None))

        threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True).start()
        threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True).start()

        tweets, by_id, user = [], {}, {}
        errors, stderr_tail = [], []
        done, stopped, status = False, False, None
        open_streams = 2
        last = time.monotonic()
        try:
            while open_streams:
                if cancelled():
                    return {"tweets": tweets, "user": user, "stopped": False, "cancelled": True}
                if time.monotonic() - last > idle_timeout:
                    raise SiteError(f"x.com not responding (nothing for {idle_timeout}s)")
                try:
                    tag, ln = lines.get(timeout=0.25)
                except queue.Empty:
                    continue
                last = time.monotonic()
                if ln is None:
                    open_streams -= 1
                    continue
                if tag == "err":
                    if ln.strip():
                        stderr_tail = (stderr_tail + [ln.strip()])[-5:]
                    continue
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                t = d.get("t")
                if t == "tweet":
                    d["files"] = []
                    tweets.append(d)
                    by_id[d["id"]] = d
                    on_event("tweet", d)
                elif t == "file":
                    tw = by_id.get(d.get("tweet_id"))
                    if tw is not None:
                        tw["files"].append(d)
                elif t == "user":
                    user = d
                    on_event("user", d)
                elif t == "log":
                    if d.get("level") in ("error", "critical"):
                        errors.append(d.get("msg") or "")
                    on_event("log", d)
                elif t == "done":
                    done, stopped, status = True, bool(d.get("stopped")), int(d.get("status") or 0)
        finally:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        if not done:
            raise SiteError("gallery-dl stopped unexpectedly"
                            + (f" ({stderr_tail[-1][:200]})" if stderr_tail else ""))
        if status and errors:
            raise _classify(errors, status)
        if status and not tweets:
            raise _classify(errors or stderr_tail, status)
        return {"tweets": tweets, "user": user, "stopped": stopped, "cancelled": False}
    finally:
        try:
            os.remove(cfg)
        except OSError:
            pass


# ── tweets → PMV items ──────────────────────────────────────────────

def _title(content):
    t = re.sub(r"\s+", " ", content or "").strip()
    return t[:300]


def video_items(tweets, username):
    """Only tweets carrying at least one `video` file, newest-first as walked,
    in the shape pmv_tracker merges."""
    out = []
    for pos, tw in enumerate(tweets):
        vids = [f for f in tw.get("files") or [] if f.get("type") == "video"]
        if not vids:
            continue
        qual = [min(f.get("width") or 0, f.get("height") or 0) for f in vids]
        dur = max((f.get("duration") or 0) for f in vids)
        out.append({
            "id": str(tw["id"]),
            "title": _title(tw.get("content")),
            "url": status_url(tw.get("author") or username, tw["id"]),
            "date": tw.get("date"),
            "duration": int(round(dur)) if dur else None,
            "quality": max(qual) if qual and max(qual) else None,
            "media_kinds": ["video"],
            "video_count": len(vids),
            "pos": pos,
        })
    return out
