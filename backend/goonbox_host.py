"""goonbox.cr — the image host SimpCity embeds (successor of jpg5/jpg6.su).

Measured 2026-10-17:
  * `/img/<id>` pages are behind a Cloudflare challenge, but the site is a
    Laravel SPA and its JSON API is not: `GET /sanctum/csrf-cookie` (sets
    `XSRF-TOKEN` + `goonbox_session`), then `GET /api/images/<id>` with
    `Accept: application/json` and `X-XSRF-TOKEN` → `{image: {original_url,
    size_bytes, created_at, original_filename, …}}`. Without the handshake the
    API answers 403 `{"message":"Forbidden"}`.
  * Files live on `simpN.cuckcapital.cr` (Cloudflare, but unguarded; Range OK).
  * In a forum post an image is `[url=https://goonbox.cr/img/ID][img]thumb[/img][/url]`
    — the link carries the id; the <img> is only the thumbnail.
  * Albums: `/api/albums/<id>` and `/api/albums/<id>/images?page=N&per_page=100`
    (`pagination.last_page`).
"""

import re
import threading
from urllib.parse import unquote, urlsplit

BASE = "https://goonbox.cr"
CDN_SUFFIX = "cuckcapital.cr"
_HOSTS = ("goonbox.cr",)
_IMG_RE = re.compile(r"^/img/(?:[^/]*\.)?([A-Za-z0-9]+)/?$")
_ALBUM_RE = re.compile(r"^/a/(?:[^/]*\.)?([A-Za-z0-9]+)/?$")


def _host(netloc):
    return (netloc or "").lower().split(":")[0]


def is_host(host):
    h = _host(host)
    return any(h == d or h.endswith("." + d) for d in _HOSTS)


def is_cdn(host):
    h = _host(host)
    return h == CDN_SUFFIX or h.endswith("." + CDN_SUFFIX)


def parse_url(url):
    """→ ("image"|"album", id) for a goonbox page URL, ("direct", url) for a CDN
    file, else None."""
    try:
        p = urlsplit((url or "").strip())
    except ValueError:
        return None
    if is_cdn(p.netloc):
        return "direct", url
    if not is_host(p.netloc):
        return None
    m = _IMG_RE.match(p.path)
    if m:
        return "image", m.group(1)
    m = _ALBUM_RE.match(p.path)
    if m:
        return "album", m.group(1)
    return None


class GoonboxGone(Exception):
    """The image/album no longer exists."""


class GoonboxAPI:
    """Thread-safe wrapper owning one session + its XSRF handshake."""

    def __init__(self, session=None):
        if session is None:
            from backend.pawchive_scraper import make_session
            session = make_session()
        self.s = session
        self._lock = threading.Lock()
        self._ready = False

    def _handshake(self):
        r = self.s.get(BASE + "/sanctum/csrf-cookie", timeout=(15, 30),
                       headers={"Accept": "application/json", "Referer": BASE + "/"})
        if r.status_code >= 400:
            raise RuntimeError(f"goonbox handshake failed (HTTP {r.status_code})")
        self._ready = True

    def _headers(self, referer):
        tok = ""
        try:
            tok = unquote(self.s.cookies.get("XSRF-TOKEN") or "")
        except Exception:
            pass
        return {"Accept": "application/json", "Referer": referer,
                "X-XSRF-TOKEN": tok, "X-Requested-With": "XMLHttpRequest"}

    def _get_json(self, path, referer, params=None):
        for attempt in (1, 2):
            with self._lock:
                if not self._ready:
                    self._handshake()
                r = self.s.get(BASE + path, params=params, timeout=(15, 30),
                               headers=self._headers(referer))
            if r.status_code in (404, 410):
                raise GoonboxGone(path)
            if r.status_code in (401, 403, 419) and attempt == 1:
                with self._lock:
                    self._ready = False     # session expired → new handshake
                continue
            r.raise_for_status()
            data = r.json()
            if data.get("is_deleted"):
                raise GoonboxGone(path)
            return data
        raise RuntimeError(f"goonbox API refused {path}")

    def image(self, image_id):
        """{id, url, filename, size, created_at, mime}. Raises GoonboxGone."""
        data = self._get_json(f"/api/images/{image_id}", f"{BASE}/img/{image_id}")
        return _image_info(data.get("image") or {}, image_id)

    def album(self, album_id):
        """(title, [image info…]) for a whole album. Raises GoonboxGone."""
        ref = f"{BASE}/a/{album_id}"
        meta = self._get_json(f"/api/albums/{album_id}", ref, {"per_page": 100})
        title = ((meta.get("album") or {}).get("title") or "").strip()
        out, page = [], 1
        while True:
            data = self._get_json(f"/api/albums/{album_id}/images", ref,
                                  {"page": page, "per_page": 100})
            for img in data.get("images") or []:
                out.append(_image_info(img, img.get("encoded_id") or img.get("id")))
            last = ((data.get("pagination") or {}).get("last_page")) or 1
            if page >= int(last) or not data.get("images"):
                break
            page += 1
        return title, out

    def probe_alive(self, url):
        parsed = parse_url(url)
        if not parsed or parsed[0] == "direct":
            return None
        kind, ident = parsed
        try:
            if kind == "image":
                self.image(ident)
            else:
                self._get_json(f"/api/albums/{ident}", f"{BASE}/a/{ident}", {"per_page": 1})
            return True
        except GoonboxGone:
            return False
        except Exception:
            return None


def _image_info(img, image_id):
    url = img.get("original_url") or ""
    name = img.get("original_filename") or url.rsplit("/", 1)[-1] or f"{image_id}.jpg"
    return {"id": str(image_id), "url": url, "filename": name,
            "size": img.get("size_bytes"), "created_at": img.get("created_at"),
            "mime": img.get("mime")}


def thumb_to_full(url):
    """Legacy chevereto-style thumbnail → full image (`x.md.jpg` / `x.th.jpg` → `x.jpg`)."""
    p = urlsplit(url)
    name = p.path.rsplit("/", 1)[-1]
    for trash in (".md.", ".th.", ".fr."):
        if trash in name:
            name = name.replace(trash, ".", 1)
            return url.replace(p.path, p.path.rsplit("/", 1)[0] + "/" + name, 1)
    return url
