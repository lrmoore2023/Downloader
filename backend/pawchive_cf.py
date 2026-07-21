"""Clear pawchive.pw's Cloudflare challenge with the app's embedded browser and
capture the resulting `cf_clearance` cookie + matching User-Agent for the downloader.

pawchive.pw moved its API behind Cloudflare's "Just a moment…" managed JS challenge.
A plain HTTP client (even curl_cffi's Chrome impersonation) can't solve it — only a
real browser can, and the app already embeds one (pywebview / EdgeWebView2). So we
open pawchive in a short-lived webview, wait for the challenge to pass, then read the
`cf_clearance` cookie and the browser's exact UA. We persist the cookie as a Netscape
`cookies.txt` and store the UA; `pawchive_scraper.make_session` then replays the cookie
with a TLS fingerprint + UA aligned to the browser that earned it, so it validates.

This module is deliberately UI-agnostic: `capture_via_window` drives an *already
created* pywebview Window (so both the app and the standalone probe can reuse it),
and the pure cookie-file helpers are import-safe with no pywebview dependency.
"""

import os
import time
from http.cookies import SimpleCookie

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Where the captured cookie jar lives. Kept beside app_state.json; the runner is
# pointed at this path via app_state["pawchive_cookies_path"].
DEFAULT_COOKIES_PATH = os.path.join(APP_DIR, ".pawchive_cookies.txt")

# Domains we keep from the browser jar (drop unrelated cookies for a tidy file).
PAWCHIVE_DOMAINS = ("pawchive.pw", "pawchive.st")

# The one cookie that proves the challenge was solved.
CF_COOKIE = "cf_clearance"

# The page whose challenge we solve. Hitting the API path directly means the jar we
# read back is scoped to exactly what the downloader calls.
CONNECT_URL = "https://pawchive.pw/"


# ── cookie normalisation ────────────────────────────────────────────

def _normalize(cookies):
    """Turn pywebview `Window.get_cookies()` output — a list of `SimpleCookie` — (or a
    list of plain dicts, for tests) into uniform dicts: {name, value, domain, path,
    secure, expires}. Unknown shapes are skipped rather than raising."""
    out = []
    for c in cookies or []:
        try:
            if isinstance(c, SimpleCookie):
                for name, morsel in c.items():
                    out.append({
                        "name": name,
                        "value": morsel.value,
                        "domain": morsel["domain"] or "",
                        "path": morsel["path"] or "/",
                        "secure": bool(morsel["secure"]),
                        "expires": morsel["expires"] or "",
                    })
            elif isinstance(c, dict):
                out.append({
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "domain": c.get("domain", "") or "",
                    "path": c.get("path", "/") or "/",
                    "secure": bool(c.get("secure", False)),
                    "expires": c.get("expires", "") or "",
                })
        except Exception:
            continue
    return [c for c in out if c["name"]]


def _is_pawchive(domain):
    d = (domain or "").lstrip(".").lower()
    return any(d == host or d.endswith("." + host) for host in PAWCHIVE_DOMAINS)


def has_cf_clearance(cookies):
    """True if a normalised/raw cookie list carries a pawchive `cf_clearance`."""
    for c in _normalize(cookies):
        if c["name"] == CF_COOKIE and _is_pawchive(c["domain"]) and c["value"]:
            return True
    return False


def write_cookies_txt(cookies, path=None):
    """Write pawchive cookies to a Netscape `cookies.txt` (the format
    `pawchive_scraper._load_cookies` reads via MozillaCookieJar).

    Returns {"ok", "path", "count", "has_cf_clearance"}. Expiry is written as 0
    (session): the loader uses ignore_expires/ignore_discard, so the cookie is kept
    regardless, and we avoid brittle RFC-date parsing of the browser's expiry string.
    """
    path = path or DEFAULT_COOKIES_PATH
    rows = [c for c in _normalize(cookies) if _is_pawchive(c["domain"])]
    lines = [
        "# Netscape HTTP Cookie File",
        "# Written by pawchive_cf.py — captured Cloudflare clearance.",
        "",
    ]
    for c in rows:
        domain = c["domain"] if c["domain"].startswith(".") else "." + c["domain"].lstrip(".")
        include_sub = "TRUE"
        secure = "TRUE" if c["secure"] else "FALSE"
        # domain \t include_subdomains \t path \t secure \t expiry \t name \t value
        lines.append("\t".join([
            domain, include_sub, c["path"] or "/", secure, "0", c["name"], c["value"],
        ]))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return {
        "ok": True,
        "path": path,
        "count": len(rows),
        "has_cf_clearance": has_cf_clearance(rows),
    }


def validate_cookies_file(path):
    """Check a Netscape file carries a pawchive `cf_clearance`. Mirrors
    `cookie_manager.validate_cookies_file` (Twitter) but scoped to pawchive."""
    if not path or not os.path.isfile(path):
        return {"valid": False, "message": "No cookies captured yet"}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return {"valid": False, "message": f"Cannot read file: {e}"}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and _is_pawchive(parts[0]) and parts[5] == CF_COOKIE and parts[6]:
            return {"valid": True, "message": "Cloudflare clearance present"}
    return {"valid": False, "message": "No cf_clearance — click Reconnect"}


# ── webview-driven capture ──────────────────────────────────────────

def capture_via_window(window, cookies_path=None, timeout=45.0, poll=1.0,
                       on_status=None):
    """Drive an already-created pywebview Window that has loaded `CONNECT_URL`,
    waiting for Cloudflare's challenge to pass, then persist the cookie + UA.

    `window` — a pywebview Window (or any object exposing `.get_cookies()`,
    `.evaluate_js(str)` and, ideally, `.load_url(str)`).
    Returns {"ok": bool, "message": str, "cookies_path"?, "user_agent"?, "count"?}.
    Does NOT create or destroy the window — the caller owns its lifecycle (webview
    windows must be created on the GUI thread)."""
    cookies_path = cookies_path or DEFAULT_COOKIES_PATH

    def _status(msg):
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    _status("Opening pawchive…")
    deadline = time.monotonic() + max(5.0, float(timeout))
    cookies = []
    while time.monotonic() < deadline:
        try:
            cookies = window.get_cookies()
        except Exception:
            cookies = []
        if has_cf_clearance(cookies):
            break
        _status("Waiting for Cloudflare check to pass…")
        time.sleep(max(0.2, float(poll)))
    else:
        return {"ok": False,
                "message": "Cloudflare check didn't complete in time. Leave the "
                           "window open a moment and try Reconnect again."}

    # Grab the browser's exact UA so the replayed cookie validates.
    user_agent = None
    try:
        ua = window.evaluate_js("navigator.userAgent")
        if isinstance(ua, str) and ua.strip():
            user_agent = ua.strip()
    except Exception:
        user_agent = None

    result = write_cookies_txt(cookies, cookies_path)
    if not result.get("has_cf_clearance"):
        return {"ok": False, "message": "Captured cookies but no cf_clearance found."}

    _status("Connected.")
    return {
        "ok": True,
        "message": "Connected to pawchive.",
        "cookies_path": result["path"],
        "user_agent": user_agent,
        "count": result["count"],
    }
