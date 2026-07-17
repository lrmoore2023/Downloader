"""Stateful per-creator manifest of pawchive external links.

Every pawchive post can carry external links (mega, google drive, gofile,
catbox, porn3dx, tweets, ...) that aren't on-site files. The runner auto-grabs
the safely-verifiable *direct* ones; everything else must be downloaded by hand.
This module is the authoritative record of those links and, crucially, remembers
which the user has already dealt with so the UI never nags about them twice.

Two files live at the creator's destination root (no subfolder, by design):
    <dest>/_pawchive_links.json   authoritative state (this module owns it)
    <dest>/_pawchive_links.md     human-readable rollup, regenerated on each write

Per-link status is what the *runner* determined:
    grabbed  – a direct file we downloaded & verified (not shown as outstanding)
    pending  – a manual/reference link the user must handle
    failed   – a direct file we tried but couldn't complete (MUST be surfaced)
`resolved` is a separate user-set flag (the UI checkbox): once true, the link is
never shown as outstanding again, and re-scans never reset it.
"""

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from backend.pawchive_scraper import filename_prefix

VERSION = 1
KEY_SEP = "\t"   # opaque key = f"{post_id}{KEY_SEP}{url}" (tab: never in a URL)


def _replace_with_retry(src, dst, attempts=10, delay=0.15):
    """os.replace hardened against transient Windows/SMB locks (WinError 5).

    On a NAS share the destination is momentarily held open by antivirus, the
    Search indexer, or a cloud-sync agent, so MoveFileEx fails with access-denied
    even though nothing is wrong. Retry a few times with a short backoff."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))


def make_key(post_id, url):
    return f"{post_id}{KEY_SEP}{url}"


def split_key(key):
    post_id, _, url = (key or "").partition(KEY_SEP)
    return post_id, url


def _html_to_text(html):
    if not html:
        return ""
    try:
        return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    except Exception:
        return ""


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class PawchiveLinks:
    """Loads/merges/persists the external-links manifest for one creator."""

    def __init__(self, json_path):
        self.json_path = json_path
        self.md_path = os.path.splitext(json_path)[0] + ".md"
        self._lock = threading.RLock()
        # When False, upsert_post mutates in memory only and the caller flushes
        # with save() — so a bulk crawl writes the NAS once, not once per post.
        self.autosave = True
        self.data = self._load()

    # ── persistence ──────────────────────────────────────────────
    def _load(self):
        try:
            with open(self.json_path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict) and isinstance(d.get("posts"), dict):
                d.setdefault("version", VERSION)
                return d
        except (FileNotFoundError, ValueError, OSError):
            pass
        return {"version": VERSION, "posts": {}}

    def save(self):
        """Flush the manifest to disk (JSON + Markdown). Call after a batch of
        upsert_post()s when autosave is off."""
        with self._lock:
            self._save()

    def reload_resolved_from_disk(self):
        """Pull user 'resolved' flags that another instance wrote to disk (the API's
        check-off action) into our in-memory data, so a subsequent save() from a
        long-lived instance (the runner holds one for the whole crawl) doesn't clobber
        the checkboxes the user ticked meanwhile. Only ever sets resolved=True in — it
        never un-resolves, so it can't fight a concurrent writer."""
        with self._lock:
            disk = self._load()
            for pid, dpost in disk.get("posts", {}).items():
                mpost = self.data.get("posts", {}).get(pid)
                if not isinstance(mpost, dict):
                    continue
                mlinks = mpost.get("links", {})
                for url, dlink in (dpost.get("links", {}) or {}).items():
                    if not isinstance(dlink, dict) or not dlink.get("resolved"):
                        continue
                    mlink = mlinks.get(url)
                    if isinstance(mlink, dict):
                        mlink["resolved"] = True
                        mlink["resolved_at"] = dlink.get("resolved_at")

    def _save(self):
        """Atomic write of the JSON + regenerated Markdown. Best-effort: if the
        destination isn't writable (e.g. NAS offline) this raises OSError, which
        the caller logs but doesn't treat as fatal."""
        os.makedirs(os.path.dirname(os.path.abspath(self.json_path)), exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(os.path.abspath(self.json_path)), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
            _replace_with_retry(tmp, self.json_path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        try:
            self._write_markdown()
        except OSError:
            pass

    # ── mutation ─────────────────────────────────────────────────
    def upsert_post(self, post, link_states=None):
        """Record a post's text + external links, merging over any prior state.

        `post` is a normalised dict from pawchive_scraper.parse_post().
        `link_states` maps url -> {status, filename?, missing?, note?} for links
        the runner acted on this run (esp. direct grabs); links absent from it
        default to status 'pending'. Existing `resolved`/`resolved_at` flags are
        always preserved.
        """
        link_states = link_states or {}
        with self._lock:
            posts = self.data.setdefault("posts", {})
            pid = post["post_id"]
            prev_links = (posts.get(pid) or {}).get("links", {})

            links = {}
            for l in post.get("external_links", []):
                url = l["url"]
                prev = prev_links.get(url, {})
                st = link_states.get(url, {})
                default_status = "pending"
                links[url] = {
                    "url": url,
                    "label": l.get("label", ""),
                    "host": l.get("host", ""),
                    "kind": l.get("kind", "reference"),
                    "status": st.get("status", prev.get("status", default_status)),
                    "filename": st.get("filename", prev.get("filename")),
                    "missing": st.get("missing", prev.get("missing")),
                    "note": st.get("note", prev.get("note")),
                    # user checkbox — never reset by a re-scan
                    "resolved": bool(prev.get("resolved", False)),
                    "resolved_at": prev.get("resolved_at"),
                }

            posts[pid] = {
                "post_id": pid,
                "title": post.get("title", ""),
                "url": post.get("url", ""),
                "service": post.get("service", ""),
                "date": post["dt"].strftime("%Y-%m-%d %H:%M:%S") if post.get("dt") else "",
                "prefix": filename_prefix(post.get("dt"), post.get("service")),
                "body_text": _html_to_text(post.get("content_html", "")),
                "links": links,
            }
            if self.autosave:
                self._save()

    def mark_resolved(self, key, resolved=True):
        """Set/clear a link's user 'resolved' flag. Returns True if found."""
        post_id, url = split_key(key)
        with self._lock:
            link = (self.data.get("posts", {}).get(post_id, {})
                    .get("links", {}).get(url))
            if not link:
                return False
            link["resolved"] = bool(resolved)
            link["resolved_at"] = _now_iso() if resolved else None
            self._save()
            return True

    def mark_many_resolved(self, keys, resolved=True):
        """Set/clear the 'resolved' flag on several links in a single write
        (post-level checkbox and undo). Returns the number actually updated."""
        n = 0
        with self._lock:
            for key in keys or []:
                post_id, url = split_key(key)
                link = (self.data.get("posts", {}).get(post_id, {})
                        .get("links", {}).get(url))
                if not link:
                    continue
                link["resolved"] = bool(resolved)
                link["resolved_at"] = _now_iso() if resolved else None
                n += 1
            if n:
                self._save()
        return n

    # ── queries ──────────────────────────────────────────────────
    def is_resolved(self, post_id, url):
        """True if the user checked this link off (so the runner must not re-grab it —
        same rule as a dismissed error). Unknown links are NOT resolved (up for grabs)."""
        with self._lock:
            link = (self.data.get("posts", {}).get(post_id, {})
                    .get("links", {}).get(url))
            return bool(link and link.get("resolved"))

    @staticmethod
    def _is_outstanding(link):
        if link.get("resolved"):
            return False
        if link.get("status") == "grabbed":
            return False
        # A direct file is the runner's job: only surface it when the auto-grab
        # actually FAILED (pending == still in progress / will be retried). Every
        # manual/reference link always needs the user until resolved.
        if link.get("kind") == "direct":
            return link.get("status") == "failed"
        return True

    def pending(self):
        """Outstanding links for the UI panel, newest post first."""
        out = []
        with self._lock:
            for p in self.data.get("posts", {}).values():
                for link in p.get("links", {}).values():
                    if not self._is_outstanding(link):
                        continue
                    out.append({
                        "key": make_key(p["post_id"], link["url"]),
                        "post_id": p["post_id"],
                        "title": p.get("title", ""),
                        "post_url": p.get("url", ""),
                        "date": p.get("date", ""),
                        "prefix": p.get("prefix", ""),
                        "url": link["url"],
                        "label": link.get("label", ""),
                        "host": link.get("host", ""),
                        "kind": link.get("kind", "reference"),
                        "status": link.get("status", "pending"),
                        "missing": link.get("missing"),
                    })
        out.sort(key=lambda i: i["date"], reverse=True)
        return out

    def counts(self):
        pend = self.pending()
        return {"outstanding": len(pend),
                "failed": sum(1 for i in pend if i["status"] == "failed")}

    # ── markdown rollup ──────────────────────────────────────────
    def _write_markdown(self):
        pend = self.pending()
        manual = [i for i in pend if i["kind"] != "reference"]
        refs = [i for i in pend if i["kind"] == "reference"]
        resolved = []
        for p in self.data.get("posts", {}).values():
            for link in p.get("links", {}).values():
                if link.get("resolved"):
                    resolved.append((p, link))

        lines = ["# Pawchive external links", ""]
        lines.append(f"_Regenerated {_now_iso()}_  ·  "
                     f"{len(manual)} needing manual download, {len(refs)} reference, "
                     f"{len(resolved)} resolved")
        lines.append("")

        lines.append(f"## ⚠ Needs manual download ({len(manual)})")
        lines.append("")
        if not manual:
            lines.append("_None — you're all caught up._")
        for i in manual:
            tag = " **[auto-download FAILED]**" if i["status"] == "failed" else ""
            lines.append(f"- **{i['title'] or '(untitled)'}** · {i['date']}{tag}")
            lines.append(f"  - {i['host']} — <{i['url']}>"
                         + (f"  ({i['label']})" if i['label'] else ""))
            lines.append(f"  - rename prefix: `{i['prefix']}`")
            if i.get("missing"):
                lines.append(f"  - still missing: {', '.join(i['missing'])}")
            lines.append(f"  - post: <{i['post_url']}>")
        lines.append("")

        lines.append(f"## 🔗 Reference links ({len(refs)})")
        lines.append("")
        if not refs:
            lines.append("_None._")
        for i in refs:
            lines.append(f"- **{i['title'] or '(untitled)'}** · {i['date']} — "
                         f"{i['host']} <{i['url']}>"
                         + (f"  ({i['label']})" if i['label'] else ""))
        lines.append("")

        lines.append(f"## ✓ Resolved ({len(resolved)})")
        lines.append("")
        for p, link in sorted(resolved, key=lambda t: t[0].get("date", ""), reverse=True):
            lines.append(f"- {p.get('date','')} — {link.get('host','')} "
                         f"<{link['url']}>  ({p.get('title','')})")

        with open(self.md_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
