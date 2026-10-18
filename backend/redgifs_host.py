"""redgifs — embedded in SimpCity posts as `<iframe src="https://redgifs.com/ifr/<id>">`.

Measured 2026-10-17: `GET api.redgifs.com/v2/auth/temporary` → {token} (anonymous);
`GET /v2/gifs/<id>` with `Authorization: Bearer <token>` → {gif: {urls: {hd, sd, …},
createDate}}; unknown id → 404 GifNotFound. The media CDN 403s without a redgifs
Referer.
"""

import re
import threading
import time
from urllib.parse import urlsplit

API = "https://api.redgifs.com/v2"
DOWNLOAD_HEADERS = {"Referer": "https://www.redgifs.com/", "Origin": "https://www.redgifs.com"}
_ID_RE = re.compile(r"^/(?:ifr|watch|i)/([A-Za-z]+)")
_lock = threading.Lock()
_token = {"value": None, "ts": 0.0}


class RedgifsGone(Exception):
    pass


def parse_url(url):
    """Lower-case gif id from a redgifs watch/ifr URL, else None."""
    try:
        p = urlsplit(url or "")
    except ValueError:
        return None
    host = (p.netloc or "").lower().split(":")[0]
    if not (host == "redgifs.com" or host.endswith(".redgifs.com")):
        return None
    m = _ID_RE.match(p.path or "")
    return m.group(1).lower() if m else None


def _get_token(session, fresh=False):
    with _lock:
        if fresh or not _token["value"] or time.time() - _token["ts"] > 3600:
            r = session.get(API + "/auth/temporary", timeout=(15, 30))
            r.raise_for_status()
            _token["value"], _token["ts"] = r.json()["token"], time.time()
        return _token["value"]


def resolve(session, gif_id):
    """{url, filename, created} for the best available rendition. Raises RedgifsGone."""
    for attempt in (1, 2):
        tok = _get_token(session, fresh=attempt == 2)
        r = session.get(f"{API}/gifs/{gif_id}", timeout=(15, 30),
                        headers={"Authorization": f"Bearer {tok}"})
        if r.status_code in (404, 410):
            raise RedgifsGone(gif_id)
        if r.status_code == 401 and attempt == 1:
            continue
        r.raise_for_status()
        gif = (r.json() or {}).get("gif") or {}
        urls = gif.get("urls") or {}
        url = urls.get("hd") or urls.get("sd")
        if not url:
            raise RuntimeError("redgifs: no video URL")
        return {"url": url, "filename": url.rsplit("/", 1)[-1].split("?")[0],
                "created": gif.get("createDate")}
    raise RuntimeError("redgifs: token refused")
