"""SimpCity thread parsing, classification and the per-thread DB."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import simpcity_scraper as sc
from backend.simpcity_archive import ThreadDB


def post(pid, ts, body, attachments="", sig="<div class='message-signature'><a href='https://sig.example/x.jpg'><img class='bbImage' src='https://sig.example/x.jpg'></a></div>"):
    return f"""
<article class="message message--post js-post" data-author="someone" data-content="post-{pid}" id="js-post-{pid}">
  <div class="message-cell message-cell--user"><img class="avatar" src="https://simpcity.cr/data/avatars/m/1/1.jpg"></div>
  <header class="message-attribution"><time class="u-dt" datetime="x" data-timestamp="{ts}">x</time></header>
  <div class="message-content js-messageContent">
    <div class="message-userContent lbContainer js-lbContainer">
      <article class="message-body js-selectToQuote"><div class="bbWrapper">{body}</div></article>
      {attachments}
    </div>
    {sig}
    <footer class="message-footer"><a href="https://footer.example/zz">x</a></footer>
  </div>
</article>"""


def page(posts, cur=1, last=3, logged_in="true"):
    nav = "".join(
        f'<li class="pageNav-page{" pageNav-page--current" if n == cur else ""}"><a href="/threads/t.1/page-{n}">{n}</a></li>'
        for n in range(1, last + 1))
    nxt = f'<a class="pageNav-jump pageNav-jump--next" href="/threads/t.1/page-{cur + 1}">Next</a>' if cur < last else ""
    return f"""<html id="XF" data-logged-in="{logged_in}"><body>
<h1 class="p-title-value"><span class="label">Request</span> Atowerss</h1>
<div class="pageNav"><ul class="pageNav-main">{nav}</ul>{nxt}</div>
{''.join(posts)}</body></html>"""


BODY = """
Here you go
<a href="https://goonbox.cr/img/tSk0Ku9" class="link"><img class="bbImage" src="https://simp6.cuckcapital.cr/images4/thumb.jpg" data-url="https://simp6.cuckcapital.cr/images4/thumb.jpg"></a>
<div class="bbMediaWrapper"><iframe src="https://turbo.cr/embed/zcIToefVY52jO"></iframe></div>
<a href="https://turbo.cr/d/zcIToefVY52jO">Download</a>
<span class="iframe-wrapper" onclick="loadMedia(this, '//turbo.cr/embed/SECONDvid')">play</span>
<a href="https://bunkr.cr/a/7Z7niuP1">album</a> <a href="https://bunkr.pk/f/E4ZM1KwzCWSAc">file</a>
<a href="https://simpcity.cr/redirect/?to=https%3A%2F%2Fgofile.io%2Fd%2FESrvqaG1">gofile</a>
<a href="https://mega.nz/folder/abc#key">mega</a>
<a href="https://simpcity.cr/threads/other.5/">other thread</a>
<a href="https://simpcity.cr/members/bob.1/" class="username">bob</a>
<img class="smilie" src="https://simpcity.cr/styles/smilie.png">
<img class="bbImage" src="https://i.unknownhost.example/full/pic.png">
<iframe src="https://streamable.com/e/someclip"></iframe>
<blockquote class="bbCodeBlock bbCodeBlock--quote"><a href="https://turbo.cr/d/QUOTED">quoted</a></blockquote>
"""

ATTACH = """<section class="message-attachments"><ul>
<li><a href="/attachments/clip-mp4.991/" target="_blank">clip.mp4</a></li></ul></section>"""


def test_parse_thread_url():
    t = sc.parse_thread_url("https://simpcity.cr/threads/atowerss.244743/page-11#post-9")
    assert t == {"thread_id": "244743", "slug": "atowerss",
                 "canonical": "https://simpcity.cr/threads/atowerss.244743/"}
    assert sc.parse_thread_url("https://simpcity.su/threads/x.5/")["canonical"] == \
        "https://simpcity.cr/threads/x.5/"
    assert sc.parse_thread_url("https://example.com/threads/x.5/") is None
    assert sc.parse_thread_url("https://simpcity.cr/forums/x.5/") is None
    assert sc.page_url("https://simpcity.cr/threads/x.5/", 3) == "https://simpcity.cr/threads/x.5/page-3"
    assert sc.page_url("https://simpcity.cr/threads/x.5/", 1) == "https://simpcity.cr/threads/x.5/"


def test_logged_in_flag():
    assert sc.logged_in_flag('<html id="XF" data-logged-in="true">') is True
    assert sc.logged_in_flag('<html data-x="1"\n data-logged-in="false" >') is False
    assert sc.logged_in_flag("<html>") is None


def test_classify_hosts():
    c = sc.classify
    assert c("https://turbo.cr/d/abc")["kind"] == "media"
    assert c("https://saint2.su/embed/abc")["host"] == "turbo"
    assert c("https://turbo.cr/a/abc")["kind"] == "album"
    assert c("https://goonbox.cr/img/x1") == {"kind": "media", "media": "image",
                                             "host": "goonbox", "ref": "x1",
                                             "url": "https://goonbox.cr/img/x1"}
    assert c("https://goonbox.cr/a/x1")["kind"] == "album"
    assert c("https://bunkr.cr/a/7Z7niuP1")["kind"] == "album"
    assert c("https://bunkrr.su/v/abc")["kind"] == "file"
    assert c("https://bunkr.pk/f/E4ZM1KwzCWSAc") == {"kind": "file", "host": "bunkr",
                                                     "ref": "E4ZM1KwzCWSAc",
                                                     "url": "https://bunkr.pk/f/E4ZM1KwzCWSAc"}
    assert c("https://gofile.io/d/ESrvqaG1")["kind"] == "album"
    assert c("https://pixeldrain.com/l/abc")["kind"] == "album"
    assert c("https://pixeldrain.com/u/abc")["kind"] == "file"
    assert c("https://cyberdrop.cr/a/abc")["kind"] == "album"
    assert c("https://mega.nz/folder/x")["kind"] == "other"
    assert c("https://simpcity.cr/threads/x.1/") is None
    att = c("https://simpcity.cr/attachments/clip-mp4.991/")
    assert att["kind"] == "media" and att["media"] == "video" and att["ref"] == "att991"


def test_parse_page_extracts_content_only():
    html = page([post(101, 1756000000, BODY, ATTACH)], cur=2, last=5)
    p = sc.parse_page(html, base_url="https://simpcity.cr/threads/t.1/page-2")
    assert p["title"] == "Atowerss"
    assert p["page"] == 2 and p["last_page"] == 5
    assert p["next_url"] == "https://simpcity.cr/threads/t.1/page-3"
    assert p["logged_in"] is True
    [only] = p["posts"]
    assert only["post_id"] == "101"
    assert only["date"].startswith("2025-08-24")
    got = [(i["kind"], i["host"], i["ref"]) for i in only["items"]]
    assert got == [
        ("media", "goonbox", "tSk0Ku9"),                 # link target, not the thumbnail
        ("media", "turbo", "zcIToefVY52jO"),             # iframe + download link deduped
        ("media", "turbo", "SECONDvid"),                 # lazy loadMedia embed
        ("album", "bunkr", "7Z7niuP1"),
        ("file", "bunkr", "E4ZM1KwzCWSAc"),
        ("album", "gofile", "ESrvqaG1"),                 # redirect unwrapped
        ("other", "mega", "https://mega.nz/folder/abc#key"),
        ("media", "unknownhost", "https://i.unknownhost.example/full/pic.png"),
        ("embed", "streamable", "https://streamable.com/e/someclip"),
        ("media", "simpcity", "att991"),                 # attachment
    ]
    urls = " ".join(i["url"] for i in only["items"])
    for junk in ("QUOTED", "sig.example", "footer.example", "avatars", "smilie", "members"):
        assert junk not in urls


def test_parse_page_multiple_posts_and_last_page():
    html = page([post(1, 1700000000, "<a href='https://turbo.cr/d/a'>x</a>"),
                 post(2, 1700000100, "no media here")], cur=3, last=3)
    p = sc.parse_page(html)
    assert [x["post_id"] for x in p["posts"]] == ["1", "2"]
    assert p["posts"][1]["items"] == []
    assert p["next_url"] is None and p["last_page"] == 3


def test_parse_page_single_page_thread():
    html = '<html data-logged-in="true"><h1 class="p-title-value">T</h1>' + \
        post(5, 1700000000, "hi") + "</html>"
    p = sc.parse_page(html)
    assert p["page"] == 1 and p["last_page"] == 1 and len(p["posts"]) == 1


# ── ThreadDB ────────────────────────────────────────────────────────

def test_threaddb_link_status_survives_rewalk(tmp_path):
    db = ThreadDB(str(tmp_path / "t.db"))
    item = {"url": "https://bunkr.cr/a/x", "host": "bunkr", "kind": "album", "ref": "x"}
    assert db.upsert_link(item, "1", "2025-01-01T00:00:00+00:00", "u", 1) is True
    db.set_link_status(item["url"], "dismissed")
    db.set_link_alive(item["url"], False)
    assert db.upsert_link(item, "1", "2025-01-01T00:00:00+00:00", "u", 1) is False
    row = db.link(item["url"])
    assert row["status"] == "dismissed" and row["alive"] == "dead" and row["dead_since"]
    first_dead = row["dead_since"]
    db.set_link_alive(item["url"], False)
    assert db.link(item["url"])["dead_since"] == first_dead
    db.set_link_alive(item["url"], True)
    assert db.link(item["url"])["dead_since"] is None
    db.close()


def test_threaddb_media_lifecycle(tmp_path):
    db = ThreadDB(str(tmp_path / "t.db"))
    item = {"url": "https://turbo.cr/d/a", "host": "turbo", "ref": "a", "media": "video"}
    k = db.note_media(item, "7", "2025-01-01T00:00:00+00:00", 2)
    assert db.media(k)["status"] == "new"
    db.note_media(item, "9", "2026-01-01T00:00:00+00:00", 5)       # repost keeps first post
    assert db.media(k)["post_id"] == "7"
    db.mark_failed(k, "HTTP 500")
    assert db.media(k)["status"] == "failed" and db.media(k)["attempts"] == 1
    db.mark_downloaded(k, "x.mp4", "2025/x.mp4", 10, "abc")
    assert db.media_counts() == {"downloaded": 1}
    assert db.filenames_taken() == {"2025/x.mp4"}
    db.set(last_page=4, title="T")
    assert db.get("last_page") == "4" and db.state()["title"] == "T"
    db.close()


# ── markup measured on real thread pages (2026-10-17) ───────────────

REAL_BODY = """
<div class="bbCodeBlock bbCodeBlock--unfurl js-unfurl fauxBlockLink" data-host="gofile.io" data-url="https://gofile.io/d/ESrvqaG1">
  <div class="contentRow"><div class="contentRow-figure"><img alt="gofile.io" src="https://gofile.io/assets/img/logo-small-og.png"/></div>
  <div class="contentRow-main"><h3><a class="link link--external fauxBlockLink-blockLink" href="/redirect/?to=aHR0cHM6Ly9nb2ZpbGUuaW8vZC9FU3J2cWFHMQ&amp;e=1&amp;m=b64">Gofile</a></h3>
  <span class="js-unfurl-favicon"><img class="bbCodeBlockUnfurl-icon" src="https://gofile.io/assets/img/favicon16.png"/></span></div></div>
</div>
<a class="link link--external" href="/redirect/?to=aHR0cHM6Ly9jb29tZXIuc3Qvb25seWZhbnMvdXNlci9hdG93ZXJzcw&amp;e=1&amp;m=b64">coomer</a>
<div class="bbImageWrapper js-lbImage" data-src="https://pbs.twimg.com/media/GW6SB9kX0AAYvWt?format=jpg&amp;name=large">
  <img class="bbImage" data-url="https://pbs.twimg.com/media/GW6SB9kX0AAYvWt?format=jpg&amp;name=large" src="https://pbs.twimg.com/media/GW6SB9kX0AAYvWt?format=jpg&amp;name=large"/></div>
<div class="generic2wide-iframe-div"><iframe class="saint-iframe" src="https://cyberdrop.cr/e/ZpqWkU8hr9JPH"></iframe></div>
<img class="bbImage" src="https://simp6.cuckcapital.cr/images3/IMG_27120b4432253d122084.md.png" data-url="https://simp6.cuckcapital.cr/images3/IMG_27120b4432253d122084.md.png"/>
<a class="link link--external" href="https://simp6.cuckcapital.cr/images4/aaa.jpg%0Ahttps://simp6.cuckcapital.cr/images4/bbb.jpg">two</a>
<a class="link link--internal" href="https://simpcity.cr/[MEDIA=saint_vid]FUGnMPVMHsO[/MEDIA]%20[MEDIA=saint_vid]urwP21LChmU[/MEDIA]">x</a>
<a class="link link--external" href="https://i.redd.it/u48vrq5lo3yf1.gif">gif</a>
<iframe src="https://redgifs.com/ifr/exaltedspeedyinexpectatumpleco"></iframe>
<img class="smilie smilie--emoji" src="https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/1f642.png"/>
"""


def test_real_markup():
    html = page([post(51960587, 1789000000, REAL_BODY, sig="")], cur=1, last=1)
    [p] = sc.parse_page(html, base_url="https://simpcity.cr/threads/t.1/")["posts"]
    got = [(i["kind"], i["host"], i["ref"]) for i in p["items"]]
    assert got == [
        ("album", "gofile", "ESrvqaG1"),            # the unfurl card → its link only
        ("other", "coomer", "https://coomer.st/onlyfans/user/atowerss"),   # base64 redirect
        ("media", "twitter", "GW6SB9kX0AAYvWt"),
        ("media", "cyberdrop", "ZpqWkU8hr9JPH"),     # /e/ = embedded player
        ("media", "goonbox", "/images3/IMG_27120b4432253d122084.png"),     # .md. thumb → original
        ("media", "goonbox", "/images4/aaa.jpg"),    # %0A-joined URLs split
        ("media", "goonbox", "/images4/bbb.jpg"),
        ("media", "redd", "https://i.redd.it/u48vrq5lo3yf1.gif"),          # direct file link
        ("media", "redgifs", "exaltedspeedyinexpectatumpleco"),
        ("media", "turbo", "FUGnMPVMHsO"),           # broken saint BBCode → turbo
        ("media", "turbo", "urwP21LChmU"),
    ]
    tw = next(i for i in p["items"] if i["host"] == "twitter")
    assert tw["url"].endswith("name=orig")
    urls = " ".join(i["url"] for i in p["items"])
    assert "logo-small-og" not in urls and "favicon" not in urls and "twemoji" not in urls
