"""iwara.tv — metadata-only listing client for the PMV tracker.

iwara exposes a public JSON API (verified live 2026-09-18):

    GET https://api.iwara.tv/profile/<username>            → {"user": {"id": <uuid>, "name", …}}
    GET https://api.iwara.tv/videos?user=<uuid>&sort=date&page=<0-based>&limit=50
        → {"count", "limit", "page", "results": [{id, slug, title, createdAt,
           private, unlisted, rating, file: {width, height, duration}|nulls, …}]}

Anonymous listing works and even includes `private: true` items, but some
uploads only appear (or only resolve) for a logged-in account, so login is
optional and layered on top. The flow (same one yt-dlp uses):

    POST /user/login  {email, password}          → {"token": <user JWT, ~3 weeks>}
    POST /user/token  Authorization: Bearer <user token>, empty body
                                                 → {"accessToken": <JWT, ~1 hour>}

and listing calls carry `Authorization: Bearer <accessToken>`. `IwaraAuth` owns
that dance and degrades to anonymous (never raises into the listing loop) if
credentials are missing or rejected.
"""

import base64
import json
import re
import time

import requests

from backend.coomerfans_scraper import DEFAULT_UA

API = "https://api.iwara.tv"
SITE = "https://www.iwara.tv"
PAGE_LIMIT = 50
MAX_PAGES = 2000

_PROFILE_RE = re.compile(r"iwara\.tv/profile/([^/?#]+)", re.I)
_VIDEO_RE = re.compile(r"iwara\.tv/videos?/([A-Za-z0-9]+)", re.I)


class SiteError(Exception):
    pass


class NotFound(SiteError):
    pass


class AuthError(SiteError):
    """401 — the access token was rejected (expired, or the account can't see this)."""


# ── URLs ────────────────────────────────────────────────────────────

def parse_profile_url(url):
    m = _PROFILE_RE.search(url or "")
    return {"username": m.group(1)} if m else None


def profile_url(username):
    return f"{SITE}/profile/{username}"


def video_url(video_id, slug=None):
    return f"{SITE}/video/{video_id}" + (f"/{slug}" if slug else "")


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": SITE,
        "Referer": SITE + "/",
    })
    return s


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


# ── parsing ─────────────────────────────────────────────────────────

def parse_profile(data):
    u = (data or {}).get("user") or {}
    return {"id": u.get("id") or "", "name": u.get("name") or "",
            "username": u.get("username") or ""}


def _norm_date(s):
    """'2026-09-10T00:08:28.000Z' → '2026-09-10T00:08:28+00:00' (sortable ISO)."""
    if not s:
        return None
    s = str(s).strip()
    s = re.sub(r"\.\d+Z$", "Z", s)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return s


def parse_video(raw):
    f = raw.get("file") or {}
    w, h = f.get("width"), f.get("height")
    quality = None
    if isinstance(w, int) and isinstance(h, int) and w and h:
        quality = min(w, h)
    dur = f.get("duration")
    return {
        "id": str(raw.get("id") or ""),
        "slug": raw.get("slug") or "",
        "title": raw.get("title") or "",
        "url": video_url(raw.get("id"), raw.get("slug")),
        "date": _norm_date(raw.get("createdAt")),
        "duration": int(dur) if isinstance(dur, (int, float)) else None,
        "quality": quality,
        "private": bool(raw.get("private")),
        "unlisted": bool(raw.get("unlisted")),
    }


def jwt_exp(token):
    """The `exp` claim of a JWT (no signature check), or None."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        exp = data.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def token_valid(token, now=None, margin=60):
    if not token:
        return False
    exp = jwt_exp(token)
    if exp is None:
        return False
    return exp > (now if now is not None else time.time()) + margin


# ── fetching ────────────────────────────────────────────────────────

def _retry_after(resp, default):
    try:
        return float(resp.headers.get("Retry-After") or default)
    except (TypeError, ValueError):
        return default


def fetch_json(session, url, params=None, token=None, throttle=None, should_cancel=None,
               retries=3, timeout=(15, 60)):
    cancelled = should_cancel or (lambda: False)
    last = "no attempt"
    for attempt in range(retries + 1):
        if cancelled():
            raise SiteError("cancelled")
        if throttle:
            throttle.wait(cancelled)
        try:
            r = session.get(url, params=params, headers=_auth_headers(token), timeout=timeout)
        except requests.RequestException as e:
            last = e.__class__.__name__
            if throttle:
                throttle.on_throttled(1.5 * (attempt + 1))
            else:
                time.sleep(1.0)
            continue
        if r.status_code == 404:
            raise NotFound(url)
        if r.status_code in (401, 403):
            raise AuthError(f"HTTP {r.status_code}")
        if r.status_code == 429 or 500 <= r.status_code < 600:
            last = f"HTTP {r.status_code}"
            wait = _retry_after(r, 2.0 * (attempt + 1))
            if throttle:
                throttle.on_throttled(wait)
            else:
                time.sleep(min(wait, 10))
            continue
        if r.status_code != 200:
            raise SiteError(f"HTTP {r.status_code} for {url}")
        if throttle:
            throttle.on_success()
        try:
            return r.json()
        except ValueError:
            raise SiteError(f"non-JSON response for {url}")
    raise SiteError(f"{last} after {retries + 1} attempts: {url}")


def fetch_profile(session, username, token=None, throttle=None, should_cancel=None):
    data = fetch_json(session, f"{API}/profile/{username}", token=token,
                      throttle=throttle, should_cancel=should_cancel)
    p = parse_profile(data)
    if not p["id"]:
        raise SiteError("profile response had no user id")
    return p


def iter_videos(session, user_uuid, token=None, throttle=None, should_cancel=None, limit=PAGE_LIMIT):
    """Yield (page_no, [raw video dicts], count) newest-first until a page comes
    back short or empty.

    `count` is reported for display only: live on 2026-09-18 a profile said
    count=51 on page 0 and count=83 on page 1 (83 was right), so stopping at
    `count` would have silently dropped 33 videos. A short page is the only
    trustworthy end signal."""
    page = 0
    while page < MAX_PAGES:
        if should_cancel and should_cancel():
            return
        data = fetch_json(session, f"{API}/videos",
                          params={"user": user_uuid, "sort": "date", "page": page, "limit": limit},
                          token=token, throttle=throttle, should_cancel=should_cancel)
        results = (data or {}).get("results") or []
        count = (data or {}).get("count")
        if not results:
            return
        yield page, results, count
        if len(results) < limit:
            return
        page += 1


# ── auth ────────────────────────────────────────────────────────────

class IwaraAuth:
    """Turns saved credentials into a Bearer access token, refreshing/relogging
    as needed. Never raises: on any failure `access_token()` returns None and
    `auth_failed` / `message` say why, so listing continues anonymously."""

    def __init__(self, session, email, password, user_token=None, throttle=None, now=None):
        self._session = session
        self.email = (email or "").strip()
        self.password = password or ""
        self.user_token = (user_token or "").strip() or None
        self._throttle = throttle
        self._now = now or time.time
        self._access = None
        self.auth_failed = False
        self.message = ""
        self.user_token_changed = False

    @property
    def enabled(self):
        return bool(self.email and self.password)

    def _post(self, path, headers, body):
        if self._throttle:
            self._throttle.wait(lambda: False)
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        return self._session.post(f"{API}/{path}", data=body, headers=h, timeout=(15, 60))

    def _login(self):
        try:
            r = self._post("user/login", {}, json.dumps({"email": self.email, "password": self.password}))
        except requests.RequestException as e:
            self.auth_failed, self.message = True, f"login failed: {e.__class__.__name__}"
            return False
        try:
            data = r.json()
        except ValueError:
            data = {}
        tok = data.get("token") if isinstance(data, dict) else None
        if r.status_code == 200 and tok:
            self.user_token = tok
            self.user_token_changed = True
            self.auth_failed, self.message = False, ""
            return True
        msg = (data.get("message") if isinstance(data, dict) else "") or f"HTTP {r.status_code}"
        if "invalidLogin" in str(msg):
            msg = "invalid email or password"
        self.auth_failed, self.message = True, f"login failed: {msg}"
        return False

    def _refresh(self):
        try:
            r = self._post("user/token", _auth_headers(self.user_token), b"")
        except requests.RequestException as e:
            self.auth_failed, self.message = True, f"token refresh failed: {e.__class__.__name__}"
            return False
        if r.status_code == 200:
            try:
                tok = (r.json() or {}).get("accessToken")
            except ValueError:
                tok = None
            if tok:
                self._access = tok
                self.auth_failed, self.message = False, ""
                return True
        return False

    def access_token(self, force_refresh=False):
        if not self.enabled:
            return None
        if self._access and not force_refresh and token_valid(self._access, self._now()):
            return self._access
        if not token_valid(self.user_token, self._now()):
            if not self._login():
                return None
        if self._refresh():
            return self._access
        # The user token was rejected although unexpired — log in once more.
        if self._login() and self._refresh():
            return self._access
        if not self.auth_failed:
            self.auth_failed, self.message = True, "token refresh rejected"
        return None
