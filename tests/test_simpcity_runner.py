"""SimpcityRunner end to end against a fake forum + fake hosts (no network)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from backend import http_download as hd
from backend import simpcity_runner as sr
from backend.download_errors import FailureStore
from backend.simpcity_archive import ThreadDB
from tests.test_simpcity_scraper import page, post

THREAD = "https://simpcity.cr/threads/t.1/"


class Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class Forum:
    def __init__(self, pages):
        self.pages = pages            # {n: html}
        self.hits = []

    def get(self, url, timeout=None, **kw):
        n = int(url.rsplit("page-", 1)[1]) if "page-" in url else 1
        self.hits.append(n)
        html = self.pages.get(n)
        return Resp(200, html) if html is not None else Resp(404)


class FakeGoon:
    def __init__(self, *a, **k):
        pass

    def image(self, ref):
        if ref == "DEAD":
            raise sr.gb.GoonboxGone(ref)
        return {"url": f"https://cdn/{ref}.jpg", "filename": f"{ref}.jpg", "size": 3,
                "mime": "image/jpeg"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    calls = []

    def fake_resolve(session, ref):
        if ref == "GONEVID":
            raise sr.th.TurboGone(ref)
        return {"url": f"https://tcdn/{ref}.mp4", "filename": f"{ref}.mp4", "size": 3,
                "md5": None, "upload_date": "2026-01-01"}

    def fake_download(session, url, path, **kw):
        calls.append(url)
        if "FAILVID" in url:
            return hd.Result("failed", reason="HTTP 500")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"abc")
        return hd.Result("ok", 3, 200)

    monkeypatch.setattr(sr.th, "resolve", fake_resolve)
    monkeypatch.setattr(sr.th, "make_session", lambda: object())
    monkeypatch.setattr(sr.gb, "GoonboxAPI", FakeGoon)
    monkeypatch.setattr(sr.hd, "download", fake_download)
    dest = tmp_path / "Creator"
    dest.mkdir()
    return {"dest": str(dest), "db": str(tmp_path / "simpcity_1.db"),
            "err": str(tmp_path / "simpcity_1.errors.db"), "calls": calls}


def run(env, forum, mode="full", **kw):
    events, errors, done = [], [], {}
    r = sr.SimpcityRunner(workers=2)
    r.run({"url": THREAD}, env["dest"], mode, env["db"], events.append,
          done.update, errors.append, errors_path=env["err"], session=forum,
          page_interval=0.01, **kw)
    return done, events, errors


P1 = page([post(1, 1704110400, "<a href='https://goonbox.cr/img/IMG1'><img class='bbImage' src='https://simp6.cuckcapital.cr/t.jpg'></a>"
                               "<a href='https://bunkr.cr/a/ALB'>album</a>")], cur=1, last=2)
P2 = page([post(2, 1735732800, "<iframe src='https://turbo.cr/embed/VID1'></iframe>"
                               "<iframe src='https://turbo.cr/embed/VID1'></iframe>"),
           post(3, 1735732900, "<a href='https://turbo.cr/d/VID1'>repost</a>")], cur=2, last=2)


def test_full_walk_downloads_and_records(env):
    forum = Forum({1: P1, 2: P2})
    done, events, errors = run(env, forum)
    assert done["downloaded"] == 2 and done["errors"] == 0, errors
    root = os.path.join(env["dest"], "Unsorted")
    assert os.path.isfile(os.path.join(root, "Images", "2024", "2024.01.01 - Simpcity - IMG1.jpg"))
    assert os.path.isfile(os.path.join(root, "2025", "2025.01.01 - Simpcity - VID1.mp4"))
    db = ThreadDB(env["db"])
    assert db.get("last_page") == "2" and db.get("title") == "Atowerss"
    assert [l["url"] for l in db.links()] == ["https://bunkr.cr/a/ALB"]
    assert db.post_count() == 3
    db.close()
    kinds = [e["type"] for e in events]
    assert kinds.count("dl_start") == kinds.count("dl_stop") == 2


def test_latest_resumes_from_last_page_and_skips_known(env):
    forum = Forum({1: P1, 2: P2})
    run(env, forum)
    forum.hits.clear()
    env["calls"].clear()
    forum.pages[2] = page([post(2, 1735732800, "<iframe src='https://turbo.cr/embed/VID1'></iframe>"),
                           post(4, 1767268800, "<iframe src='https://turbo.cr/embed/VID2'></iframe>")],
                          cur=2, last=2)
    done, _, _ = run(env, forum, mode="latest")
    assert forum.hits == [2]
    assert done["downloaded"] == 1
    assert env["calls"] == ["https://tcdn/VID2.mp4"]


def test_missing_file_is_refetched_on_full(env):
    forum = Forum({1: P1, 2: P2})
    run(env, forum)
    os.remove(os.path.join(env["dest"], "Unsorted", "2025", "2025.01.01 - Simpcity - VID1.mp4"))
    env["calls"].clear()
    done, _, _ = run(env, forum)
    assert done["downloaded"] == 1 and env["calls"] == ["https://tcdn/VID1.mp4"]


def test_failures_and_gone_are_recorded_and_retried(env):
    forum = Forum({1: page([post(1, 1704110400,
                                 "<iframe src='https://turbo.cr/embed/FAILVID'></iframe>"
                                 "<iframe src='https://turbo.cr/embed/GONEVID'></iframe>"
                                 "<a href='https://goonbox.cr/img/DEAD'><img class='bbImage' src='x.jpg'></a>")],
                           cur=1, last=1)})
    done, _, errors = run(env, forum)
    assert done["errors"] == 3 and done["downloaded"] == 0
    store = FailureStore(env["err"])
    states = sorted(f["state"] for f in store.list_failures())
    store.close()
    assert states == ["failed", "gone", "gone"]
    db = ThreadDB(env["db"])
    assert db.media_counts() == {"failed": 1, "gone": 2}
    db.close()
    # 'errors' mode retries without walking
    forum.hits.clear()
    env["calls"].clear()
    run(env, forum, mode="errors")
    assert forum.hits == []
    assert "https://tcdn/FAILVID.mp4" in env["calls"]


def test_fetch_prefs_skip_videos(env):
    forum = Forum({1: P1, 2: P2})
    done, _, _ = run(env, forum, include_videos=False)
    assert done["downloaded"] == 1
    assert env["calls"] == ["https://cdn/IMG1.jpg"]


def test_signed_out_stops_with_needs_auth(env):
    forum = Forum({1: page([post(1, 1704110400, "x")], logged_in="false")})
    done, _, errors = run(env, forum)
    assert done["needs_simpcity_auth"] is True
    assert any("Sign in" in e["message"] for e in errors)


def test_forbidden_stops_with_needs_auth(env):
    class Forbidden:
        def get(self, *a, **k):
            return Resp(403, "Access Forbidden")
    done, _, _ = run(env, Forbidden())
    assert done["needs_simpcity_auth"] is True


def test_no_cookies_needs_auth(env, tmp_path):
    events, errors, done = [], [], {}
    sr.SimpcityRunner().run({"url": THREAD}, env["dest"], "full", env["db"], events.append,
                            done.update, errors.append, cookies_path=str(tmp_path / "nope.txt"))
    assert done["needs_simpcity_auth"] is True


def test_existing_identical_file_is_adopted(env):
    root = os.path.join(env["dest"], "Unsorted", "2025")
    os.makedirs(root)
    with open(os.path.join(root, "2025.01.01 - Simpcity - VID1.mp4"), "wb") as f:
        f.write(b"abc")
    forum = Forum({1: P2.replace("pageNav-page--current", "x")})
    forum.pages = {1: page([post(2, 1735732800, "<iframe src='https://turbo.cr/embed/VID1'></iframe>")],
                           cur=1, last=1)}
    done, _, _ = run(env, forum)
    assert env["calls"] == [] and done["skipped"] >= 1
    db = ThreadDB(env["db"])
    assert db.media_counts() == {"downloaded": 1}
    db.close()


def test_name_collision_gets_suffix(env):
    root = os.path.join(env["dest"], "Unsorted", "2025")
    os.makedirs(root)
    with open(os.path.join(root, "2025.01.01 - Simpcity - VID1.mp4"), "wb") as f:
        f.write(b"different-size")
    forum = Forum({1: page([post(2, 1735732800, "<iframe src='https://turbo.cr/embed/VID1'></iframe>")],
                           cur=1, last=1)})
    run(env, forum)
    assert os.path.isfile(os.path.join(root, "2025.01.01 - Simpcity - VID1_2.mp4"))



def test_thread_file_moved_into_images_is_recognised(env):
    """A recorded thread file the user moved (e.g. into Images/<year>) is found and
    its record re-pointed — not downloaded again."""
    forum = Forum({1: P1, 2: P2})
    run(env, forum)
    root = os.path.join(env["dest"], "Unsorted")
    src = os.path.join(root, "2025", "2025.01.01 - Simpcity - VID1.mp4")
    os.makedirs(os.path.join(root, "Images", "2025"), exist_ok=True)
    os.replace(src, os.path.join(root, "Images", "2025", "2025.01.01 - Simpcity - VID1.mp4"))
    env["calls"].clear()
    done, _, _ = run(env, forum)
    assert env["calls"] == [] and done["downloaded"] == 0
    db = ThreadDB(env["db"])
    assert db.media("turbo:VID1")["path_key"] == "Images/2025/2025.01.01 - Simpcity - VID1.mp4"
    db.close()
