import json
import os
import tempfile
import uuid


def _base_config(destination, cookies_path=None, cookies_browser=None, has_videos=True):
    """Build the base gallery-dl config dict."""
    # When the artist has videos/gifs, separate images into an Images/ subfolder.
    # When they don't, everything goes directly into year folders.
    if has_videos:
        directory = {
            "extension in ('jpg', 'jpeg', 'png', 'webp')": ["Images", "{date:%Y}"],
            "": ["{date:%Y}"],
        }
    else:
        directory = ["{date:%Y}"]

    twitter_config = {
        "retweets": False,
        "replies": False,
        "quoted": False,
        "videos": True,
        "filename": "{date:%Y.%m.%d} - Twitter - {tweet_id}_{num}.{extension}",
        "directory": directory,
        "sleep-request": 1.0,
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


def build_config(destination, cookies_path=None, cookies_browser=None, has_videos=True):
    """Build a gallery-dl config for full artist download. Returns temp config file path."""
    config = _base_config(destination, cookies_path, cookies_browser, has_videos)
    return _write_temp_config(config)


def build_latest_config(destination, cookies_path=None, cookies_browser=None, latest_year=2026, has_videos=True):
    """Build a gallery-dl config for fetching latest posts only. Returns temp config file path."""
    config = _base_config(destination, cookies_path, cookies_browser, has_videos)
    config["extractor"]["twitter"]["post-filter"] = f"date.year >= {latest_year}"
    return _write_temp_config(config)


def build_redownload_config(destination, year, cookies_path=None, cookies_browser=None, has_videos=True):
    """Build a gallery-dl config for redownloading a specific year.

    No archive is used — gallery-dl's file-existence check (skip: true)
    ensures only missing files are downloaded.
    """
    config = _base_config(destination, cookies_path, cookies_browser, has_videos)
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
