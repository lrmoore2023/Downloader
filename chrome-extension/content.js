/* PMV Prefix — content script.
   On a video page, mounts a button that copies "SITE - YYYY.MM.DD - " (with the
   trailing space) to the clipboard. The date is the upload date the site itself
   records, read from the page's structured data / meta tags or, for the
   single-page apps, from the site's own JSON API. Same convention as the
   Downloader's PMV tab: the date part is the UTC calendar day of the timestamp.

   When the uploader published more than one video that day, the date alone
   would name two files identically and lose their order, so the prefix carries
   that day's running count — "R34 - 2026.06.20 02 - ", oldest first. Working
   that out needs the uploader's listing, so it happens on click (a handful of
   requests to the site you are already on), not on every page view. */
(() => {
  'use strict';

  const ID = 'pmvx-copy-btn';
  const MAX_PAGES = 60;          // listing walk guard (all sites)
  const R34_MAX_PROBE = 8;       // video pages read either side of a r34 card

  // ── pure helpers (also exercised by tools/pmvx_check.js) ─────────

  function isoDay(value) {
    // '2026-05-04T13:47:43.703Z' | '2026-05-04 00:38:23' | '2026-05-04' → '2026.05.04'
    const m = /(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
    return m ? `${m[1]}.${m[2]}.${m[3]}` : '';
  }

  function pad2(n) {
    return n < 10 ? `0${n}` : String(n);
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

  function decodeXml(s) {
    return String(s || '')
      .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
      .replace(/&#0?39;|&apos;/g, "'").replace(/&amp;/g, '&');
  }

  function tagText(block, tag) {
    const m = new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`, 'i').exec(block || '');
    if (!m) return '';
    const cdata = /^\s*<!\[CDATA\[([\s\S]*?)\]\]>\s*$/.exec(m[1]);
    return decodeXml((cdata ? cdata[1] : m[1]).trim());
  }

  function rfc822Iso(value) {
    // 'Thu, 18 Sep 2026 14:03:00 +0900' → '2026-09-18T14:03:00+09:00', i.e. the
    // site's own wall clock — the same string Python's parsedate_to_datetime +
    // isoformat gives the PMV tab, so both agree on which day a post belongs to.
    const s = String(value || '').trim();
    const t = Date.parse(s);
    if (isNaN(t)) return '';
    const off = /([+-])(\d{2})(\d{2})\s*$/.exec(s);
    const mins = off ? (off[1] === '-' ? -1 : 1) * (Number(off[2]) * 60 + Number(off[3])) : 0;
    const local = new Date(t + mins * 60000).toISOString().replace(/\.\d+Z$/, '');
    return local + (off ? `${off[1]}${off[2]}:${off[3]}` : '+00:00');
  }

  // Video cards in one rule34video listing block, newest (highest id) first.
  function r34Cards(html) {
    const s = String(html || '');
    const at = s.indexOf('list_videos_uploaded_videos_items');
    const body = at >= 0 ? s.slice(at) : s;
    const re = /data-video-card-id="(\d+)"[\s\S]{0,600}?href="([^"]*\/video\/\d+\/[^"]*)"/g;
    const out = [];
    let m;
    while ((m = re.exec(body))) out.push({ id: Number(m[1]), url: m[2] });
    return out;
  }

  // One page of an HMVMania author feed: the items plus the "Page N of M" the
  // channel title carries (past the end the channel is "Page not found").
  function parseFeed(xml) {
    const s = String(xml || '');
    const ch = /<channel>[\s\S]*?<title>([\s\S]*?)<\/title>/i.exec(s);
    const title = decodeXml(ch ? ch[1].trim() : '');
    const paged = /Page\s+(\d+)\s+of\s+(\d+)/.exec(title);
    const items = [];
    const re = /<item>([\s\S]*?)<\/item>/gi;
    let m;
    while ((m = re.exec(s))) {
      const link = tagText(m[1], 'link');
      const guid = tagText(m[1], 'guid');
      const pid = /[?&](?:amp;|#038;)?p=(\d+)/.exec(guid);
      items.push({
        id: pid ? pid[1] : (link.replace(/\/+$/, '').split('/').pop() || ''),
        url: link,
        date: rfc822Iso(tagText(m[1], 'pubDate')),
      });
    }
    return { items, pages: paged ? Number(paged[2]) : 0, notFound: /page not found/i.test(title) };
  }

  // Upload order within a day: the timestamp wherever the site publishes one,
  // then the id (numerically when it is numeric). Mirrors the PMV tab.
  function cmpUpload(a, b) {
    const da = String(a.date || '');
    const db = String(b.date || '');
    if (da !== db) return da < db ? -1 : 1;
    const ia = String(a.id);
    const ib = String(b.id);
    if (/^\d+$/.test(ia) && /^\d+$/.test(ib)) return Number(ia) - Number(ib);
    return ia < ib ? -1 : ia > ib ? 1 : 0;
  }

  // 1-based place of the entry `isMe` picks among one day's uploads, or null
  // when it is alone that day (no counter) or cannot be found.
  function rankOnDay(entries, isMe) {
    const seen = new Map();
    for (const e of entries || []) if (!seen.has(String(e.id))) seen.set(String(e.id), e);
    const list = Array.from(seen.values()).sort(cmpUpload);
    if (list.length < 2) return null;
    const i = list.findIndex(isMe);
    return i < 0 ? null : i + 1;
  }

  // ── listing walk ──────────────────────────────────────────────────

  function memo(fn) {
    const cache = new Map();
    return (n) => {
      if (!cache.has(n)) cache.set(n, fn(n));
      return cache.get(n);
    };
  }

  /* Every one of these listings is newest-first and date-ordered, so a day is a
     run of consecutive pages. `getPage(n)` returns a 0-based page as
     [{id, date}] (null/[] past the end); `pages` is the page count where the
     site states one, which lets us bisect instead of walking from the top. */
  async function entriesOnDay(getPage, pages, day) {
    const limit = pages > 0 ? pages : MAX_PAGES;
    let hit = -1;
    if (pages > 0) {
      let lo = 0;
      let hi = pages - 1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const rows = await getPage(mid);
        if (!rows || !rows.length) { hi = mid - 1; continue; }
        if (isoDay(rows[rows.length - 1].date) > day) lo = mid + 1;        // page is all newer
        else if (isoDay(rows[0].date) < day) hi = mid - 1;                 // page is all older
        else { hit = mid; break; }
      }
    } else {
      for (let n = 0; n < limit; n++) {
        const rows = await getPage(n);
        if (!rows || !rows.length) break;
        if (isoDay(rows[0].date) < day) break;                             // walked past the day
        if (isoDay(rows[rows.length - 1].date) <= day) { hit = n; break; }
      }
    }
    if (hit < 0) return [];
    const out = [];
    for (let n = hit; n >= 0; n--) {                                       // widen towards newer
      const rows = await getPage(n);
      if (!rows || !rows.length) break;
      out.push(...rows.filter((r) => isoDay(r.date) === day));
      if (isoDay(rows[0].date) !== day) break;
    }
    for (let n = hit + 1; n < limit; n++) {                                // ... then older
      const rows = await getPage(n);
      if (!rows || !rows.length) break;
      out.push(...rows.filter((r) => isoDay(r.date) === day));
      if (isoDay(rows[rows.length - 1].date) !== day) break;
    }
    return out;
  }

  /* rule34video states no time of day and its listing blocks carry no date at
     all — but a member's listing is ordered by video id and ids ascend with
     upload time, so one day is a run of neighbouring cards. Find our card by
     bisecting on the id, then read outwards one video page at a time until the
     date changes. The PMV tab ranks the same videos by their (locked, also
     oldest-first) catalogue numbers, which gives the same answer. */
  async function r34DaySeq(memberId, videoId, day) {
    const id = Number(videoId);
    if (!id) return null;
    const getPage = memo(async (n) => r34Cards(await getText(
      `${location.origin}/members/${memberId}/?mode=async&function=get_block`
      + `&block_id=list_videos_uploaded_videos&sort_by=&from_videos=${n}`)));
    let below = 0;                       // last page known to be entirely above our id
    let found = 0;                       // first galloped page that reaches it (1-based)
    for (let n = 1; n <= MAX_PAGES; n *= 2) {
      const rows = await getPage(n);
      if (!rows.length || rows[rows.length - 1].id <= id) { found = n; break; }
      below = n;
    }
    if (!found) return null;
    let lo = below + 1;
    let hi = found;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      const rows = await getPage(mid);
      if (!rows.length || rows[rows.length - 1].id <= id) hi = mid;
      else lo = mid + 1;
    }
    const around = [];
    for (const n of [lo - 1, lo, lo + 1]) {
      if (n >= 1) around.push(...await getPage(n));
    }
    const byId = new Map();
    for (const c of around) if (!byId.has(c.id)) byId.set(c.id, c);
    const cards = Array.from(byId.values()).sort((a, b) => b.id - a.id);
    const idx = cards.findIndex((c) => c.id === id);
    if (idx < 0) return null;
    const sameDay = [id];
    for (const step of [-1, 1]) {
      for (let i = idx + step, n = 0; i >= 0 && i < cards.length && n < R34_MAX_PROBE; i += step, n++) {
        const html = await getText(absolute(cards[i].url));
        if (isoDay(ldJsonValue(html, 'uploadDate')) !== day) break;
        sameDay.push(cards[i].id);
      }
    }
    if (sameDay.length < 2) return null;
    sameDay.sort((a, b) => a - b);
    return sameDay.indexOf(id) + 1;
  }

  // ── fetching ──────────────────────────────────────────────────────

  function absolute(url) {
    try { return new URL(url, location.origin).href; } catch (e) { return url; }
  }

  function pathOf(url) {
    try { return new URL(url, location.origin).pathname.replace(/\/+$/, ''); } catch (e) { return ''; }
  }

  // Same-origin reads need no host permission and carry the site's own cookies
  // (pawchive's API only answers with the Cloudflare clearance the browser holds).
  async function getText(url) {
    try {
      const r = await fetch(url, { credentials: 'same-origin' });
      return r.ok ? await r.text() : '';
    } catch (e) {
      return '';
    }
  }

  async function getJson(url) {
    try {
      const r = await fetch(url, { credentials: 'same-origin' });
      return r.ok ? await r.json() : null;
    } catch (e) {
      return null;
    }
  }

  // Cross-origin JSON (api.iwara.tv) goes through the service worker, which
  // holds the host permission and the login-token exchange.
  function fetchJson(url, extra) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(Object.assign({ type: 'fetchJson', url }, extra || {}), (res) => {
          resolve(res && res.ok ? res.data : null);
        });
      } catch (e) {
        resolve(null);
      }
    });
  }

  // ── per-site adapters ─────────────────────────────────────────────
  // `match(path)` → capture groups or null; `code(m)` → site code;
  // `dateFromHtml(html, m)` for server-rendered pages; `dateFromApi(m, ctx)`
  // for SPAs; `daySeq(m, ctx)` → this video's place among that day's uploads;
  // `anchors` = where to mount, first match wins (else floating).
  const SITES = [
    {
      host: /(^|\.)rule34video\.com$/,
      match: (p) => /^\/video\/(\d+)\//.exec(p),
      code: () => 'R34',
      dateFromHtml: (html) => ldJsonValue(html, 'uploadDate'),
      daySeq: (m, ctx) => {
        const member = /\/members\/(\d+)\//.exec(document.documentElement.innerHTML);
        return member ? r34DaySeq(member[1], m[1], ctx.day) : null;
      },
      anchors: ['h1.title_video', '.heading h1', 'h1'],
    },
    {
      host: /(^|\.)iwara\.tv$/,
      match: (p) => /^\/videos?\/([A-Za-z0-9]+)/.exec(p),
      code: () => 'Iwara',
      dateFromApi: async (m, ctx) => {
        // Login-only videos 404 anonymously. The site keeps the signed-in user's
        // token in localStorage("token"); the background swaps it for an access
        // token exactly like iwara's own app does, so those videos resolve too.
        try { ctx.userToken = localStorage.getItem('token') || ''; } catch (e) { /* ignore */ }
        ctx.api = await fetchJson(`https://api.iwara.tv/video/${m[1]}`, { iwaraUserToken: ctx.userToken });
        return ctx.api && ctx.api.createdAt;
      },
      daySeq: async (m, ctx) => {
        const uid = ctx.api && ctx.api.user && ctx.api.user.id;
        if (!uid) return null;
        let pages = 0;
        const getPage = memo(async (n) => {
          const r = await fetchJson(`https://api.iwara.tv/videos?user=${encodeURIComponent(uid)}`
            + `&sort=date&page=${n}&limit=50`, { iwaraUserToken: ctx.userToken });
          if (!r || !Array.isArray(r.results)) return null;
          if (!pages && r.count) pages = Math.ceil(r.count / (r.limit || 50));
          return r.results.map((v) => ({ id: String(v.id || ''), date: v.createdAt }));
        });
        await getPage(0);                       // learn the page count, then bisect
        const rows = await entriesOnDay(getPage, pages, ctx.day);
        return rankOnDay(rows, (e) => String(e.id) === m[1]);
      },
      // The title is a Text component rendered with size "h1" and class "mb-1"
      // as the first child of .page-video__details (from the app's own bundle).
      anchors: ['.page-video__details h1', '.page-video__details .text--h1', '.page-video__details .mb-1',
                '.page-video__details > :first-child', '.page-video__details', 'h1'],
    },
    {
      host: /(^|\.)hmvmania\.com$/,
      match: (p) => /^\/video\/[^/]+\/?$/.exec(p),
      code: () => 'HMVMania',
      dateFromHtml: (html) => ldJsonValue(html, 'datePublished')
        || metaContent(html, 'article:published_time'),
      daySeq: async (m, ctx) => {
        // /wp-json and admin-ajax are WAF-blocked for non-browsers; the author
        // RSS feed the PMV tab walks is not, and it is 10 posts a page.
        const author = /\/author\/([^/"'?#]+)\//.exec(document.documentElement.innerHTML);
        if (!author) return null;
        let pages = 0;
        const getPage = memo(async (n) => {
          const url = `${location.origin}/author/${author[1]}/feed/?post_type=video`
            + (n ? `&paged=${n + 1}` : '');
          const feed = parseFeed(await getText(url));
          if (feed.notFound || !feed.items.length) return null;
          if (!pages && feed.pages) pages = feed.pages;
          return feed.items;
        });
        await getPage(0);
        const rows = await entriesOnDay(getPage, pages, ctx.day);
        const here = pathOf(location.pathname);
        return rankOnDay(rows, (e) => pathOf(e.url) === here);
      },
      anchors: ['h1.video-entry-title', '.entry-content h1', 'h1'],
    },
    {
      host: /(^|\.)pmvhaven\.com$/,
      match: (p) => /^\/video\/[^/]*_([0-9a-fA-F]{24})/.exec(p),
      code: () => 'PMVHaven',
      dateFromApi: async (m, ctx) => {
        const r = await fetchJson(`https://pmvhaven.com/api/videos/${m[1]}`);
        ctx.api = (r && (r.data || r.video)) || r;
        return ctx.api && (ctx.api.uploadDate || ctx.api.releaseDate);
      },
      dateFromHtml: (html) => ldJsonValue(html, 'uploadDate'),
      daySeq: async (m, ctx) => {
        const html = document.documentElement.innerHTML;
        const uid = (ctx.api && (ctx.api.uploaderId || ctx.api.uploader))
          || (/user-profile-([0-9a-fA-F]{24})/.exec(html) || [])[1]
          || (/"uploaderId"\s*:\s*"([0-9a-fA-F]{24})"/.exec(html) || [])[1];
        if (!uid || typeof uid !== 'string') return null;
        let pages = 0;
        const getPage = memo(async (n) => {
          const r = await getJson(`${location.origin}/api/videos?uploader=${uid}&limit=100&page=${n + 1}`);
          const vids = r && (r.videos || r.data);
          if (!Array.isArray(vids) || !vids.length) return null;
          const p = r.pagination || {};
          if (!pages && p.totalPages) pages = Number(p.totalPages);
          return vids.map((v) => ({ id: String(v._id || ''), alt: String(v.oldId || ''),
                                    date: v.uploadDate || v.releaseDate }));
        });
        await getPage(0);
        const rows = await entriesOnDay(getPage, pages, ctx.day);
        // The URL carries oldId for videos migrated from the previous site.
        return rankOnDay(rows, (e) => e.id === m[1] || e.alt === m[1]);
      },
      // The page has a "Dashboard" h1 in the nav; the title is the last h1 in main.
      anchorFn: () => {
        const hs = Array.from(document.querySelectorAll('main h1, h1'))
          .filter((h) => h.textContent.trim() && !/^dashboard$/i.test(h.textContent.trim()));
        return hs.length ? hs[hs.length - 1] : null;
      },
    },
    {
      host: /(^|\.)pawchive\.(pw|st)$/,
      match: (p) => /^\/([a-z0-9_]+)\/user\/([^/]+)\/post\/([^/?#]+)/.exec(p),
      code: (m) => titleCase(m[1]),                     // patreon → Patreon, fanbox → Fanbox
      dateFromHtml: (html) => metaContent(html, 'published')
        || ldJsonValue(html, 'datePublished')
        || ((/Published:<\/div>\s*([\d-]+)/.exec(html) || [])[1] || ''),
      daySeq: async (m, ctx) => {
        const getPage = memo(async (n) => {
          const o = n * 50;
          const rows = await getJson(`${location.origin}/api/v1/${m[1]}/user/${m[2]}`
            + (o ? `?o=${o}` : ''));
          if (!Array.isArray(rows) || !rows.length) return null;
          return rows.map((p) => ({ id: String(p.id || ''), date: p.published || p.added }));
        });
        // The listing states no total, so it is walked from the newest page.
        const rows = await entriesOnDay(getPage, 0, ctx.day);
        return rankOnDay(rows, (e) => e.id === m[3]);
      },
      anchors: ['h1.post__title', '.post__info h1', 'h1'],
    },
  ];

  // ── mounting ──────────────────────────────────────────────────────

  let lastUrl = '';
  let busy = false;
  let current = null;   // the video the button on screen belongs to

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

  function buildPrefix(ctx) {
    if (!ctx.day) return `${ctx.code} - `;
    return `${ctx.code} - ${ctx.day}${ctx.seq ? ` ${pad2(ctx.seq)}` : ''} - `;
  }

  async function copyToClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      // Clipboard API can refuse outside a secure context; fall back to a textarea.
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
      ta.remove();
      return ok;
    }
  }

  async function onCopy(ev) {
    ev.preventDefault();
    ev.stopPropagation();
    const btn = ev.currentTarget;
    const ctx = current;
    if (!ctx) return;
    // The day's count needs the uploader's listing, so it is resolved on the
    // first copy and remembered for this video.
    if (ctx.day && !ctx.resolved) {
      const label = btn.textContent;
      btn.textContent = `${label} …`;
      btn.disabled = true;
      try {
        const n = ctx.site.daySeq ? await ctx.site.daySeq(ctx.m, ctx) : null;
        ctx.seq = typeof n === 'number' && n > 0 ? n : null;
      } catch (e) {
        ctx.seq = null;
      }
      ctx.resolved = true;
      ctx.prefix = buildPrefix(ctx);
      btn.disabled = false;
      btn.textContent = ctx.prefix.trimEnd();
      btn.title = `Copy "${ctx.prefix}" (with the trailing space) to the clipboard`;
    }
    flash(btn, (await copyToClipboard(ctx.prefix)) ? 'Copied ✓' : 'Copy failed');
  }

  function makeButton(ctx) {
    const btn = document.createElement('button');
    btn.id = ID;
    btn.type = 'button';
    btn.className = 'pmvx-btn' + (ctx.day ? '' : ' pmvx-nodate');
    btn.textContent = ctx.prefix.trimEnd();
    btn.title = ctx.day
      ? `Copy "${ctx.prefix}" (with the trailing space) to the clipboard`
      : 'Upload date not found on this page — copies the site code only';
    btn.addEventListener('click', onCopy);
    return btn;
  }

  function flash(btn, text) {
    const orig = btn.textContent;
    btn.textContent = text;
    btn.classList.add('pmvx-flash');
    setTimeout(() => { btn.textContent = orig; btn.classList.remove('pmvx-flash'); }, 1400);
  }

  function mount(ctx) {
    removeButton();
    const btn = makeButton(ctx);
    const anchor = findAnchor(ctx.site);
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
    if (!m) { removeButton(); current = null; return; }
    if (busy) return;
    busy = true;
    try {
      // Keep an already-resolved count when the SPA merely re-renders the page.
      const keep = current && current.key === m[0] ? current : null;
      const ctx = keep || { site, m, key: m[0], code: site.code(m), seq: null, resolved: false };
      if (!keep) {
        let raw = '';
        if (site.dateFromHtml) raw = site.dateFromHtml(document.documentElement.innerHTML, m) || '';
        if (!raw && site.dateFromApi) raw = (await site.dateFromApi(m, ctx)) || '';
        // Guard against the SPA having navigated while we awaited.
        if (!site.match(location.pathname) || site.match(location.pathname)[0] !== m[0]) return;
        ctx.raw = raw;
        ctx.day = isoDay(raw);
        ctx.prefix = buildPrefix(ctx);
      }
      current = ctx;
      mount(ctx);
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
  if (typeof module !== 'undefined') {
    module.exports = { isoDay, pad2, ldJsonValue, metaContent, titleCase, rfc822Iso,
                       r34Cards, parseFeed, cmpUpload, rankOnDay, entriesOnDay, r34DaySeq, SITES };
  }
})();
