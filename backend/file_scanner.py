import os
import re


def scan_destination(dest):
    """Scan an artist destination folder for existing year structure.

    Returns:
        {
            "latest_year": int or None,
            "has_archive": bool,
            "years": {2026: {"images": 5, "videos": 3}, ...}
        }
    """
    result = {
        "latest_year": None,
        "has_archive": False,
        "years": {},
    }

    if not dest or not os.path.isdir(dest):
        return result

    result["has_archive"] = os.path.isfile(
        os.path.join(dest, ".gallery-dl-archive.db")
    )

    year_pattern = re.compile(r"^\d{4}$")

    # Scan top-level year folders (videos/gifs)
    for entry in os.listdir(dest):
        entry_path = os.path.join(dest, entry)
        if os.path.isdir(entry_path) and year_pattern.match(entry):
            year = int(entry)
            if year not in result["years"]:
                result["years"][year] = {"images": 0, "videos": 0}
            result["years"][year]["videos"] = _count_files(entry_path)

    # Scan Images/ subfolder year directories
    images_dir = os.path.join(dest, "Images")
    if os.path.isdir(images_dir):
        for entry in os.listdir(images_dir):
            entry_path = os.path.join(images_dir, entry)
            if os.path.isdir(entry_path) and year_pattern.match(entry):
                year = int(entry)
                if year not in result["years"]:
                    result["years"][year] = {"images": 0, "videos": 0}
                result["years"][year]["images"] = _count_files(entry_path)

    if result["years"]:
        result["latest_year"] = max(result["years"].keys())

    return result


def _count_files(directory):
    """Count files (not directories) in a directory."""
    try:
        return sum(
            1
            for f in os.listdir(directory)
            if os.path.isfile(os.path.join(directory, f))
        )
    except OSError:
        return 0
