"""Sign in to a site in the app's embedded browser and keep the session cookies.

The PMV "Check for new" job reads each site's *followed* feed (rule34video's
/my/subscriptions/, pawchive's favorites), which only exists for a signed-in
account. Rather than reimplementing every login form (captchas, Cloudflare,
"remember me" quirks), the user signs in normally in a pywebview window; we poll
the window's cookie jar and, whenever it changes, test the jar with a real HTTP
request against a members-only URL. The first jar that passes is written as a
Netscape cookies.txt that the site's `make_session(cookies_path=…)` replays.

Detection is by *verification*, not by guessing cookie names: a jar is accepted
only once a request made with it is actually let in.

pawchive shares its jar with the Cloudflare Connect (`.pawchive_cookies.txt`):
the login window clears the challenge too, so one file carries both the
cf_clearance and the session, plus the browser UA that earned the clearance.

pmvhaven needs no window — it issues personal API keys (`pmvh_…`,
https://pmvhaven.com/api-keys) sent as `Authorization: Bearer`.
"""

import os
import tempfile
import time

from backend.pawchive_cf import _normalize

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SITES = {
    "rule34video": {
        "label": "rule34video",
        "domains": ("rule34video.com",),
        "login_url": "https://rule34video.com/login/",
        "cookies_file": ".r34video_cookies.txt",
        "state_key": "r34video_cookies_path",
        "signed_in_key": "r34video_signed_in_at",
    },
    "pawchive": {
        "label": "pawchive",
        "domains": ("pawchive.pw", "pawchive.st"),
        "login_url": "https://pawchive.pw/account/login",
        "cookies_file": ".pawchive_cookies.txt",
        "state_key": "pawchive_cookies_path",
        "signed_in_key": "pawchive_signed_in_at",
    },
}


def default_cookies_path(platform):
    return os.path.join(APP_DIR, SITES[platform]["cookies_file"])


def _domain_ok(domain, domains):
    d = (domain or "").lstrip(".").lower()
    return any(d == h or d.endswith("." + h) for h in domains)


def site_cookies(cookies, platform):
    """The normalised cookies that belong to `platform`'s domains."""
    domains = SITES[platform]["domains"]
    return [c for c in _normalize(cookies) if _domain_ok(c["domain"], domains) and c["value"]]


def write_cookies(rows, path, note=""):
    """Atomically write normalised cookie rows as a Netscape cookies.txt (the
    format `pawchive_scraper._load_cookies` reads). Expiry 0 = session; the
    loader ignores expiry, so the site decides when a session is over."""
    lines = ["# Netscape HTTP Cookie File", f"# Written by site_login.py {note}".rstrip(), ""]
    for c in rows:
        domain = c["domain"] if c["domain"].startswith(".") else "." + c["domain"].lstrip(".")
        lines.append("\t".join([domain, "TRUE", c["path"] or "/",
                                "TRUE" if c["secure"] else "FALSE", "0", c["name"], c["value"]]))
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return path


# ── verification: is this jar actually signed in? ───────────────────

def make_feed_session(platform, cookies_path=None, user_agent=None):
    """A session carrying the saved sign-in, for reading followed feeds."""
    if platform == "rule34video":
        from backend import r34video_scraper as r34
        return r34.make_session(cookies_path=cookies_path)
    if platform == "pawchive":
        from backend import pawchive_scraper as pw
        return pw.make_session(user_agent=user_agent or None, cookies_path=cookies_path)
    raise ValueError(platform)


def verify(platform, cookies_path, user_agent=None, session=None):
    """(signed_in: bool|None, message). None = couldn't tell (network/Cloudflare)."""
    try:
        s = session or make_feed_session(platform, cookies_path, user_agent)
        if platform == "rule34video":
            from backend import r34video_scraper as r34
            r = s.get(r34.BASE + "/my/subscriptions/", timeout=(15, 30), allow_redirects=False)
            if r.status_code == 200:
                return True, "Signed in"
            if r.status_code in (301, 302, 303):
                return False, "Not signed in"
            return None, f"HTTP {r.status_code}"
        if platform == "pawchive":
            from backend import pawchive_scraper as pw
            r = s.get("https://pawchive.pw/api/v1/account/favorites?type=artist", timeout=(15, 30))
            if pw.is_cloudflare_challenge(r):
                return None, "Cloudflare check — sign in again"
            if r.status_code == 200:
                return True, "Signed in"
            if r.status_code in (401, 403):
                return False, "Not signed in"
            return None, f"HTTP {r.status_code}"
    except Exception as e:
        return None, f"{e.__class__.__name__}: {e}"
    raise ValueError(platform)


# ── webview-driven capture ──────────────────────────────────────────

def capture_login(window, platform, cookies_path=None, timeout=300.0, poll=1.0,
                  verify_every=3.0, on_status=None, is_closed=None, verifier=None):
    """Drive an already-created pywebview Window showing the site's login page
    until the user is signed in (verified), the window is closed, or `timeout`.

    Returns {"ok", "message", "cookies_path"?, "user_agent"?}. Never creates or
    destroys the window — the caller owns it (GUI-thread rules)."""
    cookies_path = cookies_path or default_cookies_path(platform)
    verifier = verifier or verify
    is_closed = is_closed or (lambda: False)

    def _status(msg):
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    def _ua():
        try:
            ua = window.evaluate_js("navigator.userAgent")
            return ua.strip() if isinstance(ua, str) and ua.strip() else None
        except Exception:
            return None

    user_agent = None
    last_sig, last_check = None, 0.0
    deadline = time.monotonic() + max(10.0, float(timeout))
    _status("Sign in in the window that just opened…")
    while time.monotonic() < deadline:
        if is_closed():
            return {"ok": False, "message": "Window closed before signing in"}
        try:
            rows = site_cookies(window.get_cookies(), platform)
        except Exception:
            rows = []
        sig = tuple(sorted((c["name"], c["value"]) for c in rows))
        now = time.monotonic()
        if rows and (sig != last_sig) and now - last_check >= verify_every:
            last_sig, last_check = sig, now
            if user_agent is None:
                user_agent = _ua()
            # Test the jar from a scratch file so a failed attempt never
            # clobbers a previously working sign-in.
            fd, tmp = tempfile.mkstemp(suffix=".txt")
            os.close(fd)
            try:
                write_cookies(rows, tmp)
                ok, msg = verifier(platform, tmp, user_agent)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if ok:
                # The page may have been mid-navigation when the UA was first
                # asked for; pawchive's Cloudflare clearance is bound to it.
                for _ in range(5):
                    if user_agent:
                        break
                    time.sleep(0.5)
                    user_agent = _ua()
                write_cookies(rows, cookies_path, f"— {platform} sign-in")
                _status("Signed in.")
                return {"ok": True, "message": "Signed in", "cookies_path": cookies_path,
                        "user_agent": user_agent}
        time.sleep(max(0.2, float(poll)))
    return {"ok": False, "message": "Timed out waiting for sign-in"}
