"""Unlock password-protected filester folders.

gallery-dl's filester extractor has no password support, but filester's unlock is
purely cookie-based: POST the (encoded) password to `/f/<slug>` and the server
sets a `folder_access_token` JWT cookie that keeps the folder open. So we unlock
here with curl_cffi, dump that cookie to a Netscape cookies.txt, and hand it to
gallery-dl (with `extractor.filester.domain = "auto"` so it talks to the same
host the cookie is scoped to). Verified against a real locked folder.

The page's inline JS encodes the password as:
    payload  = password + "|" + Date.now() + "|" + nonce
    encoded  = base64(utf8(payload))
where `nonce` is a hidden form field that rotates each page load and must be
POSTed back alongside the encoded password.
"""

import base64
import os
import re
import tempfile
import time
from urllib.parse import urlparse

try:
    from curl_cffi import requests as _requests
    _IMPERSONATE = "chrome"
except Exception:                              # pragma: no cover - fallback path
    import requests as _requests
    _IMPERSONATE = None

_NONCE_RE = re.compile(r'id="nonce"[^>]*value="([^"]*)"')


def _session():
    if _IMPERSONATE:
        return _requests.Session(impersonate=_IMPERSONATE)
    return _requests.Session()


def unlock_to_cookies(url, password):
    """Unlock `url` with `password` and return a temp Netscape cookies.txt path.

    Returns None if the folder isn't password-locked, the password is wrong, or
    anything goes sideways — the caller then just runs gallery-dl without cookies
    (an open folder needs none; a still-locked one will surface as a failure).
    """
    if not password:
        return None
    p = urlparse(url)
    host = p.hostname
    if not host:
        return None
    root = f"{p.scheme or 'https'}://{host}"
    m = re.search(r"/f/([^/?#]+)", p.path or "")
    if not m:
        return None
    slug = m.group(1)

    s = _session()
    try:
        html = s.get(f"{root}/f/{slug}", timeout=30).text
    except Exception:
        return None
    nm = _NONCE_RE.search(html)
    if not nm:
        return None  # no nonce -> not a locked folder; nothing to unlock
    nonce = nm.group(1)

    payload = f"{password}|{int(time.time() * 1000)}|{nonce}"
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    try:
        r = s.post(f"{root}/f/{slug}",
                   data={"nonce": nonce, "password": encoded},
                   headers={"referer": f"{root}/f/{slug}"}, timeout=30)
    except Exception:
        return None
    # Wrong password re-renders the password form without any file items.
    if "password-form" in r.text and "file-item" not in r.text:
        return None

    token = next((c.value for c in s.cookies.jar
                  if c.name == "folder_access_token"), None)
    if not token:
        return None
    return _write_cookies(host, token)


def _write_cookies(host, token):
    fd, path = tempfile.mkstemp(prefix="filester-cookies-", suffix=".txt")
    expiry = int(time.time()) + 3600
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write(f".{host}\tTRUE\t/\tFALSE\t{expiry}\tfolder_access_token\t{token}\n")
    return path
