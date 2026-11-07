import json
import os
import tempfile
import uuid


# Twitter/X media downloads (pbs.twimg.com) are fairly forgiving, so pace the
# extractor lighter than the old 1.0s. This is gallery-dl's own pacing and is
# entirely independent of the pawchive adaptive throttle. Lower = faster; raise
# toward 1.0 if X starts 429'ing.
TWITTER_SLEEP_REQUEST = 0.5


def _base_config(destination, cookies_path=None, cookies_browser=None):
    """Build the base gallery-dl config dict.

    gallery-dl writes every file into a flat <year> folder here. It CANNOT split
    images vs videos into different folders itself: the download directory is
    chosen per-tweet (on gallery-dl's Message.Directory), before any file's
    extension/type is known, so a conditional like "extension in (...)" never
    matches. Instead, the runner moves images into Images/<year> after the
    download (see archive_sync.reorganize_twitter_media), giving the same
    images→Images/<year>, videos/gifs→<year> layout as coomerfans/pawchive."""
    directory = ["{date:%Y}"]

    twitter_config = {
        "retweets": False,
        "replies": False,
        "quoted": False,
        "videos": True,
        "filename": "{date:%Y.%m.%d} - Twitter - {tweet_id}_{num}.{extension}",
        "directory": directory,
        "sleep-request": TWITTER_SLEEP_REQUEST,
    }

    if cookies_path:
        twitter_config["cookies"] = cookies_path.replace("\\", "/")
    elif cookies_browser:
        twitter_config["cookies-from-browser"] = cookies_browser

    config = {
        "extractor": {
            "base-directory": destination.replace("\\", "/"),
            "twitter": twitter_config,
        },
        "downloader": {
            "retries": 10,
            "timeout": 60.0,
            "rate": None,
        },
    }

    return config


def build_config(destination, cookies_path=None, cookies_browser=None):
    """Build a gallery-dl config for full artist download. Returns temp config file path."""
    config = _base_config(destination, cookies_path, cookies_browser)
    return _write_temp_config(config)


def build_latest_config(destination, cookies_path=None, cookies_browser=None, latest_year=2026):
    """Build a gallery-dl config for fetching latest posts only. Returns temp config file path."""
    config = _base_config(destination, cookies_path, cookies_browser)
    config["extractor"]["twitter"]["post-filter"] = f"date.year >= {latest_year}"
    return _write_temp_config(config)


def build_redownload_config(destination, year, cookies_path=None, cookies_browser=None):
    """Build a gallery-dl config for redownloading a specific year.

    No archive is used — gallery-dl's file-existence check (skip: true)
    ensures only missing files are downloaded.
    """
    config = _base_config(destination, cookies_path, cookies_browser)
    config["extractor"]["twitter"]["post-filter"] = f"date.year == {year}"
    return _write_temp_config(config)


# gallery-dl's own pacing for Bluesky API calls. The AppView allows ~3000
# requests / 5 min per IP and blobs come from the account's PDS, so this is only
# politeness; 429s are already waited out by the extractor (RateLimit-Reset).
BLUESKY_SLEEP_REQUEST = 0.3

# 'Fetch Latest' stops once this many files in a row are already in the
# archive (the feed is newest first, so everything past that is known).
BLUESKY_LATEST_ABORT = 20


def build_bluesky_config(destination, mode="full", year=None, username=None,
                         password=None, api_server=None, images=True, videos=True):
    """gallery-dl config for one Bluesky profile (Creator tab).

    Same layout and naming as Twitter: everything lands flat in <year>/ as
    `YYYY.MM.DD - Bluesky - <post id>_<n>.<ext>` (post date, UTC, like
    Twitter's), and the runner then moves images into Images/<year>/
    (archive_sync.reorganize_twitter_media).

    The profile URL dispatches to gallery-dl's `media` feed (posts with media,
    the author's own; reposts and quoted posts are skipped). The archive key is
    pinned to `bluesky_<post id>_<n>` so it can be rebuilt from filenames on
    disk (archive_sync.sync_archive(site="bluesky")). The default key is the
    blob CID, which isn't in the filename.

    gallery-dl's login cache is turned off: it keeps a refresh token per
    username for 84 days and never falls back to the password when that token
    is revoked. A replaced app password would then break every run. Without
    the cache it logs in once per run instead.
    """
    bsky = {
        "filename": "{date:%Y.%m.%d} - Bluesky - {post_id}_{num}.{extension}",
        "directory": ["{date:%Y}"],
        "videos": bool(videos),
        "quoted": False,
        "reposts": False,
        "archive-format": "{post_id}_{num}",
        "archive-prefix": "bluesky_",
        "sleep-request": BLUESKY_SLEEP_REQUEST,
    }
    if not images:
        # Only video blobs (always mp4 on Bluesky).
        bsky["image-filter"] = "extension == 'mp4'"
    if username and password:
        bsky["username"] = username
        bsky["password"] = password
        if api_server:
            bsky["api-server"] = api_server
    if mode == "latest":
        bsky["skip"] = f"abort:{BLUESKY_LATEST_ABORT}"
    elif mode == "redownload_year" and year:
        bsky["post-filter"] = f"date.year == {int(year)}"

    config = {
        "cache": {"file": None},
        "extractor": {
            "base-directory": destination.replace("\\", "/"),
            "bluesky": bsky,
        },
        "downloader": {
            "retries": 10,
            "timeout": 60.0,
            "rate": None,
        },
    }
    return _write_temp_config(config)


def build_album_config(destination, cookies_path=None, cookies_browser=None,
                       filester_cookies=None):
    """Build a gallery-dl config for album downloads (Albums tab).

    Used as the sole engine for filester and as the fallback engine for
    bunkr/cyberdrop. gallery-dl auto-detects the extractor from the URL, so we
    only set: a base directory, a per-extractor `directory` template that drops
    each album into its own titled subfolder under `destination`, and a
    generous retry budget to soak up bunkr's flaky files. Original filenames are
    kept (no `filename` override).

    The per-site `directory` keys mirror each extractor's own metadata (verified
    via `gallery-dl -j`): bunkr/cyberdrop expose `album_name`; filester exposes
    `folder_name` (+ `folder_id`, appended since folder_name can be empty).

    `filester_cookies` is a per-link Netscape cookies.txt from unlocking a
    password-protected filester folder (see backend/filester_unlock). filester is
    pinned to `domain: "auto"` so gallery-dl talks to the link's own host — the
    host the unlock cookie is scoped to (e.g. filester.gg, not the default .me).
    Returns a temp config file path.
    """
    def _cookies(block):
        if cookies_path:
            block["cookies"] = cookies_path.replace("\\", "/")
        elif cookies_browser:
            block["cookies-from-browser"] = cookies_browser
        return block

    filester = _cookies({
        "directory": ["{folder_name} ({folder_id})"],
        "domain": "auto",
    })
    if filester_cookies:
        filester["cookies"] = filester_cookies.replace("\\", "/")

    config = {
        "extractor": {
            "base-directory": destination.replace("\\", "/"),
            "bunkr": _cookies({"directory": ["{album_name}"]}),
            "cyberdrop": _cookies({"directory": ["{album_name}"]}),
            "filester": filester,
        },
        "downloader": {
            "retries": 15,
            "timeout": 60.0,
            "rate": None,
        },
    }
    return _write_temp_config(config)


def _write_temp_config(config):
    """Write config dict to a temp JSON file and return its path."""
    temp_dir = tempfile.gettempdir()
    filename = f"downloader-config-{uuid.uuid4().hex[:8]}.json"
    config_path = os.path.join(temp_dir, filename)

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    return config_path


def cleanup_config(config_path):
    """Delete a temporary config file."""
    try:
        if config_path and os.path.isfile(config_path):
            os.unlink(config_path)
    except OSError:
        pass
