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
.venv/Scripts/python.exe tests/test_pawchive.py               # 134 checks
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
- **SimpCity + tracked albums** (Creator tab, **Real creators only** — destination
  under `P:\Real\…`, enforced in `resolve_link`/`save_creator` via
  `_is_real_dest`): `backend/simpcity_scraper.py` (thread URL/page parsing + link
  classification), `simpcity_archive.py` (per-thread DB), `simpcity_runner.py`
  (walk + embedded-media download), `album_track_runner.py` (bunkr/gofile/
  cyberdrop/pixeldrain/filester/turbo/goonbox albums as creator links, plus the
  review list's one-off downloads), host resolvers `turbo_host.py` /
  `goonbox_host.py`, and `http_download.py` (shared resumable download verified by
  size + MD5/ffprobe). UI: `frontend/js/simpcity.js` (the "SimpCity & albums"
  panel under the links panel) + Settings ▸ SimpCity sign-in. Link platforms
  `simpcity` and `album` (field `site`). Layout: `<dest>/Simpcity/<year>/` +
  `Simpcity/Images/<year>/` for thread media (`YYYY.MM.DD - Simpcity - <name>`,
  post date in local time), `<dest>/Simpcity/<Site>/<year>/` for album files
  (`YYYY.MM.DD - Bunkr - <name>`, the host's upload date in UTC, falling back to
  the post date the link came from).
- **PMV tab** (metadata only, never downloads): `backend/pmv_tracker.py` (manifests +
  the two numbering rules), `backend/pmv_runner.py` (the fetch job),
  `backend/r34video_scraper.py`, `backend/iwara_scraper.py`,
  `backend/hmvmania_scraper.py`, `backend/pmvhaven_scraper.py` (pawchive reuses
  `pawchive_scraper`), `frontend/js/pmv.js`. Api methods are the `*_pmv_*` group.
  `chrome-extension/` is a standalone MV3 extension (load unpacked) that copies
  `SITE - YYYY.MM.DD - ` from a video page, resolving the same-day count from the
  uploader's listing on click; `node tools/pmvx_check.js` runs its offline checks
  (pass a dir of saved video pages to also check the date extractors).
- **PMV ▸ Latest / Check for new**: `backend/pmv_feeds.py` (each site's *followed*
  feed), `backend/pmv_check.py` (the check job: feeds → only the changed links
  through the unchanged `PmvRunner`), `frontend/js/pmv_latest.js` (the unified
  new-video list). Feeds are only hints — every tracked link is either covered by
  a readable feed or fetched directly (HMVMania, not followed, not signed in, feed
  error, walk can't reach the cutoff). Feed state (`since`, per-site last good
  check, seen ids, pending, untracked) is `<pmv root>/_feed_state.json`; nothing
  before `since` (the day it was switched on) is backfilled. Sign-ins:
  `backend/site_login.py` (pawchive window), a PMVHaven API key, and for
  rule34video — **one session per account**, so the app can't log in itself —
  the extension popup hands Chrome's session to `backend/ext_bridge.py`
  (127.0.0.1:47834, `chrome-extension://` Origin only). Probe feeds read-only with
  `tools/pmv_feed_probe.py`.
- `frontend/js/` — `app.js` (shell/stats), `creators.js` (creator panel + URL panel +
  `switchView`), `overlays.js` (Configure/Settings dialogs), `album.js`, `dupes.js`,
  `pmv.js`, `pmv_latest.js`. The PMV tab is the first and default view (it opens on
  Latest); Creator is second.
- **Themes / design tokens**: `css/tokens.css` (spacing, type, motion, shape defaults,
  and the list of semantic tokens every theme must define) → `css/themes/<id>.css`
  (`brutalist` = default, monochrome; `studio` = the original look — both light +
  dark; `neon` dark-only, `nursery` light-only — keyed off `data-theme` /
  `data-mode` on `<html>`; single-mode themes lock the Mode control) → `main.css` / `layout.css` /
  `components.css`, which use tokens only — no colour literals. `js/theme.js` holds
  the theme registry + Settings ▸ Appearance picker; the choice persists as
  `ui_theme` / `ui_mode` / `ui_motion` in `app_state.json`. pywebview runs private
  (no lasting localStorage), so `main.py` writes the saved choice to
  `frontend/js/theme-pref.js` (gitignored) before launch and sets the window
  background from it — no flash of the default theme. New theme = one CSS file,
  a `<link>` in `index.html`, a `THEMES` entry, and a `THEME_BACKGROUNDS` row
  (+ `THEME_FIXED_MODE` if single-mode). Theme display fonts (Unbounded, Fredoka,
  Nunito — SIL OFL, latin subset) live in `frontend/fonts/`.
  `js/ui.js` owns dialog open/close (focus trap + return), busy buttons
  (`UI.busy`) and tab-strip keyboard handling — use it rather than toggling
  `.visible` by hand.

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
  (append-only, a deleted video keeps its slot), pawchive is **chronological**
  (recomputed by date every walk because it back-fills old posts; posts can be
  marked "not counted" / `excluded`). **Prefixes are dated and site-first on every
  site**: `Name - R34 - 2026.06.20 - ` (pawchive's code = origin service, Patreon /
  Fanbox, never "Pawchive"); the number is only a rank and only stands in for a
  video whose date isn't known yet. Two or more uploads on the same day take
  that day's running count (`… - 2026.06.20 02 - `, oldest first) so their order
  survives; the count includes every dated item, gone or excluded, so it doesn't
  move and the extension derives the same number. `iwara_email/iwara_password/iwara_token` are credentials — never in
  the NAS payload. Probe numbering read-only with `tools/pmv_probe.py <url>`.
- **SimpCity thread DBs** are `<archive_dir>/simpcity_<thread_id>.db` (+ `.errors.db`):
  walk state, every post, every embedded file (`media`, key `<host>:<ref>`) and
  every external link with the user's review decision (`pending/dismissed/
  downloaded/tracked`) and liveness. **Tracked albums** each get
  `<archive_dir>/album_<site>_<id>.db` (files fetched by host file id, title,
  `alive`, `dead_since`, `unreachable_since`/`fail_streak`). Both are keyed only
  by the site's own ids, so re-adding never re-downloads. cyberdrop-dl's history
  for tracked albums lives in `<archive_dir>/_engines/cyberdrop/`. SimpCity
  cookies + UA (`simpcity_cookies_path`, `simpcity_user_agent`) are credentials.
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
  URL is resolved to the id from the page's `__NUXT_DATA__` payload. Documented
  API at `/api/openapi.json`; personal keys (`Authorization: Bearer pmvh_…`) unlock
  `/api/notifications` (type `new_video`: `sender._id` + `video`; `PUT` with
  `notificationIds` marks read) and `/api/users/<me>/subscriptions` (`data[].id`;
  me = `/api/user/profile` → `data.userId`).
- **Followed feeds** (PMV ▸ Check for new) — iwara: `/videos?subscribed=true&sort=date`
  (token; full history, newest first), following `/user/<me>/following`.
  rule34video: `/my/subscriptions/` async block `list_videos_videos_from_my_subscriptions`
  (`from=NN`, 24/page, 404 past the end); cards have **no uploader and only a
  relative date**, so a never-seen video's page is read once for its uploader;
  followed members = block `list_members_subscriptions_my_subscriptions`.
  pawchive: `/api/v1/account/favorites?type=artist` (signed in; every favourite in
  one response) — `updated` = new content, `last_imported` = last import run; the
  public `/api/v1/<svc>/user/<id>/profile` carries `updated` too.
  pmvhaven: notifications are **not** a coverage signal — measured 2026-10-14,
  27 of 34 uploads from followed creators never produced one, and clearing
  deletes them. Each tracked link is instead head-checked
  (`/api/videos?uploader=<id>&limit=3&sort=-uploadDate`, public, no key) and
  fetched only if an id is unknown; uploads only ever go on top, so that is
  exact. Notifications just surface untracked uploaders. The API allows 120
  requests/window (`X-RateLimit-Remaining`, `X-RateLimit-Reset` in epoch ms);
  `pmvhaven_scraper._get` waits out the window instead of spending the last few.
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
- **simpcity.cr** — XenForo 2.3 behind DDoS-Guard. **Threads are members-only**: a
  guest gets 403 "Access Forbidden" on every `/threads/` URL (the index is public),
  so everything runs on the Settings ▸ SimpCity sign-in (login form has a Turnstile
  captcha → in-app window). Signed-in is read from `<html data-logged-in="true">`;
  the cookie prefix (`data-cookie-prefix`) is rotated, so never key on cookie names.
  DDoS-Guard cookies are UA-bound → the UA is kept with the jar. cyberdrop-dl
  paces it at 1 request / 20 s; we measured 11 pages at a 4 s pace with no pushback;
  pages go through `AdaptiveThrottle`. Probe a thread read-only with
  `tools/simpcity_probe.py <url> --pages 1,11 [--raw]`. Real post markup (measured
  on a full 11-page thread): turbo videos are `iframe.saint-iframe` `/embed/<id>`;
  goonbox images are `img.bbImage` thumbs inside `<a href=goonbox.cr/img/<id>>`, older
  ones bare `simpN.cuckcapital.cr/images3/x.md.png` (drop `.md.` → original);
  `cyberdrop.cr/e/<id>` iframes (api.cyberdrop.cr `file/info` + `file/auth`);
  `pbs.twimg.com/media/<id>?format=` images (`name=orig`); redgifs `/ifr/<id>`
  (temp-token API, CDN needs a redgifs Referer); old posts carry unrendered
  `[MEDIA=saint_vid]<id>[/MEDIA]` in hrefs — saint ids resolve on turbo. External
  links are `/redirect/?to=<base64>&m=b64`; link previews are
  `.bbCodeBlock--unfurl[data-url]` whose images are site logos, not content.
- **turbo.cr** (SimpCity's video host, ex-saint) — the altcha proof-of-work only
  gates the page button: `GET /api/sign?v=<id>` returns a signed, Range-capable CDN
  URL anonymously. The `/d/<id>` page prints the exact size, MD5 and upload date.
- **goonbox.cr** (SimpCity's image host, ex-jpg5/6) — pages are a Cloudflare
  challenge, the Laravel API isn't: `GET /sanctum/csrf-cookie`, then
  `/api/images/<id>` with `X-XSRF-TOKEN` → `original_url` on `simpN.cuckcapital.cr`.
  Posts embed the thumbnail inside `<a href="goonbox.cr/img/<id>">`.
- **bunkr** — native engine `backend/bunkr_host.py` (cyberdrop-dl no longer used for
  bunkr). Measured 2026-10-18: `/a/<id>?advanced=1` embeds `window.albumFiles` (id,
  original name, exact size, upload time UTC); a lone `/f/` page has the dl id
  (`dl.bunkr.cr/file/<id>`) but no date — the CDN's `Last-Modified` equals the upload
  time. `POST dl.bunkr.cr/api/_001_v2 {"id": "<id>"}` (**string** id; an int is "400
  Invalid JSON payload") + `glb-apisign.cdn.cr/sign?path=` give the signed CDN URL.
  **A CDN node serves two downloads per client; the third gets an instant 503** — the
  engine caps per node (`NodeLimiter`) and waits out 503s. Cold storage: files served
  from `psu.scdn.st` take 1–11 s to the first byte and their first full transfer can be
  very slow (68 MB took 146 s cold, 9 s warm); the engine warms each file, waits
  patiently, and re-tries files that won't start in later passes (`COLD_ROUNDS`). A
  removed album/file is a clean 404 in 0.1 s; during outages (2026-10-17) requests time
  out/5xx instead — never "gone". cyberdrop-dl's state must stay off the NAS: its
  SQLite schema setup there took 41 s and its log writer broke, silencing progress.
- **gofile** — the website-token salt rotates; when every listing returns
  `error-notPremium` it's the salt (check the bundled gallery-dl/cyberdrop-dl
  `gofile.py`). Account creation is rate-limited (429), so the guest token is
  persisted in `.gofile_token.json`.
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
