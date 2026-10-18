"""cyberdrop.cr single files — SimpCity posts embed them as `<iframe src="https://cyberdrop.cr/e/<id>">`.

Measured 2026-10-17, anonymous:
  GET https://api.cyberdrop.cr/api/file/info/<id> → {name, type, size, slug, …}
  GET https://api.cyberdrop.cr/api/file/auth/<id> → {url: signed cdn.cyberdrop.cr link}
(the same two calls cyberdrop-dl makes). A missing file is a 404.
"""

import re
from urllib.parse import urlsplit

API = "https://api.cyberdrop.cr/api"
_FILE_RE = re.compile(r"^/(?:e|f|api/file/(?:d|info|auth))/([A-Za-z0-9_-]+)")


class CyberdropGone(Exception):
    pass


def parse_file_url(url):
    """File id from a cyberdrop /e/, /f/ or API URL, else None."""
    try:
        p = urlsplit(url or "")
    except ValueError:
        return None
    if not re.search(r"(?:^|\.)cyberdrop\.[a-z]+$", (p.netloc or "").lower().split(":")[0]):
        return None
    m = _FILE_RE.match(p.path or "")
    return m.group(1) if m else None


def _get(session, path):
    r = session.get(API + path, timeout=(15, 30),
                    headers={"Accept": "application/json", "Referer": "https://cyberdrop.cr/"})
    if r.status_code in (404, 410):
        raise CyberdropGone(path)
    r.raise_for_status()
    return r.json()


def sign(session, file_id):
    """A fresh signed download URL. Raises CyberdropGone."""
    url = (_get(session, f"/file/auth/{file_id}") or {}).get("url")
    if not url:
        raise RuntimeError("cyberdrop: no download URL")
    return url


def resolve(session, file_id):
    """{url, filename, size, mime}. Raises CyberdropGone."""
    info = _get(session, f"/file/info/{file_id}") or {}
    return {"url": sign(session, file_id), "filename": info.get("name") or f"{file_id}.mp4",
            "size": info.get("size"), "mime": info.get("type") or ""}
