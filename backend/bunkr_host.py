"""bunkr — native album/file resolver (replaces cyberdrop-dl for bunkr).

Measured 2026-10-18 (bunkr up; read-only probes + capped test downloads):
  * Album: `GET /a/<id>?advanced=1` embeds `window.albumFiles = [ {id, name, original,
    slug, type, size, timestamp: "HH:MM:SS DD/MM/YYYY", …}, … ]` — every file with its
    exact size and upload time (UTC). Title = og:title.
  * File page `/f/<slug>` (`/v/`, `/i/`, `/d/` redirect there) carries the numeric file
    id in its download button `https://dl.bunkr.cr/file/<id>` and the original name in
    og:title. It shows no date.
  * `POST https://dl.bunkr.cr/api/_001_v2 {"id": "<id>"}` (id as a STRING — an int
    is "400 Invalid JSON payload") → {mediafiles: "https://<node>.cdn.cr", path,
    original}; `GET https://glb-apisign.cdn.cr/sign?path=<path>` → {token, ex};
    file = <mediafiles><path>?token=&ex=. Album `id`s work with the same API.
  * The CDN's Last-Modified equals the listed upload time (to the second), so it
    dates a lone file.
  * Concurrency: a CDN node serves TWO downloads per client at once; the third gets
    an immediate 503 (c6no3-b: 3 at once → 1×503, 6 → 4×503). → callers cap per node.
  * Cold storage: some files come from `psu.scdn.st` with a 1–11 s time-to-first-
    byte; a second request is instant (0.1 s) — the first request warms them.
  * A missing album/file is a clean 404 (0.1 s). During an outage requests time out
    or 5xx instead — never read those as "gone".
"""

import html as _html
import json
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

DL_API = "https://dl.bunkr.cr/api/_001_v2"
SIGN_API = "https://glb-apisign.cdn.cr/sign"
API_HEADERS = {"Referer": "https://dl.bunkr.cr/", "Origin": "https://dl.bunkr.cr",
               "Accept": "application/json"}
DOWNLOAD_HEADERS = {"Referer": "https://bunkr.cr/"}
# Mirrors of the HTML site; one may be down or DNS-blocked while others answer.
HOSTS = ("bunkr.cr", "bunkr.site", "bunkr.ph", "bunkr.pk")
PER_NODE = 2            # downloads per CDN node before it 503s

_HOST_RE = re.compile(r"(?:^|\.)bunkrr?\.[a-z]+$|(?:^|\.)bunkr-albums\.[a-z]+$", re.I)
_good_host = {"value": None}
_host_lock = threading.Lock()


class BunkrGone(Exception):
    """A definite 404: the album/file was removed."""


class BunkrUnavailable(Exception):
    """bunkr didn't answer usefully (outage, maintenance, 5xx) — retry later."""


def is_host(host):
    return bool(_HOST_RE.search((host or "").lower().split(":")[0]))


def parse_url(url):
    """→ ("album", id) / ("file", slug) for a bunkr URL, else None."""
    try:
        p = urlsplit((url or "").strip())
    except ValueError:
        return None
    if not is_host(p.netloc):
        return None
    m = re.match(r"^/a/([A-Za-z0-9_-]+)", p.path or "")
    if m:
        return "album", m.group(1)
    m = re.match(r"^/(?:f|v|i|d)/([A-Za-z0-9_.-]+)", p.path or "")
    if m:
        return "file", m.group(1)
    return None


def make_session():
    from backend.pawchive_scraper import make_session as _make
    s = _make()
    s.headers.update({"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    return s


def _page(session, path):
    """GET a bunkr HTML page, falling over to a mirror host when one is down."""
    hosts = list(HOSTS)
    if _good_host["value"]:
        hosts.remove(_good_host["value"])
        hosts.insert(0, _good_host["value"])
    last = None
    for host in hosts:
        try:
            r = session.get(f"https://{host}{path}", timeout=(15, 40))
        except Exception as e:
            last = f"{e.__class__.__name__}"
            continue
        if r.status_code == 404:
            raise BunkrGone(path)
        if r.status_code == 200:
            if "server under maintenance" in (r.text or "").lower():
                raise BunkrUnavailable("bunkr server under maintenance")
            with _host_lock:
                _good_host["value"] = host
            return r.text
        last = f"HTTP {r.status_code}"
    raise BunkrUnavailable(f"bunkr didn't answer ({last})")


def _js_str(raw):
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return raw.strip('"')
    if raw.startswith("'"):
        return raw.strip("'")
    return raw


def parse_album_page(text):
    """(title, [ {id, name, original, slug, type, size, uploaded} ]) from an album page."""
    m = re.search(r'og:title"\s+content="([^"]*)"', text or "")
    title = _html.unescape(m.group(1)).strip() if m else ""
    start = (text or "").find("window.albumFiles")
    if start < 0:
        return title, []
    end = text.find("];", start)
    body = text[start:end if end > 0 else len(text)]
    files = []
    for obj in re.findall(r"\{(.*?)\}", body, re.S):
        fields = dict(re.findall(r'(\w+)\s*:\s*("(?:[^"\\]|\\.)*"|\'[^\']*\'|-?\d+)', obj))
        if "slug" not in fields or "id" not in fields:
            continue
        ts = _js_str(fields.get("timestamp", '""'))
        uploaded = None
        try:
            uploaded = datetime.strptime(ts, "%H:%M:%S %d/%m/%Y").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        size = fields.get("size", "0")
        files.append({"id": _js_str(fields["id"]), "slug": _js_str(fields["slug"]),
                      "name": _js_str(fields.get("name", '""')),
                      "original": _js_str(fields.get("original", '""')) or None,
                      "type": _js_str(fields.get("type", '""')),
                      "size": int(size) if str(size).lstrip("-").isdigit() else None,
                      "uploaded": uploaded})
    return title, files


def album(session, album_id):
    """(title, files). Raises BunkrGone / BunkrUnavailable."""
    return parse_album_page(_page(session, f"/a/{album_id}?advanced=1"))


def parse_file_page(text):
    """{id, name} from a /f/ page (id = the dl.bunkr.cr file id)."""
    m = re.search(r"dl\.bunkr\.[a-z]+/file/(\d+)", text or "")
    t = re.search(r'og:title"\s+content="([^"]*)"', text or "")
    return {"id": m.group(1) if m else None,
            "name": _html.unescape(t.group(1)).strip() if t else None}


def file_info(session, slug):
    """{id, name} for a lone file. Raises BunkrGone / BunkrUnavailable."""
    info = parse_file_page(_page(session, f"/f/{slug}"))
    if not info["id"]:
        raise BunkrUnavailable("bunkr file page had no download id")
    return info


def resolve(session, file_id):
    """Signed download URL → {url, node, original}. Raises BunkrGone / BunkrUnavailable."""
    r = session.post(DL_API, json={"id": str(file_id)}, timeout=(15, 30), headers=API_HEADERS)
    if r.status_code == 404:
        raise BunkrGone(str(file_id))
    if r.status_code != 200:
        raise BunkrUnavailable(f"bunkr download API: HTTP {r.status_code}")
    d = r.json()
    if not d.get("path") or not d.get("mediafiles"):
        raise BunkrUnavailable("bunkr download API: no file path")
    sg = session.get(SIGN_API, params={"path": d["path"]}, timeout=(15, 30), headers=API_HEADERS)
    if sg.status_code != 200:
        raise BunkrUnavailable(f"bunkr sign API: HTTP {sg.status_code}")
    sg = sg.json()
    node = urlsplit(d["mediafiles"]).netloc
    return {"url": f"https://{node}{d['path']}?token={sg['token']}&ex={sg['ex']}",
            "node": node, "original": d.get("original")}


def warm(session, url, read_timeout=120):
    """Ask the CDN for the first byte: pulls a cold file out of storage and returns
    (status, total_size, last_modified) — status None on timeout/connection error."""
    try:
        r = session.get(url, timeout=(20, read_timeout),
                        headers=dict(DOWNLOAD_HEADERS, Range="bytes=0-0"))
    except Exception:
        return None, None, None
    try:
        size = None
        m = re.search(r"/\s*(\d+)\s*$", r.headers.get("Content-Range") or "")
        if m:
            size = int(m.group(1))
        lm = None
        if r.headers.get("Last-Modified"):
            try:
                lm = parsedate_to_datetime(r.headers["Last-Modified"])
            except (TypeError, ValueError):
                lm = None
        return r.status_code, size, lm
    finally:
        try:
            r.close()
        except Exception:
            pass


class NodeLimiter:
    """At most PER_NODE concurrent downloads per CDN node (bunkr 503s the next one)."""

    def __init__(self, per_node=PER_NODE):
        self._per = per_node
        self._lock = threading.Lock()
        self._sems = {}

    def __call__(self, node):
        with self._lock:
            sem = self._sems.get(node)
            if sem is None:
                sem = self._sems[node] = threading.BoundedSemaphore(self._per)
            return sem


def probe_alive(session, url):
    """True / False / None for a bunkr album or file URL (404 = gone)."""
    parsed = parse_url(url)
    if not parsed:
        return None
    kind, ident = parsed
    try:
        _page(session, f"/a/{ident}?advanced=1" if kind == "album" else f"/f/{ident}")
        return True
    except BunkrGone:
        return False
    except Exception:
        return None


def sleep_cancellable(seconds, cancelled):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not cancelled():
        time.sleep(0.25)
