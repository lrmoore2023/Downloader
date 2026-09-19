"""PMV fetch job: walk each tracked site listing and fold it into its manifest.

Metadata only — this runner never downloads a file. One `PmvRunner.run(jobs)`
processes creators/links strictly in order on the calling (daemon) thread, so
the Api can start it exactly like a creator download and cancel it with an
Event. Progress goes out through `on_progress`, one `on_complete` always fires.

Per platform:
* rule34video — async listing blocks newest-first (404 = end). Incremental mode
  stops after the first page whose ids are all already known; a full walk (or the
  very first walk, which must be complete before numbers are assigned) goes to
  the end. New videos get one extra video-page fetch for the exact upload date
  and best quality; a failed detail fetch is retried on the next run.
* iwara — JSON API, same stop rules; optional login via IwaraAuth, degrading to
  anonymous when credentials are missing/rejected.
* pawchive — always a full walk through the existing pawchive_scraper (the
  numbering is chronological and old posts get back-filled), reusing the saved
  Cloudflare cookie/UA; a challenge page surfaces as `needs_cf_auth` so the UI
  can offer Reconnect, exactly as creator downloads do.
"""

import threading
import time

from backend import pmv_tracker as pt
from backend import r34video_scraper as r34
from backend import iwara_scraper as iw
from backend import pawchive_scraper as pw
from backend.pawchive_links import post_media_kinds
from backend.rate_limit import AdaptiveThrottle


class LinkError(Exception):
    def __init__(self, message, needs_cf_auth=False):
        super().__init__(message)
        self.needs_cf_auth = needs_cf_auth


class PmvRunner:
    # (interval, floor, ceil) — conservative: a creator download may be hitting
    # pawchive at the same time, and none of this is latency-sensitive.
    THROTTLES = {
        "rule34video": (1.0, 0.5, 30.0),
        "iwara": (0.7, 0.3, 30.0),
        "pawchive": (1.5, 1.0, 60.0),
    }

    def __init__(self, *, manifest_root_for, state, cancel=None,
                 on_progress=None, on_complete=None, sessions=None):
        self._root_for = manifest_root_for
        self._state = state or {}
        self._cancel = cancel or threading.Event()
        self._on_progress = on_progress or (lambda d: None)
        self._on_complete = on_complete or (lambda d: None)
        self._sessions = dict(sessions or {})
        self._throttles = {p: AdaptiveThrottle(*v) for p, v in self.THROTTLES.items()}
        self._iwara_auth = None
        self.is_running = False
        self.result = None

    # ── plumbing ────────────────────────────────────────────────────

    def cancel(self):
        self._cancel.set()

    def _cancelled(self):
        return self._cancel.is_set()

    def _session(self, platform):
        s = self._sessions.get(platform)
        if s is None:
            if platform == "rule34video":
                s = r34.make_session()
            elif platform == "iwara":
                s = iw.make_session()
            else:
                s = pw.make_session(user_agent=self._state.get("pawchive_user_agent") or None,
                                    cookies_path=self._state.get("pawchive_cookies_path") or None)
            self._sessions[platform] = s
        return s

    def _iwara(self):
        if self._iwara_auth is None:
            self._iwara_auth = iw.IwaraAuth(
                self._session("iwara"),
                self._state.get("iwara_email") or "",
                self._state.get("iwara_password") or "",
                user_token=self._state.get("iwara_token") or None,
                throttle=self._throttles["iwara"])
        return self._iwara_auth

    def _emit(self, kind, job, message, **extra):
        d = {
            "type": kind,
            "creator_id": job["creator"].get("id", ""),
            "creator_name": job["creator"].get("name", ""),
            "link_key": pt.link_key(job["link"]),
            "platform": job["link"].get("platform", ""),
            "site_code": job["link"].get("site_code") or pt.default_site_code(job["link"].get("platform")),
            "message": message,
        }
        d.update(extra)
        try:
            self._on_progress(d)
        except Exception:
            pass

    # ── main loop ───────────────────────────────────────────────────

    def run(self, jobs):
        self.is_running = True
        result = {
            "cancelled": False, "total_new": 0, "per_creator": {}, "errors": [],
            "needs_cf_auth": False, "iwara_auth_failed": False, "iwara_message": "",
            "iwara_token": None, "links_done": 0,
        }
        try:
            for job in jobs:
                if self._cancelled():
                    result["cancelled"] = True
                    break
                creator, link = job["creator"], job["link"]
                cid = creator.get("id", "")
                pc = result["per_creator"].setdefault(cid, {"new": 0, "gone": 0, "errors": 0})
                platform = link.get("platform")
                key = pt.link_key(link)
                path = pt.manifest_path(self._root_for(cid), key)
                label = f"{creator.get('name', '?')} · {link.get('site_code') or pt.default_site_code(platform)}"
                self._emit("start", job, f"{label}: fetching…")
                try:
                    fetcher = {"rule34video": self._fetch_r34, "iwara": self._fetch_iwara,
                               "pawchive": self._fetch_pawchive}.get(platform)
                    if fetcher is None:
                        raise LinkError(f"unsupported platform {platform!r}")
                    with pt.manifest_lock(path):
                        manifest = pt.load_manifest(path, platform, link.get("user_id"))
                        fetched, full, complete = fetcher(job, manifest)
                        if self._cancelled():
                            result["cancelled"] = True
                            self._emit("info", job, f"{label}: cancelled — nothing recorded")
                            break
                        now = pt.now_iso()
                        res = pt.merge(manifest, fetched, full=full, complete=complete, now=now)
                        manifest["last_fetch"] = now
                        manifest["last_fetch_new"] = res["new"]
                        manifest["last_error"] = ""
                        if full and complete:
                            manifest["last_full_scan"] = now
                        if manifest.get("numbering") == pt.LOCKED and not manifest.get("initial_complete"):
                            manifest["last_error"] = "Initial scan incomplete — run Fetch latest again"
                        pt.save_manifest(path, manifest)
                    pc["new"] += res["new"]
                    pc["gone"] += res.get("gone", 0)
                    result["total_new"] += res["new"]
                    result["links_done"] += 1
                    self._emit("link_done", job,
                               f"{label}: {res['new']} new" + (f", {res['gone']} gone" if res.get("gone") else ""),
                               new=res["new"], gone=res.get("gone", 0), total=len(manifest["items"]))
                except LinkError as e:
                    self._record_error(result, pc, job, path, str(e), e.needs_cf_auth)
                except Exception as e:                       # never let one site kill the run
                    self._record_error(result, pc, job, path, f"{e.__class__.__name__}: {e}", False)
        finally:
            auth = self._iwara_auth
            if auth is not None:
                if auth.auth_failed:
                    result["iwara_auth_failed"] = True
                    result["iwara_message"] = auth.message
                if auth.user_token_changed:
                    result["iwara_token"] = auth.user_token
            self.result = result
            self.is_running = False
            try:
                self._on_complete(result)
            except Exception:
                pass
        return result

    def _record_error(self, result, pc, job, path, message, needs_cf_auth):
        pc["errors"] += 1
        result["errors"].append({"creator_id": job["creator"].get("id", ""),
                                 "link_key": pt.link_key(job["link"]), "message": message})
        if needs_cf_auth:
            result["needs_cf_auth"] = True
        try:
            with pt.manifest_lock(path):
                m = pt.load_manifest(path, job["link"].get("platform"), job["link"].get("user_id"))
                m["last_error"] = message
                pt.save_manifest(path, m)
        except Exception:
            pass
        self._emit("error", job, message)

    # ── rule34video ─────────────────────────────────────────────────

    @staticmethod
    def _is_numbered(manifest):
        return any(isinstance(it.get("number"), int) for it in manifest.get("items", {}).values())

    def _fetch_r34(self, job, manifest):
        link, mode = job["link"], job.get("mode", "latest")
        session = self._session("rule34video")
        throttle = self._throttles["rule34video"]
        numbered = self._is_numbered(manifest)
        incremental = (mode == "latest") and numbered
        known = manifest.get("items", {})
        fetched, pos, stopped_early = [], 0, False
        try:
            for page, items in r34.iter_listing_pages(session, link["user_id"], throttle, self._cancelled):
                all_known = True
                for it in items:
                    it = dict(it)
                    it["pos"] = pos
                    pos += 1
                    fetched.append(it)
                    if it["id"] not in known:
                        all_known = False
                self._emit("page", job, f"page {page}: {len(items)} videos", page=page, count=len(fetched))
                if incremental and all_known:
                    stopped_early = True
                    break
        except r34.SiteError as e:
            raise LinkError(f"rule34video: {e}")
        if self._cancelled():
            return fetched, False, False
        if not fetched:
            if numbered:
                raise LinkError("rule34video: listing came back empty — nothing changed")
            info = self._safe_member(session, link["user_id"], throttle)
            if info is None:
                raise LinkError("rule34video: member page not found (deleted account?)")
            # A real member with zero uploads: an empty complete walk is fine.
        complete = not stopped_early
        # Detail pass: only videos we have never seen (or whose detail failed before).
        for it in fetched:
            if self._cancelled():
                return fetched, False, False
            prev = known.get(it["id"])
            if prev is not None and not prev.get("detail_pending"):
                continue
            try:
                d = r34.fetch_video_detail(session, it["url"], throttle, self._cancelled)
                it["date"] = d.get("date")
                it["quality"] = d.get("quality")
                if d.get("duration"):
                    it["duration"] = d["duration"]
                it["detail_pending"] = False
                self._emit("detail", job, f"detail: {it.get('title') or it['id']}", count=len(fetched))
            except r34.SiteError as e:
                it["detail_pending"] = True
                self._emit("info", job, f"detail fetch failed for {it['id']} ({e}); will retry next run")
        return fetched, (not incremental), complete

    def _safe_member(self, session, user_id, throttle):
        try:
            return r34.fetch_member(session, user_id, throttle, self._cancelled)
        except r34.SiteError:
            return None

    # ── iwara ───────────────────────────────────────────────────────

    def _fetch_iwara(self, job, manifest):
        link, mode = job["link"], job.get("mode", "latest")
        session = self._session("iwara")
        throttle = self._throttles["iwara"]
        auth = self._iwara()
        token = auth.access_token() if auth.enabled else None
        uuid = link.get("user_id") or ""
        if not uuid:
            try:
                uuid = iw.fetch_profile(session, link.get("username", ""), token, throttle, self._cancelled)["id"]
            except iw.SiteError as e:
                raise LinkError(f"iwara: profile lookup failed ({e})")
        numbered = self._is_numbered(manifest)
        incremental = (mode == "latest") and numbered
        attempts = 0
        while True:
            attempts += 1
            try:
                return self._walk_iwara(job, manifest, session, uuid, token, throttle, incremental)
            except iw.AuthError as e:
                if token and attempts == 1:
                    token = auth.access_token(force_refresh=True)
                    if token:
                        continue
                if token:
                    token = None                      # fall back to anonymous
                    auth.auth_failed, auth.message = True, f"listing rejected the login ({e})"
                    continue
                raise LinkError(f"iwara: {e}")
            except iw.SiteError as e:
                raise LinkError(f"iwara: {e}")

    def _walk_iwara(self, job, manifest, session, uuid, token, throttle, incremental):
        known = manifest.get("items", {})
        fetched, pos, stopped_early = [], 0, False
        for page, results, count in iw.iter_videos(session, uuid, token, throttle, self._cancelled):
            all_known = True
            for raw in results:
                it = iw.parse_video(raw)
                if not it["id"]:
                    continue
                it["pos"] = pos
                pos += 1
                fetched.append(it)
                if it["id"] not in known:
                    all_known = False
            self._emit("page", job, f"page {page + 1}: {len(results)} videos (site total {count})",
                       page=page + 1, count=len(fetched))
            if incremental and all_known:
                stopped_early = True
                break
        if self._cancelled():
            return fetched, False, False
        if not fetched and known:
            raise LinkError("iwara: listing came back empty — nothing changed")
        return fetched, (not incremental), (not stopped_early)

    # ── pawchive ────────────────────────────────────────────────────

    def _fetch_pawchive(self, job, manifest):
        link = job["link"]
        session = self._session("pawchive")
        throttle = self._throttles["pawchive"]
        service, uid = link.get("service") or "", link.get("user_id") or ""
        pages = {"n": 0}

        def fetch(sess, url):
            last = "no attempt"
            for attempt in range(4):
                if self._cancelled():
                    return None
                throttle.wait(self._cancelled)
                try:
                    r = sess.get(url, timeout=(15, 60))
                except pw.RequestException as e:
                    last = e.__class__.__name__
                    throttle.on_throttled(2.0 * (attempt + 1))
                    continue
                if pw.is_cloudflare_challenge(r):
                    raise LinkError("pawchive: Cloudflare challenge — click Reconnect in Settings",
                                    needs_cf_auth=True)
                if r.status_code == 404:
                    raise LinkError("pawchive: creator not found (404)")
                if r.status_code == 429 or 500 <= r.status_code < 600:
                    last = f"HTTP {r.status_code}"
                    try:
                        wait = float(r.headers.get("Retry-After") or 3.0 * (attempt + 1))
                    except (TypeError, ValueError):
                        wait = 3.0 * (attempt + 1)
                    throttle.on_throttled(wait)
                    continue
                if r.status_code != 200:
                    raise LinkError(f"pawchive: HTTP {r.status_code}")
                throttle.on_success()
                try:
                    return r.json()
                except ValueError:
                    raise LinkError("pawchive: non-JSON listing response")
            raise LinkError(f"pawchive: {last} after 4 attempts")

        def on_page(n, size):
            pages["n"] = n
            self._emit("page", job, f"page {n}: {size} posts", page=n)

        fetched, pos = [], 0
        for raw in pw.iter_posts(session, service, uid, on_page=on_page,
                                 should_cancel=self._cancelled, fetch=fetch):
            p = pw.parse_post(raw)
            if not p["post_id"]:
                continue
            fetched.append({
                "id": p["post_id"],
                "title": p["title"],
                "url": p["url"],
                "date": p["dt"].isoformat(timespec="seconds") if p["dt"] else None,
                "duration": None,
                "quality": None,
                "media_kinds": post_media_kinds(p),
                "link_hosts": sorted({l.get("host") for l in p["external_links"] if l.get("host")}),
                "preview_state": p.get("preview_state") or "",
                "pos": pos,
            })
            pos += 1
        if self._cancelled():
            return fetched, False, False
        if not fetched and manifest.get("items"):
            raise LinkError("pawchive: listing came back empty — nothing changed")
        return fetched, True, True
