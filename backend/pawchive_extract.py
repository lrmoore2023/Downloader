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


def _looks_japanese(s):
    """True if `s` contains any Japanese character (kana / CJK ideograph / CJK
    punctuation like 【】、。 / fullwidth forms). Used to confirm a mojibake reversal
    actually produced Japanese text, so a legitimately non-ASCII Latin name is never
    mangled by a coincidental decode."""
    for c in s:
        o = ord(c)
        if (0x3040 <= o <= 0x30FF          # hiragana + katakana
                or 0x4E00 <= o <= 0x9FFF   # CJK unified ideographs
                or 0x3000 <= o <= 0x303F   # CJK symbols & punctuation
                or 0xFF00 <= o <= 0xFFEF):  # halfwidth/fullwidth forms
            return True
    return False


def repair_mojibake_name(name):
    """Recover a filename that is really **CP932/EUC-JP** text mis-decoded as
    **CP437** — the classic "legacy Japanese zip unpacked on a non-Japanese Windows"
    mojibake. Returns the corrected name, or None if `name` isn't such mojibake (so
    the caller leaves it untouched).

    Python's ``zipfile`` decodes a member name as CP437 whenever the entry's UTF-8
    flag (general-purpose bit 11, 0x800) is unset; Japanese packing tools (the Fanbox
    packs here) write Shift-JIS/CP932 names without that flag, so the bytes for
    '秋菜ちゃん【本編】' surface as 'ÅHì╪é┐éßé±üyû{ò╥üz'. We reverse it by re-encoding to
    the exact CP437 bytes and decoding as the real encoding.

    Guarded against false positives: the name must re-encode cleanly to CP437, and
    the CP932/EUC result must actually contain Japanese. A correctly-stored UTF-8
    Japanese name can't re-encode to CP437 (→ None); a legitimate Latin-1 name won't
    decode to Japanese (→ None). UTF-8 bytes written without the flag are also caught
    (a strict UTF-8 decode only succeeds on genuine UTF-8). Verified against a real
    Buckethead Fanbox pack — every name round-trips to clean Japanese."""
    try:
        raw = name.encode("cp437")         # the bytes zipfile/Explorer saw
    except UnicodeEncodeError:
        return None                        # not a clean CP437 string; leave as-is
    try:
        u = raw.decode("utf-8")            # real UTF-8 written without the flag
        if u != name:
            return u
    except UnicodeDecodeError:
        pass
    for enc in ("cp932", "euc_jp"):
        try:
            fixed = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if fixed != name and _looks_japanese(fixed):
            return fixed
    return None


def _zip_member_name(zinfo):
    """A zip member's real filename. Entries that set the UTF-8 flag are trusted as-is;
    otherwise we try to repair CP437-rendered CP932/EUC-JP mojibake (see
    repair_mojibake_name), falling back to zipfile's own decode when it isn't that."""
    name = zinfo.filename
    if zinfo.flag_bits & 0x800:
        return name                        # already proper UTF-8 per the zip
    return repair_mojibake_name(name) or name


def _is_unsafe_member(name):
    """Zip-slip guard on the (decoded) member name: reject absolute paths or any
    component that escapes the extraction root."""
    norm = os.path.normpath(name)
    if os.path.isabs(name) or os.path.isabs(norm):
        return True
    parts = norm.replace("\\", "/").split("/")
    return norm.startswith("..") or ".." in parts


def _extract_zip(path, dest):
    with zipfile.ZipFile(path) as zf:
        for zi in zf.infolist():
            name = _zip_member_name(zi)
            # zip-slip guard on the DECODED name. (We also only ever process files
            # found *under* dest afterwards, as a second net.)
            if _is_unsafe_member(name):
                continue
            target = os.path.join(dest, os.path.normpath(name))
            if zi.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # Extract by ZipInfo (not by name) so the read is unaffected by our
            # rename, streaming so a multi-GB member never loads into memory.
            with zf.open(zi) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
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
