"""Per-creator SQLite download archive for pawchive.st.

pawchive files are named after the file's own name (not the post id), so — as
with coomerfans — the post id is not recoverable from disk and this archive is
the authoritative "what has been downloaded" record. The schema and thread
model are identical to coomerfans, so we reuse that Archive class outright and
only differ in the entry-key prefix and DB filename.

Entry keys:
    on-site media          -> "pawchive_{post_id}_{index}"       (index: 1-based)
    external direct grabs   -> "pawchive_{post_id}_ext_{index}"

DB filename (chosen by the runner/api): pawchive_{service}_{user_id}.db
"""

from backend.coomerfans_archive import Archive  # re-exported; identical schema


def entry_key(post_id, index):
    return f"pawchive_{post_id}_{index}"


def ext_entry_key(post_id, index):
    return f"pawchive_{post_id}_ext_{index}"


__all__ = ["Archive", "entry_key", "ext_entry_key"]
