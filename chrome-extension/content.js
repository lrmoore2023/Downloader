/* PMV Prefix — content script.
   On a video page, mounts a button that copies "SITE - YYYY.MM.DD - " (with the
   trailing space) to the clipboard. The date is the upload date the site itself
   records, read from the page's structured data / meta tags or, for the
   single-page apps, from the site's own JSON API. Same convention as the
   Downloader's PMV tab: the date part is the UTC calendar day of the timestamp. */
(() => {
  'use strict';

  const ID = 'pmvx-copy-btn';

  // ── pure helpers (also exercised by tools/pmvx_check.js) ─────────

  function isoDay(value) {
    // '2026-05-04T13:47:43.703Z' | '2026-05-04 00:38:23' | '2026-05-04' → '2026.05.04'
    const m = /(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
    return m ? `${m[1]}.${m[2]}.${m[3]}` : '';
  }

  function ldJsonValue(html, key) {
    const re = new RegExp(`"${key}"\\s*:\\s*"([^"]+)"`);
    const m = re.exec(html || '');
    return m ? m[1] : '';
  }

  function metaContent(html, name) {
    const re = new RegExp(`<meta[^>]+name=["']${name}["'][^>]+content=["']([^"']+)["']`, 'i');
    const m = re.exec(html || '');
    if (m) return m[1];
    const re2 = new RegExp(`<meta[^>]+content=["']([^"']+)["'][^>]+name=["']${name}["']`, 'i');
    const m2 = re2.exec(html || '');
    return m2 ? m2[1] : '';
  }

  function titleCase(s) {
    return s ? s.charAt(0).toUpperCase() + s.slice(1).toLowerCase() : '';
  }

  // Per-site adapters. `match(path)` → capture groups or null; `code(m)` → site
  // code; `dateFromHtml(html, m)` for server-rendered pages; `dateFromApi(m)`
  // for SPAs; `anchors` = where to mount, first match wins (else floating).
  const SITES = [
    {
      host: /(^|\.)rule34video\.com$/,
      match: (p) => /^\/video\/(\d+)\//.exec(p),
      code: () => 'R34',
      dateFromHtml: (html) => ldJsonValue(html, 'uploadDate'),
      anchors: ['h1.title_video', '.heading h1', 'h1'],
    },
    {
      host: /(^|\.)iwara\.tv$/,
      match: (p) => /^\/videos?\/([A-Za-z0-9]+)/.exec(p),
      code: () => 'Iwara',
      dateFromApi: async (m) => {
        const r = await fetchJson(`https://api.iwara.tv/video/${m[1]}`);
        return r && r.createdAt;
      },
      anchors: ['.page-video__details h1', '.page-video__details .text', '.page-video h1', 'h1'],
    },
    {
      host: /(^|\.)hmvmania\.com$/,
      match: (p) => /^\/video\/[^/]+\/?$/.exec(p),
      code: () => 'HMVMania',
      dateFromHtml: (html) => ldJsonValue(html, 'datePublished')
        || metaContent(html, 'article:published_time'),
      anchors: ['h1.video-entry-title', '.entry-content h1', 'h1'],
    },
    {
      host: /(^|\.)pmvhaven\.com$/,
      match: (p) => /^\/video\/[^/]*_([0-9a-fA-F]{24})/.exec(p),
      code: () => 'PMVHaven',
      dateFromApi: async (m) => {
        const r = await fetchJson(`https://pmvhaven.com/api/videos/${m[1]}`);
        const d = r && (r.data || r.video || r);
        return d && (d.uploadDate || d.releaseDate);
      },
      dateFromHtml: (html) => ldJsonValue(html, 'uploadDate'),
      // The page has a "Dashboard" h1 in the nav; the title is the last h1 in main.
      anchorFn: () => {
        const hs = Array.from(document.querySelectorAll('main h1, h1'))
          .filter((h) => h.textContent.trim() && !/^dashboard$/i.test(h.textContent.trim()));
        return hs.length ? hs[hs.length - 1] : null;
      },
    },
    {
      host: /(^|\.)pawchive\.(pw|st)$/,
      match: (p) => /^\/([a-z0-9_]+)\/user\/[^/]+\/post\/[^/?#]+/.exec(p),
      code: (m) => titleCase(m[1]),                     // patreon → Patreon, fanbox → Fanbox
      dateFromHtml: (html) => metaContent(html, 'published')
        || ldJsonValue(html, 'datePublished')
        || ((/Published:<\/div>\s*([\d-]+)/.exec(html) || [])[1] || ''),
      anchors: ['h1.post__title', '.post__info h1', 'h1'],
    },
  ];

  function fetchJson(url) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type: 'fetchJson', url }, (res) => {
          resolve(res && res.ok ? res.data : null);
        });
      } catch (e) {
        resolve(null);
      }
    });
  }

  // ── mounting ──────────────────────────────────────────────────────

  let lastUrl = '';
  let busy = false;

  function siteFor(host) {
    return SITES.find((s) => s.host.test(host)) || null;
  }

  function removeButton() {
    const old = document.getElementById(ID);
    if (old) old.remove();
  }

  function findAnchor(site) {
    if (site.anchorFn) {
      try { const el = site.anchorFn(); if (el) return el; } catch (e) { /* fall through */ }
    }
    for (const sel of site.anchors || []) {
      const el = document.querySelector(sel);
      if (el && el.textContent.trim()) return el;
    }
    return null;
  }

  function makeButton(prefix, dateKnown) {
    const btn = document.createElement('button');
    btn.id = ID;
    btn.type = 'button';
    btn.className = 'pmvx-btn' + (dateKnown ? '' : ' pmvx-nodate');
    btn.textContent = prefix.trimEnd();
    btn.title = dateKnown
      ? `Copy "${prefix}" (with the trailing space) to the clipboard`
      : 'Upload date not found on this page — copies the site code only';
    btn.addEventListener('click', async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      try {
        await navigator.clipboard.writeText(prefix);
        flash(btn, 'Copied ✓');
      } catch (e) {
        // Clipboard API can refuse outside a secure context; fall back to a textarea.
        const ta = document.createElement('textarea');
        ta.value = prefix;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); flash(btn, 'Copied ✓'); }
        catch (e2) { flash(btn, 'Copy failed'); }
        ta.remove();
      }
    });
    return btn;
  }

  function flash(btn, text) {
    const orig = btn.textContent;
    btn.textContent = text;
    btn.classList.add('pmvx-flash');
    setTimeout(() => { btn.textContent = orig; btn.classList.remove('pmvx-flash'); }, 1400);
  }

  function mount(site, prefix, dateKnown) {
    removeButton();
    const btn = makeButton(prefix, dateKnown);
    const anchor = findAnchor(site);
    if (anchor) {
      btn.classList.add('pmvx-inline');
      anchor.insertAdjacentElement('afterend', btn);
    } else {
      btn.classList.add('pmvx-floating');
      document.body.appendChild(btn);
    }
  }

  async function render() {
    const site = siteFor(location.hostname);
    if (!site) return;
    const m = site.match(location.pathname);
    if (!m) { removeButton(); return; }
    if (busy) return;
    busy = true;
    try {
      const code = site.code(m);
      let raw = '';
      if (site.dateFromHtml) raw = site.dateFromHtml(document.documentElement.innerHTML, m) || '';
      if (!raw && site.dateFromApi) raw = (await site.dateFromApi(m)) || '';
      // Guard against the SPA having navigated while we awaited.
      if (!site.match(location.pathname) || site.match(location.pathname)[0] !== m[0]) return;
      const day = isoDay(raw);
      const prefix = day ? `${code} - ${day} - ` : `${code} - `;
      mount(site, prefix, !!day);
    } finally {
      busy = false;
    }
  }

  function tick() {
    const url = location.href;
    if (url !== lastUrl) {
      lastUrl = url;
      render();
      return;
    }
    // SPA re-renders can drop the button (or its anchor) — put it back.
    const site = siteFor(location.hostname);
    if (site && site.match(location.pathname) && !document.getElementById(ID)) render();
  }

  // history hooks so client-side navigation re-evaluates immediately
  for (const fn of ['pushState', 'replaceState']) {
    const orig = history[fn];
    history[fn] = function () { const r = orig.apply(this, arguments); setTimeout(tick, 50); return r; };
  }
  window.addEventListener('popstate', () => setTimeout(tick, 50));
  setInterval(tick, 1000);
  tick();

  // expose pure helpers for the offline check script
  if (typeof module !== 'undefined') module.exports = { isoDay, ldJsonValue, metaContent, titleCase, SITES };
})();
