import json
import os
import sys
from wsgiref.simple_server import WSGIServer

import webview

# pywebview serves frontend/ from a wsgiref server whose listen backlog is the
# socketserver default of 5. WebView2 opens more connections than that at page
# load, and on Windows the overflow is refused outright (status 0, no retry) —
# a stylesheet dropped that way leaves the whole UI unstyled. pywebview builds
# its server class internally, so raise the backlog on the base class it uses.
WSGIServer.request_queue_size = 128

# Ensure imports work when running from any directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.api import Api

# Window ground per theme/mode, painted by the native window before the page
# loads. Keep in step with --bg-deep in frontend/css/themes/*.css.
THEME_BACKGROUNDS = {
    ("brutalist", "light"): "#e9e6de",
    ("brutalist", "dark"): "#0a0a0a",
    ("studio", "light"): "#eef1f4",
    ("studio", "dark"): "#0b0e13",
    ("neon", "dark"): "#0a0a0f",
    ("nursery", "light"): "#f7f4f0",
}

# Themes that ship one mode only (ignore the Light/Dark/System setting).
THEME_FIXED_MODE = {"neon": "dark", "nursery": "light"}


def _system_mode():
    """'light' or 'dark' from Windows' app theme setting (dark if unknown)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except Exception:
        return "dark"


def _theme_bootstrap(state, frontend_dir):
    """Hand the saved theme to the page before its first paint.

    pywebview runs private, so the page has no storage that survives a restart,
    and the JS API only exists after load — too late to avoid a flash of the
    default theme. Writing a one-line script the page loads synchronously in
    <head> closes that gap. Returns the window background colour."""
    pref = {
        "theme": state.get("ui_theme") or "brutalist",
        "mode": state.get("ui_mode") or "system",
        "motion": state.get("ui_motion") or "auto",
    }
    try:
        with open(os.path.join(frontend_dir, "js", "theme-pref.js"), "w", encoding="utf-8") as f:
            f.write("window.__THEME_PREF = %s;\n" % json.dumps(pref))
    except OSError:
        pass
    mode = THEME_FIXED_MODE.get(pref["theme"]) or (
        pref["mode"] if pref["mode"] in ("light", "dark") else _system_mode())
    return THEME_BACKGROUNDS.get((pref["theme"], mode), "#0a0a0a")


def main():
    api = Api()
    frontend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
    index_path = os.path.join(frontend_dir, "index.html")

    state = api.load_state()
    background = _theme_bootstrap(state, frontend_dir)

    # Restore saved window geometry (size/position) if present.
    saved = state.get("window", {}) or {}
    width = saved.get("width") or 1100
    height = saved.get("height") or 750

    create_kwargs = dict(
        url=index_path,
        js_api=api,
        width=width,
        height=height,
        min_size=(900, 600),
        background_color=background,
    )
    # Only restore a saved position if it's plausibly on-screen. A minimized
    # or mid-close window reports a large-negative sentinel (~-32000) on
    # Windows; restoring that would open the window where it can't be seen, so
    # fall back to letting the OS center it.
    sx, sy = saved.get("x"), saved.get("y")
    if isinstance(sx, int) and isinstance(sy, int) and -10000 < sx < 30000 and -10000 < sy < 30000:
        create_kwargs["x"] = sx
        create_kwargs["y"] = sy

    window = webview.create_window("Downloader", **create_kwargs)
    api.set_window(window)

    # Persist size/position as the user resizes/moves the window.
    def _save_geometry(*_args):
        try:
            api.save_window_geometry(window.width, window.height, window.x, window.y)
        except Exception:
            pass

    window.events.resized += _save_geometry
    window.events.moved += _save_geometry
    window.events.closing += _save_geometry

    webview.start(debug=False)


if __name__ == "__main__":
    main()
