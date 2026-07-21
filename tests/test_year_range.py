"""Offline unit checks for per-creator Fetch-Latest year-range scoping (no network).

Covers the pure filter semantics shared by the pawchive + coomerfans crawls
(norm_year_range / year_in_range / year_scan_decision), the property that a single
year behaves exactly like the degenerate range (y, y) — so the existing "Download
Year" path is unchanged — and the API's stored-range cleaner. Run:

    python tests/test_year_range.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.coomerfans_scraper import norm_year_range as N, year_in_range as R, year_scan_decision as D
from backend.api import Api

_results = []


def check(name, ok, detail=""):
    _results.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def t_norm():
    check("none/none -> None", N(None, None) is None)
    check("single year -> (y,y)", N(2024, None) == (2024, 2024))
    check("string year coerced", N("2024", None) == (2024, 2024))
    check("dict start/end", N(None, {"start": 2024, "end": 2026}) == (2024, 2026))
    check("dict reversed reordered", N(None, {"start": 2026, "end": 2024}) == (2024, 2026))
    check("open lower", N(None, {"start": None, "end": 2020}) == (None, 2020))
    check("open upper", N(None, {"start": 2024, "end": None}) == (2024, None))
    check("blank dict -> None", N(None, {"start": "", "end": ""}) is None)
    check("tuple form", N(None, (2024, 2026)) == (2024, 2026))
    check("range wins over single year", N(2010, {"start": 2024, "end": 2026}) == (2024, 2026))
    check("garbage bounds -> None", N(None, {"start": "abc", "end": None}) is None)


def t_in_range():
    check("in range", R(2025, (2024, 2026)) is True)
    check("below range", R(2023, (2024, 2026)) is False)
    check("above range", R(2027, (2024, 2026)) is False)
    check("inclusive lower", R(2024, (2024, 2026)) is True)
    check("inclusive upper", R(2026, (2024, 2026)) is True)
    check("undated excluded from bounded range", R(None, (2024, 2026)) is False)
    check("undated allowed when no range", R(None, None) is True)
    check("open lower includes old", R(1999, (None, 2020)) is True)
    check("open lower excludes newer", R(2021, (None, 2020)) is False)
    check("open upper includes new", R(2030, (2024, None)) is True)
    check("open upper excludes older", R(2020, (2024, None)) is False)


def t_single_year_equivalence():
    # A single year MUST behave identically to the range (y, y) for every candidate —
    # this is what guarantees 'Download Year' is unchanged.
    rng = N(2024, None)              # (2024, 2024)
    ok = all(R(y, rng) == (y == 2024) for y in range(2020, 2029))
    check("single-year == (y,y) membership", ok)
    check("single-year (y,y) undated skipped", R(None, rng) is False)


def t_scan_decision():
    rng = (2024, 2026)
    check("newer than range -> skip (keep paging down)", D(2028, rng) == "skip")
    check("in range -> take", D(2025, rng) == "take")
    check("below range -> stop (early-stop)", D(2023, rng) == "stop")
    check("undated -> skip, never stop", D(None, rng) == "skip")
    check("no range -> take", D(2025, None) == "take")
    # Open lower bound: nothing is ever 'below', so the crawl must never early-stop.
    open_lo = (None, 2020)
    check("open lower never stops", all(D(y, open_lo) != "stop" for y in range(1990, 2030)))
    check("open lower: newer skipped", D(2021, open_lo) == "skip")
    check("open lower: older taken", D(2000, open_lo) == "take")
    # Single-year early-stop: years before it stop, the year itself takes, after skips.
    yr = (2024, 2024)
    check("single-year below -> stop", D(2023, yr) == "stop")
    check("single-year hit -> take", D(2024, yr) == "take")
    check("single-year above -> skip", D(2025, yr) == "skip")


def t_api_clean_year_range():
    a = Api()
    check("clean orders bounds", a._clean_year_range({"start": "2026", "end": "2024"}) == {"start": 2024, "end": 2026})
    check("clean open lower", a._clean_year_range({"start": "", "end": 2020}) == {"start": None, "end": 2020})
    check("clean blank -> None", a._clean_year_range({"start": "", "end": ""}) is None)
    check("clean non-dict -> None", a._clean_year_range("2024") is None)
    check("clean None -> None", a._clean_year_range(None) is None)


def main():
    print("Running Fetch-Latest year-range offline tests...")
    for t in (t_norm, t_in_range, t_single_year_equivalence, t_scan_decision, t_api_clean_year_range):
        try:
            t()
        except Exception as e:
            check(t.__name__, False, detail=f"exception: {e}")
    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed.")
    sys.exit(0 if passed == len(_results) else 1)


if __name__ == "__main__":
    main()
