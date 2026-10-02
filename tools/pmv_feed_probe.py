"""Read-only live probe of the PMV followed-feeds, using the app's saved sign-ins.

Prints a short summary per site and saves the raw responses under --out (default:
a temp folder — never the repo, the responses name creators). It only ever GETs;
it never marks a notification read.

    .venv/Scripts/python.exe tools/pmv_feed_probe.py [--site iwara|rule34video|pawchive|pmvhaven] [--pages 2]
"""
import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import iwara_scraper as iw
from backend import pmvhaven_scraper as pmvh
from backend import r34video_scraper as r34
from backend import site_login

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _save(out, name, text):
    p = os.path.join(out, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, indent=1))
    return p


def probe_iwara(state, out, pages):
    s = iw.make_session()
    auth = iw.IwaraAuth(s, state.get("iwara_email", ""), state.get("iwara_password", ""),
                        user_token=state.get("iwara_token") or None)
    tok = auth.access_token()
    print("iwara: token", bool(tok), auth.message)
    for page in range(pages):
        d = iw.fetch_json(s, f"{iw.API}/videos", params={"subscribed": "true", "sort": "date",
                                                         "page": page, "limit": 50}, token=tok)
        _save(out, f"iwara_feed_{page}.json", d)
        res = d.get("results") or []
        print(f"  page {page}: {len(res)} (count {d.get('count')})",
              res[0]["createdAt"] if res else "", "→", res[-1]["createdAt"] if res else "")


def probe_pmvhaven(state, out, pages):
    key = (state.get("pmvhaven_api_key") or "").strip()
    if not key:
        print("pmvhaven: no API key saved")
        return
    s = pmvh.make_session(api_key=key)
    for path in ("/auth/session", "/user/profile", "/notifications/unread-count"):
        r = s.get(pmvh.API + path, timeout=30)
        print(f"  GET {path}: {r.status_code}")
        _save(out, "pmvhaven" + path.replace("/", "_") + ".json", r.text)
    for page in range(1, pages + 1):
        r = s.get(pmvh.API + "/notifications", params={"page": page, "limit": 100}, timeout=30)
        print(f"  notifications page {page}: HTTP {r.status_code}")
        _save(out, f"pmvhaven_notifications_{page}.json", r.text)
        try:
            d = r.json()
        except ValueError:
            break
        body = d.get("data") if isinstance(d.get("data"), dict) else d
        ns = body.get("notifications") or []
        types = {}
        for n in ns:
            types[n.get("type")] = types.get(n.get("type"), 0) + 1
        print(f"    {len(ns)} notifications, types {types}, pagination {body.get('pagination')}")
        if ns:
            print("    first:", json.dumps(ns[0], ensure_ascii=False)[:600])
        if not (body.get("pagination") or {}).get("hasNext"):
            break


def probe_r34(state, out, pages):
    path = state.get("r34video_cookies_path") or ""
    if not (path and os.path.isfile(path)):
        print("rule34video: not signed in")
        return
    s = r34.make_session(cookies_path=path)
    r = s.get(r34.BASE + "/my/subscriptions/", timeout=30, allow_redirects=False)
    print(f"  /my/subscriptions/: HTTP {r.status_code} {r.headers.get('location') or ''} {len(r.text)} bytes")
    _save(out, "r34_subscriptions.html", r.text)
    import re
    for m in sorted(set(re.findall(r'block_id[=:"\' ]+([A-Za-z0-9_]+)', r.text))):
        print("    block id:", m)
    for m in sorted(set(re.findall(r'data-parameters="([^"]+)"', r.text)))[:10]:
        print("    pager params:", m)
    for m in sorted(set(re.findall(r'href="(https://rule34video\.com/my/[^"]*)"', r.text))):
        print("    my link:", m)


def probe_pawchive(state, out, pages):
    path = state.get("pawchive_cookies_path") or ""
    s = site_login.make_feed_session("pawchive", path or None, state.get("pawchive_user_agent") or None)
    r = s.get("https://pawchive.pw/api/v1/account/favorites?type=artist", timeout=30)
    print(f"  favorites: HTTP {r.status_code} {r.headers.get('content-type')}")
    _save(out, "pawchive_favorites.json", r.text)
    try:
        favs = r.json()
    except ValueError:
        return
    if isinstance(favs, list):
        print(f"    {len(favs)} favorites; keys {sorted(favs[0].keys()) if favs else []}")
        for f in sorted(favs, key=lambda f: f.get("updated") or "", reverse=True)[:5]:
            print("    ", {k: f.get(k) for k in ("service", "id", "name", "updated", "faved_seq", "last_imported")})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="all")
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "pmv_feed_probe"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(APP_DIR, "app_state.json"), encoding="utf-8") as f:
        state = json.load(f)
    probes = {"iwara": probe_iwara, "pmvhaven": probe_pmvhaven,
              "rule34video": probe_r34, "pawchive": probe_pawchive}
    for name, fn in probes.items():
        if a.site in ("all", name):
            print(f"== {name}")
            try:
                fn(state, a.out, a.pages)
            except Exception as e:
                print(f"  failed: {e.__class__.__name__}: {e}")
    print("raw responses saved in", a.out)


if __name__ == "__main__":
    main()
