"""Readers for each site's "what's new from people I follow" feed (PMV ▸ Check for new).

A feed is only ever a *hint* about which tracked links to fetch; the manifests
stay the source of truth (see pmv_check). Each source yields newest-first pages
of normalised items:

    {platform, video_id, uploader_id, uploader_name, title, url,
     date,        # best known upload/notification time (ISO, UTC) or None
     newest,      # the latest time the item can possibly be from — the stop rule
                  # compares this against the cutoff, so a coarse date ("2 days
                  # ago") can never stop a walk early
     ref}         # PMVHaven notification id (for mark-read), else None

Verified live 2026-10-02:
* iwara — `GET api.iwara.tv/videos?subscribed=true&sort=date&page=N&limit=50`
  (Bearer access token), newest-first by createdAt, back to the account's first
  follow; `count` is unreliable, a short page ends it. Following:
  `/user/<me>/following` (me from `GET /user`).
* PMVHaven — personal API key (`Authorization: Bearer pmvh_…`).
  `GET /api/notifications?page&limit≤100` → `data.notifications` (or top-level),
  type `new_video` with `sender._id` (uploader), `video` (_id), `createdAt`.
  `PUT /api/notifications {notificationIds}` marks read. Following:
  `/api/users/<me>/subscriptions` → `data[].id` (me = `/api/user/profile` →
  `data.userId`).
* rule34video — signed-in session shared from Chrome (one session per account).
  `/my/subscriptions/?mode=async&function=get_block&block_id=
  list_videos_videos_from_my_subscriptions&sort_by=&from=N`, 24 per page, past
  the end = 404. Cards carry no uploader and only a relative date ("22 hours
  ago"); the uploader comes from the video page. Followed members: block
  `list_members_subscriptions_my_subscriptions`, `from_my_subscriptions=N`.
* pawchive — `GET /api/v1/account/favorites?type=artist` (signed in) lists every
  favourite in one response with `updated` (new content) and `last_imported`;
  the public `/api/v1/<service>/user/<id>/profile` carries `updated` too.
"""

import re
from datetime import datetime, timedelta, timezone

import requests

from backend import iwara_scraper as iw
from backend import pmvhaven_scraper as pmvh
from backend import r34video_scraper as r34


class FeedAuthError(Exception):
    """The saved sign-in was refused — the UI says 'Sign in again'."""


class FeedError(Exception):
    pass


def _item(platform, video_id, uploader_id="", uploader_name="", title="", url="",
          date=None, newest=None, ref=None):
    return {"platform": platform, "video_id": str(video_id or ""),
            "uploader_id": str(uploader_id or ""), "uploader_name": uploader_name or "",
            "title": title or "", "url": url or "", "date": date,
            "newest": newest if newest is not None else date, "ref": ref}


def _cancelled(should_cancel):
    return bool(should_cancel and should_cancel())


# ── iwara ───────────────────────────────────────────────────────────

class IwaraFeed:
    platform = "iwara"
    history_complete = True  # subscriptions go back to the first follow
    cheap_depth = True  # uploader is in the feed: reading back costs ~nothing
    PAGE = 50

    def __init__(self, session, auth, throttle=None, should_cancel=None):
        self.s, self.auth, self.t, self.cancel = session, auth, throttle, should_cancel
        self._token = None

    def available(self):
        if not (self.auth and self.auth.enabled):
            return False, "No iwara login in Settings"
        self._token = self.auth.access_token()
        if not self._token:
            return False, f"iwara login failed ({self.auth.message or 'no token'})"
        return True, ""

    def _get(self, url, params=None):
        try:
            return iw.fetch_json(self.s, url, params=params, token=self._token,
                                 throttle=self.t, should_cancel=self.cancel)
        except iw.AuthError as e:
            raise FeedAuthError(f"iwara refused the login ({e})")
        except iw.SiteError as e:
            raise FeedError(f"iwara: {e}")

    def pages(self):
        page = 0
        while True:
            if _cancelled(self.cancel):
                return
            data = self._get(f"{iw.API}/videos", {"subscribed": "true", "sort": "date",
                                                  "page": page, "limit": self.PAGE})
            raws = (data or {}).get("results") or []
            if not raws:
                return
            out = []
            for raw in raws:
                v = iw.parse_video(raw)
                u = raw.get("user") or {}
                out.append(_item("iwara", v["id"], u.get("id"), u.get("username") or u.get("name"),
                                 v["title"], v["url"], v["date"]))
            yield out
            if len(raws) < self.PAGE:
                return
            page += 1

    def following(self):
        me = ((self._get(f"{iw.API}/user") or {}).get("user") or {}).get("id")
        if not me:
            raise FeedError("iwara: couldn't read own user id")
        ids, page = set(), 0
        while page < 200:
            data = self._get(f"{iw.API}/user/{me}/following", {"page": page, "limit": 50})
            res = (data or {}).get("results") or []
            for r in res:
                u = r.get("user") or {}
                if u.get("id"):
                    ids.add(u["id"].lower())
            if len(res) < 50:
                break
            page += 1
        return ids


# ── PMVHaven ────────────────────────────────────────────────────────

class PmvhavenFeed:
    platform = "pmvhaven"
    history_complete = False  # only as far back as notifications are kept
    cheap_depth = True  # uploader is in the notification
    PAGE = 100

    def __init__(self, session, api_key, throttle=None, should_cancel=None):
        self.s, self.key, self.t, self.cancel = session, (api_key or "").strip(), throttle, should_cancel

    def available(self):
        return (True, "") if self.key else (False, "No PMVHaven API key in Settings")

    def _get(self, path, params=None):
        try:
            r = pmvh._get(self.s, pmvh.API + path, params=params, throttle=self.t,
                          should_cancel=self.cancel)
        except pmvh.SiteError as e:
            if re.search(r"HTTP (401|403)", str(e)):
                raise FeedAuthError("PMVHaven refused the API key")
            raise FeedError(f"pmvhaven: {e}")
        try:
            return r.json()
        except ValueError:
            raise FeedError(f"pmvhaven: non-JSON response for {path}")

    @staticmethod
    def _body(d):
        return d.get("data") if isinstance((d or {}).get("data"), dict) else (d or {})

    def pages(self):
        page = 1
        while page <= 1000:
            if _cancelled(self.cancel):
                return
            body = self._body(self._get("/notifications", {"page": page, "limit": self.PAGE}))
            ns = body.get("notifications") or []
            out = []
            for n in ns:
                if n.get("type") != "new_video":
                    continue
                vid = n.get("video")
                if isinstance(vid, dict):
                    vid = vid.get("_id") or vid.get("id")
                sender = n.get("sender") if isinstance(n.get("sender"), dict) else {}
                when = pmvh._norm_date(n.get("createdAt"))
                out.append(_item("pmvhaven", vid, (sender.get("_id") or sender.get("id") or "").lower(),
                                 sender.get("username") or n.get("senderUsername"),
                                 n.get("videoTitle") or "", f"{pmvh.SITE}/video/{vid}" if vid else "",
                                 when, ref=n.get("_id")))
            # Pages of other notification types still count as "older than the
            # cutoff" only through their dates, so hand the caller an empty page
            # carrying the oldest date rather than nothing.
            dates = [pmvh._norm_date(n.get("createdAt")) for n in ns if n.get("createdAt")]
            yield out if out else ([_item("pmvhaven", "", date=min(dates))] if dates else [])
            if not ns or not (body.get("pagination") or {}).get("hasNext"):
                return
            page += 1

    def following(self):
        prof = self._body(self._get("/user/profile"))
        me = prof.get("userId") or ""
        if not me:
            raise FeedError("pmvhaven: couldn't read own user id")
        ids, page = set(), 1
        while page < 100:
            d = self._get(f"/users/{me}/subscriptions", {"page": page, "limit": 100})
            rows = d.get("data") if isinstance(d.get("data"), list) else (d.get("subscriptions") or [])
            for u in rows:
                uid = u.get("id") or u.get("_id")
                if uid:
                    ids.add(uid.lower())
            pg = d.get("pagination") or {}
            if not (pg.get("hasMore") or pg.get("hasNext")):
                break
            page += 1
        return ids

    def mark_read(self, refs):
        refs = [r for r in refs if r]
        if not refs:
            return 0
        done = 0
        for i in range(0, len(refs), 100):
            chunk = refs[i:i + 100]
            r = self.s.put(pmvh.API + "/notifications", json={"action": "markAsRead",
                                                              "notificationIds": chunk},
                           timeout=(15, 60))
            if r.status_code == 403 and "permission" in r.text:
                raise FeedError("the PMVHaven API key is read-only — make one with write "
                                "permission at pmvhaven.com/api-keys to let the app mark "
                                "notifications read (checking works either way)")
            if r.status_code != 200:
                raise FeedError(f"pmvhaven mark-read: HTTP {r.status_code}")
            done += len(chunk)
        return done


# ── rule34video ─────────────────────────────────────────────────────

FEED_BLOCK = "list_videos_videos_from_my_subscriptions"
MEMBERS_BLOCK = "list_members_subscriptions_my_subscriptions"
_REL_RE = re.compile(r"(\d+|an?|one)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I)
_UNIT = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400,
         "month": 31 * 86400, "year": 366 * 86400}


def relative_newest(text, now):
    """'22 hours ago' → the *latest* moment it can mean (now − 22h). Unknown text
    → None (treated as new, so it can never end a walk early)."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if "just now" in t or "today" in t:
        return now
    m = _REL_RE.search(t)
    if not m:
        return None
    n = m.group(1)
    n = 1 if n in ("a", "an", "one") else int(n)
    return now - timedelta(seconds=n * _UNIT[m.group(2)])


def parse_r34_feed(html, now):
    """Feed cards → items (uploader unknown). Ids/titles reuse r34.parse_listing."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    container = soup.find(id=f"{FEED_BLOCK}_items") or soup
    added = {}
    # Old layout: `.added`; since ~2026-10 the age is one of the `.ma-v__m` spans.
    for card in container.select("div.item[data-video-card-id], div.item[data-rdm-item][data-id]"):
        a = card.select_one(".added")
        text = a.get_text(" ", strip=True) if a else ""
        if not text:
            text = next((sp.get_text(" ", strip=True) for sp in card.select(".ma-v__m span")
                         if relative_newest(sp.get_text(" ", strip=True), now)), "")
        added[(card.get("data-video-card-id") or card.get("data-id") or "").strip()] = text
    out = []
    for it in r34.parse_listing(str(container)):
        newest = relative_newest(added.get(it["id"]), now)
        out.append(_item("rule34video", it["id"], title=it["title"], url=it["url"],
                         newest=newest.isoformat(timespec="seconds") if newest else None))
    return out


class R34Feed:
    platform = "rule34video"
    history_complete = True  # subscriptions go back to the first follow
    cheap_depth = False  # no uploader on cards: each unseen video costs a page load

    def __init__(self, session, signed_in, throttle=None, should_cancel=None, now=None):
        self.s, self.signed_in, self.t, self.cancel = session, signed_in, throttle, should_cancel
        self._now = now or (lambda: datetime.now(timezone.utc))

    def available(self):
        if not self.signed_in:
            return False, "No rule34video sign-in (send it from the Chrome extension)"
        return True, ""

    def _block(self, block, params):
        q = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{r34.BASE}/my/subscriptions/?mode=async&function=get_block&block_id={block}&{q}"
        if _cancelled(self.cancel):
            return None
        if self.t:
            self.t.wait(self.cancel or (lambda: False))
        try:
            r = self.s.get(url, timeout=(15, 60), allow_redirects=False)
        except requests.RequestException as e:
            raise FeedError(f"rule34video: {e.__class__.__name__}")
        if r.status_code in (301, 302, 303):
            raise FeedAuthError("rule34video sign-in expired — send it again from the Chrome extension")
        if r.status_code == 404:
            return ""
        if r.status_code != 200:
            raise FeedError(f"rule34video: HTTP {r.status_code}")
        if self.t:
            self.t.on_success()
        return r.text

    def pages(self):
        n = 1
        while n <= 1000:
            html = self._block(FEED_BLOCK, {"sort_by": "", "from": f"{n:02d}"})
            if not html:
                return
            items = parse_r34_feed(html, self._now())
            if not items:
                return
            yield items
            n += 1

    def following(self):
        ids, n = set(), 1
        while n <= 500:
            html = self._block(MEMBERS_BLOCK, {"sort_by": "added_date", "from_my_subscriptions": f"{n:02d}"})
            got = re.findall(r'href="https://rule34video\.com/members/(\d+)/"', html or "")
            if not got:
                break
            ids.update(got)
            n += 1
        return ids

    def resolve_uploader(self, item):
        """The feed card has no uploader: read it (and the exact date) from the
        video page. One request per video the app has never seen. None = the
        video page is gone; raises FeedError when it can't be read right now."""
        try:
            d = r34.fetch_video_detail(self.s, item["url"], self.t, self.cancel)
        except r34.NotFound:
            return None                    # deleted since it was listed
        except r34.SiteError as e:
            raise FeedError(f"rule34video: video {item['video_id']}: {e}")
        if d.get("date"):
            item["date"] = d["date"]
        return d.get("uploader_id") or ""


# ── pawchive ────────────────────────────────────────────────────────

def pawchive_signature(rec):
    """What 'this creator changed' means: new content (`updated`)."""
    return str((rec or {}).get("updated") or "")


def pawchive_favorites(fetch):
    """{(service, id): record} from the account favourites. `fetch(url)` returns
    parsed JSON or raises (the runner's fetch handles Cloudflare/retries)."""
    data = fetch(f"https://pawchive.pw/api/v1/account/favorites?type=artist")
    if not isinstance(data, list):
        raise FeedError("pawchive: unexpected favourites response")
    return {((f.get("service") or "").lower(), str(f.get("id") or "")): f for f in data}


def pawchive_profile(fetch, service, user_id):
    data = fetch(f"https://pawchive.pw/api/v1/{service}/user/{user_id}/profile")
    if not isinstance(data, dict):
        raise FeedError("pawchive: unexpected profile response")
    return data


# ── wiring ──────────────────────────────────────────────────────────

def make_sources(state, should_cancel=None, throttles=None):
    """Feed sources + pawchive fetch from the app state's saved sign-ins.
    → (sources {platform: src}, pawchive_fetch(url) | None)."""
    import os
    from backend import pawchive_scraper as pw
    from backend.rate_limit import AdaptiveThrottle
    from backend.pmv_runner import PmvRunner
    th = throttles or {p: AdaptiveThrottle(*v) for p, v in PmvRunner.THROTTLES.items()}
    sources = {}

    iw_session = iw.make_session()
    auth = iw.IwaraAuth(iw_session, state.get("iwara_email") or "", state.get("iwara_password") or "",
                        user_token=state.get("iwara_token") or None, throttle=th["iwara"])
    sources["iwara"] = IwaraFeed(iw_session, auth, th["iwara"], should_cancel)

    key = (state.get("pmvhaven_api_key") or "").strip()
    sources["pmvhaven"] = PmvhavenFeed(pmvh.make_session(api_key=key or None), key,
                                       th["pmvhaven"], should_cancel)

    r34_path = state.get("r34video_cookies_path") or ""
    signed = bool(r34_path and os.path.isfile(r34_path) and state.get("r34video_signed_in_at"))
    sources["rule34video"] = R34Feed(r34.make_session(cookies_path=r34_path if signed else None),
                                     signed, th["rule34video"], should_cancel)

    paw = pw.make_session(user_agent=state.get("pawchive_user_agent") or None,
                          cookies_path=state.get("pawchive_cookies_path") or None)
    paw_t = th["pawchive"]

    def paw_fetch(url):
        last = "no attempt"
        for attempt in range(4):
            if _cancelled(should_cancel):
                raise FeedError("cancelled")
            paw_t.wait(should_cancel or (lambda: False))
            try:
                r = paw.get(url, timeout=(15, 60))
            except pw.RequestException as e:
                last = e.__class__.__name__
                paw_t.on_throttled(2.0 * (attempt + 1))
                continue
            if pw.is_cloudflare_challenge(r):
                raise FeedAuthError("pawchive: Cloudflare challenge — Reconnect / Sign in again in Settings")
            if r.status_code in (401, 403):
                raise FeedAuthError(f"pawchive: HTTP {r.status_code} (signed out?)")
            if r.status_code == 429 or 500 <= r.status_code < 600:
                last = f"HTTP {r.status_code}"
                paw_t.on_throttled(3.0 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise FeedError(f"pawchive: HTTP {r.status_code}")
            paw_t.on_success()
            try:
                return r.json()
            except ValueError:
                raise FeedError("pawchive: non-JSON response")
        raise FeedError(f"pawchive: {last} after 4 attempts")

    return sources, paw_fetch
