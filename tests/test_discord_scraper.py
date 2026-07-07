"""Unit tests for backend.discord_scraper (pure functions — no network)."""

from datetime import datetime, timezone

import pytest

from backend import discord_scraper as ds


# ── URL parsing ─────────────────────────────────────────────────────

def test_parse_creator_url_channel():
    assert ds.parse_creator_url(
        "https://discord.com/channels/111/222") == {"guild_id": "111", "channel_id": "222"}


def test_parse_creator_url_jump_link_ignores_message_id():
    # A message jump link (…/channel/message) still resolves to the channel.
    assert ds.parse_creator_url(
        "https://discord.com/channels/111/222/333333") == {
            "guild_id": "111", "channel_id": "222"}


def test_parse_creator_url_dm_and_discordapp_host():
    assert ds.parse_creator_url(
        "https://discordapp.com/channels/@me/444") == {
            "guild_id": "@me", "channel_id": "444"}


@pytest.mark.parametrize("url", [
    "", None, "https://example.com/channels/1/2",
    "https://discord.com/users/123", "not a url",
])
def test_parse_creator_url_rejects_junk(url):
    assert ds.parse_creator_url(url) is None


def test_channel_url_canonical():
    assert ds.channel_url("111", "222") == "https://discord.com/channels/111/222"
    assert ds.channel_url("@me", "444") == "https://discord.com/channels/@me/444"


# ── Snowflake / date helpers ────────────────────────────────────────

def test_snowflake_dt_roundtrip():
    dt = datetime(2022, 6, 15, 8, 30, 0, tzinfo=timezone.utc)
    sf = ds.dt_to_snowflake(dt)
    back = ds.snowflake_to_dt(sf)
    # Snowflake ms precision -> within a second of the original.
    assert abs((back - dt).total_seconds()) < 1


def test_year_bounds_bracket_the_year():
    after, before = ds.year_bounds(2023)
    assert ds.snowflake_to_dt(after).year == 2023
    assert ds.snowflake_to_dt(after) == datetime(2023, 1, 1, tzinfo=timezone.utc)
    assert ds.snowflake_to_dt(before) == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert after < before


def test_parse_dt_iso():
    dt = ds.parse_dt("2026-05-05T12:00:00.000000+00:00")
    assert dt == datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    assert ds.parse_dt(None) is None


# ── Filenames ───────────────────────────────────────────────────────

def test_build_filename_keeps_prefix():
    dt = datetime(2026, 5, 5, tzinfo=timezone.utc)
    fn = ds.build_filename(dt, "cool render.png")
    assert fn == "2026.05.05 - Discord - cool render.png"
    # Missing date -> zeroed prefix (matches the other engines).
    assert ds.build_filename(None, "x.jpg").startswith("0000.00.00 - Discord - ")


def test_build_filename_prefix_matches_errors_panel_regex():
    # api._PREFIX_RE must recognise the name so the errors/manual-grab panel works.
    import re
    prefix_re = re.compile(r"^(\d{4}\.\d{2}\.\d{2}(?: \d{2}\.\d{2})? - [^-]+ - )")
    dt = datetime(2026, 1, 7, tzinfo=timezone.utc)
    assert prefix_re.match(ds.build_filename(dt, "clip.mp4"))


def test_build_filename_label_override():
    dt = datetime(2026, 5, 3, tzinfo=timezone.utc)
    assert ds.build_filename(dt, "set.zip", "patreon") == "2026.05.03 - Patreon - set.zip"
    assert ds.build_filename(dt, "set.zip", "fanbox") == "2026.05.03 - Fanbox - set.zip"
    assert ds.build_filename(dt, "set.zip", "onlyfans") == "2026.05.03 - OF - set.zip"
    # empty / unknown -> default Discord
    assert ds.build_filename(dt, "set.zip", "") == "2026.05.03 - Discord - set.zip"
    assert ds.build_filename(dt, "set.zip", "bogus") == "2026.05.03 - Discord - set.zip"


def test_add_index_suffix():
    assert ds.add_index_suffix("2026.05.05 - Discord - a.png", 1) == \
        "2026.05.05 - Discord - a_1.png"


def test_sanitize_filename_strips_illegal():
    assert "/" not in ds.sanitize_filename("a/b:c?.png")
    assert ds.sanitize_filename("") == "file"


# ── Message normalisation ───────────────────────────────────────────

def _msg():
    return {
        "id": "1234567890123456789",
        "timestamp": "2026-05-05T12:00:00.000000+00:00",
        "content": ("see https://example.com/page and "
                    "https://cdn.discordapp.com/attachments/1/2/z.png"),
        "attachments": [
            {"id": "999", "filename": "cool render.png",
             "url": "https://cdn.discordapp.com/attachments/1/2/cool%20render.png?ex=abc",
             "content_type": "image/png", "size": 100},
            {"id": "1000", "filename": "clip.mp4",
             "url": "https://cdn.discordapp.com/attachments/1/2/clip.mp4",
             "content_type": "video/mp4"},
        ],
        "embeds": [
            {"type": "image",
             "image": {"url": "https://media.discordapp.net/attachments/1/2/pic.jpg"}},
            {"type": "link", "url": "https://twitter.com/someone/status/1",
             "title": "Tweet"},
        ],
    }


def test_parse_message_media_attachments_and_embed():
    m = ds.parse_message(_msg())
    assert m["message_id"] == "1234567890123456789"
    assert m["dt"] == datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)

    entries = {x["entry"]: x for x in m["media"]}
    assert "discord_999" in entries and entries["discord_999"]["media_kind"] == "image"
    assert "discord_1000" in entries and entries["discord_1000"]["media_kind"] == "video"
    # embed image -> keyed by message id + embed index
    embed_media = [x for x in m["media"] if x["entry"].startswith("discord_1234567890123456789_e")]
    assert len(embed_media) == 1
    assert embed_media[0]["media_kind"] == "image"


def test_parse_message_external_links_only():
    m = ds.parse_message(_msg())
    urls = {l["url"] for l in m["links"]}
    # external links are recorded; Discord-hosted media is downloaded, not linked
    assert "https://example.com/page" in urls
    assert "https://twitter.com/someone/status/1" in urls
    assert not any("discordapp.com" in u for u in urls)


def test_kind_for():
    assert ds._kind_for("mp4") == "video"
    assert ds._kind_for("png") == "image"
    assert ds._kind_for("", "video/webm") == "video"
    assert ds._kind_for("weirdext") == "image"   # unknown -> image


# ── Pagination (with a stub fetch, no network) ──────────────────────

def test_iter_messages_paginates_backward():
    # Two full-ish pages then a short page ends it. fetch returns lists.
    page1 = [{"id": str(200 - i)} for i in range(100)]     # ids 200..101
    page2 = [{"id": str(100 - i)} for i in range(50)]      # ids 100..51 (short)
    calls = []

    def fake_fetch(session, url):
        calls.append(url)
        return page1 if "before=" not in url else page2

    got = list(ds.iter_messages(None, "222", fetch=fake_fetch))
    assert len(got) == 150
    # first call has no cursor; second pages before the oldest of page 1 (id 101)
    assert "before=" not in calls[0]
    assert "before=101" in calls[1]


def test_iter_messages_stops_at_after_bound():
    page = [{"id": str(500 - i)} for i in range(100)]   # ids 500..401

    def fake_fetch(session, url):
        return page

    # after_bound above the page's oldest id -> stop after one page.
    got = list(ds.iter_messages(None, "222", fetch=fake_fetch, after_bound=450))
    assert len(got) == 100
