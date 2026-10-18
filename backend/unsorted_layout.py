"""Where SimpCity media and tracked-album files go: one flat "Unsorted" area.

    <dest>/Unsorted/<year>/YYYY.MM.DD - <Site> - <name>           videos / everything else
    <dest>/Unsorted/Images/<year>/YYYY.MM.DD - <Site> - <name>    images

No per-site folders (the user's call, 2026-10-18) — the site lives in the
filename (`Simpcity`, `Bunkr`, `Gofile`, `Filester`, …).

Records store a path relative to the Unsorted root. Files can be moved by hand
(the user sorted the first batch themselves), so `locate()` also looks for a
recorded file at its canonical spot for its name, and callers re-point the
record instead of downloading it again.
"""

import os
import re

from backend.coomerfans_scraper import target_path

SUBDIR = "Unsorted"
LEGACY_SUBDIR = "Simpcity"     # the first layout: Simpcity/<year>, Simpcity/<Site>/<year>

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".jfif", ".heic")
_DATE_RE = re.compile(r"^(\d{4})\.\d{2}\.\d{2}")


def root_of(destination):
    return os.path.join(destination, SUBDIR) if destination else ""


def kind_of(filename):
    """"image" for image files, "video" for everything else (videos, archives…),
    matching the Images/<year> vs <year> split used across the library."""
    return "image" if (filename or "").lower().endswith(_IMAGE_EXT) else "video"


def target(root, dt, filename, kind=None):
    """Absolute path for a dated filename under the Unsorted root."""
    return target_path(root, kind or kind_of(filename), dt, filename)


def relkey(root, path):
    return os.path.relpath(path, root).replace("\\", "/")


def legacy_present(root, path_key):
    """True if a recorded file still sits in the first layout (`<dest>/Simpcity/…`,
    incl. its per-site folders) — e.g. a run before the user finished moving
    things. Counted as present so nothing is fetched twice; not re-pointed."""
    if not (root and path_key):
        return False
    old = os.path.join(os.path.dirname(root), LEGACY_SUBDIR)
    if not os.path.isdir(old):
        return False
    if os.path.isfile(os.path.join(old, path_key)):
        return True
    try:
        subs = [d for d in os.listdir(old) if os.path.isdir(os.path.join(old, d))]
    except OSError:
        return False
    return any(os.path.isfile(os.path.join(old, d, path_key)) for d in subs)


def locate(root, path_key):
    """The relative path where a recorded file actually is, or None.

    Tries the recorded path, then the canonical spot for its name
    (`<year>/` or `Images/<year>/`, the year taken from the YYYY.MM.DD prefix or
    the recorded path) — so files moved out of the old per-site folders, or
    between Images/ and the year folder, are still recognised."""
    if not (root and path_key):
        return None
    if os.path.isfile(os.path.join(root, path_key)):
        return path_key
    name = path_key.replace("\\", "/").rsplit("/", 1)[-1]
    years = []
    m = _DATE_RE.match(name)
    if m:
        years.append(m.group(1))
    years += [p for p in path_key.replace("\\", "/").split("/") if re.fullmatch(r"\d{4}", p)]
    seen = set()
    for y in years:
        for rel in (f"{y}/{name}", f"Images/{y}/{name}"):
            if rel not in seen:
                seen.add(rel)
                if os.path.isfile(os.path.join(root, rel)):
                    return rel
    return None
