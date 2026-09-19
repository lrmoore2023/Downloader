# PMV Prefix (Chrome extension)

Adds a small pill button next to the title of a **video page** on rule34video,
iwara, HMVMania, PMVHaven and pawchive. One click copies the filename prefix

    R34 - 2026.06.20 - 

(site code, the site's own upload date as `YYYY.MM.DD`, and a trailing space) so
you can paste it and type the rest of the name. Site codes: `R34`, `Iwara`,
`HMVMania`, `PMVHaven`, and for pawchive the origin service (`Patreon`, `Fanbox`).
It is the same convention the Downloader's PMV tab uses; the creator name is
deliberately left out.

When the uploader posted **more than one video that day** the prefix carries
that day's running count, oldest first:

    R34 - 2026.01.03 01 - 
    R34 - 2026.01.03 02 - 

Without it two files from the same day would be named identically and lose
their order. A day with a single video is left bare. The number matches the one
the PMV tab shows for the same video.

Nothing is sent anywhere. The only network calls are to the site you are on
(iwara's and PMVHaven's own JSON APIs, to read the upload date on those
single-page apps, and the uploader's listing for the day count). No data is
stored.

## Install (unpacked)

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. **Load unpacked** → pick this `chrome-extension` folder.
4. Open any video page. The button appears right after the title; if a site
   changes its layout and the title can't be found, it floats bottom-right.

After pulling an update to this folder, hit the ↻ reload icon on the
extension's card.

## Where the date comes from

| Site | Source on the video page |
|---|---|
| rule34video | structured data `uploadDate` |
| iwara | `api.iwara.tv/video/<id>` → `createdAt` |
| HMVMania | structured data `datePublished` |
| PMVHaven | `/api/videos/<id>` → `uploadDate` (falls back to the page's structured data) |
| pawchive | `<meta name="published">` |

The day is the UTC calendar day of that timestamp, matching the PMV tab. If a
page has no date the button turns amber and copies just `SITE - `.

## Where the day count comes from

Working out whether a video shares its day — and which of that day's uploads it
is — needs the uploader's listing, not just the page you are on, so it is
resolved **on the first click** (the button shows `…` for a moment) and then
remembered for that video. It is a handful of requests to the site itself:

| Site | Listing walked |
|---|---|
| rule34video | the member's video blocks, bisected on the video id, then the neighbouring video pages until the date changes — the site publishes no time of day, and its ids ascend with upload time |
| iwara | `api.iwara.tv/videos?user=…&sort=date` (50/page, bisected using `count`) |
| HMVMania | the author RSS feed `/author/<slug>/feed/?post_type=video` (10/page, bisected using its "Page N of M") |
| PMVHaven | `/api/videos?uploader=…` (100/page, bisected using `pagination.totalPages`) |
| pawchive | `/api/v1/<service>/user/<id>?o=…` (50/page, walked from the newest) |

If the listing can't be read (not logged in, a WAF, an offline API) the button
quietly copies the plain dated prefix.

**iwara and login-only videos.** Many iwara uploads are hidden from guests and
the API answers 404 for them anonymously. When you are logged in to iwara in
this browser, the extension reuses the site's own login token (it reads the
same `token` entry iwara keeps in the page's local storage and exchanges it for
an access token the way the site does), so those videos resolve too. Logged
out, only public videos get a date.
