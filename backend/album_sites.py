"""Registry of file-host album sites supported by the Albums tab.

The Albums tab downloads whole albums from a few file-hosts (bunkr, cyberdrop,
filester) into an ad-hoc directory. Each site is downloaded by whichever engine
handles it best:

  * bunkr, cyberdrop -> cyberdrop-dl (primary), gallery-dl (fallback). cyberdrop-dl
    is the most hardened tool for bunkr's flaky, domain-rotating CDN.
  * filester        -> gallery-dl (cyberdrop-dl doesn't support filester).

`detect_site(url)` maps a pasted link to its site entry (and therefore engine).
Adding a new site later is a single `SITES` entry.

Links may carry an optional per-link password using inline `<url> | <password>`
syntax (only some filester folders need one). `parse_entry` splits that off.
"""

import re
from urllib.parse import urlparse


# host_re matches the registrable-name portion of the hostname, TLD-agnostic, so
# bunkr's domain rotation (bunkr.cr / .si / .ph / bunkrr.su ...) and filester's
# (filester.gg / .me / .si) all resolve to one entry. `title_field` is the
# gallery-dl metadata key used to name the per-album subfolder on the gallery-dl
# path (cyberdrop-dl names album subfolders itself).
# `tuning` = cyberdrop-dl download settings, derived empirically per CDN:
#   per_domain — simultaneous downloads per host; segments — connections per file
#   (so peak host connections ≈ per_domain × segments); jitter — random 0..N s spacing.
# Bunkr's CDN 429-storms hard above ~2 simultaneous and multiplies connections via
# segments, so keep both low. Cyberdrop's CDN is robust — it handles 4× cleanly and
# fast (per_domain 8 soft-blocks, so cap at 4). gallery-dl (filester) downloads
# sequentially and needs no such tuning.
SITES = [
    {
        "key": "bunkr",
        "host_re": r"^(?:www\.)?bunkrr?\.",
        "engine": "cyberdrop-dl",
        "extractor": "bunkr",
        "title_field": "album_name",
        "tuning": {"per_domain": 2, "segments": 2, "jitter": 2},
    },
    {
        "key": "cyberdrop",
        "host_re": r"^(?:www\.)?cyberdrop\.",
        "engine": "cyberdrop-dl",
        "extractor": "cyberdrop",
        "title_field": "album_name",
        "tuning": {"per_domain": 4, "segments": 4, "jitter": 0},
    },
    {
        "key": "filester",
        "host_re": r"^(?:www\.)?filester\.",
        # Native downloader — gallery-dl's filester extractor resolves to dead
        # cache*.filester.me hosts; we use filester's v2 API + c-fs.cdn.cr instead.
        "engine": "filester",
        "extractor": "filester",
        "title_field": "folder_name",
    },
]

_COMPILED = [(re.compile(s["host_re"], re.IGNORECASE), s) for s in SITES]


def detect_site(url):
    """Return the SITES entry for `url`, or None if no known site matches.

    Matches on hostname only, so query strings / paths never cause false hits.
    """
    if not url:
        return None
    host = urlparse(url.strip()).hostname or ""
    for pattern, site in _COMPILED:
        if pattern.search(host):
            return site
    return None


def parse_entry(raw):
    """Parse one textarea line into (url, password).

    Accepts optional inline `<url> | <password>` syntax; password is None when
    absent. Blank lines yield (None, None).
    """
    if not raw:
        return None, None
    line = raw.strip()
    if not line:
        return None, None
    if "|" in line:
        url, _, pwd = line.partition("|")
        url = url.strip()
        pwd = pwd.strip() or None
        return (url or None), pwd
    return line, None
