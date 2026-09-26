"""Sign in to Terabox with the app's embedded browser and keep the session cookies.

Terabox hands out a download link to anyone but only serves the bytes to a
signed-in account (403 error_code 31045 otherwise — see terabox_downloader). So,
like pawchive's Cloudflare connect, we open terabox.app's own login page in a
short-lived pywebview window, wait for the `ndus` session cookie to appear, and
store the terabox cookies + the browser's User-Agent in app_state. They are
credentials: app_state's NAS mirror is an allowlist that never includes them.
"""

import time

from backend.pawchive_cf import _normalize

LOGIN_URL = "https://www.terabox.app/login"
SESSION_COOKIE = "ndus"


def _is_terabox(domain):
    d = (domain or "").lstrip(".").lower()
    return d == "terabox.app" or d.endswith(".terabox.app")


def terabox_cookies(cookies):
    """{name: value} of the terabox.app cookies in a pywebview get_cookies() result."""
    return {c["name"]: c["value"] for c in _normalize(cookies)
            if c.get("name") and _is_terabox(c.get("domain"))}


def capture_via_window(window, timeout=300.0, poll=1.0, on_status=None):
    """Wait (up to `timeout`) for the user to sign in inside `window`, then return
    {"ok", "message", "cookies"?, "user_agent"?}. The caller owns the window."""
    def _status(msg):
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    _status("Sign in to Terabox in the window that opened…")
    deadline = time.monotonic() + max(5.0, float(timeout))
    jar = {}
    while time.monotonic() < deadline:
        try:
            jar = terabox_cookies(window.get_cookies())
        except Exception:
            jar = {}
        if jar.get(SESSION_COOKIE):
            break
        time.sleep(max(0.2, float(poll)))
    else:
        return {"ok": False, "message": "No Terabox sign-in within 5 minutes — try Connect again."}
    ua = None
    try:
        v = window.evaluate_js("navigator.userAgent")
        ua = v.strip() if isinstance(v, str) and v.strip() else None
    except Exception:
        pass
    _status("Connected.")
    return {"ok": True, "message": "Connected to Terabox.", "cookies": jar, "user_agent": ua}
