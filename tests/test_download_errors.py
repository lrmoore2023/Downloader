"""Offline unit checks for the persistent failure store + the pawchive
redownload-errors matching logic (no network). Run directly:

    python tests/test_download_errors.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.download_errors import FailureStore, FAILED, GONE
from backend.pawchive_runner import PawchiveRunner

_results = []


def check(name, ok, detail=""):
    _results.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def test_failure_store():
    d = tempfile.mkdtemp()
    s = FailureStore(os.path.join(d, "x.errors.db"))

    s.record_failure("e1", platform="pawchive", post_id="123", filename="a.gif",
                     url="https://f/data/x.gif?f=a.gif", page_url="https://p/123",
                     media_kind="image", status=404, reason="http_gone")
    check("record: one row", s.count() == 1)
    check("record: state failed", s.list_failures()[0]["state"] == FAILED)

    # re-failure of the same entry bumps attempts, keeps first_seen
    first_seen = s.list_failures()[0]["first_seen"]
    s.record_failure("e1", platform="pawchive", post_id="123", filename="a.gif",
                     url="https://f/data/x.gif?f=a.gif", status=404, reason="http_gone")
    row = s.list_failures()[0]
    check("upsert: attempts bumped", row["attempts"] == 2, f"attempts={row['attempts']}")
    check("upsert: first_seen preserved", row["first_seen"] == first_seen)
    check("upsert: still one row", s.count() == 1)

    # a second, different failure
    s.record_failure("e2", platform="coomerfans", post_id="9", filename="b.mp4",
                     url="u", page_url="p", media_kind="video", status=410,
                     reason="http_gone")
    check("two entries", s.count() == 2)

    s.mark_gone("e1", page_url="https://p/123")
    check("mark_gone: state gone", s.list_failures(state=GONE)[0]["entry"] == "e1")
    check("mark_gone: failed count", s.count(state=FAILED) == 1)
    check("mark_gone: gone count", s.count(state=GONE) == 1)

    # a fresh failure on a 'gone' entry resets it to failed (worth another look)
    s.record_failure("e1", platform="pawchive", post_id="123", filename="a.gif",
                     url="u2", reason="http_gone")
    check("re-failure resets gone->failed", s.count(state=FAILED) == 2)

    s.clear_failure("e1")
    check("clear removes the row", s.count() == 1 and not s.list_failures(state=GONE))

    s.clear_all(platform="coomerfans")
    check("clear_all by platform", s.count() == 0)
    s.close()


def test_pawchive_matching():
    r = PawchiveRunner()
    post = {"media": [
        {"name": "a.gif", "url": "u1", "kind": "image"},
        {"name": "ExJ - The Reunion.mp4", "url": "u2", "kind": "video"},
    ]}

    check("name_from_url decodes ?f=",
          PawchiveRunner._name_from_url(
              "https://file/data/x.mp4?f=ExJ%20-%20The%20Reunion.mp4")
          == "ExJ - The Reunion.mp4")
    check("name_from_url none when absent",
          PawchiveRunner._name_from_url("https://file/data/x.mp4") is None)

    m = r._match_media(post, "pawchive_9_1",
                       "https://file/data/x.mp4?f=ExJ%20-%20The%20Reunion.mp4")
    check("match by name", m is not None and m["url"] == "u2")

    # no ?f -> fall back to the 1-based index in the entry key
    m = r._match_media(post, "pawchive_9_1", "https://file/data/x.gif")
    check("match by index fallback", m is not None and m["url"] == "u1")

    m = r._match_media(post, "pawchive_9_5", "https://file/data/x.gif?f=nope.gif")
    check("no match -> None", m is None)


def test_ext_not_recorded():
    """External-link (jobkind='ext') failures must NOT go into the media store —
    they're surfaced by the pawchive links manifest instead."""
    d = tempfile.mkdtemp()
    r = PawchiveRunner()
    r._errors = FailureStore(os.path.join(d, "y.errors.db"))
    r._service = "patreon"
    r._user_id = "1"
    r._record_failure("pawchive_1_ext_1", {"jobkind": "ext", "post_id": "1"},
                      "cat.mp4", "video", "https://catbox/cat.mp4",
                      {"reason": "http_gone", "status": 404})
    check("ext failure not recorded", r._errors.count() == 0)
    r._record_failure("pawchive_1_1", {"jobkind": "media", "post_id": "1"},
                      "a.gif", "image", "https://f/x.gif?f=a.gif",
                      {"reason": "http_gone", "status": 404})
    check("media failure recorded", r._errors.count() == 1)
    row = r._errors.list_failures()[0]
    check("media failure has page_url",
          row["page_url"] == "https://pawchive.pw/patreon/user/1/post/1",
          f"page_url={row['page_url']}")
    r._errors.close()


if __name__ == "__main__":
    print("Running download-errors tests...")
    test_failure_store()
    test_pawchive_matching()
    test_ext_not_recorded()
    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed.")
    sys.exit(0 if passed == len(_results) else 1)
