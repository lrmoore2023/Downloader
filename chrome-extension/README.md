# PMV Prefix (Chrome extension)

Adds a small pill button next to the title of a **video page** on rule34video,
iwara, HMVMania, PMVHaven and pawchive. One click copies the filename prefix

    R34 - 2026.06.20 - 

(site code, the site's own upload date as `YYYY.MM.DD`, and a trailing space) so
you can paste it and type the rest of the name. Site codes: `R34`, `Iwara`,
`HMVMania`, `PMVHaven`, and for pawchive the origin service (`Patreon`, `Fanbox`).
It is the same convention the Downloader's PMV tab uses; the creator name is
deliberately left out.

Nothing is sent anywhere. The only network calls are to the site you are on
(iwara's and PMVHaven's own JSON APIs, to read the upload date on those
single-page apps). No data is stored.

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
