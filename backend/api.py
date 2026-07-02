import base64
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from datetime import datetime, timezone

import webview

from backend.cookie_manager import validate_cookies_file, extract_from_browser, get_available_browsers
from backend.coomerfans_runner import CoomerfansRunner
from backend.coomerfans_scraper import parse_creator_url, make_session, BASE
from backend.coomerfans_verify import find_broken, repair_broken
from backend.coomerfans_archive import Archive as CfArchive
from backend.pawchive_scraper import (
    parse_creator_url as pw_parse_creator_url, make_session as pw_make_session,
    fetch_profile as pw_fetch_profile, BASE as PW_BASE,
)
from backend.pawchive_links import PawchiveLinks
from backend.creator_runner import (
    CreatorRunner, filter_links, link_label, cf_archive_path,
)
from backend.library_import import scan_library
from backend.media_library import scan_creator_media
from backend.media_server import MediaServer

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(APP_DIR, "app_state.json")
AVATAR_DIR = os.path.join(APP_DIR, ".avatars")


def _replace_with_retry(src, dst, attempts=10, delay=0.1):
    """os.replace(src, dst) hardened against transient Windows locks.

    On Windows the destination can be momentarily held open by antivirus
    real-time scanning, the Search indexer, or a cloud-sync/backup agent
    (E:\\Photos is exactly the kind of folder those watch). MoveFileEx then
    fails with WinError 5 (access denied) even though nothing is permanently
    wrong, so retry a few times with a short backoff before giving up.
    """
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            # If the destination picked up a read-only attribute, clear it.
            try:
                if os.path.exists(dst):
                    os.chmod(dst, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            time.sleep(delay)


class Api:
    def __init__(self):
        self._window = None
        self._creator_runner = None          # active download orchestrator
        self._creator_thread = None
        self._creator_aux_thread = None       # verify/repair worker
        self._aux_cancel = threading.Event()  # cancel for verify/repair
        self._verify_broken = []              # [{link, broken:[...]}] from last verify
        self._state_lock = threading.Lock()   # serialize app_state.json writes
        self._media = MediaServer()           # local streaming/thumbnail server

    def set_window(self, window):
        self._window = window
        try:
            self._media.start()
        except Exception:
            pass

    # ── path helpers ───────────────────────────────────────────────

    @staticmethod
    def _fwd(path):
        """Forward-slashed, trailing-slash-trimmed path (for storage/display)."""
        return (path or "").replace("\\", "/").rstrip("/")

    @staticmethod
    def _norm_dest(path):
        """Stable creator key: forward-slashed, trimmed, lowercased."""
        return (path or "").replace("\\", "/").rstrip("/").lower()

    @staticmethod
    def _basename(path):
        return ((path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1])

    @staticmethod
    def _extract_username(url):
        """Extract the Twitter/X username from a URL (or None)."""
        m = re.search(r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)", url or "")
        return m.group(1).lower() if m else None

    # ── State persistence ──────────────────────────────────────────

    def _default_state(self):
        return {
            "creators": {},
            "archive_dir": "",
            "library_root": "",
            "cf_concurrency": 5,
            "cookies_path": "",
            "cookies_browser": "",
            "auth_method": "file",
            "last_creator": "",
            "window": {},
        }

    def load_state(self):
        if not os.path.exists(STATE_FILE):
            return self._default_state()
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            # Tolerate a corrupt/partially-written file: salvage the leading
            # valid JSON object (ignoring any trailing garbage), else back the
            # file up and fall back to defaults so the app always launches.
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    obj, _ = json.JSONDecoder().raw_decode(f.read().lstrip())
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
            try:
                os.replace(STATE_FILE, STATE_FILE + ".corrupt")
            except OSError:
                pass
            return self._default_state()

    def save_state(self, state):
        # Merge into existing state so partial updates from the UI (which omit
        # keys like creators / window) never clobber data managed elsewhere.
        # Locked + atomic (write temp, then os.replace) so concurrent saves from
        # the UI / download / window-geometry threads can't corrupt the file.
        with self._state_lock:
            merged = self.load_state()
            merged.update(state or {})
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE_FILE), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(merged, f, indent=2)
                _replace_with_retry(tmp, STATE_FILE)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # ── Migration: legacy artist_map/cf_artist_map → creators ───────

    def migrate_state(self):
        """One-time consolidation of the old per-tab maps into the creator model.

        Groups links by their (shared) destination folder. Idempotent: if a
        `creators` map already exists it does nothing. Backs up app_state.json
        before writing so the original flat state is always recoverable.
        """
        state = self.load_state()
        if state.get("creators"):
            return {"migrated": False, "creators": len(state["creators"])}

        try:
            if os.path.exists(STATE_FILE):
                shutil.copy2(STATE_FILE, STATE_FILE + ".pre-migrate.bak")
        except Exception:
            pass

        creators = {}
        self._merge_legacy_maps(creators, state.get("artist_map"), state.get("cf_artist_map"))

        # Unify settings (single archive dir + library root for both platforms).
        if not (state.get("archive_dir") or "").strip():
            state["archive_dir"] = state.get("cf_archive_dir", "") or ""
        if not (state.get("library_root") or "").strip():
            state["library_root"] = state.get("cf_library_root", "") or ""
        if not state.get("cf_concurrency"):
            state["cf_concurrency"] = 5

        state["creators"] = creators
        self.save_state(state)
        return {"migrated": True, "creators": len(creators)}

    def _merge_legacy_maps(self, creators, artist_map, cf_artist_map):
        """Fold legacy artist_map (twitter) + cf_artist_map (coomerfans) into a
        `creators` dict, grouping links by destination folder. Additive and
        deduped by URL, so it's safe to call across multiple legacy snapshots.
        Returns the number of links added."""
        added = 0

        def ensure(dest):
            key = self._norm_dest(dest)
            if key not in creators:
                creators[key] = {
                    "name": self._basename(dest),
                    "destination": self._fwd(dest),
                    "has_videos": False,
                    "links": [],
                    "last_used": "",
                }
            return creators[key]

        def add_link(c, link, last_used):
            nonlocal added
            url = (link.get("url") or "").lower()
            if not url or any((l.get("url") or "").lower() == url for l in c["links"]):
                return
            c["links"].append(link)
            added += 1
            if (last_used or "") > (c["last_used"] or ""):
                c["last_used"] = last_used

        for info in (cf_artist_map or {}).values():
            dest = info.get("destination")
            if not dest:
                continue
            add_link(ensure(dest), {
                "platform": "coomerfans",
                "service": info.get("service"),
                "user_id": info.get("user_id"),
                "name": info.get("name"),
                "url": info.get("url"),
            }, info.get("last_used", ""))

        for username, info in (artist_map or {}).items():
            dest = info.get("destination")
            if not dest:
                continue
            d = self._fwd(dest)
            # Twitter content nests under <creator>/twitter — group at the root.
            root = d[: -len("/twitter")] if d.lower().endswith("/twitter") else d
            add_link(ensure(root), {
                "platform": "twitter",
                "username": username,
                "url": info.get("url") or f"https://x.com/{username}",
            }, info.get("last_used", ""))

        return added

    def recover_from_backup(self):
        """Fold legacy maps from app_state.json.corrupt / .pre-migrate.bak back
        into the current creators model. Recovers folder↔URL mappings that were
        lost when the live state file was truncated. Additive and non-destructive."""
        state = self.load_state()
        creators = state.get("creators", {})
        before = len(creators)
        added, used = 0, []

        for path in (STATE_FILE + ".corrupt", STATE_FILE + ".pre-migrate.bak"):
            if not os.path.isfile(path):
                continue
            data = None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data, _ = json.JSONDecoder().raw_decode(f.read().lstrip())
                except Exception:
                    data = None
            if not isinstance(data, dict):
                continue
            added += self._merge_legacy_maps(
                creators, data.get("artist_map"), data.get("cf_artist_map"))
            used.append(os.path.basename(path))

        state["creators"] = creators
        self.save_state(state)
        return {
            "links_added": added,
            "creators_added": len(creators) - before,
            "creators_total": len(creators),
            "sources": used,
        }

    # ── Import from disk (rebuild index from NAS) ───────────────────

    def _creator_folder(self, destination):
        """A 'destination-ish' path → the creator root folder (strips /twitter)."""
        d = self._fwd(destination)
        return d[: -len("/twitter")] if d.lower().endswith("/twitter") else d

    def _derive_roots(self, state):
        """Library roots = parent folders of every destination we know about
        (current creators + the legacy/backup maps). Deduped case-insensitively
        so a root recorded with different casing isn't scanned twice."""
        roots = {}   # normalized(lower) -> original casing

        def add_from(dest):
            folder = self._creator_folder(dest)
            if folder and "/" in folder:
                root = folder.rsplit("/", 1)[0]
                roots.setdefault(root.lower(), root)

        for c in (state.get("creators") or {}).values():
            add_from(c.get("destination", ""))

        for path in (STATE_FILE + ".corrupt", STATE_FILE + ".pre-migrate.bak"):
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            for info in (data.get("cf_artist_map") or {}).values():
                add_from(info.get("destination", ""))
            for info in (data.get("artist_map") or {}).values():
                add_from(info.get("destination", ""))

        return [r for r in roots.values() if os.path.isdir(r)]

    def _known_cf_index(self, state):
        """{(service, user_id): {name, url}} from creators we already have, so
        the importer skips the online name lookup where it can."""
        idx = {}
        for c in (state.get("creators") or {}).values():
            for l in c.get("links", []):
                if l.get("platform") == "coomerfans":
                    idx[(l.get("service"), l.get("user_id"))] = {
                        "name": l.get("name"), "url": l.get("url")}
        return idx

    def list_library_roots(self):
        """Roots the importer would scan by default (for display in the UI)."""
        return self._derive_roots(self.load_state())

    def import_from_disk(self, roots=None):
        """Scan library roots + archive DBs and rebuild creators from disk."""
        if self._aux_busy():
            return {"error": "An operation is already in progress"}
        state = self.load_state()
        archive_dir = (state.get("archive_dir") or "").strip()
        if not archive_dir or not os.path.isdir(archive_dir):
            return {"error": "Set a valid Archive Dir in Settings first."}
        roots = [r for r in (roots or []) if r] or self._derive_roots(state)
        if not roots:
            return {"error": "No library roots to scan — add a creator first, or browse to a folder."}

        self._aux_cancel.clear()
        self._creator_aux_thread = threading.Thread(
            target=self._run_import, args=(roots, archive_dir), daemon=True)
        self._creator_aux_thread.start()
        return {"status": "started", "roots": roots}

    def _run_import(self, roots, archive_dir):
        try:
            session = make_session()
            state = self.load_state()
            creators = state.get("creators", {})
            known = self._known_cf_index(state)
            log = lambda m: self._push_js("onImportProgress", {"type": "info", "message": m})
            cf_map, tw_map, report = scan_library(
                roots, archive_dir, session, known_cf=known,
                log=log, should_cancel=self._aux_cancel.is_set,
            )
            added = self._merge_legacy_maps(creators, tw_map, cf_map)
            state["creators"] = creators
            self.save_state(state)
            report.update({
                "links_added": added,
                "creators_total": len(creators),
                "cancelled": self._aux_cancel.is_set(),
            })
            self._push_js("onImportComplete", report)
        except Exception as e:
            self._push_js("onImportComplete", {"error": str(e)})

    # ── File/folder dialogs ────────────────────────────────────────

    def select_folder(self):
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if result and len(result) > 0:
            return result[0]
        return None

    def select_cookies_file(self):
        file_types = ("Cookie Files (*.txt)",)
        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN, file_types=file_types
        )
        if result and len(result) > 0:
            return result[0]
        return None

    def open_folder(self, path):
        """Open a folder in the system file explorer."""
        path = (path or "").strip()
        if not path or not os.path.isdir(path):
            return {"error": "Folder does not exist yet"}
        try:
            os.startfile(path)  # noqa: S606 - Windows only, intentional
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    def open_url(self, url):
        """Open an external URL in the system default browser."""
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return {"error": "Invalid URL"}
        try:
            import webbrowser
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    # ── Cookie management (Twitter auth) ────────────────────────────

    def validate_cookies(self, path):
        return validate_cookies_file(path)

    def get_browsers(self):
        return get_available_browsers()

    def extract_cookies(self, browser):
        return extract_from_browser(browser)

    # ── Window geometry ─────────────────────────────────────────────

    def save_window_geometry(self, width, height, x=None, y=None):
        """Persist the window size/position so it can be restored next launch."""
        try:
            state = self.load_state()
            window = state.get("window", {}) or {}
            if width:
                window["width"] = int(width)
            if height:
                window["height"] = int(height)
            # When the window is minimized (or mid-close) Windows reports a
            # large-negative sentinel position (~-32000). Don't persist that,
            # or the window restores off-screen and looks like it never opened.
            if x is not None and y is not None and int(x) > -10000 and int(y) > -10000:
                window["x"] = int(x)
                window["y"] = int(y)
            state["window"] = window
            self.save_state(state)
        except Exception:
            pass
        return {"ok": True}

    # ── Link parsing ────────────────────────────────────────────────

    def resolve_link(self, url):
        """Validate/parse a pasted link for the Configure Links overlay.

        Returns {valid, platform, ...platform fields, url} or {valid: False}.
        """
        url = (url or "").strip()
        cf = parse_creator_url(url)
        if cf:
            return {"valid": True, "platform": "coomerfans", "service": cf["service"],
                    "user_id": cf["user_id"], "name": cf["name"], "url": url}
        if "pawchive" in url.lower():
            pw = pw_parse_creator_url(url)
            if pw:
                # Best-effort name lookup — the URL doesn't carry the artist name.
                name = ""
                try:
                    name = pw_fetch_profile(
                        pw_make_session(), pw["service"], pw["user_id"]).get("name") or ""
                except Exception:
                    pass
                return {"valid": True, "platform": "pawchive", "service": pw["service"],
                        "user_id": pw["user_id"], "name": name, "url": url}
        username = self._extract_username(url)
        if username:
            return {"valid": True, "platform": "twitter", "username": username, "url": url}
        return {"valid": False}

    def _link_from_url(self, url):
        """Build a normalized link record from a URL, or None if unrecognized."""
        info = self.resolve_link(url)
        if not info.get("valid"):
            return None
        if info["platform"] == "coomerfans":
            return {"platform": "coomerfans", "service": info["service"],
                    "user_id": info["user_id"], "name": info["name"], "url": info["url"]}
        if info["platform"] == "pawchive":
            return {"platform": "pawchive", "service": info["service"],
                    "user_id": info["user_id"], "name": info["name"], "url": info["url"]}
        return {"platform": "twitter", "username": info["username"], "url": info["url"]}

    # ── Creator CRUD ────────────────────────────────────────────────

    @staticmethod
    def _summary(links):
        s = {"onlyfans": 0, "fansly": 0, "twitter": 0, "patreon": 0, "fanbox": 0}
        for l in links or []:
            if l.get("platform") == "twitter":
                s["twitter"] += 1
            elif l.get("platform") in ("coomerfans", "pawchive"):
                svc = l.get("service")
                s[svc] = s.get(svc, 0) + 1
        return s

    # Parent library folder → display category. Anything else falls back to the
    # folder's own (title-cased) name, so new roots categorize themselves.
    _CATEGORY_MAP = {"creators": "Real", "furry": "Furry"}

    def _derive_category(self, destination):
        d = self._fwd(destination)
        if "/" not in d:
            return ""
        parent = d.rsplit("/", 1)[0].rsplit("/", 1)[-1]   # basename of dirname
        return self._CATEGORY_MAP.get(parent.lower(), parent.title())

    def _category_of(self, c):
        return c.get("category") or self._derive_category(c.get("destination", ""))

    def list_creators(self):
        """Recently used creators, most-recent first, with a platform summary."""
        creators = self.load_state().get("creators", {})
        out = []
        for cid, c in creators.items():
            out.append({
                "id": cid,
                "name": c.get("name") or self._basename(c.get("destination", "")),
                "destination": c.get("destination", ""),
                "category": self._category_of(c),
                "summary": self._summary(c.get("links", [])),
                "link_count": len(c.get("links", [])),
                "last_used": c.get("last_used", ""),
            })
        out.sort(key=lambda a: a["last_used"], reverse=True)
        return out

    def get_creator(self, creator_id):
        c = self.load_state().get("creators", {}).get(creator_id)
        if not c:
            return None
        return {
            "id": creator_id,
            "name": c.get("name") or self._basename(c.get("destination", "")),
            "destination": c.get("destination", ""),
            "category": self._category_of(c),
            "avatar": c.get("avatar", ""),
            "has_videos": bool(c.get("has_videos")),
            "links": c.get("links", []),
        }

    @staticmethod
    def _avatar_account(creator):
        """The coomerfans account whose avatar represents the creator. Honors a
        per-creator `avatar` override ("<service>_<user_id>") if it still matches
        a link, else falls back to priority OnlyFans → Fansly → other. Returns
        (service, user_id) or None (e.g. a twitter-only creator)."""
        cf = [l for l in creator.get("links", []) if l.get("platform") == "coomerfans"]
        if not cf:
            return None
        override = creator.get("avatar")
        if override:
            for l in cf:
                if f"{l.get('service')}_{l.get('user_id')}" == override:
                    return (l.get("service"), l.get("user_id"))
        priority = {"onlyfans": 0, "fansly": 1}
        cf.sort(key=lambda l: priority.get(l.get("service"), 2))
        return (cf[0].get("service"), cf[0].get("user_id"))

    def set_creator_avatar(self, creator_id, key):
        """Pick which coomerfans account's icon represents the creator.
        key = "<service>_<user_id>" (or "" to revert to the default)."""
        state = self.load_state()
        creators = state.get("creators", {})
        c = creators.get(creator_id)
        if not c:
            return {"error": "Creator not found"}
        valid = {f"{l.get('service')}_{l.get('user_id')}"
                 for l in c.get("links", []) if l.get("platform") == "coomerfans"}
        if key and key not in valid:
            return {"error": "Unknown account"}
        c["avatar"] = key or ""
        creators[creator_id] = c
        state["creators"] = creators
        self.save_state(state)
        return {"ok": True, "avatar": c["avatar"]}

    def get_creator_avatar(self, creator_id):
        """Return the creator's coomerfans profile image as a base64 data URI,
        or {"none": True}. Cached under .avatars/ after the first fetch."""
        c = self.load_state().get("creators", {}).get(creator_id)
        if not c:
            return {"none": True}
        acct = self._avatar_account(c)
        # Fall back to a pawchive account so pawchive-only creators still get an
        # avatar (icon endpoint: /icons/{service}/{user_id}).
        if not acct:
            pw = next((l for l in c.get("links", []) if l.get("platform") == "pawchive"), None)
            if pw:
                return self._pawchive_avatar(pw.get("service"), pw.get("user_id"))
            return {"none": True}
        service, user_id = acct
        cache = os.path.join(AVATAR_DIR, f"coomerfans_{service}_{user_id}.jpg")

        data = None
        try:
            if os.path.isfile(cache) and os.path.getsize(cache) > 0:
                with open(cache, "rb") as f:
                    data = f.read()
            else:
                r = make_session().get(f"{BASE}/istorage/{user_id}.jpg", timeout=(15, 30))
                ctype = (r.headers.get("Content-Type") or "").lower()
                if r.status_code == 200 and r.content and "image" in ctype:
                    data = r.content
                    try:
                        os.makedirs(AVATAR_DIR, exist_ok=True)
                        with open(cache, "wb") as f:
                            f.write(data)
                    except OSError:
                        pass
        except Exception:
            data = None

        if not data:
            return {"none": True}
        return {"data": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}

    def _pawchive_avatar(self, service, user_id):
        """A pawchive creator's icon as a base64 data URI (cached), or {none}."""
        if not service or not user_id:
            return {"none": True}
        cache = os.path.join(AVATAR_DIR, f"pawchive_{service}_{user_id}.jpg")
        data = None
        try:
            if os.path.isfile(cache) and os.path.getsize(cache) > 0:
                with open(cache, "rb") as f:
                    data = f.read()
            else:
                r = pw_make_session().get(
                    f"{PW_BASE}/icons/{service}/{user_id}", timeout=(15, 30))
                ctype = (r.headers.get("Content-Type") or "").lower()
                if r.status_code == 200 and r.content and "image" in ctype:
                    data = r.content
                    try:
                        os.makedirs(AVATAR_DIR, exist_ok=True)
                        with open(cache, "wb") as f:
                            f.write(data)
                    except OSError:
                        pass
        except Exception:
            data = None
        if not data:
            return {"none": True}
        return {"data": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}

    # ── Pawchive external-links manifest ────────────────────────────

    def _pawchive_links_path(self, creator):
        return os.path.join(creator.get("destination", ""), "_pawchive_links.json")

    def list_pending_links(self, creator_id):
        """Outstanding external links (manual downloads + failed auto-grabs) for a
        creator's pawchive posts. reachable:false when the destination folder
        can't be read (e.g. the NAS is offline)."""
        c = self.load_state().get("creators", {}).get(creator_id)
        if not c:
            return {"reachable": False, "items": [], "counts": {}, "error": "Creator not found"}
        has_pw = any(l.get("platform") == "pawchive" for l in c.get("links", []))
        if not has_pw:
            return {"reachable": True, "items": [], "counts": {}, "pawchive": False}
        dest = c.get("destination", "")
        if not dest or not os.path.isdir(dest):
            return {"reachable": False, "items": [], "counts": {}, "pawchive": True}
        try:
            pl = PawchiveLinks(self._pawchive_links_path(c))
            return {"reachable": True, "pawchive": True,
                    "items": pl.pending(), "counts": pl.counts()}
        except Exception as e:
            return {"reachable": False, "items": [], "counts": {}, "error": str(e)}

    def set_link_resolved(self, creator_id, key, resolved=True):
        """Mark a pending external link resolved (the UI checkbox) so it stops
        appearing; persisted so re-scans never resurrect it."""
        c = self.load_state().get("creators", {}).get(creator_id)
        if not c:
            return {"error": "Creator not found"}
        dest = c.get("destination", "")
        if not dest or not os.path.isdir(dest):
            return {"error": "Destination folder is not reachable"}
        try:
            pl = PawchiveLinks(self._pawchive_links_path(c))
            ok = pl.mark_resolved(key, bool(resolved))
            if not ok:
                return {"error": "Link not found"}
            return {"ok": True, "resolved": bool(resolved), "counts": pl.counts()}
        except Exception as e:
            return {"error": str(e)}

    def save_creator(self, creator):
        """Create or update a creator (from the Configure Links overlay).

        Re-parses each link's URL so platform/service/user_id/username are
        authoritative, dedupes, re-keys by normalized destination, and creates
        the folder. Does not touch downloaded files or archive DBs.
        """
        creator = creator or {}
        destination = self._fwd(creator.get("destination"))
        if not destination:
            return {"error": "Destination folder is required"}

        links, seen = [], set()
        for l in creator.get("links", []):
            built = self._link_from_url((l or {}).get("url", ""))
            if not built:
                continue
            key = built["url"].lower()
            if key in seen:
                continue
            seen.add(key)
            links.append(built)

        name = (creator.get("name") or "").strip() or self._basename(destination)
        category = (creator.get("category") or "").strip() or self._derive_category(destination)
        new_id = self._norm_dest(destination)

        try:
            os.makedirs(destination, exist_ok=True)
        except OSError:
            pass

        state = self.load_state()
        creators = state.get("creators", {})
        old_id = creator.get("id")
        if old_id and old_id != new_id:
            creators.pop(old_id, None)
        existing = creators.get(new_id, {})
        # Preserve a chosen icon if that account still has a link.
        avatar = creator.get("avatar") or existing.get("avatar") or ""
        valid_accts = {f"{l['service']}_{l['user_id']}" for l in links if l["platform"] == "coomerfans"}
        if avatar not in valid_accts:
            avatar = ""
        creators[new_id] = {
            "name": name,
            "destination": destination,
            "category": category,
            "avatar": avatar,
            "has_videos": bool(creator.get("has_videos")),
            "links": links,
            "last_used": existing.get("last_used") or datetime.now(timezone.utc).isoformat(),
        }
        state["creators"] = creators
        self.save_state(state)
        return {"id": new_id, "name": name}

    def delete_creator(self, creator_id):
        """Forget a creator's mapping. Never deletes files or archive DBs."""
        state = self.load_state()
        creators = state.get("creators", {})
        if creators.pop(creator_id, None) is not None:
            state["creators"] = creators
            self.save_state(state)
            return {"ok": True}
        return {"ok": False}

    def _touch_creator(self, creator_id):
        try:
            state = self.load_state()
            creators = state.get("creators", {})
            if creator_id in creators:
                creators[creator_id]["last_used"] = datetime.now(timezone.utc).isoformat()
                state["creators"] = creators
                self.save_state(state)
        except Exception:
            pass

    @staticmethod
    def _years_in(base, images_name):
        """4-digit year folders directly under `base` and under base/<images_name>."""
        years = set()
        year_re = re.compile(r"^\d{4}$")

        def scan(d):
            try:
                for e in os.listdir(d):
                    if year_re.match(e) and os.path.isdir(os.path.join(d, e)):
                        years.add(int(e))
            except OSError:
                pass

        if base and os.path.isdir(base):
            scan(base)
            sub = os.path.join(base, images_name)
            if os.path.isdir(sub):
                scan(sub)
        return years

    def get_creator_years(self, creator_id):
        """Years present across the creator's coomerfans subtree and twitter subtree."""
        c = self.load_state().get("creators", {}).get(creator_id)
        if not c:
            return []
        dest = c.get("destination", "")
        years = self._years_in(dest, "images")                       # coomerfans
        years |= self._years_in(os.path.join(dest, "twitter"), "Images")  # twitter
        return sorted(years, reverse=True)

    # ── In-app media browser/player ─────────────────────────────────

    def _thumbs_dir(self, state=None):
        state = state or self.load_state()
        ad = (state.get("archive_dir") or "").strip()
        return os.path.join(ad, ".thumbs") if ad else os.path.join(APP_DIR, ".thumbs")

    def _configure_media(self, state):
        roots = [c.get("destination", "") for c in (state.get("creators") or {}).values()
                 if c.get("destination")]
        self._media.configure(roots, self._thumbs_dir(state))

    def media_base_url(self):
        """Base URL + token the frontend uses to build /media and /thumb requests."""
        self._media.start()
        self._configure_media(self.load_state())
        return {"base": self._media.base_url(), "token": self._media.token}

    def list_creator_media(self, creator_id):
        """A creator's media items (newest first), or reachable:false if the
        destination folder can't be read (e.g. the NAS is offline)."""
        state = self.load_state()
        c = state.get("creators", {}).get(creator_id)
        if not c:
            return {"reachable": False, "items": [], "error": "Creator not found"}
        self._media.start()
        self._configure_media(state)
        res = scan_creator_media(c.get("destination", ""))
        res["name"] = c.get("name") or self._basename(c.get("destination", ""))
        res["destination"] = c.get("destination", "")
        return res

    # ── Download orchestration ──────────────────────────────────────

    def start_creator_download(self, creator_id, scope="everything", mode="full", year=None):
        """Run a download across a scope of the creator's links.

        scope ∈ {everything, twitter, coomerfans, onlyfans, fansly, pawchive,
                 patreon, fanbox}
        mode  ∈ {full, latest, redownload_year}
        """
        if self._creator_runner and self._creator_runner.is_running:
            return {"error": "A download is already in progress"}

        state = self.load_state()
        c = state.get("creators", {}).get(creator_id)
        if not c:
            return {"error": "Creator not found"}
        if not c.get("links"):
            return {"error": "This creator has no links yet — add some in Configure Links."}
        if mode == "redownload_year" and not year:
            return {"error": "Select a year to redownload"}
        if not filter_links(c["links"], scope):
            return {"error": f"No '{scope}' links for this creator."}

        try:
            workers = max(3, min(10, int(state.get("cf_concurrency") or 5)))
        except (TypeError, ValueError):
            workers = 5

        self._touch_creator(creator_id)
        self._creator_runner = CreatorRunner(workers=workers)
        self._creator_thread = threading.Thread(
            target=self._run_creator_download,
            args=(c, scope, mode, year),
            daemon=True,
        )
        self._creator_thread.start()
        return {"status": "started", "mode": mode, "scope": scope}

    def _run_creator_download(self, creator, scope, mode, year):
        state = self.load_state()
        self._creator_runner.run(
            creator=creator,
            scope=scope,
            mode=mode,
            year=year,
            archive_dir=(state.get("archive_dir") or "").strip(),
            cookies_path=state.get("cookies_path") or "",
            cookies_browser=state.get("cookies_browser") or "",
            on_progress=lambda d: self._push_js("onCreatorProgress", d),
            on_complete=lambda d: self._push_js("onCreatorComplete", d),
            on_error=lambda d: self._push_js("onCreatorError", d),
        )

    def cancel_creator_download(self):
        self._aux_cancel.set()   # stops a verify/repair in progress
        if self._creator_runner and self._creator_runner.is_running:
            self._creator_runner.cancel()
        return {"status": "cancelling"}

    def get_creator_status(self):
        r = self._creator_runner
        if r:
            return {
                "running": r.is_running,
                "downloaded": r.downloaded_count,
                "skipped": r.skipped_count,
                "errors": r.error_count,
            }
        return {"running": False, "downloaded": 0, "skipped": 0, "errors": 0}

    def _aux_busy(self):
        return ((self._creator_runner and self._creator_runner.is_running)
                or (self._creator_aux_thread and self._creator_aux_thread.is_alive()))

    @staticmethod
    def _tag(link, data):
        d = dict(data)
        if d.get("message"):
            d["message"] = f"[{link_label(link)}] {d['message']}"
        return d

    # ── Verify & Repair (coomerfans links only) ─────────────────────

    def start_creator_verify(self, creator_id):
        """Scan a creator's coomerfans files for present-but-broken media across
        all of their coomerfans links. Missing files are ignored (intentional
        deletions)."""
        if self._aux_busy():
            return {"error": "An operation is already in progress"}

        state = self.load_state()
        c = state.get("creators", {}).get(creator_id)
        if not c:
            return {"error": "Creator not found"}
        cf_links = [l for l in c.get("links", []) if l.get("platform") == "coomerfans"]
        if not cf_links:
            return {"error": "This creator has no coomerfans links to verify."}

        archive_dir = (state.get("archive_dir") or "").strip()
        self._aux_cancel.clear()
        self._verify_broken = []
        self._creator_aux_thread = threading.Thread(
            target=self._run_creator_verify, args=(c, cf_links, archive_dir), daemon=True)
        self._creator_aux_thread.start()
        return {"status": "started"}

    def _run_creator_verify(self, creator, cf_links, archive_dir):
        try:
            session = make_session()
            dest = creator["destination"]
            checked = present = 0
            items = []
            for link in cf_links:
                if self._aux_cancel.is_set():
                    break
                archive_path = cf_archive_path(archive_dir, link, dest)
                if not os.path.isfile(archive_path):
                    self._push_js("onCreatorProgress", self._tag(
                        link, {"type": "info", "message": "no archive — nothing to verify"}))
                    continue
                res = find_broken(
                    archive_path, dest, link["service"], link["user_id"], session,
                    on_progress=lambda d, _l=link: self._push_js("onCreatorProgress", self._tag(_l, d)),
                    should_cancel=self._aux_cancel.is_set,
                )
                checked += res["checked"]
                present += res["present"]
                if res["broken"]:
                    self._verify_broken.append({"link": link, "broken": res["broken"]})
                    for b in res["broken"]:
                        items.append({"filename": b["filename"], "reason": b["reason"]})
            self._push_js("onCreatorVerifyResult", {
                "checked": checked, "present": present, "count": len(items),
                "items": items, "cancelled": self._aux_cancel.is_set(),
            })
        except Exception as e:
            self._push_js("onCreatorVerifyResult", {"error": str(e)})

    def start_creator_repair(self, creator_id):
        """Re-download the present-but-broken files found by the last verify."""
        if self._aux_busy():
            return {"error": "An operation is already in progress"}
        if not self._verify_broken:
            return {"error": "Nothing to repair — run Verify first."}

        state = self.load_state()
        c = state.get("creators", {}).get(creator_id)
        if not c:
            return {"error": "Creator not found"}
        archive_dir = (state.get("archive_dir") or "").strip()
        self._aux_cancel.clear()
        self._creator_aux_thread = threading.Thread(
            target=self._run_creator_repair, args=(c, archive_dir), daemon=True)
        self._creator_aux_thread.start()
        count = sum(len(g["broken"]) for g in self._verify_broken)
        return {"status": "started", "count": count}

    def _run_creator_repair(self, creator, archive_dir):
        session = make_session()
        dest = creator["destination"]
        total_repaired = total_bad = 0
        try:
            for group in self._verify_broken:
                if self._aux_cancel.is_set():
                    break
                link, broken = group["link"], group["broken"]
                archive_path = cf_archive_path(archive_dir, link, dest)
                runner = CoomerfansRunner()
                runner._session = session
                runner._destination = dest
                runner._service = link["service"]
                runner._archive = CfArchive(archive_path)
                runner._on_progress = lambda d, _l=link: self._push_js(
                    "onCreatorProgress", self._tag(_l, d))
                runner._cancel_event = self._aux_cancel   # share cancel
                runner._running = True
                try:
                    res = repair_broken(
                        broken, dest, link["service"], link["user_id"], runner, session,
                        on_progress=lambda d, _l=link: self._push_js(
                            "onCreatorProgress", self._tag(_l, d)),
                        should_cancel=self._aux_cancel.is_set,
                    )
                    total_repaired += res["repaired"]
                    total_bad += res["still_bad"]
                finally:
                    runner._running = False
                    try:
                        runner._archive.close()
                    except Exception:
                        pass
            self._verify_broken = []
            self._push_js("onCreatorRepairComplete", {
                "repaired": total_repaired, "still_bad": total_bad,
                "cancelled": self._aux_cancel.is_set(),
            })
        except Exception as e:
            self._push_js("onCreatorRepairComplete", {
                "repaired": total_repaired, "still_bad": total_bad, "error": str(e)})

    # ── JS bridge helper ───────────────────────────────────────────

    def _push_js(self, func_name, data):
        if self._window:
            try:
                js_data = json.dumps(data)
                self._window.evaluate_js(f"window.{func_name}({js_data})")
            except Exception:
                pass
