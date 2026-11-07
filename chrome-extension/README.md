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

Nothing is sent anywhere unless you ask (see *Sharing the rule34video sign-in*
below). The only network calls are to the site you are on
(iwara's and PMVHaven's own JSON APIs, to read the upload date on those
single-page apps, and the uploader's listing for the day count). No data is
stored.

## Sharing the rule34video sign-in

The Downloader's PMV ▸ **Check for new** reads rule34video's subscriptions feed,
which needs a signed-in session — but rule34video allows one session per
account, so the app logging in would sign Chrome out (and the other way round).
Instead, click the extension's toolbar icon ▸ **Send rule34video sign-in** while
the app is open: it reads Chrome's rule34video.com cookies (that site only) and
posts them to the app on `127.0.0.1:47834`, nowhere else. Both then share one
session. Do it again if the app later reports the sign-in expired.

## Sending the Discord token

The Downloader's Discord links need the account's token. With discord.com open
(signed in) in a Chrome tab, click the toolbar icon ▸ **Send Discord token**.
The extension reads the token from the `Authorization` header that Discord's own
tab sends to `discord.com/api` (it watches those requests; it never changes or
blocks them). It keeps only the latest one, in session storage, which is cleared
when Chrome closes. It sends the token to `127.0.0.1:47834` only when you click
the button. The app checks it with `/users/@me`, then saves it as a *user* token
in Settings ▸ Discord. If the popup says no token was seen, reload the Discord
tab and try again. This replaces the app's token with **the account Chrome is
signed in to**, so if the app should use a secondary account, sign that account
in to Chrome's Discord tab before you click.

## Sending the X/Twitter sign-in

Signed in at x.com in Chrome, click the toolbar icon ▸ **Send X sign-in**. The
extension reads Chrome's x.com / twitter.com cookies (those sites only) and posts
them to `127.0.0.1:47834`. The app writes them to `.twitter_cookies.txt`
(gitignored) and sets that file as Settings ▸ Twitter's cookie file, so there's no
cookies.txt to export and browse to. It needs `auth_token` and `ct0`. The app
shares Chrome's session, so it doesn't sign Chrome out. If a fetch reports
"AuthRequired" later, click it again.

## Sending the Bluesky sign-in

Optional: the app downloads public Bluesky profiles without signing in. Signed
in at bsky.app in a Chrome tab, click the toolbar icon ▸ **Send Bluesky sign-in**.
Bluesky has no login cookies (bsky.app keeps its session in localStorage), so the
extension reads that tab's current session (its short-lived access token, never
the refresh token) and posts it to `127.0.0.1:47834`. The app uses it once to
create its own app password named **Downloader** (replacing an older one of that
name) and saves it as Settings ▸ Bluesky. Chrome stays signed in. Revoke it any
time at bsky.app ▸ Settings ▸ Privacy and security ▸ App passwords. If the saved
password still works, clicking again changes nothing.

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
