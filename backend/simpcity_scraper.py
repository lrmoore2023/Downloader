"""SimpCity (simpcity.cr) thread scraper — sessions, URL parsing, page parsing.

SimpCity is a XenForo 2.3 forum behind DDoS-Guard. Threads are members-only:
a guest gets a 403 "Access Forbidden" page on every /threads/ URL (the forum
index alone is public), so everything here runs on the signed-in jar captured
by site_login ("simpcity"), replayed with the browser UA that earned it.

Whether a jar is signed in is read off the page itself: XenForo stamps
`data-logged-in="true|false"` on <html>. The cookie *names* are no use — the
cookie prefix (`data-cookie-prefix`) is rotated by the site.
"""

import re
from urllib.parse import urlsplit

BASE = "https://simpcity.cr"
HOSTS = ("simpcity.cr", "simpcity.su")


def make_session(cookies_path=None, user_agent=None):
    """curl_cffi Chrome-impersonating session carrying the saved sign-in.

    Same builder as pawchive: when the UA the login window used is known it is
    pinned and the impersonation profile aligned to its Chrome major, so the
    replayed DDoS-Guard cookies validate."""
    from backend.pawchive_scraper import make_session as _make
    s = _make(user_agent=user_agent or None, cookies_path=cookies_path)
    s.headers.update({
        "Referer": BASE + "/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s


_LOGGED_IN_RE = re.compile(r'<html\b[^>]*\bdata-logged-in="(true|false)"', re.I | re.S)


def logged_in_flag(html):
    """True / False from XenForo's <html data-logged-in>, None if absent."""
    m = _LOGGED_IN_RE.search(html or "")
    if not m:
        return None
    return m.group(1).lower() == "true"


def check_signed_in(session):
    """(signed_in: bool|None, message) — None = couldn't tell (block/maintenance)."""
    r = session.get(BASE + "/", timeout=(15, 30))
    flag = logged_in_flag(r.text if r.status_code == 200 else "")
    if r.status_code == 200 and flag is True:
        return True, "Signed in"
    if r.status_code == 200 and flag is False:
        return False, "Not signed in"
    return None, f"HTTP {r.status_code}"


# ── URL helpers ─────────────────────────────────────────────────────

_THREAD_RE = re.compile(r"/threads/(?:([^/]*?)\.)?(\d+)(?:/|$)", re.I)


def is_simpcity_host(host):
    h = (host or "").lower().split(":")[0]
    return any(h == d or h.endswith("." + d) for d in HOSTS)


def parse_thread_url(url):
    """A SimpCity thread URL (any page / post anchor) →
    {thread_id, slug, canonical}, or None."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if not is_simpcity_host(parts.netloc):
        return None
    m = _THREAD_RE.search(parts.path)
    if not m:
        return None
    slug, tid = (m.group(1) or ""), m.group(2)
    seg = f"{slug}.{tid}" if slug else tid
    return {"thread_id": tid, "slug": slug, "canonical": f"{BASE}/threads/{seg}/"}


def page_url(canonical, page):
    return canonical if int(page) <= 1 else f"{canonical}page-{int(page)}"


# ── link classification ─────────────────────────────────────────────
#
# Every URL found in a post becomes one item:
#   kind  "media"  — content hosted on an embed host; downloaded automatically
#         "album" / "file" — a link to a host we have an engine for (review list:
#                    Download / Download & track; a "file" can't be tracked)
#         "other"  — any other external link (review list: open / dismiss)
#         "embed"  — embedded media we can't resolve; never silently dropped
#   host  short site id (turbo, goonbox, simpcity, bunkr, gofile, …)
#   ref   stable identity within the host (file id, album id, or the URL)
#   media "video" | "image" for media items

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".jfif", ".heic")
_VIDEO_EXT = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".wmv", ".flv", ".ts")

# (host id, hostname regex, [(path regex, kind)])
_LINK_HOSTS = [
    ("bunkr", re.compile(r"(?:^|\.)bunkrr?\.[a-z]+$|(?:^|\.)bunkr-albums\.[a-z]+$"),
     [(re.compile(r"^/a/([A-Za-z0-9_-]+)"), "album"),
      (re.compile(r"^/(?:f|v|i|d)/([A-Za-z0-9_.-]+)"), "file")]),
    ("gofile", re.compile(r"(?:^|\.)gofile\.io$"),
     [(re.compile(r"^/d/([A-Za-z0-9]+)"), "album")]),
    ("cyberdrop", re.compile(r"(?:^|\.)cyberdrop\.[a-z]+$"),
     [(re.compile(r"^/a/([A-Za-z0-9_-]+)"), "album"),
      (re.compile(r"^/(?:f|e)/([A-Za-z0-9_-]+)"), "file")]),
    ("pixeldrain", re.compile(r"(?:^|\.)pixeldrain\.(?:com|net)$"),
     [(re.compile(r"^/l/([A-Za-z0-9]+)"), "album"),
      (re.compile(r"^/u/([A-Za-z0-9]+)"), "file")]),
    ("filester", re.compile(r"(?:^|\.)filester\.[a-z]+$"),
     [(re.compile(r"^/f/([A-Za-z0-9_-]+)"), "album"),
      (re.compile(r"^/d/([A-Za-z0-9_-]+)"), "file")]),
]


def media_kind_of(path):
    """"image" / "video" from a path's extension, else None."""
    p = (path or "").lower().split("?")[0]
    if p.endswith(_IMAGE_EXT):
        return "image"
    if p.endswith(_VIDEO_EXT):
        return "video"
    return None


def normalize_url(url, base_url=None):
    """Absolute http(s) URL with XenForo redirect wrappers unwrapped, or None."""
    from urllib.parse import parse_qs, urljoin
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if base_url:
        url = urljoin(base_url, url)
    if not url.lower().startswith(("http://", "https://")):
        return None
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    if is_simpcity_host(p.netloc) and p.path.rstrip("/").endswith(("/redirect", "/goto/link")):
        q = parse_qs(p.query)
        inner = (q.get("to") or q.get("url") or [None])[0]
        if inner:
            return normalize_url(inner)
    return url


def short_host(host):
    h = (host or "").lower().split(":")[0]
    h = h[4:] if h.startswith("www.") else h
    parts = h.split(".")
    return parts[-2] if len(parts) >= 2 else h


def classify(url):
    """→ item dict {kind, host, ref, url[, media]} or None for URLs that are not
    content (forum navigation, member pages, …)."""
    from backend import goonbox_host as gb
    from backend import turbo_host as th
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    host = (p.netloc or "").lower().split(":")[0]
    path = p.path or "/"

    t = th.parse_url(url)
    if t:
        kind, ident = t
        if kind == "file":
            return {"kind": "media", "media": "video", "host": "turbo", "ref": ident, "url": url}
        return {"kind": "album", "host": "turbo", "ref": ident, "url": url}

    g = gb.parse_url(url)
    if g:
        kind, ident = g
        if kind == "image":
            return {"kind": "media", "media": "image", "host": "goonbox", "ref": ident, "url": url}
        if kind == "direct":
            full = gb.thumb_to_full(url)
            fpath = urlsplit(full).path
            return {"kind": "media", "media": media_kind_of(fpath) or "image",
                    "host": "goonbox", "ref": fpath, "url": full, "direct": True}
        return {"kind": "album", "host": "goonbox", "ref": ident, "url": url}

    if is_simpcity_host(host):
        m = re.search(r"/attachments/([^/]*?)\.?(\d+)/?$", path)
        if m:
            slug = m.group(1)
            # XenForo slugs the filename: "clip-mp4.123/" → "clip.mp4"
            guess = re.sub(r"-(\w{2,4})$", r".\1", slug)
            return {"kind": "media", "media": media_kind_of(guess) or "image",
                    "host": "simpcity", "ref": "att" + m.group(2), "url": url}
        if path.startswith("/data/") and media_kind_of(path):
            return {"kind": "media", "media": media_kind_of(path), "host": "simpcity",
                    "ref": path, "url": url, "direct": True}
        return None   # forum navigation: threads, members, posts, tags…

    for hid, hrx, paths in _LINK_HOSTS:
        if hrx.search(host):
            for prx, kind in paths:
                m = prx.match(path)
                if m:
                    return {"kind": kind, "host": hid, "ref": m.group(1), "url": url}
            return {"kind": "other", "host": hid, "ref": url, "url": url}

    return {"kind": "other", "host": short_host(host), "ref": url, "url": url}


def direct_media(url):
    """An item for a bare media file URL on a host we have no resolver for
    (embedded <img>/<video> sources). None if the URL isn't obviously media."""
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    kind = media_kind_of(p.path)
    if not kind:
        return None
    return {"kind": "media", "media": kind, "host": short_host(p.netloc),
            "ref": url, "url": url, "direct": True}


# ── page parsing ────────────────────────────────────────────────────

def _soup(html):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html or "", "html.parser")


def _int(s):
    m = re.search(r"\d+", s or "")
    return int(m.group(0)) if m else None


def parse_page(html, base_url=None):
    """One thread page → {title, page, last_page, next_url, logged_in, posts:[…]}.

    Each post: {post_id, date (ISO, UTC), author, url, items:[…]}. Items come only
    from the post body (.message-userContent) and its attachment list — never the
    signature, footer, reactions, avatars, or quoted text (a quote repeats
    another post's media). Within a post, items are de-duplicated by (host, ref)."""
    from datetime import datetime, timezone
    soup = _soup(html)
    title = ""
    title_el = soup.select_one("h1.p-title-value")
    if title_el:
        for lab in title_el.select(".label, .labelLink, .label-append"):
            lab.decompose()
        title = title_el.get_text(" ", strip=True)
    cur = soup.select_one(".pageNav-page--current")
    page = _int(cur.get_text()) if cur else 1
    last = None
    for a in soup.select(".pageNav-main .pageNav-page"):
        n = _int(a.get_text())
        if n:
            last = max(last or 0, n)
    nxt = soup.select_one("a.pageNav-jump--next[href]")
    next_url = normalize_url(nxt["href"], base_url) if nxt else None

    posts, seen_ids = [], set()
    for art in soup.select("article.message"):
        pid = re.sub(r"\D", "", art.get("data-content") or art.get("id") or "")
        if not pid or pid in seen_ids:
            continue
        seen_ids.add(pid)
        head = art.select_one("header.message-attribution") or art
        t = head.select_one("time[data-timestamp]") or art.select_one("time[data-timestamp]")
        date = None
        if t is not None:
            try:
                date = datetime.fromtimestamp(int(t["data-timestamp"]), timezone.utc).isoformat()
            except (ValueError, KeyError):
                date = t.get("datetime")
        body = art.select_one(".message-userContent") or art.select_one(".message-content")
        items = extract_items(body, art, base_url) if body is not None else []
        posts.append({"post_id": pid, "date": date, "author": art.get("data-author") or "",
                      "url": f"{BASE}/posts/{pid}/", "items": items,
                      "_html": str(body) if body is not None else ""})
    return {"title": title, "page": page or 1, "last_page": last or page or 1,
            "next_url": next_url, "logged_in": logged_in_flag(html), "posts": posts}


_JUNK = ("blockquote, .bbCodeBlock--quote, .message-signature, .message-footer, "
         ".reactionsBar, img.smilie, .avatar, a.username")


def extract_items(body, article=None, base_url=None):
    """Ordered, de-duplicated content items of one post body (a bs4 Tag)."""
    for junk in body.select(_JUNK):
        junk.decompose()

    out, seen = [], set()

    def add(item):
        if not item:
            return
        key = (item["host"], item["ref"])
        if key in seen:
            return
        seen.add(key)
        out.append(item)

    def from_url(raw, embedded=False):
        u = normalize_url(raw, base_url)
        if not u:
            return
        item = classify(u)
        if item and item["kind"] == "other" and embedded:
            item = direct_media(u) or dict(item, kind="embed")
        add(item)

    # Document order, so items keep the post's order.
    for el in body.find_all(["img", "a", "iframe", "video", "source", "span", "div"]):
        if el.name == "img":
            parent = el.find_parent("a")
            if parent is not None and parent.get("href"):
                href = normalize_url(parent.get("href"), base_url)
                item = classify(href) if href else None
                if item and item["kind"] in ("media", "album", "file"):
                    add(item)
                    continue
            from_url(el.get("data-url") or el.get("data-src") or el.get("src"), embedded=True)
        elif el.name == "a":
            if el.find("img") is not None:
                continue   # handled via its <img>
            href = el.get("href")
            if href and not href.startswith("#"):
                from_url(href)
        elif el.name == "iframe":
            from_url(el.get("data-src") or el.get("src"), embedded=True)
        elif el.name in ("video", "source"):
            from_url(el.get("src") or el.get("data-src"), embedded=True)
        else:
            m = re.search(r"loadMedia\(this,\s*'([^']+)'", el.get("onclick") or "")
            if m:
                from_url(m.group(1), embedded=True)

    if article is not None:
        for a in article.select(".message-attachments a[href]"):
            from_url(a.get("href"))
    return out
