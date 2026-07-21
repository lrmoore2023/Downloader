"""Phase-0 feasibility probe: prove the in-app Cloudflare bypass end to end.

Opens pawchive in the app's embedded browser, waits for the "Just a moment…" JS
challenge to pass, captures the cf_clearance cookie + the browser's User-Agent, then
hits the real listing API through curl_cffi with that cookie/UA and reports the HTTP
status. A **200** means the whole chain (browser-solve → cookie replay → matching TLS
+ UA) works, and the production `connect_pawchive` flow is sound.

Run it yourself (it needs a display — the webview window must actually render):

    .venv\\Scripts\\python.exe tools\\paw_cf_probe.py

A small window opens, flickers through the Cloudflare check, then closes on its own;
the verdict is printed to the console.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webview  # noqa: E402

from backend import pawchive_cf  # noqa: E402
from backend.pawchive_scraper import make_session, API, USING_IMPERSONATION  # noqa: E402

# A known creator with content (hooves-art / patreon).
TEST_LISTING = f"{API}/patreon/user/4231621"


def _worker(window):
    print(f"curl_cffi impersonation: {USING_IMPERSONATION}")
    res = pawchive_cf.capture_via_window(
        window, timeout=60, on_status=lambda m: print(f"[status] {m}")
    )
    print(f"capture: {res}")

    if res.get("ok"):
        ua = res.get("user_agent")
        print(f"captured UA: {ua}")
        s = make_session(cookies_path=res["cookies_path"], user_agent=ua)
        try:
            r = s.get(TEST_LISTING, timeout=(15, 60))
            server = r.headers.get("server")
            print(f"LISTING: HTTP {r.status_code}  server={server!r}  len={len(r.content)}")
            if r.status_code == 200:
                data = r.json()
                n = len(data) if isinstance(data, list) else "?"
                print(f"posts on first page: {n}")
                print("\nRESULT: PASS  ✅  (cookie replay cleared Cloudflare)")
            else:
                print("\nRESULT: FAIL  ❌  (cookie captured but API still blocked — "
                      "likely a UA/TLS mismatch; inspect body below)")
                print(r.text[:300])
        except Exception as e:
            print(f"request error: {type(e).__name__}: {e}")
            print("\nRESULT: FAIL  ❌")
    else:
        print("\nRESULT: FAIL  ❌  (challenge not solved / no cf_clearance)")

    try:
        window.destroy()
    except Exception:
        pass


def main():
    win = webview.create_window(
        "Connecting to pawchive… (probe)",
        url=pawchive_cf.CONNECT_URL,
        width=480,
        height=620,
    )
    webview.start(_worker, win)


if __name__ == "__main__":
    main()
