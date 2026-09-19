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
- **PMV tab** (metadata only, never downloads): `backend/pmv_tracker.py` (manifests +
  the two numbering rules), `backend/pmv_runner.py` (the fetch job),
  `backend/r34video_scraper.py`, `backend/iwara_scraper.py`,
  `backend/hmvmania_scraper.py`, `backend/pmvhaven_scraper.py` (pawchive reuses
  `pawchive_scraper`), `frontend/js/pmv.js`. Api methods are the `*_pmv_*` group.
- `frontend/js/` — `app.js` (shell/stats), `creators.js` (creator panel + URL panel +
  `switchView`), `overlays.js` (Configure/Settings dialogs), `album.js`, `dupes.js`,
  `pmv.js`. The PMV tab is the first and default view; Creator is second.

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
- **PMV creators** are a separate registry (`pmv_creators`, generated `pc_` ids,
  snapshotted + NAS-mirrored like `creators`). Each site link owns one JSON
  manifest at `<archive_dir>/pmv/<pc_id>/<platform>_<user_id>.json`
  (`pawchive_<service>_<id>` for pawchive; `<app>/.pmv/` when no archive dir) holding
  every listed video, its catalogue number and the ✓/✗ status. Numbers are the
  user's filename contract: rule34video/iwara/hmvmania/pmvhaven are **locked**
  (append-only, a deleted video keeps its slot; prefix `Name - R34 - 02 - `, always
  2-digit padding, 103 prints as 103), pawchive is **chronological** (recomputed by
  date every walk because it back-fills old posts) and its prefix is **dated**
  (`Name - 2026.05.04 - Patreon - `; site code = origin service, never "Pawchive");
  pawchive posts can be marked "not counted" (`excluded`). `iwara_email/iwara_password/iwara_token` are credentials — never in
  the NAS payload. Probe numbering read-only with `tools/pmv_probe.py <url>`.
- **The creator index is also mirrored off-machine** to
  `<archive_dir>/.state-backups/` on every index change (last 20, written on a
  daemon thread so a slow NAS never stalls a save). `app_state.json` used to live
  only on the app's own drive, and when that drive died the index went with it
  while every archive DB survived. `recover_from_backup()` reads these too, so a
  fresh install with an empty index can rebuild from the NAS alone. These
  snapshots carry **no credentials** — the archive dir is a shared network share.

### Re-adding a creator never re-downloads

Archive DB paths are derived from `archive_dir` + platform + service + user_id
(`cf_archive_path` / `pawchive_archive_path` in `creator_runner.py`) — never from
the creator id, name, or when the record was created. So pointing a brand-new
creator record at an existing folder with the same link resolves to the same DB
and the same `.errors.db` sidecar: downloads are skipped and dismissed errors
stay dismissed. Checked-off external links live in `_pawchive_links.json` in the
**destination folder**, so they survive too as long as the folder is the same.

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

- **rule34video** (PMV tab) — KVS engine. Listing pages are async blocks
  (`?mode=async&function=get_block&block_id=list_videos_uploaded_videos&from_videos=NN`,
  8 per page, newest first) and a page past the end is a **404**, which is the
  clean end signal, not an error. Exact date + best quality (`'4k'`) are only on
  the video page, fetched once per new video.
- **iwara** (PMV tab) — public JSON API (`api.iwara.tv/profile/<user>`,
  `/videos?user=<uuid>&sort=date&page=N&limit=50`), 0-based pages, stop at
  `count`. Login is optional: `POST /user/login` → 3-week user JWT, `POST
  /user/token` → 1-hour access token; on 401 the walk degrades to anonymous.
- **hmvmania** (PMV tab) — WordPress; `/wp-json`, `?rest_route=` and admin-ajax
  are 403'd by a Cloudflare WAF rule for non-browsers, but the author **RSS feed**
  is not: `/author/<slug>/feed/?post_type=video[&paged=N]` (10/page, page 1 has no
  `paged`, past the end = 404 with a "Page not found" channel; the channel title
  carries "Page N of M"). Titles come prefixed "[Author] " — stripped.
- **pmvhaven** (PMV tab) — Nuxt; public JSON: `/api/users/<24-hex id>` and
  `/api/videos?uploader=<id>&limit=100&page=N` (1-based, `pagination.hasNext`).
  Video page = `/video/<slugified title>_<oldId or _id>`. A `/profile/<username>`
  URL is resolved to the id from the page's `__NUXT_DATA__` payload.
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

**The GitHub repo is public.** Never push the archive DBs, the creator index, or
anything else naming a creator or a downloaded file. The archive dir records
every tracked account and every filename downloaded; publishing it would be
permanent and world-readable. Durable copies of that data belong on the NAS (see
the mirror above), not in git. Code only.
