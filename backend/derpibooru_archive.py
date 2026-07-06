"""Per-query SQLite download archive for derpibooru.org.

derpibooru files are named after the image ID, but that ID is embedded in a
'date - SITE - id.ext' filename we choose, so — as with coomerfans/pawchive —
this archive is the authoritative "what has been downloaded" record. The schema
and thread model are identical to coomerfans, so we reuse that Archive class
outright and only differ in the entry-key prefix and DB filename.

Entry key:
    image -> "derpibooru_{image_id}"

DB filename (chosen by the runner/api): derpibooru_{query_slug}.db
"""

from backend.coomerfans_archive import Archive  # re-exported; identical schema


def entry_key(image_id):
    return f"derpibooru_{image_id}"


__all__ = ["Archive", "entry_key"]
