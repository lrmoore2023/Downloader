"""turbo.cr — the video host SimpCity embeds (formerly saint / turbovid).

Measured 2026-10-17:
  * `/d/<id>` is the download page. Its altcha proof-of-work widget only gates
    the *button*: `GET /api/sign?v=<id>` answers anonymously with
    `{success, url, filename, original_filename}`. The signed CDN URL
    (`dlNNN.turbocdn.st/...?exp=…&token=…`) serves Range requests (206) and a
    `content-disposition` with the original filename. Append `&dl=1`, as the page does.
  * The `/d/` page carries the exact byte size, the MD5 checksum and the upload
    date (`YYYY-MM-DD`), used to verify downloads and to date album files.
  * `/embed/<id>` and `/v/<id>` are the same file; older posts use the
    saint / turbovid domains with the same paths.
  * Albums: `/a/<id>` lists files as `#fileTbody tr[data-id]`, paged by `?page=N`.
"""

import html as _html
import re
from urllib.parse import urlsplit

BASE = "https://turbo.cr"
_HOST_RE = re.compile(r"(?:^|\.)(?:turbo(?:vid)?\.cr|saint\d*\.(?:su|pk|cr|to))$", re.I)
_FILE_RE = re.compile(r"^/(?:d|v|embed)/([A-Za-z0-9_-]+)")
_DATA_RE = re.compile(r"^/(?:api/)?data/([A-Za-z0-9_-]+)\.\w+$")
_ALBUM_RE = re.compile(r"^/a/([A-Za-z0-9_-]+)")


def is_host(host):
    return bool(_HOST_RE.search((host or "").lower().split(":")[0]))


def parse_url(url):
    """→ ("file"|"album", id) for a turbo/saint URL, else None."""
    try:
        p = urlsplit((url or "").strip())
    except ValueError:
        return None
    if not is_host(p.netloc):
        return None
    for rx, kind in ((_FILE_RE, "file"), (_DATA_RE, "file"), (_ALBUM_RE, "album")):
        m = rx.match(p.path)
        if m:
            return kind, m.group(1)
    return None


def file_page_url(file_id):
    return f"{BASE}/d/{file_id}"


def make_session():
    from backend.pawchive_scraper import make_session as _make
    s = _make()
    s.headers.update({"Referer": BASE + "/", "Accept": "text/html,application/json,*/*"})
    return s


class TurboGone(Exception):
    """The file/album no longer exists on turbo."""


def _text(rx, html, flags=re.S | re.I):
    m = re.search(rx, html or "", flags)
    return _html.unescape(m.group(1)).strip() if m else ""


def parse_file_page(html):
    """{md5, size, upload_date, filename} from a `/d/<id>` page (any may be missing)."""
    md5 = _text(r"Checksum \(MD5\)\s*</div>\s*<div[^>]*>\s*([0-9a-fA-F]{32})\s*<", html).lower()
    size = None
    raw = _text(r'id="fileSizeBytes"[^>]*>\s*([^<]+)<', html)
    if raw:
        try:
            size = int(float(raw.replace(",", "")))
        except ValueError:
            size = None
    date = ""
    m = re.search(r"ID:\s*<span[^>]*>[^<]*</span></span>.{0,200}?<span>(\d{4}-\d{2}-\d{2})</span>",
                  html or "", re.S)
    if m:
        date = m.group(1)
    filename = _text(r"<dt[^>]*>\s*Filename\s*</dt>\s*<dd[^>]*>([^<]+)</dd>", html)
    return {"md5": md5 or None, "size": size, "upload_date": date or None,
            "filename": filename or None}


def sign(session, file_id):
    """Fresh signed download URL → (url, original_filename). Raises TurboGone."""
    r = session.get(f"{BASE}/api/sign", params={"v": file_id}, timeout=(15, 30),
                    headers={"Referer": file_page_url(file_id), "Accept": "application/json"})
    if r.status_code in (404, 410):
        raise TurboGone(file_id)
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"turbo sign: unexpected response (HTTP {r.status_code})")
    if not data.get("success") or not data.get("url"):
        msg = str(data.get("message") or data.get("error") or "")
        if "not found" in msg.lower() or "deleted" in msg.lower():
            raise TurboGone(file_id)
        raise RuntimeError(f"turbo sign failed: {msg or data}")
    url = data["url"]
    url += ("&" if "?" in url else "?") + "dl=1"
    return url, (data.get("original_filename") or data.get("filename") or f"{file_id}.mp4")


def resolve(session, file_id):
    """Everything needed to download one turbo file:
    {id, url, filename, size, md5, upload_date}. Raises TurboGone."""
    meta = {}
    r = session.get(file_page_url(file_id), timeout=(15, 30))
    if r.status_code in (404, 410):
        raise TurboGone(file_id)
    if r.status_code == 200:
        meta = parse_file_page(r.text)
    url, original = sign(session, file_id)
    return {"id": file_id, "url": url, "filename": original or meta.get("filename"),
            "size": meta.get("size"), "md5": meta.get("md5"),
            "upload_date": meta.get("upload_date")}


def parse_album_page(html):
    """(title, [file ids], has_next) from an `/a/<id>` page."""
    title = _text(r"<h1[^>]*>(.*?)</h1>", html)
    title = re.sub(r"<[^>]+>", "", title).strip()
    body = html or ""
    m = re.search(r'id="fileTbody"(.*?)</tbody>', body, re.S)
    ids = re.findall(r'<tr[^>]*\bdata-id="([A-Za-z0-9_-]+)"', m.group(1) if m else body)
    has_next = bool(re.search(r'<a[^>]+href="[^"]*\?page=\d+[^"]*"[^>]*>[^<]*Next', body, re.I))
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return title, out, has_next


def album_files(session, album_id, max_pages=200):
    """(title, [file ids]) across every page of an album. Raises TurboGone."""
    title, out = "", []
    for page in range(1, max_pages + 1):
        url = f"{BASE}/a/{album_id}" + (f"?page={page}" if page > 1 else "")
        r = session.get(url, timeout=(15, 30))
        if r.status_code in (404, 410):
            if page == 1:
                raise TurboGone(album_id)
            break
        r.raise_for_status()
        t, ids, has_next = parse_album_page(r.text)
        title = title or t
        new = [i for i in ids if i not in out]
        out.extend(new)
        if not has_next or not new:
            break
    return title, out


def probe_alive(session, url):
    """True / False / None (couldn't tell) for a turbo file or album URL."""
    parsed = parse_url(url)
    if not parsed:
        return None
    kind, ident = parsed
    path = f"/d/{ident}" if kind == "file" else f"/a/{ident}"
    try:
        r = session.get(BASE + path, timeout=(15, 30))
    except Exception:
        return None
    if r.status_code in (404, 410):
        return False
    if r.status_code == 200:
        return True
    return None
