import os
import sys
import webview

# Ensure imports work when running from any directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.api import Api


def main():
    api = Api()
    frontend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
    index_path = os.path.join(frontend_dir, "index.html")

    # Restore saved window geometry (size/position) if present.
    saved = api.load_state().get("window", {}) or {}
    width = saved.get("width") or 1100
    height = saved.get("height") or 750

    create_kwargs = dict(
        url=index_path,
        js_api=api,
        width=width,
        height=height,
        min_size=(900, 600),
        background_color="#0e1116",
    )
    if "x" in saved and "y" in saved:
        create_kwargs["x"] = saved["x"]
        create_kwargs["y"] = saved["y"]

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
