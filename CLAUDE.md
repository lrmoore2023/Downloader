# Downloader

A local pywebview desktop app (Python backend + vanilla JS frontend) that archives
media from a set of sites per "creator". Runs on Windows, stores media on a NAS.

## Run / test

```bash
start.bat                                   # builds .venv on first run, then launches
.venv/Scripts/python.exe -m pytest tests/ -q
```

**Not every suite is pytest-collected.** Four files are standalone check-style
scripts with their own `main`; `pytest tests/` collects **zero** tests from them
and will happily report all-green while they are broken. Always run them too:

```bash
.venv/Scripts/python.exe tests/test_pawchive.py               # 93 checks
.venv/Scripts/python.exe tests/test_pawchive_cf.py            # 18 checks
.venv/Scripts/python.exe tests/test_year_range.py             # 40 checks
.venv/Scripts/python.exe tests/test_coomerfans_integrity.py   # 18 checks
```

This is not hypothetical: a stale `parse_post` stub in the integrity suite shipped
broken precisely because only `pytest` was run. `pytest` is also not in
`requirements.txt` — install it separately.

## Layout

- `main.py` — pywebview entry point; builds `Api` and loads `frontend/index.html`.
- `backend/api.py` — the JS-facing API surface (every `pywebview.api.*` call) **and**
  the state store. Large; it is the main integration seam.
- `backend/creator_runner.py` — dispatcher. Maps a creator's links to per-platform
  runners and owns the fetch-prefs / track-only logic.
- `backend/<platform>_{scraper,runner,archive}.py` — one engine per site
  (pawchive, coomerfans, twitter, derpibooru, discord), plus the Albums tab
  (`album_runner`, `gofile_downloader`, `cyberdrop_dl_runner`, `filester_*`).
- `backend/rate_limit.py` — `AdaptiveThrottle`, the shared AIMD request pacer.
- `frontend/js/` — `app.js` (shell/stats), `creators.js` (creator panel + URL panel),
  `overlays.js` (Configure dialogs), `album.js`, `dupes.js`.

## State model

- `app_state.json` at the repo root is the **single** creator index + settings.
  Gitignored (it holds NAS paths and personal history).
- `app_state.backups/` holds rolling snapshots, written on every creator-index
  change and pruned to 40. `recover_from_backup()` restores from these.
  A single file must never be the point of failure — this exists because a bad
  read once silently wiped all 78 creators.
- **Archive DBs live in `archive_dir`** (a NAS folder), *not* in the destination.
  They are the per-creator "already downloaded" record: one SQLite file per
  creator. Losing them means re-downloading everything; losing `app_state.json`
  only loses the creator list. Keep that distinction in mind.
- A track-only creator has a blank destination, a generated `tc_` id, and keeps
  its manifests under `<archive_dir>/tracked/<id>/`.

## Standing rules

1. **Verify empirically.** For download bugs, write a read-only probe script using
   the app's own `make_session()` (so curl_cffi impersonation matches) and
   characterise the live site *before* editing. Plausible-but-wrong guesses have
   cost real time here. Don't assume a 403 is what it looks like.
2. **Never skip a file that can be downloaded.** Large, slow, needs retries or
   resume — still download it. Only genuinely absent files may be recorded as
   errors. Fixes should be general (all creators, all file types: images, videos,
   rars, zips), not narrowed to the one reported failure.
3. **Don't let one file be a single point of failure.** Back up on every change.

## Site gotchas

- **pawchive** — the API is behind a Cloudflare JS challenge; only `file.pawchive.pw`
  is not. The in-app solve is Settings ▸ Pawchive ▸ Connect, which captures
  `cf_clearance` + the browser UA (`backend/pawchive_cf.py`). `cf_clearance` is
  IP+UA-bound and expires. A CF 403 is classified `needs_cf_auth`, not "not retriable".
- **coomerfans** — the HTML host (not the media CDN) runs a scoring bot-guard:
  `X-Bg-Score` / `X-Bg-Decision` on every response, 503 challenge past ~1.0. The
  score is a slowly-decaying *budget*, not a rate gate, so there is no safe fixed
  rate — `AdaptiveThrottle.on_score` widens the interval as the score climbs and
  idles near the top. The User-Agent was never the cause. The CDN is unguarded, so
  don't throttle downloads.
- Availability on pawchive is `preview_state` (`scraped` = present, `pending` =
  not imported yet), **not** `has_full`.

## Git

Commit and push as work completes — do not wait to be asked. After each
self-contained change that is verified (tests run, including the standalone
suites above), stage it, write a descriptive commit message explaining *why*, and
`git push`. Prefer several focused commits over one large one.

**Do not add any Claude/AI attribution to commits** — no `Co-Authored-By: Claude`
trailer, no "generated with" line, nothing in the message body. Commits are
authored as `lrmoore2023 <lrmoore2023@users.noreply.github.com>`.

`main` is the trunk and is what to work on. Never commit `app_state.json`,
`*_cookies.txt`, or anything else in `.gitignore` — it is personal data.
