"""Perceptual + exact signatures for images and videos, tuned to czkawka/Krokiet.

Image similarity reproduces czkawka's shipped default *exactly*: Mean (average)
hash at size 16 -> 256-bit, computed over a Lanczos-resized greyscale image, which
is precisely what ``imagehash.average_hash(img, hash_size=16)`` does (imagehash
resizes with LANCZOS by default). Hamming distances are therefore directly
comparable to czkawka's ``SIMILAR_VALUES`` table.

Video similarity reproduces czkawka's ``similario`` 3D-DCT spatiotemporal hash:
5 windows spread across the clip, 16 greyscale frames per window scaled to 16x16,
stacked into a 16x16x16 cube, separable 3D DCT-II, keep the low-frequency
10x10x10 sub-cube, binarise ``> 0`` -> a 1000-bit hash per window. Frames come
from the bundled ffmpeg binary (no ffprobe needed — duration/resolution are parsed
from ``ffmpeg -i`` stderr).

This module is pure signal: it reads files and returns dicts. Persistence lives in
media_sig_cache.py; grouping/matching lives in dedupe.py.
"""

import hashlib
import os
import re
import subprocess

# ── tunables (mirror czkawka/similario defaults) ──────────────────────
IMAGE_HASH_SIZE = 16                  # czkawka DEFAULT_HASH_SIZE -> 256-bit hash
IMAGE_HASH_BITS = IMAGE_HASH_SIZE * IMAGE_HASH_SIZE

# Hamming cutoffs for hash size 16 (czkawka SIMILAR_VALUES row 1, out of 256 bits).
IMAGE_SIMILARITY_LEVELS = {          # label -> max distance
    "very_high": 2,
    "high": 5,
    "medium": 15,
    "small": 30,
}
IMAGE_DEFAULT_DISTANCE = 10           # czkawka DEFAULT_IMAGE_SIMILARITY (slider)
IMAGE_UPGRADE_DISTANCE = IMAGE_SIMILARITY_LEVELS["high"]   # tight gate for _new upgrades

# similario visual defaults
VID_WINDOW_COUNT = 5
VID_SKIP_SECS = 15.0
VID_WINDOW_SECS = 6.0
VID_FRAMES_PER_WINDOW = 16
VID_FRAME_DIM = 16                    # 16x16 greyscale
VID_SUBCUBE = 10                      # keep 10x10x10 low-frequency coeffs -> 1000 bits
VID_WINDOW_BITS = VID_SUBCUBE ** 3    # 1000
VID_TOLERANCE = 0.375                 # czkawka default slider 15 -> 15/40
VID_MIN_MATCHING_WINDOWS = 0.6
VID_DURATION_TOLERANCE_PCT = 20.0

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_LINE_RE = re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")


# ── path normalisation (MUST match media_server._norm for a shared cache key) ──
def norm_path(p):
    return os.path.normpath(p or "").replace("\\", "/").rstrip("/").lower()


# ── exact-duplicate hashing ───────────────────────────────────────────
def file_blake2b(path, _chunk=1 << 20):
    """Streaming blake2b of the file's bytes (exact-duplicate key)."""
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_chunk), b""):
            h.update(block)
    return h.hexdigest()


# ── hamming over hex-encoded hashes ───────────────────────────────────
def hamming_hex(a, b):
    """Hamming distance between two equal-length hex hash strings."""
    if not a or not b:
        return None
    return (int(a, 16) ^ int(b, 16)).bit_count()


# ── image signatures ──────────────────────────────────────────────────
def image_signature(path, orientations=False):
    """Return {ahash, phash, width, height, orient?} or None if undecodable.

    ``orientations`` mirrors czkawka's GeometricInvariance: when True the 8
    dihedral variants are hashed too so a rotated/mirrored near-dupe still matches.
    """
    from PIL import Image, ImageOps

    try:
        import imagehash
    except Exception:
        return None

    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)          # honour EXIF rotation
            im = im.convert("RGB")
            width, height = im.size
            ahash = str(imagehash.average_hash(im, hash_size=IMAGE_HASH_SIZE))
            phash = str(imagehash.phash(im, hash_size=IMAGE_HASH_SIZE))
            sig = {"ahash": ahash, "phash": phash, "width": width, "height": height}
            if orientations:
                variants = _dihedral(im)
                sig["orient"] = [
                    str(imagehash.average_hash(v, hash_size=IMAGE_HASH_SIZE))
                    for v in variants
                ]
            return sig
    except Exception:
        return None


def _dihedral(im):
    """The 8 dihedral orientations (identity, 3 rotations, and their mirrors)."""
    from PIL import Image

    out = []
    cur = im
    for _ in range(4):
        out.append(cur)
        out.append(cur.transpose(Image.FLIP_LEFT_RIGHT))
        cur = cur.transpose(Image.ROTATE_90)
    return out


def image_distance(sig_a, sig_b):
    """Min Hamming distance between two image signatures (average hash),
    considering any stored dihedral orientations of A."""
    hashes_a = [sig_a["ahash"]] + list(sig_a.get("orient") or [])
    best = None
    for ha in hashes_a:
        d = hamming_hex(ha, sig_b["ahash"])
        if d is not None and (best is None or d < best):
            best = d
    return best


# ── video signatures ──────────────────────────────────────────────────
def _ffmpeg_probe(ffmpeg_exe, path):
    """Parse (duration_secs, width, height) from ``ffmpeg -i`` stderr. Returns
    (None, w, h) if duration is unreadable, (None, None, None) on failure."""
    try:
        proc = subprocess.run(
            [ffmpeg_exe, "-hide_banner", "-i", path],
            capture_output=True, timeout=60,
        )
    except Exception:
        return (None, None, None)
    err = proc.stderr.decode("utf-8", "replace")
    duration = None
    m = _DURATION_RE.search(err)
    if m:
        hh, mm, ss = int(m.group(1)), int(m.group(2)), float(m.group(3))
        duration = hh * 3600 + mm * 60 + ss
    width = height = None
    vm = _VIDEO_LINE_RE.search(err)
    if vm:
        width, height = int(vm.group(1)), int(vm.group(2))
    return (duration, width, height)


def _window_positions(duration):
    """Start times (secs) for VID_WINDOW_COUNT windows, following similario:
    usable range [min(skip, 0.15*dur) .. dur-0.5], spread evenly."""
    if not duration or duration <= 0:
        return None
    start = min(VID_SKIP_SECS, 0.15 * duration)
    end = max(start, duration - 0.5)
    if VID_WINDOW_COUNT == 1:
        return [start]
    step = (end - start) / (VID_WINDOW_COUNT - 1)
    return [start + i * step for i in range(VID_WINDOW_COUNT)]


def _extract_window(ffmpeg_exe, path, start):
    """Extract VID_FRAMES_PER_WINDOW greyscale 16x16 frames from a window, as a
    numpy uint8 array shaped (frames, 16, 16). Fewer frames are tiled up to the
    target; returns None if nothing decodes."""
    import numpy as np

    fps = VID_FRAMES_PER_WINDOW / VID_WINDOW_SECS
    vf = f"fps={fps},scale={VID_FRAME_DIM}:{VID_FRAME_DIM}:flags=bilinear,format=gray"
    cmd = [
        ffmpeg_exe, "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, start):.3f}", "-i", path,
        "-t", f"{VID_WINDOW_SECS:.3f}", "-vf", vf,
        "-frames:v", str(VID_FRAMES_PER_WINDOW),
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=90)
    except Exception:
        return None
    frame_bytes = VID_FRAME_DIM * VID_FRAME_DIM
    data = proc.stdout
    n = len(data) // frame_bytes
    if n == 0:
        return None
    arr = np.frombuffer(data[: n * frame_bytes], dtype=np.uint8).reshape(
        n, VID_FRAME_DIM, VID_FRAME_DIM)
    if n < VID_FRAMES_PER_WINDOW:
        reps = (VID_FRAMES_PER_WINDOW + n - 1) // n
        arr = np.tile(arr, (reps, 1, 1))[:VID_FRAMES_PER_WINDOW]
    return arr.astype(np.float64)


def _window_hash(cube):
    """3D DCT-II of a (16,16,16) cube -> low-freq 10^3 sub-cube -> 1000-bit hash
    (hex). Pixels are centred to [-128,127] first, matching similario."""
    import numpy as np
    from scipy.fft import dctn

    centred = cube - 128.0
    coeffs = dctn(centred, type=2, norm="ortho")
    sub = coeffs[:VID_SUBCUBE, :VID_SUBCUBE, :VID_SUBCUBE]
    bits = (sub > 0).flatten()
    packed = np.packbits(bits)
    return packed.tobytes().hex()


def video_signature(path, ffmpeg_exe):
    """Return {duration, width, height, windows:[hex,...]} or None on failure."""
    if not ffmpeg_exe:
        return None
    duration, width, height = _ffmpeg_probe(ffmpeg_exe, path)
    positions = _window_positions(duration)
    if positions is None:
        # Unknown/zero duration: fall back to fixed early offsets so at least a
        # short clip yields a signature (percentage placement is impossible
        # without a duration).
        positions = [0.0, 1.0, 3.0, 8.0, 20.0][:VID_WINDOW_COUNT]
    windows = []
    for start in positions:
        cube = _extract_window(ffmpeg_exe, path, start)
        if cube is None:
            continue
        windows.append(_window_hash(cube))
    if not windows:
        return None
    return {"duration": duration, "width": width, "height": height, "windows": windows}


def video_match(sig_a, sig_b):
    """(is_match, best_fraction) for two video signatures. Windows are compared
    index-aligned (both are spread evenly across the clip); a match needs
    >= VID_MIN_MATCHING_WINDOWS of comparable windows within VID_TOLERANCE and a
    duration within +/- VID_DURATION_TOLERANCE_PCT."""
    wa, wb = sig_a.get("windows") or [], sig_b.get("windows") or []
    if not wa or not wb:
        return (False, 0.0)

    da, db = sig_a.get("duration"), sig_b.get("duration")
    if da and db:
        lo, hi = sorted((da, db))
        if hi > lo * (1 + VID_DURATION_TOLERANCE_PCT / 100.0):
            return (False, 0.0)

    n = min(len(wa), len(wb))
    within = 0
    for i in range(n):
        d = hamming_hex(wa[i], wb[i])
        if d is None:
            continue
        if d / VID_WINDOW_BITS <= VID_TOLERANCE:
            within += 1
    frac = within / n if n else 0.0
    return (frac >= VID_MIN_MATCHING_WINDOWS, frac)


# ── unified entry point ───────────────────────────────────────────────
def compute_signature(path, kind, ffmpeg_exe=None, orientations=False):
    """Perceptual signature for one file. ``kind`` is 'image' or 'video'.
    Returns a dict (without size/mtime/sha — the cache layer adds those) or None."""
    if kind == "image":
        return image_signature(path, orientations=orientations)
    if kind == "video":
        return video_signature(path, ffmpeg_exe)
    return None
