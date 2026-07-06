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
