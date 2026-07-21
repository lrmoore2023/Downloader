"""Offline unit checks for the pawchive Cloudflare-clearance path (no network).

Covers the pieces the in-app auto-solve depends on: recognising Cloudflare's
challenge (vs a DDoS-Guard block or a normal reply), aligning curl_cffi's
impersonate profile + UA to the browser that earned the cookie, and the
cookies.txt writer/validator round-tripping through the session loader. Run:

    python tests/test_pawchive_cf.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import pawchive_scraper as ps
from backend import pawchive_cf as cf

_results = []


def check(name, ok, detail=""):
    _results.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class FakeResp:
    """Minimal stand-in for an HTTP response (curl_cffi / requests shaped)."""
    def __init__(self, status_code=403, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


CF_BODY = ('<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
           '<script>window._cf_chl_opt={};</script></head><body>'
           '<div class="challenge-platform"></div></body></html>')


def t_cf_detection():
    cf_resp = FakeResp(403, {"server": "cloudflare", "content-type": "text/html; charset=UTF-8"}, CF_BODY)
    check("cloudflare 403 challenge detected", ps.is_cloudflare_challenge(cf_resp) is True)

    cf_mit = FakeResp(403, {"server": "cloudflare", "cf-mitigated": "challenge",
                            "content-type": "text/html"}, "")
    check("cf-mitigated header detected", ps.is_cloudflare_challenge(cf_mit) is True)

    ddg = FakeResp(403, {"server": "ddos-guard", "content-type": "text/html"}, "blocked")
    check("ddos-guard 403 is NOT a cloudflare challenge", ps.is_cloudflare_challenge(ddg) is False)

    ok = FakeResp(200, {"server": "cloudflare", "content-type": "application/json"}, "[]")
    check("normal 200 is NOT a challenge", ps.is_cloudflare_challenge(ok) is False)

    # A genuine 404 behind cloudflare (JSON) must not be mistaken for a challenge.
    notfound = FakeResp(404, {"server": "cloudflare", "content-type": "application/json"}, "{}")
    check("cloudflare 404 json is NOT a challenge", ps.is_cloudflare_challenge(notfound) is False)

    check("None response is safe", ps.is_cloudflare_challenge(None) is False)


def t_impersonate_for_ua():
    # is_cloudflare_challenge lives in the scraper; so does the UA→profile mapper.
    ua131 = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    pick = ps._impersonate_for_ua(ua131)
    check("Chrome/131 maps to a chrome profile", isinstance(pick, str) and pick.startswith("chrome"),
          detail=str(pick))
    # A far-future major with no exact profile still resolves to some chrome target
    # (never crashes, never claims a newer build than exists in curl_cffi).
    ua999 = "Mozilla/5.0 Chrome/999.0.0.0 Safari/537.36"
    pick2 = ps._impersonate_for_ua(ua999)
    check("unknown-major UA still resolves", isinstance(pick2, str) and pick2.startswith("chrome"),
          detail=str(pick2))
    check("empty UA falls back to default", ps._impersonate_for_ua("") == ps._IMPERSONATE)


def t_cookies_roundtrip():
    cookies = [
        {"name": "cf_clearance", "value": "TOKEN123", "domain": ".pawchive.pw",
         "path": "/", "secure": True, "expires": ""},
        {"name": "__ddg1_", "value": "abc", "domain": "pawchive.pw", "path": "/",
         "secure": False, "expires": ""},
        {"name": "junk", "value": "nope", "domain": ".example.com", "path": "/",
         "secure": False, "expires": ""},
    ]
    path = os.path.join(tempfile.gettempdir(), "paw_cf_unit_cookies.txt")
    try:
        res = cf.write_cookies_txt(cookies, path)
        check("write kept only pawchive cookies", res["count"] == 2, detail=str(res))
        check("write reports cf_clearance", res["has_cf_clearance"] is True)
        check("validate_cookies_file accepts it", cf.validate_cookies_file(path)["valid"] is True)

        # Round-trips through the real session loader (curl_cffi or requests jar).
        s = ps.make_session(cookies_path=path, user_agent="Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36")
        val = None
        try:
            val = s.cookies.get("cf_clearance")
        except Exception:
            # requests jar fallback
            val = next((c.value for c in s.cookies if c.name == "cf_clearance"), None)
        check("cf_clearance loaded into session jar", val == "TOKEN123", detail=str(val))
        check("UA pinned on session", s.headers.get("User-Agent") == "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36")
    finally:
        if os.path.isfile(path):
            os.remove(path)


def t_validate_missing():
    check("missing file is invalid", cf.validate_cookies_file(
        os.path.join(tempfile.gettempdir(), "does_not_exist_paw.txt"))["valid"] is False)
    # A file without cf_clearance is invalid.
    path = os.path.join(tempfile.gettempdir(), "paw_cf_nocf.txt")
    try:
        cf.write_cookies_txt([{"name": "other", "value": "x", "domain": ".pawchive.pw",
                               "path": "/", "secure": False, "expires": ""}], path)
        check("file without cf_clearance is invalid", cf.validate_cookies_file(path)["valid"] is False)
    finally:
        if os.path.isfile(path):
            os.remove(path)


def t_has_cf_clearance_simplecookie():
    # Simulate pywebview get_cookies() output: a list of SimpleCookie objects.
    from http.cookies import SimpleCookie
    c = SimpleCookie()
    c["cf_clearance"] = "XYZ"
    c["cf_clearance"]["domain"] = ".pawchive.pw"
    c["cf_clearance"]["path"] = "/"
    c["cf_clearance"]["secure"] = True
    c["cf_clearance"]["expires"] = ""
    check("has_cf_clearance reads SimpleCookie", cf.has_cf_clearance([c]) is True)

    c2 = SimpleCookie()
    c2["session"] = "v"
    c2["session"]["domain"] = ".pawchive.pw"
    c2["session"]["path"] = "/"
    c2["session"]["secure"] = False
    c2["session"]["expires"] = ""
    check("has_cf_clearance false without cf cookie", cf.has_cf_clearance([c2]) is False)


def main():
    print("Running pawchive Cloudflare-clearance offline tests...")
    for t in (t_cf_detection, t_impersonate_for_ua, t_cookies_roundtrip,
              t_validate_missing, t_has_cf_clearance_simplecookie):
        try:
            t()
        except Exception as e:
            check(t.__name__, False, detail=f"exception: {e}")
    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed.")
    sys.exit(0 if passed == len(_results) else 1)


if __name__ == "__main__":
    main()
