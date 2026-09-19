"""PMV tracker manifests + catalogue numbering.

Locked numbering (rule34video / iwara): first complete walk numbers oldest→newest,
later walks only append, gone items keep their slot. Chronological numbering
(pawchive): renumber by date on every walk, flag back-fills and shifted ✓ posts.

    python -m pytest tests/test_pmv_tracker.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.pmv_tracker as pt


def _fetched(ids, dates=None):
    """Newest-first listing: ids[0] is the newest (pos 0)."""
    out = []
    for i, vid in enumerate(ids):
        f = {"id": str(vid), "title": f"t{vid}", "url": f"u/{vid}", "pos": i}
        if dates:
            f["date"] = dates[i]
        out.append(f)
    return out


# ── locked ──────────────────────────────────────────────────────────

def test_locked_initial_numbers_oldest_first():
    m = pt.new_manifest("rule34video", "1")
    res = pt.merge_locked(m, _fetched([30, 20, 10]), full=True, complete=True, now="T1")
    assert res == {"new": 3, "gone": 0, "numbered": True}
    assert m["items"]["10"]["number"] == 1
    assert m["items"]["20"]["number"] == 2
    assert m["items"]["30"]["number"] == 3
    assert m["next_number"] == 4
    assert m["initial_complete"] is True
    assert all(it["initial"] for it in m["items"].values())


def test_locked_initial_requires_complete_walk():
    m = pt.new_manifest("rule34video", "1")
    res = pt.merge_locked(m, _fetched([30, 20]), full=False, complete=False)
    assert res["new"] == 0 and res["numbered"] is False
    assert m["items"] == {}
    assert m["initial_complete"] is False


def test_locked_incremental_appends_in_chronological_order():
    m = pt.new_manifest("iwara", "u")
    pt.merge_locked(m, _fetched([30, 20, 10]), full=True, complete=True, now="T1")
    # Two new uploads on top; 50 is newer than 40.
    res = pt.merge_locked(m, _fetched([50, 40, 30]), full=False, complete=False, now="T2")
    assert res["new"] == 2
    assert m["items"]["40"]["number"] == 4
    assert m["items"]["50"]["number"] == 5
    assert m["next_number"] == 6
    assert not m["items"]["40"]["initial"]
    assert m["items"]["40"]["first_seen"] == "T2"
    # Incremental walks never touch `gone`.
    assert not any(it["gone"] for it in m["items"].values())


def test_locked_known_numbers_never_change():
    m = pt.new_manifest("rule34video", "1")
    pt.merge_locked(m, _fetched([30, 20, 10]), full=True, complete=True)
    before = {k: v["number"] for k, v in m["items"].items()}
    pt.merge_locked(m, _fetched([40, 30, 20, 10]), full=True, complete=True)
    pt.merge_locked(m, _fetched([40, 30, 20, 10]), full=True, complete=True)
    for k, n in before.items():
        assert m["items"][k]["number"] == n
    assert m["items"]["40"]["number"] == 4


def test_locked_gone_only_on_full_walk_and_cleared_when_back():
    m = pt.new_manifest("rule34video", "1")
    pt.merge_locked(m, _fetched([30, 20, 10]), full=True, complete=True)
    res = pt.merge_locked(m, _fetched([30, 10]), full=True, complete=True, now="T2")
    assert res["gone"] == 1
    assert m["items"]["20"]["gone"] is True and m["items"]["20"]["gone_since"] == "T2"
    assert m["items"]["20"]["number"] == 2          # slot kept
    # Reappears → not gone, same number, and the next upload continues from 4.
    pt.merge_locked(m, _fetched([40, 30, 20, 10]), full=False, complete=False)
    assert m["items"]["20"]["gone"] is False
    assert m["items"]["40"]["number"] == 4


def test_locked_refresh_keeps_learned_detail_when_listing_lacks_it():
    m = pt.new_manifest("rule34video", "1")
    pt.merge_locked(m, [{"id": "1", "title": "a", "url": "u", "pos": 0,
                         "date": "2026-01-01", "quality": 2160}], full=True, complete=True)
    pt.merge_locked(m, [{"id": "1", "title": "a2", "url": "u", "pos": 0,
                         "date": None, "quality": None}], full=False, complete=False)
    it = m["items"]["1"]
    assert it["title"] == "a2" and it["date"] == "2026-01-01" and it["quality"] == 2160


def test_locked_backfilled_flag_for_older_new_item():
    m = pt.new_manifest("iwara", "u")
    pt.merge_locked(m, _fetched([2, 1], ["2026-02-01", "2026-01-01"]), full=True, complete=True)
    pt.merge_locked(m, _fetched([2, 9, 1], ["2026-02-01", "2025-06-01", "2026-01-01"]),
                    full=False, complete=False)
    assert m["items"]["9"]["number"] == 3
    assert m["items"]["9"]["backfilled"] is True


def test_locked_full_rescan_reslots_backfills_when_nothing_is_checked():
    # First scan while logged out missed video 2 (private); after login a full
    # rescan finds it. Nothing is ✓, so the catalogue is re-sorted by date.
    m = pt.new_manifest("iwara", "u")
    pt.merge_locked(m, _fetched([3, 1], ["2026-03-01", "2026-01-01"]), full=True, complete=True)
    res = pt.merge_locked(m, _fetched([3, 2, 1], ["2026-03-01", "2026-02-01", "2026-01-01"]),
                          full=True, complete=True)
    assert res["renumbered"] is True and res["new"] == 1
    assert [m["items"][i]["number"] for i in ("1", "2", "3")] == [1, 2, 3]
    assert not m["items"]["2"]["backfilled"]
    assert m["next_number"] == 4


def test_locked_full_rescan_keeps_append_when_something_is_checked():
    m = pt.new_manifest("iwara", "u")
    pt.merge_locked(m, _fetched([3, 1], ["2026-03-01", "2026-01-01"]), full=True, complete=True)
    pt.set_status(m["items"]["3"], "downloaded")
    res = pt.merge_locked(m, _fetched([3, 2, 1], ["2026-03-01", "2026-02-01", "2026-01-01"]),
                          full=True, complete=True)
    assert "renumbered" not in res
    assert m["items"]["2"]["number"] == 3 and m["items"]["2"]["backfilled"]
    assert m["items"]["3"]["number"] == 2


def test_locked_incremental_never_auto_renumbers():
    m = pt.new_manifest("iwara", "u")
    pt.merge_locked(m, _fetched([3, 1], ["2026-03-01", "2026-01-01"]), full=True, complete=True)
    res = pt.merge_locked(m, _fetched([3, 2, 1], ["2026-03-01", "2026-02-01", "2026-01-01"]),
                          full=False, complete=False)
    assert "renumbered" not in res and m["items"]["2"]["number"] == 3


def test_renumber_locked_by_date_with_shift_tracking():
    m = pt.new_manifest("iwara", "u")
    pt.merge_locked(m, _fetched([3, 1], ["2026-03-01", "2026-01-01"]), full=True, complete=True)
    pt.set_status(m["items"]["3"], "downloaded")               # #2 at check time
    pt.merge_locked(m, _fetched([3, 2, 1], ["2026-03-01", "2026-02-01", "2026-01-01"]),
                    full=True, complete=True)                  # appended as #3
    res = pt.renumber_locked(m)
    assert res == {"changed": 2, "shifted": 1}
    assert [m["items"][i]["number"] for i in ("1", "2", "3")] == [1, 2, 3]
    assert pt.is_shifted(m["items"]["3"]) and m["items"]["3"]["number_at_check"] == 2
    assert pt.renumber_locked(m) == {"changed": 0, "shifted": 0}


def test_renumber_locked_undated_item_stays_behind_its_predecessor():
    # Video 2 has no date (r34 detail fetch failed). It inherits the date of the
    # video numbered just before it, so a renumber never moves it ahead.
    m = pt.new_manifest("rule34video", "1")
    pt.merge_locked(m, _fetched([3, 2, 1], ["2026-03-01", None, "2026-01-01"]), full=True, complete=True)
    assert [m["items"][i]["number"] for i in ("1", "2", "3")] == [1, 2, 3]
    assert pt.renumber_locked(m) == {"changed": 0, "shifted": 0}
    assert [m["items"][i]["number"] for i in ("1", "2", "3")] == [1, 2, 3]
    # A dated video appended out of order still gets sorted in; the undated one
    # follows whatever now precedes it.
    pt.merge_locked(m, _fetched([4, 3, 2, 1], ["2025-12-01", "2026-03-01", None, "2026-01-01"]),
                    full=False, complete=False)
    assert m["items"]["4"]["number"] == 4                          # appended (incremental)
    pt.renumber_locked(m)
    assert [m["items"][i]["number"] for i in ("4", "1", "2", "3")] == [1, 2, 3, 4]


# ── chronological ───────────────────────────────────────────────────

def _posts(spec):
    """spec: [(id, date)] in any order."""
    return [{"id": str(i), "title": f"p{i}", "url": f"u/{i}", "date": d, "pos": n}
            for n, (i, d) in enumerate(spec)]


def test_chronological_numbers_by_date_then_id():
    m = pt.new_manifest("pawchive", "42")
    res = pt.merge_chronological(m, _posts([(3, "2026-03-01"), (1, "2026-01-01"),
                                            (2, "2026-01-01")]), now="T1")
    assert res == {"new": 3, "gone": 0, "numbered": True}
    assert m["items"]["1"]["number"] == 1
    assert m["items"]["2"]["number"] == 2
    assert m["items"]["3"]["number"] == 3
    assert all(it["initial"] for it in m["items"].values())


def test_chronological_backfill_renumbers_and_flags():
    m = pt.new_manifest("pawchive", "42")
    pt.merge_chronological(m, _posts([(3, "2026-03-01"), (1, "2026-01-01")]))
    pt.set_status(m["items"]["3"], "downloaded", now="T1")
    assert m["items"]["3"]["number_at_check"] == 2
    res = pt.merge_chronological(m, _posts([(3, "2026-03-01"), (2, "2026-02-01"),
                                            (1, "2026-01-01")]), now="T2")
    assert res["new"] == 1
    assert m["items"]["2"]["backfilled"] is True
    assert m["items"]["2"]["number"] == 2
    assert m["items"]["3"]["number"] == 3
    assert pt.is_shifted(m["items"]["3"])
    assert not pt.is_shifted(m["items"]["1"])
    assert not m["items"]["2"]["initial"]


def test_chronological_gone_posts_keep_their_slot():
    m = pt.new_manifest("pawchive", "42")
    pt.merge_chronological(m, _posts([(3, "2026-03-01"), (2, "2026-02-01"), (1, "2026-01-01")]))
    pt.set_status(m["items"]["3"], "downloaded")
    res = pt.merge_chronological(m, _posts([(3, "2026-03-01"), (1, "2026-01-01")]), now="T2")
    assert res["gone"] == 1
    assert m["items"]["2"]["gone"] is True
    assert m["items"]["3"]["number"] == 3
    assert not pt.is_shifted(m["items"]["3"])


def test_chronological_excluded_posts_hold_no_number_and_numbers_close_up():
    m = pt.new_manifest("pawchive", "42")
    pt.merge_chronological(m, _posts([(3, "2026-03-01"), (2, "2026-02-01"), (1, "2026-01-01")]))
    pt.set_status(m["items"]["3"], "downloaded")               # #3 at check time
    pt.set_excluded(m["items"]["2"], True)                     # clears its number itself
    assert pt.renumber_chronological(m) == 1                   # only 3 → 2 moves
    assert m["items"]["1"]["number"] == 1 and m["items"]["2"]["number"] is None
    assert m["items"]["3"]["number"] == 2 and pt.is_shifted(m["items"]["3"])
    assert m["next_number"] == 3
    # Excluded posts survive a fresh walk and stay out of the count.
    pt.merge_chronological(m, _posts([(4, "2026-04-01"), (3, "2026-03-01"),
                                      (2, "2026-02-01"), (1, "2026-01-01")]))
    assert m["items"]["2"]["excluded"] and m["items"]["2"]["number"] is None
    assert m["items"]["4"]["number"] == 3
    c = pt.counts(m)
    assert c["excluded"] == 1 and c["total"] == 4 and c["unreviewed"] == 2
    # Counting it again slots it back in.
    pt.set_excluded(m["items"]["2"], False)
    pt.renumber_chronological(m)
    assert [m["items"][i]["number"] for i in ("1", "2", "3", "4")] == [1, 2, 3, 4]
    assert pt.renumber_chronological(m) == 0


def test_chronological_new_post_without_date_sorts_first():
    m = pt.new_manifest("pawchive", "42")
    pt.merge_chronological(m, _posts([(2, None), (1, "2026-01-01")]))
    assert m["items"]["2"]["number"] == 1


# ── status / counts / prefix ────────────────────────────────────────

def test_set_status_and_number_at_check():
    it = {"id": "1", "number": 7, "status": "unreviewed"}
    pt.set_status(it, "downloaded", now="T")
    assert it["status_at"] == "T" and it["number_at_check"] == 7
    pt.set_status(it, "skipped", now="T2")
    assert it["number_at_check"] is None
    pt.set_status(it, "unreviewed")
    assert it["status_at"] is None
    with pytest.raises(ValueError):
        pt.set_status(it, "bogus")


def test_counts():
    m = pt.new_manifest("rule34video", "1")
    pt.merge_locked(m, _fetched([3, 2, 1]), full=True, complete=True)
    pt.merge_locked(m, _fetched([5, 4, 3]), full=False, complete=False)
    pt.set_status(m["items"]["1"], "downloaded")
    pt.set_status(m["items"]["2"], "skipped")
    pt.set_status(m["items"]["4"], "downloaded")
    m["items"]["4"]["number"] = 99                 # simulate a shift
    m["items"]["3"]["gone"] = True
    c = pt.counts(m)
    assert c["total"] == 5
    assert c["downloaded"] == 2 and c["skipped"] == 1
    assert c["unreviewed"] == 1                    # only #5 (3 is gone)
    assert c["new"] == 1                           # #5: unreviewed and not initial
    assert c["shifted"] == 1 and c["gone"] == 1


def test_number_width_and_prefix():
    # Always two digits: 03, and 103 prints as itself — never 003 / 3-wide padding.
    assert pt.number_width({"a": {"number": 99}}) == 2
    assert pt.number_width({"a": {"number": 100}}) == 2
    assert pt.number_width({}) == 2
    assert pt.format_prefix("SadBernard", "R34", 2, 2) == "SadBernard - R34 - 02 - "
    assert pt.format_prefix("X", "Iwara", 103, 2) == "X - Iwara - 103 - "
    assert pt.format_prefix("X", "R34", None) == "X - R34 - "
    assert pt.format_prefix("X", "R34", 2).endswith(" - ")             # trailing space kept


def test_dated_prefix_site_first_on_every_site():
    assert pt.prefix_date("2026-05-04T12:00:00+00:00") == "2026.05.04"
    assert pt.prefix_date("2026-05-04") == "2026.05.04"
    assert pt.prefix_date(None) == "" and pt.prefix_date("bogus") == ""
    assert pt.format_prefix("Snuggsmutt", "Patreon", 3, 2, date="2026-05-04T12:00:00+00:00") \
        == "Snuggsmutt - Patreon - 2026.05.04 - "
    assert pt.format_prefix("SadBernard", "R34", 2, 2, date="2026-06-20") == "SadBernard - R34 - 2026.06.20 - "
    # No date known yet (r34 detail fetch pending) → the catalogue number stands in.
    assert pt.format_prefix("SadBernard", "R34", 2, 2, date="") == "SadBernard - R34 - 02 - "
    assert pt.format_prefix("X", "R34", None, date=None) == "X - R34 - "


def test_site_codes_pawchive_uses_origin_service():
    assert pt.default_site_code("pawchive", "patreon") == "Patreon"
    assert pt.default_site_code("pawchive", "fanbox") == "Fanbox"
    assert pt.default_site_code("pawchive") == "Pawchive"
    assert pt.default_site_code("rule34video") == "R34"
    paw = {"platform": "pawchive", "service": "patreon", "user_id": "1"}
    assert pt.link_site_code(paw) == "Patreon"
    assert pt.link_site_code(dict(paw, site_code="Pawchive")) == "Patreon"     # old default migrates
    assert pt.link_site_code(dict(paw, site_code="PTR")) == "PTR"              # explicit code wins
    assert pt.link_site_code({"platform": "iwara", "site_code": ""}) == "Iwara"


def test_shifted_is_ignored_on_chronological_sites():
    it = {"status": "downloaded", "number": 5, "number_at_check": 3}
    assert pt.is_shifted(it)
    assert not pt.is_shifted(it, pt.CHRONOLOGICAL)
    m = pt.new_manifest("pawchive", "42")
    m["items"]["1"] = dict(it, id="1")
    assert pt.counts(m)["shifted"] == 0


def test_is_media_post():
    assert pt.is_media_post({"id": "1"})                                   # non-pawchive
    assert pt.is_media_post({"media_kinds": ["video"], "link_hosts": []})
    assert pt.is_media_post({"media_kinds": [], "link_hosts": ["mega.nz"]})
    assert not pt.is_media_post({"media_kinds": ["image"], "link_hosts": []})


# ── IO ──────────────────────────────────────────────────────────────

def test_link_key_and_paths():
    assert pt.link_key({"platform": "rule34video", "user_id": "2472537"}) == "rule34video_2472537"
    assert pt.link_key({"platform": "pawchive", "service": "Patreon", "user_id": "42"}) == "pawchive_patreon_42"
    assert pt.link_key({"platform": "iwara", "user_id": "af0f/../x"}) == "iwara_af0f_.._x"
    assert pt.manifest_path("R", "k").replace("\\", "/") == "R/k.json"


def test_save_and_load_roundtrip(tmp_path):
    p = str(tmp_path / "pmv" / "pc_1" / "rule34video_1.json")
    m = pt.new_manifest("rule34video", "1")
    pt.merge_locked(m, _fetched([2, 1]), full=True, complete=True)
    pt.save_manifest(p, m)
    back = pt.load_manifest(p, "rule34video", "1")
    assert back["items"]["1"]["number"] == 1
    assert back["next_number"] == 3
    assert not [n for n in os.listdir(os.path.dirname(p)) if n.endswith(".tmp")]


def test_load_missing_is_fresh(tmp_path):
    m = pt.load_manifest(str(tmp_path / "nope.json"), "pawchive", "42")
    assert m["numbering"] == pt.CHRONOLOGICAL and m["items"] == {}


def test_load_corrupt_is_renamed_not_deleted(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    m = pt.load_manifest(str(p), "iwara", "u")
    assert m["items"] == {} and "unreadable" in m["last_error"]
    assert not p.exists()
    assert [n for n in os.listdir(tmp_path) if n.startswith("bad.json.corrupt-")]


def test_load_normalizes_partial_records(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"items": {"1": {"number": 1, "status": "weird"}, "2": "junk"}}),
                 encoding="utf-8")
    m = pt.load_manifest(str(p), "rule34video", "1")
    assert m["items"]["1"]["status"] == "unreviewed" and m["items"]["1"]["id"] == "1"
    assert "2" not in m["items"]
    assert m["numbering"] == pt.LOCKED


def test_manifest_lock_is_per_path():
    a = pt.manifest_lock("X/a.json")
    assert a is pt.manifest_lock("X/a.json")
    assert a is not pt.manifest_lock("X/b.json")
