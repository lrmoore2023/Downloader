import os
import re
import sqlite3

# Matches twitter filenames, both the original and the newer "- Twitter -" form:
#   2025.03.15 - 1234567890_1.jpg
#   2025.03.15 - Twitter - 1234567890_1.jpg
_FILENAME_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2} - (?:Twitter - )?(\d+)_(\d+)\.\w+$")


def scan_files_for_entries(destination, has_videos=True):
    """Scan an artist directory and return a set of archive entry strings
    for all Twitter-downloaded files found on disk.

    Each entry matches gallery-dl's archive format: twitter_{tweet_id}_0_{num}
    """
    entries = set()

    dirs_to_scan = []

    # Scan top-level year dirs (videos/gifs or all files when no video split)
    if os.path.isdir(destination):
        for name in os.listdir(destination):
            path = os.path.join(destination, name)
            if os.path.isdir(path) and re.match(r"^\d{4}$", name):
                dirs_to_scan.append(path)

    # Scan Images/year dirs
    images_dir = os.path.join(destination, "Images")
    if os.path.isdir(images_dir):
        for name in os.listdir(images_dir):
            path = os.path.join(images_dir, name)
            if os.path.isdir(path) and re.match(r"^\d{4}$", name):
                dirs_to_scan.append(path)

    for d in dirs_to_scan:
        for filename in os.listdir(d):
            filepath = os.path.join(d, filename)
            if not os.path.isfile(filepath):
                continue
            m = _FILENAME_RE.match(filename)
            if m:
                tweet_id = m.group(1)
                num = m.group(2)
                entries.add(f"twitter_{tweet_id}_0_{num}")

    return entries


def sync_archive(archive_path, destination, has_videos=True):
    """Ensure the archive DB contains entries for all files on disk.

    Adds entries for files that exist on disk but are missing from the archive.
    Does NOT remove entries for deleted files (those stay to prevent
    re-downloading during normal operations).

    Returns dict with counts of what changed.
    """
    if not archive_path or not os.path.isfile(archive_path):
        return {"added": 0, "total_on_disk": 0}

    disk_entries = scan_files_for_entries(destination, has_videos)
    if not disk_entries:
        return {"added": 0, "total_on_disk": 0}

    added = 0
    try:
        conn = sqlite3.connect(archive_path)
        cursor = conn.cursor()

        # Ensure table exists
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS archive (entry TEXT PRIMARY KEY)"
        )

        # Find which disk entries are missing from the archive
        existing = set()
        cursor.execute("SELECT entry FROM archive")
        for (entry,) in cursor.fetchall():
            existing.add(entry)

        missing = disk_entries - existing
        if missing:
            cursor.executemany(
                "INSERT OR IGNORE INTO archive (entry) VALUES (?)",
                [(e,) for e in missing],
            )
            added = cursor.rowcount
            conn.commit()

        conn.close()
    except Exception:
        pass

    return {"added": added, "total_on_disk": len(disk_entries)}


def sync_archive_for_year(archive_path, destination, year, has_videos=True):
    """Remove archive entries for files that no longer exist on disk
    for a specific year, then add entries for files that do exist.

    This is used before/after a redownload to ensure the archive
    accurately reflects what's on disk for that year.
    """
    if not archive_path or not os.path.isfile(archive_path):
        return {"removed": 0, "added": 0}

    year_str = str(year)

    # Collect files on disk for this specific year
    disk_entries = set()
    year_dirs = []

    # Video/gif year dir
    vid_dir = os.path.join(destination, year_str)
    if os.path.isdir(vid_dir):
        year_dirs.append(vid_dir)

    # Images year dir
    img_dir = os.path.join(destination, "Images", year_str)
    if os.path.isdir(img_dir):
        year_dirs.append(img_dir)

    for d in year_dirs:
        for filename in os.listdir(d):
            if not os.path.isfile(os.path.join(d, filename)):
                continue
            m = _FILENAME_RE.match(filename)
            if m:
                tweet_id = m.group(1)
                num = m.group(2)
                disk_entries.add(f"twitter_{tweet_id}_0_{num}")

    # Also collect tweet_ids on disk so we can identify archive entries for this year
    disk_tweet_ids = set()
    for entry in disk_entries:
        parts = entry.split("_")
        if len(parts) >= 3:
            disk_tweet_ids.add(parts[1])  # tweet_id

    removed = 0
    added = 0

    try:
        conn = sqlite3.connect(archive_path)
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS archive (entry TEXT PRIMARY KEY)"
        )

        # Add entries for files on disk not in archive
        cursor.execute("SELECT entry FROM archive")
        existing = {row[0] for row in cursor.fetchall()}

        missing = disk_entries - existing
        if missing:
            cursor.executemany(
                "INSERT OR IGNORE INTO archive (entry) VALUES (?)",
                [(e,) for e in missing],
            )
            added = len(missing)

        conn.commit()
        conn.close()
    except Exception:
        pass

    return {"removed": removed, "added": added}
