"""Read-only live probe for the PMV tracker scrapers.

Walks a creator's listing on rule34video / iwara / pawchive with the app's own
sessions and throttles, folds it into an in-memory manifest, and prints the
catalogue numbering — WITHOUT touching any real manifest or downloading a byte.
Use it to confirm the numbering matches your existing filenames before (or
after) adding a creator to the PMV tab.

    .venv/Scripts/python.exe tools/pmv_probe.py https://rule34video.com/members/2472537/
    .venv/Scripts/python.exe tools/pmv_probe.py https://www.iwara.tv/profile/user1833289/videos
    .venv/Scripts/python.exe tools/pmv_probe.py https://pawchive.pw/patreon/user/147694273 --name Mightty

Options: --name <prefix name>  --code <site code>  --details (r34: also fetch
each video page for date/quality; slow: one request per video)
"""
import argparse
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import pmv_tracker as pt
from backend.pmv_runner import PmvRunner


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--name", default="Creator")
    ap.add_argument("--code", default="")
    ap.add_argument("--details", action="store_true")
    args = ap.parse_args()

    from backend.api import Api
    api = Api.__new__(Api)
    api._state_lock = threading.Lock()
    info = api.resolve_pmv_link(args.url)
    if not info.get("valid"):
        print("Unrecognized URL")
        return 2
    info.pop("valid", None)
    info.pop("note", None)
    if args.code:
        info["site_code"] = args.code
    print(f"Resolved: {info}")

    state = api.load_state()
    events = []
    runner = PmvRunner(manifest_root_for=lambda cid: None, state=state,
                       on_progress=lambda d: print(f"  [{d['type']}] {d['message']}"))
    if not args.details:
        # Skip the per-video detail pass unless asked; numbering doesn't need it
        # (it comes from listing position), and it is one request per video.
        import backend.pmv_runner as prm
        prm.r34.fetch_video_detail = lambda *a, **k: {"date": None, "quality": None, "duration": None}
    job = {"creator": {"id": "probe", "name": args.name}, "link": info, "mode": "full"}
    manifest = pt.new_manifest(info["platform"], info.get("user_id"))
    fetcher = {"rule34video": runner._fetch_r34, "iwara": runner._fetch_iwara,
               "pawchive": runner._fetch_pawchive}[info["platform"]]
    fetched, full, complete = fetcher(job, manifest)
    res = pt.merge(manifest, fetched, full=full, complete=complete)
    print(f"\n{len(fetched)} videos, complete={complete}, merge={res}\n")
    width = pt.number_width(manifest["items"])
    code = info.get("site_code") or pt.default_site_code(info["platform"])
    for it in sorted(manifest["items"].values(), key=lambda i: i.get("number") or 0):
        q = f"  {it['quality']}p" if it.get("quality") else ""
        d = f"  {it['date'][:10]}" if it.get("date") else ""
        print(f"{pt.format_prefix(args.name, code, it['number'], width)}{it['title']}{d}{q}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
