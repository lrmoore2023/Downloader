"""PMV ▸ Check for new: read the followed feeds, fetch only what changed.

The contract is *miss nothing from the start date on*. A feed is a hint about
where to look; the manifests stay the only source of truth and every tracked
link ends up in exactly one of these buckets on every check:

* covered by a readable feed and nothing new in it      → not fetched
* the feed shows a video of it the manifest lacks        → fetched
* no feed for it — HMVMania, not followed on the site, not signed in, the feed
  errored, or the walk couldn't reach back to the cutoff → fetched directly
  (PmvRunner's `latest` mode: about one request per link)

pawchive has no per-video feed; each tracked creator's `updated` time (from the
account favourites, else the public profile) is compared with the value seen
when it was last fetched successfully, and a change triggers the usual full walk.

Feed walks run newest-first and stop at the first page whose items are all
older than the cutoff: the previous successful check minus OVERLAP, never
earlier than `since` (the day the feature was switched on — nothing before it
is backfilled). A feed video that still isn't in its manifest after the fetch
is kept as *pending* and its link is fetched again next time; it is never
dropped. PMVHaven notifications are marked read only once their video is in a
manifest (or recorded as untracked).

State lives in `<pmv root>/_feed_state.json` — metadata only, no credentials.
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone

from backend import pmv_tracker as pt
from backend import pmv_feeds as pf

OVERLAP = timedelta(hours=48)
MAX_PAGES = 40               # feed pages read past which the walk is called a gap
SEEN_CAP = 20000
UNTRACKED_PER_UPLOADER = 20
FEED_PLATFORMS = ("iwara", "pmvhaven", "rule34video")
STATE_VERSION = 1


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(iso):
    if not iso:
        return None
    s = str(iso).strip()
    if len(s) == 10:                       # bare date: the whole day counts
        s += "T23:59:59+00:00"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def start_of_today_utc(now=None):
    """Local midnight today, as a UTC instant — the default `since`."""
    local = (now or _utc_now()).astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


# ── feed state file ─────────────────────────────────────────────────

def new_state():
    return {"version": STATE_VERSION, "since": "", "last_check": "", "last_sweep": "",
            "platforms": {}, "seen": {}, "pending": {}, "untracked": {},
            "dismissed": [], "pawchive_sig": {}, "last_result": {}}


def load_state(path):
    if not os.path.isfile(path):
        return new_state()
    m = pt.load_manifest(path, "_feeds", "")          # same retry / set-aside rules
    st = new_state()
    for k, v in st.items():
        if isinstance(m.get(k), type(v)):
            st[k] = m[k]
    return st


def save_state(path, st):
    pt.save_manifest(path, st)


def seen_key(platform, video_id):
    return f"{platform}:{video_id}"


def uploader_key(platform, uploader_id):
    return f"{platform}:{(uploader_id or '').lower()}"


def profile_url(platform, uploader_id, name=""):
    if platform == "iwara":
        from backend.iwara_scraper import profile_url as u
        return u(name) if name else ""
    if platform == "pmvhaven":
        from backend.pmvhaven_scraper import profile_url as u
        return u(uploader_id)
    if platform == "rule34video":
        from backend.r34video_scraper import member_url
        return member_url(uploader_id)
    return ""


# ── the job ─────────────────────────────────────────────────────────

class PmvCheck:
    """One check (or full sweep). Feed phase, then the ordinary PmvRunner for
    the chosen links, then reconciliation. Synchronous on the calling thread."""

    def __init__(self, *, creators, manifest_root_for, state_path, sources,
                 pawchive_fetch=None, run_jobs, cancel=None, on_progress=None,
                 now=None, mode="check", sweep_pawchive=False):
        self.creators = creators            # {pc_id: record}
        self.root_for = manifest_root_for
        self.state_path = state_path
        self.sources = sources              # {platform: feed source} (may be partial)
        self.paw_fetch = pawchive_fetch     # fetch(url) → JSON, or None
        self.run_jobs = run_jobs            # jobs → PmvRunner result
        self.cancel = cancel or threading.Event()
        self.on_progress = on_progress or (lambda d: None)
        self.now = now or _utc_now
        self.mode = mode
        self.sweep_pawchive = sweep_pawchive

    def _cancelled(self):
        return self.cancel.is_set()

    def _emit(self, message, **extra):
        d = {"type": "check", "message": message}
        d.update(extra)
        try:
            self.on_progress(d)
        except Exception:
            pass

    # ── index ───────────────────────────────────────────────────────

    def _index(self):
        """links: [(creator, link, key, path, manifest)]; by_uploader: platform →
        uploader id/username (lower) → [link entries]; known: platform → video ids."""
        links, by_up, known = [], {}, {}
        for cid, rec in sorted(self.creators.items()):
            for link in rec.get("links") or []:
                platform = link.get("platform")
                key = pt.link_key(link)
                path = pt.manifest_path(self.root_for(cid), key)
                try:
                    m = pt.load_manifest(path, platform, link.get("user_id"))
                except pt.ManifestUnavailable:
                    m = None
                e = {"creator": rec, "cid": cid, "link": link, "key": key, "path": path, "manifest": m}
                links.append(e)
                ups = by_up.setdefault(platform, {})
                for ident in (link.get("user_id"), link.get("username")):
                    if ident:
                        ups.setdefault(str(ident).lower(), []).append(e)
                ids = known.setdefault(platform, set())
                if m:
                    ids.update(m.get("items", {}).keys())
        return links, by_up, known

    # ── feed walk ───────────────────────────────────────────────────

    def _walk(self, src, cutoff, st, known, by_up):
        """→ dict(status, message, items_new, flagged {key: [items]}, untracked
        [items], refs_known [ref], following set|None, pages)."""
        out = {"status": "ok", "message": "", "flagged": {}, "untracked": [], "refs_known": [],
               "following": None, "pages": 0, "seen_new": 0, "unresolved": []}
        platform = src.platform
        ups = by_up.get(platform, {})
        reached = False
        try:
            for items in src.pages():
                if self._cancelled():
                    out["status"] = "cancelled"
                    return out
                out["pages"] += 1
                all_old = bool(items)
                for it in items:
                    newest = _parse(it.get("newest") or it.get("date"))
                    if newest is not None and newest < cutoff:
                        continue
                    all_old = False
                    if not it.get("video_id"):
                        continue
                    self._classify(src, it, st, known, ups, out)
                self._emit(f"{platform}: feed page {out['pages']}", platform=platform)
                if all_old:
                    reached = True
                    break
                if out["pages"] >= MAX_PAGES:
                    break
            else:
                reached = True               # the feed ended: everything was read
        except pf.FeedAuthError as e:
            out.update(status="auth", message=str(e))
            return out
        except pf.FeedError as e:
            out.update(status="error", message=str(e))
            return out
        if not reached:
            out.update(status="gap", message=f"read {out['pages']} pages without reaching the last check")
            return out
        try:
            out["following"] = src.following()
        except (pf.FeedError, pf.FeedAuthError) as e:
            out["following"] = None
            out["message"] = f"following list unavailable ({e}) — every link checked directly"
        return out

    def _classify(self, src, it, st, known, ups, out):
        platform, vid = src.platform, it["video_id"]
        skey = seen_key(platform, vid)
        if vid in known.get(platform, set()):
            if it.get("ref"):
                out["refs_known"].append(it["ref"])
            st["seen"].setdefault(skey, it.get("uploader_id") or "")
            return
        up = it.get("uploader_id") or ""
        if not up and skey in st["seen"]:
            up = st["seen"][skey]
            if not up:
                return                       # looked up before: gone / no uploader
        if not up and hasattr(src, "resolve_uploader"):
            try:
                up = src.resolve_uploader(it)
            except pf.FeedError as e:
                out["unresolved"].append({"item": it, "error": str(e)})
                return
            if up is None:                   # the video is gone
                st["seen"][skey] = ""
                return
            it["uploader_id"] = up
        st["seen"][skey] = up or ""
        entries = ups.get(str(up).lower()) or ups.get(str(it.get("uploader_name") or "").lower())
        if entries:
            for e in entries:
                out["flagged"].setdefault(e["key"], []).append(it)
        elif up:
            out["untracked"].append(it)

    # ── pawchive ────────────────────────────────────────────────────

    def _pawchive(self, links, st, since):
        """→ ({link_key: sig}, {link_key: reason}, message). A link whose signature
        can't be read is fetched (reason 'no signature')."""
        sigs, flagged, msg = {}, {}, ""
        paw = [e for e in links if e["link"].get("platform") == "pawchive"]
        if not paw:
            return sigs, flagged, msg
        favs = {}
        if self.paw_fetch is not None:
            try:
                favs = pf.pawchive_favorites(self.paw_fetch)
            except Exception as e:
                msg = f"favourites unavailable ({e}); using each creator's profile"
                favs = {}
        for e in paw:
            if self._cancelled():
                break
            link = e["link"]
            svc, uid = (link.get("service") or "").lower(), str(link.get("user_id") or "")
            rec = favs.get((svc, uid))
            if rec is None and self.paw_fetch is not None:
                try:
                    rec = pf.pawchive_profile(self.paw_fetch, svc, uid)
                except Exception as ex:
                    flagged[e["key"]] = f"no signature ({ex})"
                    continue
            sig = pf.pawchive_signature(rec)
            if not sig:
                flagged[e["key"]] = "no signature"
                continue
            sigs[e["key"]] = sig
            prev = st["pawchive_sig"].get(e["key"])
            if prev is None:
                # First check for this link: only content added since the start
                # date matters (nothing before it is backfilled).
                upd = _parse(sig)
                if upd is not None and upd >= since:
                    flagged[e["key"]] = "updated today"
            elif prev != sig:
                flagged[e["key"]] = "updated"
        return sigs, flagged, msg

    # ── run ─────────────────────────────────────────────────────────

    def run(self):
        started = self.now()
        st = load_state(self.state_path)
        if not st["since"]:
            st["since"] = _iso(start_of_today_utc(started))
        since = _parse(st["since"])
        links, by_up, known = self._index()
        sweep = self.mode == "sweep"
        result = {"mode": self.mode, "platforms": {}, "fetched_links": 0, "direct_links": 0,
                  "flagged_links": 0, "pending": 0, "untracked_new": 0, "marked_read": 0,
                  "cancelled": False, "runner": None, "unfollowed": []}

        # 1. feeds
        walks = {}
        if not sweep:
            for platform in FEED_PLATFORMS:
                if self._cancelled():
                    break
                src = self.sources.get(platform)
                pstate = st["platforms"].setdefault(platform, {})
                if src is None:
                    walks[platform] = {"status": "off", "message": "no feed"}
                    continue
                ok, why = src.available()
                if not ok:
                    walks[platform] = {"status": "off", "message": why}
                    continue
                last_ok = _parse(pstate.get("last_ok"))
                cutoff = max(since, last_ok - OVERLAP) if last_ok else since
                self._emit(f"{platform}: reading followed feed…", platform=platform)
                walks[platform] = self._walk(src, cutoff, st, known, by_up)

        # 2. pawchive signatures
        paw_sigs, paw_flagged, paw_msg = ({}, {}, "")
        if not sweep and not self._cancelled():
            self._emit("pawchive: checking favourites…", platform="pawchive")
            paw_sigs, paw_flagged, paw_msg = self._pawchive(links, st, since)

        if self._cancelled():
            result["cancelled"] = True
            save_state(self.state_path, st)
            return result

        # 3. choose links
        jobs, reasons = [], {}
        for e in links:
            platform, key = e["link"].get("platform"), e["key"]
            m = e["manifest"]
            reason = None
            if sweep:
                if platform != "pawchive" or self.sweep_pawchive:
                    reason = "sweep"
            elif m is None:
                reason = "manifest unreadable"
            elif not m.get("initial_complete"):
                reason = "never fetched"
            elif platform == "pawchive":
                reason = paw_flagged.get(key)
            elif platform == "hmvmania":
                reason = "no feed"
            else:
                w = walks.get(platform) or {"status": "off"}
                if w["status"] != "ok":
                    reason = f"feed {w['status']}"
                elif key in w["flagged"]:
                    reason = "new in feed"
                elif any(p["link_key"] == key for p in st["pending"].values()):
                    reason = "pending"
                elif w.get("following") is None:
                    reason = "following unknown"
                elif str(e["link"].get("user_id") or "").lower() not in w["following"]:
                    reason = "not followed"
                    result["unfollowed"].append({"creator": e["creator"].get("name", ""),
                                                 "platform": platform, "link_key": key})
            if reason:
                reasons[(e["cid"], key)] = reason
                jobs.append({"creator": e["creator"], "link": e["link"], "mode": "latest"})
        result["fetched_links"] = len(jobs)
        result["flagged_links"] = sum(1 for r in reasons.values() if r in ("new in feed", "updated",
                                                                           "updated today", "pending"))
        result["direct_links"] = len(jobs) - result["flagged_links"]

        # 4. fetch
        runner_res = {"errors": [], "cancelled": False}
        if jobs:
            self._emit(f"fetching {len(jobs)} link(s)…")
            runner_res = self.run_jobs(jobs) or runner_res
        result["runner"] = runner_res
        failed = {(er.get("creator_id"), er.get("link_key")) for er in runner_res.get("errors") or []}
        if runner_res.get("cancelled") or self._cancelled():
            result["cancelled"] = True

        # 5. reconcile
        _, _, known_after = self._index()
        now_iso = _iso(self.now())
        for platform, w in walks.items():
            for key, items in (w.get("flagged") or {}).items():
                for it in items:
                    pk = seen_key(platform, it["video_id"])
                    if it["video_id"] in known_after.get(platform, set()):
                        st["pending"].pop(pk, None)
                        if it.get("ref"):
                            w["refs_known"].append(it["ref"])
                    else:
                        p = st["pending"].setdefault(pk, {"platform": platform, "video_id": it["video_id"],
                                                          "link_key": key, "title": it.get("title"),
                                                          "url": it.get("url"), "date": it.get("date"),
                                                          "first_seen": now_iso})
                        p["last_checked"] = now_iso
        # Pending items from earlier checks whose video has since appeared.
        for pk, p in list(st["pending"].items()):
            if p.get("video_id") in known_after.get(p.get("platform"), set()):
                st["pending"].pop(pk, None)
        result["pending"] = len(st["pending"])

        # 6. untracked
        dismissed = set(st["dismissed"])
        for platform, w in walks.items():
            for it in w.get("untracked") or []:
                ukey = uploader_key(platform, it["uploader_id"])
                if ukey in dismissed:
                    if it.get("ref"):
                        w["refs_known"].append(it["ref"])
                    continue
                u = st["untracked"].setdefault(ukey, {
                    "platform": platform, "uploader_id": it["uploader_id"],
                    "name": it.get("uploader_name") or it["uploader_id"],
                    "profile_url": profile_url(platform, it["uploader_id"], it.get("uploader_name")),
                    "items": []})
                if not any(x["video_id"] == it["video_id"] for x in u["items"]):
                    u["items"].insert(0, {k: it.get(k) for k in ("video_id", "title", "url", "date")})
                    u["items"] = u["items"][:UNTRACKED_PER_UPLOADER]
                    result["untracked_new"] += 1
                u["last_seen"] = now_iso
                if it.get("ref"):
                    w["refs_known"].append(it["ref"])

        # 7. PMVHaven mark-read — only what is safely recorded, never on cancel.
        w = walks.get("pmvhaven")
        src = self.sources.get("pmvhaven")
        if w and w.get("status") == "ok" and src is not None and not result["cancelled"]:
            refs = sorted(set(w["refs_known"]))
            if refs:
                try:
                    result["marked_read"] = src.mark_read(refs)
                except Exception as e:
                    w["message"] = f"couldn't mark notifications read ({e})"

        # 8. advance watermarks (never on cancel; per platform only when its
        # feed was read to the cutoff — failed links are covered by pending).
        if not result["cancelled"]:
            for platform, w in walks.items():
                pstate = st["platforms"].setdefault(platform, {})
                if w["status"] == "ok" and not w.get("unresolved"):
                    pstate["last_ok"] = _iso(started)
                pstate["last_status"] = w["status"]
                pstate["last_message"] = w.get("message") or ""
            for key, sig in paw_sigs.items():
                cid = next((e["cid"] for e in links if e["key"] == key), None)
                if (cid, key) not in failed:
                    st["pawchive_sig"][key] = sig
            st["last_check" if not sweep else "last_sweep"] = _iso(started)
            if sweep and self.sweep_pawchive:
                st["last_sweep_pawchive"] = _iso(started)

        for platform, w in walks.items():
            result["platforms"][platform] = {
                "status": w["status"], "message": w.get("message") or "",
                "flagged": sum(len(v) for v in (w.get("flagged") or {}).values()),
                "untracked": len(w.get("untracked") or []), "pages": w.get("pages", 0),
                "unresolved": len(w.get("unresolved") or [])}
        if not sweep:
            result["platforms"]["pawchive"] = {"status": "ok", "message": paw_msg,
                                               "flagged": len(paw_flagged), "untracked": 0, "pages": 0,
                                               "unresolved": 0}
        result["reasons"] = {f"{cid}|{key}": r for (cid, key), r in reasons.items()}
        # Bound the seen map (oldest entries first — dict keeps insertion order).
        if len(st["seen"]) > SEEN_CAP:
            for k in list(st["seen"].keys())[:len(st["seen"]) - SEEN_CAP]:
                del st["seen"][k]
        st["last_result"] = {k: result[k] for k in ("mode", "platforms", "fetched_links", "direct_links",
                                                    "flagged_links", "pending", "untracked_new",
                                                    "marked_read", "cancelled", "unfollowed")}
        st["last_result"]["at"] = _iso(started)
        save_state(self.state_path, st)
        return result
