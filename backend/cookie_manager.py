import os
import re


def validate_cookies_file(path):
    """Parse a Netscape cookies.txt file and check for Twitter auth_token."""
    if not path or not os.path.isfile(path):
        return {"valid": False, "message": "File not found"}

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return {"valid": False, "message": f"Cannot read file: {e}"}

    has_auth_token = False
    has_twitter_domain = False

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) >= 7:
            domain = parts[0].lower()
            name = parts[5]

            if ".x.com" in domain or ".twitter.com" in domain:
                has_twitter_domain = True
                if name == "auth_token":
                    has_auth_token = True

    if has_auth_token:
        return {"valid": True, "message": "auth_token found - authenticated"}
    elif has_twitter_domain:
        return {"valid": False, "message": "Twitter cookies found but missing auth_token. Log into X first."}
    else:
        return {"valid": False, "message": "No Twitter/X cookies in this file"}


def get_available_browsers():
    """Return list of browsers gallery-dl can extract cookies from."""
    return ["chrome", "firefox", "edge", "brave", "opera", "chromium"]


def extract_from_browser(browser_name):
    """Test that gallery-dl can use cookies from the specified browser."""
    import subprocess
    import sys

    browser_name = re.sub(r"[^a-zA-Z]", "", browser_name)

    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "gallery_dl",
                "--cookies-from-browser", browser_name,
                "-s",  # simulate, don't download
                "https://x.com/i/user/0",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        stderr = result.stderr.lower()
        if "error" in stderr and ("cookie" in stderr or "browser" in stderr):
            return {"valid": False, "message": f"Could not extract cookies from {browser_name}"}

        return {"valid": True, "message": f"Cookies from {browser_name} ready", "browser": browser_name}

    except subprocess.TimeoutExpired:
        return {"valid": False, "message": "Browser cookie extraction timed out"}
    except FileNotFoundError:
        return {"valid": False, "message": "gallery-dl not found. Is it installed?"}
    except Exception as e:
        return {"valid": False, "message": str(e)}
