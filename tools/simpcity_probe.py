"""Read-only probe of a SimpCity thread with the app's saved sign-in.

    .venv/Scripts/python.exe tools/simpcity_probe.py <thread-url> [--pages 1,11] [--out DIR]
                                                     [--raw] [--pace SECONDS]

Fetches the given pages (default: the URL's own page) using the session the app
captured at Settings ▸ SimpCity ▸ Sign in (cookies + UA from app_state.json),
saves each page's HTML under --out, and prints per post: id, date, and what the
parser extracts (media to auto-download, links to review). --raw also dumps every
URL-bearing attribute inside the post bodies, to characterise unfamiliar markup.

Never writes anything but the HTML dumps; never downloads media.
"""

import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import simpcity_scraper as sc  # noqa: E402


def _state():
    with open(os.path.join(ROOT, "app_state.json"), encoding="utf-8") as f:
        return json.load(f)


def _raw_urls(post_html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(post_html, "html.parser")
    out = []
    for el in soup.find_all(True):
        for attr, val in el.attrs.items():
            if isinstance(val, list):
                val = " ".join(val)
            if isinstance(val, str) and ("http" in val or val.startswith("//") or "loadMedia" in val):
                out.append(f"<{el.name} {attr}> {val[:160]}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--pages", default="")
    ap.add_argument("--out", default=os.path.join(ROOT, ".simpcity_probe"))
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--pace", type=float, default=4.0)
    a = ap.parse_args()

    t = sc.parse_thread_url(a.url)
    if not t:
        sys.exit("not a SimpCity thread URL")
    st = _state()
    cookies = st.get("simpcity_cookies_path") or ""
    if not (cookies and os.path.isfile(cookies)):
        sys.exit("Not signed in — Settings ▸ SimpCity ▸ Sign in first")
    s = sc.make_session(cookies_path=cookies, user_agent=st.get("simpcity_user_agent") or None)
    pages = [int(p) for p in a.pages.split(",") if p.strip()] or \
        [int(m.group(1)) if (m := re.search(r"/page-(\d+)", a.url)) else 1]
    os.makedirs(a.out, exist_ok=True)
    for i, page in enumerate(pages):
        if i:
            time.sleep(a.pace)
        url = sc.page_url(t["canonical"], page)
        t0 = time.monotonic()
        r = s.get(url, timeout=(15, 60))
        dt = time.monotonic() - t0
        path = os.path.join(a.out, f"{t['thread_id']}_p{page}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(r.text)
        print(f"== page {page}: HTTP {r.status_code} in {dt:.1f}s, {len(r.text)} bytes → {path}")
        print("   logged-in:", sc.logged_in_flag(r.text), " server:", r.headers.get("server"),
              " retry-after:", r.headers.get("retry-after"))
        if r.status_code != 200:
            continue
        parsed = sc.parse_page(r.text, base_url=url)
        print(f"   title={parsed['title']!r} page={parsed['page']} last_page={parsed['last_page']}"
              f" posts={len(parsed['posts'])}")
        for p in parsed["posts"]:
            print(f"   - post {p['post_id']}  {p['date']}  media={sum(1 for x in p['items'] if x['kind']=='media')}"
                  f" links={sum(1 for x in p['items'] if x['kind']!='media')}")
            for it in p["items"]:
                print(f"       [{it['kind']:5}] {it['host']:<12} {it['url'][:120]}")
            if a.raw:
                for line in _raw_urls(p.get("_html") or ""):
                    print("         raw:", line)


if __name__ == "__main__":
    main()
