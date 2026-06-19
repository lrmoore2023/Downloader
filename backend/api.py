import json
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timezone

import webview

from backend.cookie_manager import validate_cookies_file, extract_from_browser, get_available_browsers
from backend.config_builder import build_config, build_latest_config, build_redownload_config, cleanup_config
from backend.gallery_dl_runner import GalleryDlRunner
from backend.file_scanner import scan_destination
from backend.archive_sync import sync_archive, sync_archive_for_year
from backend.coomerfans_runner import CoomerfansRunner
from backend.coomerfans_scraper import parse_creator_url, make_session
from backend.coomerfans_verify import find_broken, repair_broken
from backend.coomerfans_archive import Archive as CfArchive

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(APP_DIR, "app_state.json")


class Api:
    def __init__(self):
        self._window = None
        self._runner = None
        self._download_thread = None
        self._download_context = None  # tracks info for post-download sync
        self._cf_runner = None
        self._cf_thread = None
        self._cf_aux_thread = None          # verify/repair worker
        self._cf_cancel = threading.Event()  # shared cancel for verify/repair
        self._cf_last_broken = []            # result of last verify, for repair
        self._state_lock = threading.Lock()  # serialize app_state.json writes

    def set_window(self, window):
        self._window = window

    # ── State persistence ──────────────────────────────────────────

    def _default_state(self):
        return {
            "cookies_path": "",
            "cookies_browser": "",
            "auth_method": "file",
            "last_url": "",
            "last_destination": "",
            "library_root": "",
            "artist_map": {},
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
        # keys like artist_map / window) never clobber data managed elsewhere.
        # Locked + atomic (write temp, then os.replace) so concurrent saves from
        # the UI / download / window-geometry threads can't corrupt the file.
        with self._state_lock:
            merged = self.load_state()
            merged.update(state or {})
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE_FILE), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(merged, f, indent=2)
                os.replace(tmp, STATE_FILE)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

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

    # ── Cookie management ──────────────────────────────────────────

    def validate_cookies(self, path):
        return validate_cookies_file(path)

    def get_browsers(self):
        return get_available_browsers()

    def extract_cookies(self, browser):
        return extract_from_browser(browser)

    # ── Archive directory management ─────────────────────────────────

    @staticmethod
    def _extract_username(url):
        """Extract the Twitter/X username from a URL."""
        m = re.search(r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)", url)
        return m.group(1).lower() if m else None

    def _resolve_archive_path(self, url, destination, archive_dir):
        """Return the archive DB path for a given artist."""
        archive_dir = (archive_dir or "").strip()
        if archive_dir and os.path.isdir(archive_dir):
            username = self._extract_username(url) or "unknown"
            return os.path.join(archive_dir, f"twitter_{username}.db")
        return os.path.join(destination, ".gallery-dl-archive.db")

    def _record_artist_mapping(self, url, destination, archive_dir):
        """Record which username maps to which destination, for cleanup + recall."""
        username = self._extract_username(url)
        if not username or not (destination or "").strip():
            return
        try:
            state = self.load_state()
            artist_map = state.get("artist_map", {})
            artist_map[username] = {
                "destination": destination.replace("\\", "/"),
                "archive_dir": (archive_dir or "").strip(),
                "last_used": datetime.now(timezone.utc).isoformat(),
            }
            state["artist_map"] = artist_map
            self.save_state(state)
        except Exception:
            pass

    def record_artist(self, url, destination, archive_dir=""):
        """Public wrapper so the UI can persist a mapping as soon as a folder is chosen."""
        self._record_artist_mapping(url, destination, archive_dir)
        return {"username": self._extract_username(url)}

    def resolve_destination(self, url):
        """Resolve a destination folder for an artist URL.

        Returns {username, destination, source} where source is one of:
            remembered  - a previously saved folder for this artist
            suggested   - <library_root>/<username> (folder may not exist yet)
            none        - could not determine a destination
        """
        username = self._extract_username(url)
        if not username:
            return {"username": None, "destination": "", "source": "none"}

        state = self.load_state()
        artist_map = state.get("artist_map", {})

        info = artist_map.get(username)
        if info and info.get("destination"):
            return {
                "username": username,
                "destination": info["destination"],
                "source": "remembered",
            }

        library_root = (state.get("library_root") or "").strip()
        if library_root:
            dest = os.path.join(library_root, username).replace("\\", "/")
            return {"username": username, "destination": dest, "source": "suggested"}

        return {"username": username, "destination": "", "source": "none"}

    def list_artists(self):
        """Return recently used artists, most-recent first."""
        state = self.load_state()
        artist_map = state.get("artist_map", {})
        artists = [
            {
                "username": username,
                "destination": info.get("destination", ""),
                "last_used": info.get("last_used", ""),
            }
            for username, info in artist_map.items()
            if info.get("destination")
        ]
        artists.sort(key=lambda a: a["last_used"], reverse=True)
        return artists

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

    def save_window_geometry(self, width, height, x=None, y=None):
        """Persist the window size/position so it can be restored next launch."""
        try:
            state = self.load_state()
            window = state.get("window", {}) or {}
            if width:
                window["width"] = int(width)
            if height:
                window["height"] = int(height)
            if x is not None:
                window["x"] = int(x)
            if y is not None:
                window["y"] = int(y)
            state["window"] = window
            self.save_state(state)
        except Exception:
            pass
        return {"ok": True}

    def move_archives(self, old_dir, new_dir):
        """Move archive DB files from old_dir to new_dir."""
        old_dir = (old_dir or "").strip()
        new_dir = (new_dir or "").strip()
        if not new_dir:
            return {"moved": 0, "error": "No new directory specified"}

        os.makedirs(new_dir, exist_ok=True)
        moved = 0

        if old_dir and os.path.isdir(old_dir) and old_dir != new_dir:
            for f in os.listdir(old_dir):
                if f.startswith("twitter_") and f.endswith(".db"):
                    src = os.path.join(old_dir, f)
                    dst = os.path.join(new_dir, f)
                    if os.path.isfile(src) and src != dst:
                        shutil.move(src, dst)
                        moved += 1

        return {"moved": moved}

    def cleanup_orphaned_archives(self, archive_dir):
        """Delete .db files whose artist directories no longer exist."""
        archive_dir = (archive_dir or "").strip()
        if not archive_dir or not os.path.isdir(archive_dir):
            return {"removed": 0}

        try:
            state = self.load_state()
            artist_map = state.get("artist_map", {})
        except Exception:
            return {"removed": 0}

        removed = 0
        usernames_to_remove = []

        for username, info in artist_map.items():
            dest = info.get("destination", "")
            db_path = os.path.join(archive_dir, f"twitter_{username}.db")

            # If the artist destination no longer exists, remove the .db
            if dest and not os.path.isdir(dest) and os.path.isfile(db_path):
                try:
                    os.unlink(db_path)
                    removed += 1
                    usernames_to_remove.append(username)
                except OSError:
                    pass

        # Update artist map
        if usernames_to_remove:
            for u in usernames_to_remove:
                artist_map.pop(u, None)
            state["artist_map"] = artist_map
            self.save_state(state)

        return {"removed": removed, "usernames": usernames_to_remove}

    # ── Download operations ────────────────────────────────────────

    def start_full_download(self, url, destination, cookies_path, cookies_browser, has_videos=True, archive_dir=""):
        if self._runner and self._runner.is_running:
            return {"error": "A download is already in progress"}

        url = url.strip()
        destination = destination.strip()

        if not url:
            return {"error": "URL is required"}
        if not destination:
            return {"error": "Destination folder is required"}

        os.makedirs(destination, exist_ok=True)
        self._record_artist_mapping(url, destination, archive_dir)

        archive_path = self._resolve_archive_path(url, destination, archive_dir)

        config_path = build_config(
            destination=destination,
            cookies_path=cookies_path if not cookies_browser else None,
            cookies_browser=cookies_browser or None,
            has_videos=has_videos,
        )

        self._download_context = {
            "archive_path": archive_path,
            "destination": destination,
            "has_videos": has_videos,
            "sync_type": "full",
        }

        self._runner = GalleryDlRunner()
        self._download_thread = threading.Thread(
            target=self._run_download,
            args=(url, config_path, archive_path),
            daemon=True,
        )
        self._download_thread.start()
        return {"status": "started"}

    def start_latest_download(self, url, destination, cookies_path, cookies_browser, has_videos=True, archive_dir=""):
        if self._runner and self._runner.is_running:
            return {"error": "A download is already in progress"}

        url = url.strip()
        destination = destination.strip()

        if not url:
            return {"error": "URL is required"}
        if not destination:
            return {"error": "Destination folder is required"}

        scan = scan_destination(destination)
        if scan["latest_year"] is None:
            return {"error": "No existing year folders found. Run 'Get Artist Posts' first."}

        self._record_artist_mapping(url, destination, archive_dir)
        archive_path = self._resolve_archive_path(url, destination, archive_dir)

        config_path = build_latest_config(
            destination=destination,
            cookies_path=cookies_path if not cookies_browser else None,
            cookies_browser=cookies_browser or None,
            latest_year=scan["latest_year"],
            has_videos=has_videos,
        )

        self._download_context = {
            "archive_path": archive_path,
            "destination": destination,
            "has_videos": has_videos,
            "sync_type": "full",
        }

        self._runner = GalleryDlRunner()
        self._download_thread = threading.Thread(
            target=self._run_download,
            args=(url, config_path, archive_path),
            daemon=True,
        )
        self._download_thread.start()
        return {"status": "started", "latest_year": scan["latest_year"]}

    def start_redownload_year(self, url, destination, cookies_path, cookies_browser, has_videos=True, archive_dir="", year=None):
        """Redownload all posts for a specific year.

        Runs WITHOUT archive so nothing is skipped by download history.
        After completion, syncs the archive DB to reflect what's on disk.
        """
        if self._runner and self._runner.is_running:
            return {"error": "A download is already in progress"}

        url = url.strip()
        destination = destination.strip()

        if not url:
            return {"error": "URL is required"}
        if not destination:
            return {"error": "Destination folder is required"}
        if not year:
            return {"error": "Select a year to redownload"}

        try:
            year = int(year)
        except (TypeError, ValueError):
            return {"error": "Invalid year"}

        os.makedirs(destination, exist_ok=True)
        self._record_artist_mapping(url, destination, archive_dir)

        archive_path = self._resolve_archive_path(url, destination, archive_dir)

        config_path = build_redownload_config(
            destination=destination,
            year=year,
            cookies_path=cookies_path if not cookies_browser else None,
            cookies_browser=cookies_browser or None,
            has_videos=has_videos,
        )

        # Store context so post-download sync knows what to do
        self._download_context = {
            "archive_path": archive_path,
            "destination": destination,
            "has_videos": has_videos,
            "sync_type": "redownload_year",
            "year": year,
        }

        # No archive passed to runner — gallery-dl won't skip anything
        self._runner = GalleryDlRunner()
        self._download_thread = threading.Thread(
            target=self._run_download,
            args=(url, config_path, None),
            daemon=True,
        )
        self._download_thread.start()
        return {"status": "started", "year": year}

    def get_destination_years(self, destination):
        """Return list of year numbers found in a destination folder."""
        scan = scan_destination(destination)
        years = sorted(scan.get("years", {}).keys(), reverse=True)
        return years

    def _run_download(self, url, config_path, archive_path):
        def on_progress(data):
            self._push_js("onDownloadProgress", data)

        def on_complete(data):
            cleanup_config(config_path)

            # Post-download archive sync
            ctx = self._download_context
            if ctx and not data.get("cancelled"):
                self._post_download_sync(ctx, data)

            self._download_context = None
            self._push_js("onDownloadComplete", data)

        def on_error(data):
            self._push_js("onDownloadError", data)

        self._runner.run(
            url=url,
            config_path=config_path,
            archive_path=archive_path,
            on_progress=on_progress,
            on_complete=on_complete,
            on_error=on_error,
        )

    def _post_download_sync(self, ctx, data):
        """Sync archive DB after a download completes."""
        archive_path = ctx.get("archive_path")
        destination = ctx.get("destination")
        has_videos = ctx.get("has_videos", True)
        sync_type = ctx.get("sync_type")

        if not archive_path or not destination:
            return

        try:
            if sync_type == "redownload_year":
                year = ctx.get("year")
                result = sync_archive_for_year(
                    archive_path, destination, year, has_videos
                )
                self._push_js("onDownloadProgress", {
                    "type": "info",
                    "message": f"Archive synced for {year}: {result.get('added', 0)} entries added",
                })
            else:
                # For full/latest downloads, just ensure new files are tracked
                result = sync_archive(archive_path, destination, has_videos)
                if result.get("added", 0) > 0:
                    self._push_js("onDownloadProgress", {
                        "type": "info",
                        "message": f"Archive synced: {result['added']} entries added",
                    })
        except Exception as e:
            self._push_js("onDownloadProgress", {
                "type": "info",
                "message": f"Archive sync note: {e}",
            })

    def cancel_download(self):
        if self._runner and self._runner.is_running:
            self._runner.cancel()
            return {"status": "cancelling"}
        return {"status": "not_running"}

    def get_download_status(self):
        if self._runner:
            return {
                "running": self._runner.is_running,
                "downloaded": self._runner.downloaded_count,
                "skipped": self._runner.skipped_count,
                "errors": self._runner.error_count,
            }
        return {"running": False, "downloaded": 0, "skipped": 0, "errors": 0}

    # ── Destination scanning ───────────────────────────────────────

    def scan_dest(self, destination):
        return scan_destination(destination)

    # ── Coomerfans tab ─────────────────────────────────────────────

    @staticmethod
    def _cf_key(creator):
        """Stable artist-map key for a coomerfans creator."""
        return f"{creator['service']}_{creator['user_id']}"

    def cf_resolve_url(self, url):
        """Validate/parse a coomerfans creator URL for the frontend."""
        creator = parse_creator_url(url or "")
        if not creator:
            return {"valid": False}
        creator["valid"] = True
        return creator

    def _cf_resolve_archive_path(self, creator, destination, archive_dir):
        archive_dir = (archive_dir or "").strip()
        if archive_dir and os.path.isdir(archive_dir):
            return os.path.join(
                archive_dir,
                f"coomerfans_{creator['service']}_{creator['user_id']}.db",
            )
        return os.path.join(destination, ".coomerfans-archive.db")

    def _cf_record_mapping(self, url, destination):
        creator = parse_creator_url(url or "")
        if not creator or not (destination or "").strip():
            return None
        try:
            state = self.load_state()
            cf_map = state.get("cf_artist_map", {})
            cf_map[self._cf_key(creator)] = {
                "name": creator["name"],
                "service": creator["service"],
                "user_id": creator["user_id"],
                "url": url.strip(),
                "destination": destination.replace("\\", "/"),
                "last_used": datetime.now(timezone.utc).isoformat(),
            }
            state["cf_artist_map"] = cf_map
            self.save_state(state)
        except Exception:
            pass
        return creator

    def cf_record_artist(self, url, destination):
        """Persist a creator→folder mapping as soon as a folder is chosen."""
        creator = self._cf_record_mapping(url, destination)
        return {"name": creator["name"] if creator else None}

    def cf_resolve_destination(self, url):
        """Resolve a destination folder for a creator URL (remembered or
        suggested under cf_library_root)."""
        creator = parse_creator_url(url or "")
        if not creator:
            return {"name": None, "destination": "", "source": "none"}

        state = self.load_state()
        cf_map = state.get("cf_artist_map", {})
        info = cf_map.get(self._cf_key(creator))
        if info and info.get("destination"):
            return {
                "name": creator["name"],
                "destination": info["destination"],
                "source": "remembered",
            }

        root = (state.get("cf_library_root") or "").strip()
        if root:
            dest = os.path.join(root, creator["name"]).replace("\\", "/")
            return {"name": creator["name"], "destination": dest, "source": "suggested"}

        return {"name": creator["name"], "destination": "", "source": "none"}

    def cf_list_artists(self):
        """Return recently used coomerfans creators, most-recent first."""
        state = self.load_state()
        cf_map = state.get("cf_artist_map", {})
        artists = [
            {
                "name": info.get("name", ""),
                "url": info.get("url", ""),
                "destination": info.get("destination", ""),
                "last_used": info.get("last_used", ""),
            }
            for info in cf_map.values()
            if info.get("destination")
        ]
        artists.sort(key=lambda a: a["last_used"], reverse=True)
        return artists

    def cf_get_destination_years(self, destination):
        """Years present in a coomerfans destination (top-level year folders =
        videos; images/<year> = images)."""
        years = set()
        destination = (destination or "").strip()
        if not destination or not os.path.isdir(destination):
            return []
        year_re = re.compile(r"^\d{4}$")
        try:
            for entry in os.listdir(destination):
                if year_re.match(entry) and os.path.isdir(os.path.join(destination, entry)):
                    years.add(int(entry))
            images_dir = os.path.join(destination, "images")
            if os.path.isdir(images_dir):
                for entry in os.listdir(images_dir):
                    if year_re.match(entry) and os.path.isdir(os.path.join(images_dir, entry)):
                        years.add(int(entry))
        except OSError:
            pass
        return sorted(years, reverse=True)

    def start_cf_download(self, url, destination, mode="full", year=None, archive_dir="", workers=5):
        """Start a coomerfans download. mode ∈ {full, latest, redownload_year}.

        workers = simultaneous downloads, clamped to [3, 10].
        """
        if self._cf_runner and self._cf_runner.is_running:
            return {"error": "A download is already in progress"}

        url = (url or "").strip()
        destination = (destination or "").strip()
        creator = parse_creator_url(url)

        if not creator:
            return {"error": "Invalid creator URL (expected /u/{service}/{id}/{name})"}
        if not destination:
            return {"error": "Destination folder is required"}
        if mode == "redownload_year" and not year:
            return {"error": "Select a year to redownload"}
        if mode == "latest" and not self.cf_get_destination_years(destination):
            return {"error": "No existing media found. Run 'Download Everything' first."}

        os.makedirs(destination, exist_ok=True)
        self._cf_record_mapping(url, destination)
        archive_path = self._cf_resolve_archive_path(creator, destination, archive_dir)

        try:
            workers = max(3, min(10, int(workers)))
        except (TypeError, ValueError):
            workers = 5

        self._cf_runner = CoomerfansRunner(workers=workers)
        self._cf_thread = threading.Thread(
            target=self._run_cf_download,
            args=(url, destination, mode, archive_path, year),
            daemon=True,
        )
        self._cf_thread.start()
        return {"status": "started", "mode": mode}

    def _run_cf_download(self, url, destination, mode, archive_path, year):
        self._cf_runner.run(
            creator_url=url,
            destination=destination,
            mode=mode,
            archive_path=archive_path,
            on_progress=lambda d: self._push_js("onCfProgress", d),
            on_complete=lambda d: self._push_js("onCfComplete", d),
            on_error=lambda d: self._push_js("onCfError", d),
            year=year,
        )

    def cancel_cf_download(self):
        self._cf_cancel.set()   # stops a verify/repair in progress
        if self._cf_runner and self._cf_runner.is_running:
            self._cf_runner.cancel()
        return {"status": "cancelling"}

    def _cf_active(self):
        return ((self._cf_runner and self._cf_runner.is_running)
                or (self._cf_aux_thread and self._cf_aux_thread.is_alive()))

    # ── Verify & Repair ────────────────────────────────────────────

    def cf_start_verify(self, url, destination, archive_dir=""):
        """Scan a creator's downloaded files for present-but-broken media.
        Missing files are ignored (treated as intentional deletions)."""
        if self._cf_active():
            return {"error": "A coomerfans operation is already in progress"}

        creator = parse_creator_url((url or "").strip())
        destination = (destination or "").strip()
        if not creator:
            return {"error": "Invalid creator URL"}
        if not destination or not os.path.isdir(destination):
            return {"error": "Destination folder not found"}

        archive_path = self._cf_resolve_archive_path(creator, destination, archive_dir)
        if not os.path.isfile(archive_path):
            return {"error": "No archive found for this creator — nothing to verify."}

        self._cf_cancel.clear()
        self._cf_aux_thread = threading.Thread(
            target=self._run_cf_verify, args=(creator, destination, archive_path), daemon=True)
        self._cf_aux_thread.start()
        return {"status": "started"}

    def _run_cf_verify(self, creator, destination, archive_path):
        try:
            session = make_session()
            result = find_broken(
                archive_path, destination, creator["service"], creator["user_id"], session,
                on_progress=lambda d: self._push_js("onCfProgress", d),
                should_cancel=self._cf_cancel.is_set,
            )
            self._cf_last_broken = result["broken"]
            self._push_js("onCfVerifyResult", {
                "checked": result["checked"],
                "present": result["present"],
                "count": len(result["broken"]),
                "items": [{"filename": b["filename"], "reason": b["reason"]} for b in result["broken"]],
                "cancelled": self._cf_cancel.is_set(),
            })
        except Exception as e:
            self._push_js("onCfVerifyResult", {"error": str(e)})

    def cf_start_repair(self, url, destination, archive_dir=""):
        """Re-download the present-but-broken files found by the last verify."""
        if self._cf_active():
            return {"error": "A coomerfans operation is already in progress"}
        if not self._cf_last_broken:
            return {"error": "Nothing to repair — run Verify first."}

        creator = parse_creator_url((url or "").strip())
        destination = (destination or "").strip()
        if not creator:
            return {"error": "Invalid creator URL"}

        archive_path = self._cf_resolve_archive_path(creator, destination, archive_dir)
        self._cf_cancel.clear()
        self._cf_aux_thread = threading.Thread(
            target=self._run_cf_repair, args=(creator, destination, archive_path), daemon=True)
        self._cf_aux_thread.start()
        return {"status": "started", "count": len(self._cf_last_broken)}

    def _run_cf_repair(self, creator, destination, archive_path):
        broken = self._cf_last_broken
        runner = CoomerfansRunner()
        runner._session = make_session()
        runner._destination = destination
        runner._service = creator["service"]
        runner._archive = CfArchive(archive_path)
        runner._on_progress = lambda d: self._push_js("onCfProgress", d)
        runner._cancel_event = self._cf_cancel   # share cancel
        runner._running = True
        self._cf_runner = runner
        try:
            result = repair_broken(
                broken, destination, creator["service"], creator["user_id"], runner, runner._session,
                on_progress=lambda d: self._push_js("onCfProgress", d),
                should_cancel=self._cf_cancel.is_set,
            )
            self._cf_last_broken = []
            self._push_js("onCfRepairComplete", {
                "repaired": result["repaired"],
                "still_bad": result["still_bad"],
                "cancelled": self._cf_cancel.is_set(),
            })
        except Exception as e:
            self._push_js("onCfRepairComplete", {"repaired": 0, "still_bad": len(broken), "error": str(e)})
        finally:
            runner._running = False
            try:
                runner._archive.close()
            except Exception:
                pass

    def get_cf_status(self):
        if self._cf_runner:
            return {
                "running": self._cf_runner.is_running,
                "downloaded": self._cf_runner.downloaded_count,
                "skipped": self._cf_runner.skipped_count,
                "errors": self._cf_runner.error_count,
            }
        return {"running": False, "downloaded": 0, "skipped": 0, "errors": 0}

    # ── JS bridge helper ───────────────────────────────────────────

    def _push_js(self, func_name, data):
        if self._window:
            try:
                js_data = json.dumps(data)
                self._window.evaluate_js(f"window.{func_name}({js_data})")
            except Exception:
                pass
