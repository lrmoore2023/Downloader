"""Prefetch / pre-warming efficacy experiment (manual, read-only).

Question prefetch must answer: when a video is in cold storage and slow to start,
does *touching it once* warm it so the next read is fast? If yes, we can probe
upcoming files ahead of the queue and hide the latency. If a cold file is still
slow on the second access, prefetch can't help.

Clean design (no cohort/temporal confound): for each sampled video, measure
time-to-first-byte of GET #1 (this is what a prefetch probe would pay), then
immediately GET #2 (what the real download would see). A prefetch win looks like
"cold files have a high t1 but a low t2."

Sampling spans the WHOLE catalog (stride sample across all posts) to actually
catch cold files, which the previous deep-end-only run missed.

Run on a QUIET network. Hits the live site, streams only the first chunk of each
video, writes nothing. Slow by nature — cold first-accesses can take minutes.

    python tests/experiment_prefetch.py
"""

import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import coomerfans_scraper as cf   # noqa: E402

CREATOR = "https://coomerfans.com/u/fansly/344368/LividPuppy"
TARGET_VIDEOS = 24
READ_TIMEOUT = 600          # cold-storage-aware (10 min)
COLD_THRESHOLD = 3.0        # t1 >= this (s) counts as a "cold" first access


def ttfb(session, url):
    """Seconds to first response byte (reads one chunk then closes)."""
    t0 = time.time()
    try:
        r = session.get(url, stream=True, timeout=(15, READ_TIMEOUT))
        if r.status_code >= 400:
            r.close()
            return None, f"HTTP {r.status_code}"
        first = next(r.iter_content(chunk_size=65536), b"")
        dt = time.time() - t0
        r.close()
        return dt, ("ok" if first else "empty")
    except Exception as e:
        return None, f"err {e}"


def collect_spread_videos(session, want):
    """Video URLs sampled evenly across the entire catalog (oldest..newest)."""
    print(f"Crawling {CREATOR} for post list...", flush=True)
    posts = list(cf.iter_post_urls(session, CREATOR))
    n = len(posts)
    stride = max(1, n // (want * 5))
    sampled = posts[::stride]
    print(f"  {n} posts; stride {stride} -> scanning {len(sampled)} spread across the catalog.", flush=True)
    vids = []
    for p in sampled:
        if len(vids) >= want:
            break
        try:
            info = cf.parse_post(session, p)
        except Exception:
            continue
        for m in info["media"]:
            if m["kind"] == "video":
                vids.append(m["url"])
                break   # one video per post keeps the sample spread out
    print(f"  collected {len(vids)} video URLs.", flush=True)
    return vids[:want]


def fmt(x):
    return "ERR" if x is None else f"{x:.2f}s"


def main():
    session = cf.make_session()
    vids = collect_spread_videos(session, TARGET_VIDEOS)
    if not vids:
        print("No videos found; aborting.")
        return

    print(f"\n=== first-access (t1) vs immediate second-access (t2), n={len(vids)} ===", flush=True)
    rows = []
    for i, u in enumerate(vids, 1):
        t1, n1 = ttfb(session, u)
        t2, n2 = ttfb(session, u)
        tag = "  <-- COLD" if (t1 is not None and t1 >= COLD_THRESHOLD) else ""
        print(f"  [{i:2}] t1={fmt(t1):>8}  t2={fmt(t2):>8}  ({n1}/{n2}){tag}", flush=True)
        if t1 is not None and t2 is not None:
            rows.append((t1, t2))

    print("\n================ RESULT ================")
    if not rows:
        print("No successful measurements.")
        return
    t1s = [a for a, _ in rows]
    t2s = [b for _, b in rows]
    print(f"All files (n={len(rows)}): median t1={statistics.median(t1s):.2f}s  "
          f"median t2={statistics.median(t2s):.2f}s  max t1={max(t1s):.2f}s")
    n_cold = sum(1 for a in t1s if a >= COLD_THRESHOLD)
    print(f"Cold first-accesses (t1 >= {COLD_THRESHOLD}s): {n_cold}/{len(rows)}")

    cold = [(a, b) for a, b in rows if a >= COLD_THRESHOLD]
    if cold:
        ct1 = statistics.median([a for a, _ in cold])
        ct2 = statistics.median([b for _, b in cold])
        print(f"  COLD files: median first-access {ct1:.2f}s -> median second-access {ct2:.2f}s")
        if ct1 > 0:
            print(f"  Second access is {ct1 - ct2:.2f}s faster ({(ct1 - ct2) / ct1 * 100:.0f}% reduction).")
        print("\nDECISION: build prefetch only if cold files' SECOND access is dramatically faster\n"
              "than the first (i.e. touching once warms it). If they stay slow, prefetch can't help.")
    else:
        print("No cold files in this sample — re-run (cold files appear intermittently), "
              "or coldness may be transient/load-driven rather than per-file.")


if __name__ == "__main__":
    main()
