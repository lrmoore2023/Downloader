"""Extraction of downloaded pawchive archives (zip/rar/…) into the library.

Pure, stateless helpers: unpack an archive into a temp directory and classify its
members. The *placement* of extracted files is done by the runner
(``PawchiveRunner._extract_archive``) so it can reuse the runner's collision-safe
naming and thread-safe path reservation. See the plan for the behaviour spec.

What's usable on this machine (see the exploration notes):
  * zip → stdlib ``zipfile`` (always available)
  * rar → WinRAR's ``UnRAR.exe`` (present but not on PATH — resolved by absolute path)
  * 7z/tar/gz/… → best effort via Windows ``tar`` (bsdtar/libarchive)
Anything we can't unpack leaves the archive in place (the runner never deletes it).
"""

import os
import shutil
import subprocess
import zipfile

from backend.coomerfans_scraper import IMAGE_EXTS, VIDEO_EXTS

# WinRAR's UnRAR isn't on PATH here; fall back to its known install location.
_WINRAR_DIRS = (
    r"C:\Program Files\WinRAR",
    r"C:\Program Files (x86)\WinRAR",
)

# Generous ceiling so a big multi-GB pack still finishes; a hung tool can't stall
# the run forever.
_EXTRACT_TIMEOUT = 1800  # seconds


def _ext(name):
    return os.path.splitext(name)[1].lstrip(".").lower()


def classify(name):
    """'image' | 'video' | None (non-media) from a filename's extension.

    Deliberately NOT pawchive_scraper._kind_and_ext, which defaults *unknown*
    extensions to 'image'. Here only known image/video extensions are media;
    everything else (docs, assets, nested archives, unknown types) is a leftover
    that stays in the archive-named folder."""
    e = _ext(name)
    if e in VIDEO_EXTS:
        return "video"
    if e in IMAGE_EXTS:
        return "image"
    return None


def _resolve_unrar():
    """Path to an UnRAR/Rar executable, or None. Mirrors the ffprobe pattern:
    prefer PATH, fall back to the known WinRAR install dir (it isn't on PATH)."""
    for exe in ("unrar", "UnRAR", "unrar.exe", "UnRAR.exe"):
        p = shutil.which(exe)
        if p:
            return p
    for d in _WINRAR_DIRS:
        for exe in ("UnRAR.exe", "Rar.exe"):
            cand = os.path.join(d, exe)
            if os.path.isfile(cand):
                return cand
    return None


def unrar_available():
    return _resolve_unrar() is not None


def _run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=_EXTRACT_TIMEOUT)
        return r.returncode == 0
    except Exception:
        return False


def _extract_zip(path, dest):
    with zipfile.ZipFile(path) as zf:
        # zip-slip guard: drop absolute or parent-escaping member names. (We also
        # only ever process files found *under* dest afterwards, as a second net.)
        members = [m for m in zf.namelist()
                   if not os.path.isabs(m)
                   and not os.path.normpath(m).startswith("..")]
        zf.extractall(dest, members=members)
    return True


def _extract_rar(path, dest):
    exe = _resolve_unrar()
    if not exe:
        return False
    # x = extract with full paths; -o+ overwrite; -y assume yes; -idq quiet.
    # Trailing separator tells UnRAR the target is a directory.
    return _run([exe, "x", "-o+", "-y", "-idq", path, dest + os.sep])


def _extract_tar(path, dest):
    exe = shutil.which("tar")
    if not exe:
        return False
    return _run([exe, "-xf", path, "-C", dest])


def extract_to_temp(archive_path, dest_dir):
    """Unpack ``archive_path`` into the (already existing) ``dest_dir``.

    Returns True on success, False if the format isn't supported here or the
    extraction failed. Never raises out — a False result just means 'leave the
    archive in place'."""
    try:
        if zipfile.is_zipfile(archive_path):
            return _extract_zip(archive_path, dest_dir)
        if _ext(archive_path) == "rar":
            return _extract_rar(archive_path, dest_dir)
        # 7z / tar / gz / tgz / bz2 / xz / zst — best effort via bsdtar (libarchive).
        return _extract_tar(archive_path, dest_dir)
    except Exception:
        return False
