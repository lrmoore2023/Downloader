"""Per-channel SQLite download archive for Discord.

Discord media keeps its original attachment name behind a 'date - Discord - '
prefix, so — as with coomerfans/pawchive/derpibooru — this archive is the
authoritative "what has been downloaded" record: it stores the chosen filename
per entry so re-runs are idempotent and distinct items never overwrite each
other. The schema and thread model are identical to coomerfans, so we reuse that
Archive class outright and only differ in the entry-key prefix and DB filename.

Entry key (assigned in discord_scraper.parse_message):
    attachment -> "discord_{attachment_id}"
    embed media -> "discord_{message_id}_e{index}"

The runner records `post_id = message_id` on each row, so Archive.post_seen()
answers "has this message already been fetched?" — used by 'latest' to stop the
crawl once it reaches already-downloaded history.

DB filename (chosen by the runner/api): discord_{channel_id}.db
"""

from backend.coomerfans_archive import Archive  # re-exported; identical schema

__all__ = ["Archive"]
