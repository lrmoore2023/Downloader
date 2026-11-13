"""Twitter/X creator avatars: the gallery-dl /info lookup and placeholder rejection."""
import hashlib
import json
import subprocess
from types import SimpleNamespace

from backend import twitter_pmv as twp
from backend import api as api_mod
from backend.api import Api

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"x" * 64


def _fake_run(stdout):
    def run(cmd, **kw):
        run.cmd = cmd
        return SimpleNamespace(stdout=stdout, returncode=0)
    return run


def test_profile_image_reads_child_json():
    line = json.dumps({"t": "user", "name": "someone",
                       "profile_image": "https://pbs.twimg.com/profile_images/1/a.jpg"})
    run = _fake_run("noise\n" + line + "\n")
    assert twp.profile_image("someone", cookies_path="c.txt", run=run) \
        == "https://pbs.twimg.com/profile_images/1/a.jpg"
    assert run.cmd[-1] == "someone" and run.cmd[-3].endswith("twitter_profile_dump.py")


def test_profile_image_error_or_timeout_is_blank():
    assert twp.profile_image("someone", run=_fake_run('{"t": "error", "msg": "x"}\n')) == ""

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    assert twp.profile_image("someone", run=boom) == ""
    assert twp.profile_image("", run=boom) == ""


def test_cached_placeholder_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "AVATAR_DIR", str(tmp_path))
    placeholder = b"\xff\xd8\xff\xe0placeholder-card"
    api = Api.__new__(Api)
    monkeypatch.setattr(Api, "_TWITTER_PLACEHOLDER_MD5", {hashlib.md5(placeholder).hexdigest()})
    (tmp_path / "twitter_someone.img").write_bytes(placeholder)
    fetched = []
    monkeypatch.setattr(api, "_fetch_twitter_avatar", lambda u: fetched.append(u) or JPEG,
                        raising=False)
    r = api._twitter_avatar("someone")
    assert fetched == ["someone"]
    assert r["data"].startswith("data:image/jpeg;base64,")
    assert (tmp_path / "twitter_someone.img").read_bytes() == JPEG


def test_placeholder_from_source_is_never_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(api_mod, "AVATAR_DIR", str(tmp_path))
    placeholder = b"\xff\xd8\xff\xe0placeholder-card"
    api = Api.__new__(Api)
    monkeypatch.setattr(Api, "_TWITTER_PLACEHOLDER_MD5", {hashlib.md5(placeholder).hexdigest()})
    monkeypatch.setattr(api, "_fetch_twitter_avatar", lambda u: placeholder, raising=False)
    assert api._twitter_avatar("someone") == {"none": True}
    assert not (tmp_path / "twitter_someone.img").exists()
