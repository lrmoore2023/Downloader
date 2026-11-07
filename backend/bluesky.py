"""Bluesky (bsky.app) — profile URL parsing, account lookup and sign-in.

Downloads go through gallery-dl's `bluesky` extractor (see
config_builder.build_bluesky_config and CreatorRunner._run_bluesky); this module
only holds what the app needs around it.

Measured 2026-11-07 on a public adult profile: the public AppView
(`public.api.bsky.app`) serves a profile's `posts_with_media` feed anonymously,
`porn`-labelled posts included, and every blob (image or the original uploaded
mp4) comes from the account's PDS via `com.atproto.sync.getBlob`. So signing in
is optional. It only matters for accounts whose posts the AppView hides from
logged-out viewers.

Sign-in is a handle + **app password** (gallery-dl's `username`/`password`).
Bluesky has no auth cookies: bsky.app keeps its session in localStorage. The
Chrome extension hands over that session's short-lived *access* token, and
`app_password_from_session` uses it once to mint the app's own app password,
named APP_PASSWORD_NAME. The browser's *refresh* token is never touched:
refresh tokens rotate on use, so spending it would sign Chrome out.
"""

import re
from urllib.parse import urlparse

import requests

PUBLIC_API = "https://public.api.bsky.app"
ENTRYWAY = "https://bsky.social"
APP_PASSWORD_NAME = "Downloader"
UA = "Downloader (+gallery-dl)"
TIMEOUT = (15, 30)

_PROFILE_RE = re.compile(
    # bsky.app and the embed mirrors gallery-dl also accepts (fxbsky.app, …)
    r"^(?:https?://)?(?:www\.)?(?:c|[fv]x)?bsky[ex]?\.app"
    r"/profile/([^/?#]+)", re.IGNORECASE)
_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+$", re.IGNORECASE)
_DID_RE = re.compile(r"^did:(?:plc:[a-z2-7]{24}|web:[a-z0-9.:%-]+)$", re.IGNORECASE)


def parse_profile_url(url):
    """The actor (handle or DID) of a bsky.app profile/post URL, or None."""
    m = _PROFILE_RE.match((url or "").strip())
    if not m:
        return None
    actor = m.group(1).strip()
    if actor.lower().startswith("did:"):
        return actor if _DID_RE.match(actor) else None
    actor = actor.lstrip("@").lower()
    return actor if _HANDLE_RE.match(actor) else None


def profile_url(actor):
    return f"https://bsky.app/profile/{actor}"


def archive_slug(did):
    """Filesystem-safe form of a DID for the per-link archive DB name
    (did:plc:abc → did-plc-abc). Keyed by the DID, not the handle: handles can
    change, and the archive must survive that so nothing is fetched twice."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", did or "unknown")


def _session(token=None):
    s = requests.Session()
    s.headers.update({"Accept": "application/json", "User-Agent": UA})
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def _error(r):
    try:
        d = r.json()
        return f"{d.get('error') or r.status_code}: {d.get('message') or ''}".strip(": ")
    except ValueError:
        return f"HTTP {r.status_code}"


def get_profile(actor, http=None):
    """{did, handle, display_name} for a handle or DID, from the public AppView.
    Raises ValueError with the API's reason if the account can't be found."""
    http = http or _session()
    r = http.get(f"{PUBLIC_API}/xrpc/app.bsky.actor.getProfile",
                 params={"actor": actor}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise ValueError(_error(r))
    d = r.json()
    return {"did": d["did"], "handle": d.get("handle") or actor,
            "display_name": (d.get("displayName") or "").strip(),
            "avatar": d.get("avatar") or ""}


def fetch(url):
    """Plain GET (an avatar on cdn.bsky.app) — the Response."""
    return _session().get(url, timeout=TIMEOUT)


def _https_root(url):
    """Only ever send a bearer token to an https host (the PDS the browser named)."""
    p = urlparse((url or "").strip())
    if p.scheme != "https" or not p.hostname:
        return None
    return f"https://{p.netloc}"


def api_server_for(pds):
    """gallery-dl's `api-server` for a signed-in run: None (its default, the
    bsky.social entryway) for accounts hosted by Bluesky, else the account's own
    PDS, which is the only server that can log a self-hosted account in."""
    root = _https_root(pds)
    if not root:
        return None
    host = urlparse(root).hostname or ""
    if host == "bsky.social" or host.endswith(".bsky.network"):
        return None
    return root


def verify_login(identifier, password, pds=None, http=None):
    """Log in with a handle + app password, the way gallery-dl will.
    Returns (ok, message, info) where info = {did, handle} on success."""
    identifier = (identifier or "").strip().lstrip("@")
    if not identifier or not password:
        return False, "Handle and app password are both needed", None
    root = api_server_for(pds) or ENTRYWAY
    http = http or _session()
    try:
        r = http.post(f"{root}/xrpc/com.atproto.server.createSession",
                      json={"identifier": identifier, "password": password}, timeout=TIMEOUT)
    except requests.RequestException as e:
        return None, f"{e.__class__.__name__}: {e}", None
    if r.status_code == 200:
        d = r.json()
        return True, f"Signed in as @{d.get('handle')}", {"did": d.get("did"),
                                                            "handle": d.get("handle")}
    if r.status_code in (400, 401):
        return False, f"Login rejected ({_error(r)})", None
    return None, f"HTTP {r.status_code} ({_error(r)})", None


def app_password_from_session(access_jwt, pds, http=None):
    """Mint the app's own app password from a browser session's access token.

    Any earlier password of ours (APP_PASSWORD_NAME) is revoked first: its value
    was only shown once, so it can't be read back, and app password names are
    unique. Returns {did, handle, password, pds}; raises ValueError with a
    message for the user on failure."""
    root = _https_root(pds)
    if not root:
        raise ValueError("The bsky.app session didn't name an https server")
    if not access_jwt or len(access_jwt) > 4096:
        raise ValueError("No Bluesky session token")
    http = http or _session(access_jwt)

    r = http.get(f"{root}/xrpc/com.atproto.server.getSession", timeout=TIMEOUT)
    if r.status_code != 200:
        raise ValueError("Chrome's Bluesky session wasn't accepted "
                         f"({_error(r)}). Reload the bsky.app tab and try again.")
    who = r.json()

    r = http.get(f"{root}/xrpc/com.atproto.server.listAppPasswords", timeout=TIMEOUT)
    if r.status_code != 200:
        raise ValueError(f"Couldn't list app passwords ({_error(r)})")
    names = {p.get("name") for p in (r.json().get("passwords") or [])}
    if APP_PASSWORD_NAME in names:
        r = http.post(f"{root}/xrpc/com.atproto.server.revokeAppPassword",
                      json={"name": APP_PASSWORD_NAME}, timeout=TIMEOUT)
        if r.status_code != 200:
            raise ValueError(f"Couldn't replace the old app password ({_error(r)})")

    r = http.post(f"{root}/xrpc/com.atproto.server.createAppPassword",
                  json={"name": APP_PASSWORD_NAME, "privileged": False}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise ValueError(f"Couldn't create an app password ({_error(r)})")
    return {"did": who.get("did"), "handle": who.get("handle"),
            "password": r.json().get("password"), "pds": root}
