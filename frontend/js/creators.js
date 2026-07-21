/* ── Creator view: selection, scope, and download actions ──────── */

let creatorsCache = [];
let currentCreatorId = '';
let currentCreator = null;        // full object {id, name, destination, category, has_videos, links}
let currentScope = 'everything';
// Fixed type hierarchy (mirrors the P:\{Major}\{Sub} folder layout on the NAS).
const CATEGORY_TREE = { Furry: ['2D', '3D'], Hentai: ['2D', '3D'], Real: ['Real', 'Furry'] };
let currentMajor = 'All';         // shared type filter (Creator dropdown + Gallery)
let currentSub = null;            // active subcategory within the major (null = all subs)
let currentSort = 'recent';       // 'recent' (last downloaded) | 'name' (A–Z)
let isBusy = false;

// ── Sorting (shared by the dropdown + gallery) ─────────────────

function applySort(list) {
    const sorted = list.slice();
    if (currentSort === 'name') {
        sorted.sort((a, b) => (a.name || '').localeCompare(b.name || '', undefined, { sensitivity: 'base' }));
    } else {
        sorted.sort((a, b) => (b.last_used || '').localeCompare(a.last_used || ''));
    }
    return sorted;
}

function onSortChange() {
    currentSort = document.getElementById('creatorSort').value || 'recent';
    populateCreatorDropdown();
    if (typeof renderGallery === 'function') renderGallery();
    pywebview.api.save_state({ creator_sort: currentSort });
}

// ── Summary helpers ────────────────────────────────────────────

function summarize(links) {
    const s = { onlyfans: 0, fansly: 0, twitter: 0, patreon: 0, fanbox: 0, derpibooru: 0, discord: 0 };
    (links || []).forEach(l => {
        if (l.platform === 'twitter') s.twitter += 1;
        else if (l.platform === 'derpibooru') s.derpibooru += 1;
        else if (l.platform === 'discord') s.discord += 1;
        else if (l.platform === 'coomerfans' || l.platform === 'pawchive')
            s[l.service] = (s[l.service] || 0) + 1;
    });
    return s;
}

// service slug -> short label used in chips / link labels
function svcLabel(svc) {
    return { onlyfans: 'OF', fansly: 'Fansly', patreon: 'Patreon', fanbox: 'Fanbox' }[svc]
        || (svc ? svc.charAt(0).toUpperCase() + svc.slice(1) : svc);
}

// ── Creator list / selection ───────────────────────────────────

async function refreshCreators(selectId) {
    creatorsCache = await pywebview.api.list_creators();
    buildCategoryFilter();
    populateCreatorDropdown(selectId);
    if (typeof renderGallery === 'function') renderGallery();
    await onCreatorChange();
}

// Shared type-filter predicate: major + optional subcategory (Creator list + Gallery).
function matchesCategory(c) {
    if (currentMajor === 'All') return true;
    if (c.category !== currentMajor) return false;
    return !currentSub || c.subcategory === currentSub;
}

function populateCreatorDropdown(selectId) {
    const sel = document.getElementById('creatorSelect');
    const prev = selectId || currentCreatorId || sel.value;
    const list = applySort(creatorsCache.filter(matchesCategory));

    sel.innerHTML = '<option value="">Select a creator…</option>';
    list.forEach(c => {
        const o = document.createElement('option');
        o.value = c.id;
        o.textContent = c.name;
        sel.appendChild(o);
    });
    sel.value = (prev && list.some(c => c.id === prev)) ? prev : '';
}

// ── Type filter (shared by the dropdown + gallery) ──────────────

// Two-tier type filter: a fixed row of majors (All · Furry · Hentai · Real) plus a
// subcategory row that appears under the chosen major. Both the Creator view and
// the Gallery view are kept mirrored.
function buildCategoryFilter() {
    const majors = ['All', ...Object.keys(CATEGORY_TREE)];
    if (currentMajor !== 'All' && !CATEGORY_TREE[currentMajor]) { currentMajor = 'All'; currentSub = null; }
    [['categoryFilter', 'categorySubFilter'],
     ['galleryCategoryFilter', 'galleryCategorySubFilter']].forEach(([majId, subId]) => {
        const majEl = document.getElementById(majId);
        if (majEl) {
            majEl.innerHTML = '';
            majors.forEach(m => {
                const b = document.createElement('button');
                b.className = 'seg' + (m === currentMajor ? ' active' : '');
                b.textContent = m;
                b.dataset.cat = m;
                b.onclick = () => setMajor(m);
                majEl.appendChild(b);
            });
        }
        const subEl = document.getElementById(subId);
        if (subEl) buildSubRow(subEl);
    });
}

function buildSubRow(subEl) {
    const subs = CATEGORY_TREE[currentMajor];
    if (!subs) { subEl.style.display = 'none'; subEl.innerHTML = ''; return; }
    subEl.style.display = '';
    subEl.innerHTML = '';
    subs.forEach(s => {
        const b = document.createElement('button');
        b.className = 'seg' + (currentSub === s ? ' active' : '');
        b.textContent = s;
        b.dataset.sub = s;
        b.onclick = () => setSub(s);
        subEl.appendChild(b);
    });
}

function applyCategoryFilter() {
    buildCategoryFilter();
    populateCreatorDropdown();
    onCreatorChange();
    if (typeof renderGallery === 'function') renderGallery();
}

function setMajor(m) {
    currentMajor = m;
    currentSub = null;               // switching (or re-clicking) a major clears the sub
    applyCategoryFilter();
}

function setSub(s) {
    currentSub = (currentSub === s) ? null : s;   // click the active sub to widen to all
    applyCategoryFilter();
}

// Mirror of the backend _derive_category_pair — for the Configure modal's live
// read-only Type chip when a destination folder is picked.
function deriveCategoryPair(dest) {
    const parts = (dest || '').replace(/\\/g, '/').replace(/\/+$/, '').split('/').filter(Boolean);
    if (parts.length >= 3) {
        const major = parts[parts.length - 3], sub = parts[parts.length - 2];
        if (['furry', 'hentai', 'real'].includes(major.toLowerCase())) {
            const subLabel = sub.toLowerCase() === 'creators' ? 'Real' : titleCaseFolder(sub);
            return [titleCaseFolder(major), subLabel];
        }
    }
    return ['', ''];
}

// Uppercase the first letter of each letter-run (matches Python str.title(): 2d→2D).
function titleCaseFolder(s) {
    return (s || '').toLowerCase().replace(/(^|[^a-z])([a-z])/g, (m, p, c) => p + c.toUpperCase());
}

async function onCreatorChange() {
    const id = document.getElementById('creatorSelect').value;
    if (!id) {
        currentCreatorId = '';
        currentCreator = null;
        document.getElementById('headerSubtitle').textContent = 'Library';
    } else {
        currentCreatorId = id;
        currentCreator = await pywebview.api.get_creator(id);
        document.getElementById('headerSubtitle').textContent =
            currentCreator ? currentCreator.name : 'Library';
    }
    renderChips();
    setCreatorAvatar(currentCreator);
    buildScopeControl();
    updateActionAvailability();
    updateLatestRangeInfo();
    await refreshYears();
    pendingUndo = [];          // undo history is per-creator
    errorsUndo = [];
    lastPendingSig = null;
    renderPendingLinks();
    renderErrorLinks();
    persistLastCreator(currentCreatorId);
}

// ── Pawchive: external links needing attention ──────────────────

function escapeHtml(s) {
    return (s || '').replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Undo history of resolve actions for the current creator: each entry is
// {creatorId, keys:[...]} so Ctrl-Z can bring a link (or a whole post) back.
let pendingUndo = [];
// Keys of every link currently listed — the batch for "Resolve all".
let lastPendingKeys = [];
// Undo stack for the errors panel's "Dismiss all" (Ctrl+Z restores a batch).
let errorsUndo = [];
// Monotonic stamp so a single Ctrl+Z undoes the most-recent action across both
// the pending-links and errors stacks (not just one panel's).
let undoSeq = 0;
// Signature of the last rendered item set — lets the download-time poll skip a
// DOM rebuild (which would disrupt an in-progress click) when nothing changed.
let lastPendingSig = null;

// Hosts worth spotting at a glance (rendered amber): mega, google drive, tinyurl.
function isHighlightHost(host) {
    const h = (host || '').toLowerCase();
    return h.includes('mega.') || h.includes('drive.google') ||
           h.includes('docs.google') || h.includes('tinyurl.com');
}

async function renderPendingLinks(fromPoll = false) {
    const panel = document.getElementById('pendingLinks');
    if (!panel) return;
    const id = currentCreatorId;
    const hasPw = currentCreator && currentCreator.links.some(l => l.platform === 'pawchive');
    if (!id || !hasPw) { panel.style.display = 'none'; panel.innerHTML = ''; return; }

    const res = await pywebview.api.list_pending_links(id);
    if (currentCreatorId !== id) return;    // selection changed mid-fetch
    if (!res || !res.pawchive) { panel.style.display = 'none'; return; }
    if (res.reachable === false) {
        panel.style.display = '';
        panel.innerHTML = '<div class="pending-head">Links needing attention</div>'
            + '<div class="field-hint">Folder not reachable — connect to the NAS to see outstanding links.</div>';
        return;
    }
    const items = res.items || [];
    // Skip needless re-renders during the download poll so a click isn't disrupted.
    const sig = id + '::' + items.map(i => i.key).join('|');
    if (fromPoll && sig === lastPendingSig) return;
    lastPendingSig = sig;
    if (!items.length) { panel.style.display = 'none'; panel.innerHTML = ''; return; }

    // Group the flat list by post so multiple links from one post sit together.
    const groups = [];
    const byId = {};
    items.forEach(i => {
        if (!byId[i.post_id]) {
            byId[i.post_id] = { title: i.title, post_url: i.post_url, date: i.date, prefix: i.prefix, links: [] };
            groups.push(byId[i.post_id]);
        }
        byId[i.post_id].links.push(i);
    });

    lastPendingKeys = items.map(i => i.key);

    const groupsHtml = groups.map(g => {
        // A lone-link post collapses to one compact row (post title + the single
        // link + its checkbox inline) — no separate header, no parent "check all".
        if (g.links.length === 1) return pendingSingleRow(g, g.links[0]);
        const rows = g.links.map(pendingChildRow).join('');
        const groupKeys = encodeURIComponent(JSON.stringify(g.links.map(l => l.key)));
        return `<div class="pending-group">
            <div class="pending-post">
              <input type="checkbox" class="pending-check pending-check-all"
                     title="Mark all ${g.links.length} links in this post done"
                     onchange="resolvePostLinks(this, '${groupKeys}')">
              <a class="pending-posttitle" href="#" onclick="openLink('${encodeURIComponent(g.post_url)}');return false;"
                 title="Open this post on pawchive.st">${escapeHtml(g.title || '(untitled)')}</a>
              <span class="pending-date">${escapeHtml(g.date)}</span>
              <button class="btn-tiny" onclick="copyText('${encodeURIComponent(g.prefix)}')"
                      title="Paste in front of the downloaded file's own name">Copy name prefix</button>
            </div>
            <div class="pending-rows">${rows}</div>
        </div>`;
    }).join('');

    const caret = pendingCollapsed ? '▸' : '▾';
    panel.style.display = '';
    panel.innerHTML = `<div class="pending-head" onclick="togglePending()">
          <span class="pending-caret">${caret}</span>
          <span>Links needing attention</span>
          <span class="pending-count">${items.length}</span>
          <span class="field-hint pending-hint">manual downloads &amp; failed grabs — check off when done (Ctrl+Z to undo)</span>
          <button class="btn-tiny pending-clear-all" onclick="event.stopPropagation(); resolveAllPending()"
                  title="Mark every link here done (Ctrl+Z to undo)">Resolve all</button>
        </div>
        <div class="pending-list" ${pendingCollapsed ? 'style="display:none"' : ''}>${groupsHtml}</div>`;
}

// One link row inside a multi-link post group.
function pendingChildRow(i) {
    const failed = i.status === 'failed' ? '<span class="badge badge-failed">failed</span>' : '';
    const missing = (i.missing && i.missing.length)
        ? `<span class="pending-missing">missing: ${escapeHtml(i.missing.join(', '))}</span>` : '';
    // Highlight the hosts worth spotting at a glance (mega, google drive, tinyurl).
    const alt = isHighlightHost(i.host) ? ' host-alt' : '';
    return `<div class="pending-row">
        <input type="checkbox" class="pending-check" title="Mark done — hides this link and won't come back on re-scan"
               onchange="resolvePendingLink(this, '${encodeURIComponent(i.key)}')">
        <span class="badge badge-host${alt}">${escapeHtml(i.host)}</span>${failed}
        <a class="pending-url${alt}" href="#" onclick="openLink('${encodeURIComponent(i.url)}');return false;"
           title="${escapeHtml(i.url)}">${escapeHtml(i.url)}</a>
        <button class="btn-tiny" onclick="copyText('${encodeURIComponent(i.url)}')">Copy</button>
        ${missing}
    </div>`;
}

// A single-link post: same header + rows layout as a multi-link group (title with
// the name-prefix button beside it, the lone link underneath) — no "check all"
// since the child row's own checkbox resolves the single link.
function pendingSingleRow(g, i) {
    return `<div class="pending-group pending-single">
        <div class="pending-post">
          <a class="pending-posttitle" href="#" onclick="openLink('${encodeURIComponent(g.post_url)}');return false;"
             title="Open this post on pawchive.st">${escapeHtml(g.title || '(untitled)')}</a>
          <span class="pending-date">${escapeHtml(g.date)}</span>
          <button class="btn-tiny" onclick="copyText('${encodeURIComponent(g.prefix)}')"
                  title="Paste in front of the downloaded file's own name">Copy name prefix</button>
        </div>
        <div class="pending-rows">${pendingChildRow(i)}</div>
      </div>`;
}

// "Resolve all" — mark every currently-listed link done in one batch (undoable).
async function resolveAllPending() {
    const id = currentCreatorId;
    const keys = lastPendingKeys.slice();
    if (!keys.length) return;
    const res = await pywebview.api.set_links_resolved(id, keys, true);
    if (res && res.error) { showToast(res.error, 'error'); return; }
    pendingUndo.push({ creatorId: id, keys, seq: ++undoSeq });
    lastPendingSig = null;
    renderPendingLinks();
    showToast(`Resolved ${keys.length} link${keys.length === 1 ? '' : 's'} — Ctrl+Z to undo`, 'info');
}

let pendingCollapsed = false;

function togglePending() {
    pendingCollapsed = !pendingCollapsed;
    const list = document.querySelector('#pendingLinks .pending-list');
    const caret = document.querySelector('#pendingLinks .pending-caret');
    if (list) list.style.display = pendingCollapsed ? 'none' : '';
    if (caret) caret.textContent = pendingCollapsed ? '▸' : '▾';
}

// ── Failed downloads needing attention (all platforms) ──────────
// Persistent per-file failures recorded during downloads: 'failed' items are
// retried by the Redownload Errors button; 'gone' items are confirmed missing
// upstream and listed with their direct URL + post page for a manual grab.
let currentErrorCount = 0;
let errorsCollapsed = false;
let lastErrorItems = [];   // ALL of the creator's error items (for per-year actions)
let errorsSortDir = 'desc';   // 'desc' = newest year first (default), 'asc' = oldest first
let errorsYearFilter = '';    // '' = show all years; else only this year's posts
let _errFilterCreator = null; // creator the filter belongs to (reset on switch)

// The year an error belongs to, read from its 'YYYY.MM.DD - …' filename/prefix.
function errorYear(i) {
    const m = /^(\d{4})/.exec(i.filename || i.prefix || '');
    return m ? m[1] : '';
}

// A post group's date ('YYYY.MM.DD') for sorting — from its name prefix, else the
// first file's name. '' (unknown) sorts last in either direction.
function groupDate(g) {
    const m = /(\d{4}\.\d{2}\.\d{2})/.exec(g.prefix || '');
    if (m) return m[1];
    for (const i of g.items) {
        const mm = /^(\d{4}\.\d{2}\.\d{2})/.exec(i.filename || '');
        if (mm) return mm[1];
    }
    return '';
}

async function renderErrorLinks() {
    const panel = document.getElementById('errorLinks');
    const btn = document.getElementById('btnRedownloadErrors');
    if (!panel) return;
    const id = currentCreatorId;
    if (!id) {
        currentErrorCount = 0;
        panel.style.display = 'none'; panel.innerHTML = '';
        if (btn) btn.style.display = 'none';
        updateActionAvailability();
        return;
    }
    let res;
    try { res = await pywebview.api.list_creator_errors(id, currentScope); }
    catch (e) { return; }
    if (currentCreatorId !== id) return;    // selection changed mid-fetch
    const allItems = (res && res.items) || [];
    lastErrorItems = allItems;
    currentErrorCount = res ? (res.count || 0) : 0;
    // Reset the year filter when the creator changes, so a stale filter can't hide a
    // different creator's errors.
    if (_errFilterCreator !== id) { errorsYearFilter = ''; _errFilterCreator = id; }

    if (btn) {
        btn.style.display = currentErrorCount > 0 ? '' : 'none';
        const c = btn.querySelector('.err-count');
        if (c) c.textContent = currentErrorCount;
    }
    updateActionAvailability();

    if (!allItems.length) { panel.style.display = 'none'; panel.innerHTML = ''; return; }

    // Years across ALL errors (drives the filter dropdown); then narrow the shown
    // items to the picked year — the dropdown doubles as the filter.
    const years = [...new Set(allItems.map(errorYear).filter(Boolean))].sort().reverse();
    if (errorsYearFilter && !years.includes(errorsYearFilter)) errorsYearFilter = '';
    const items = errorsYearFilter
        ? allItems.filter(i => errorYear(i) === errorsYearFilter) : allItems;

    // Group failures by post (mirrors the "Links needing attention" panel): a post's
    // files sit together under one header carrying the post-page link + name prefix,
    // each group set off by the amber left-rule so entries read clearly instead of
    // bunching. Items without a post_id stand alone.
    const groups = [];
    const byId = {};
    items.forEach(i => {
        const gid = i.post_id || i.entry;
        if (!byId[gid]) {
            byId[gid] = { page_url: i.page_url, prefix: i.prefix, link: i.link,
                          pending: false, items: [] };
            groups.push(byId[gid]);
        }
        byId[gid].items.push(i);
        if (i.reason === 'not_imported') byId[gid].pending = true;
    });

    // Order posts by date — newest year first by default, flipped by the sort toggle.
    const mult = errorsSortDir === 'desc' ? -1 : 1;
    groups.sort((a, b) => {
        const da = groupDate(a), db = groupDate(b);
        return (da < db ? -1 : da > db ? 1 : 0) * mult;
    });

    const groupsHtml = groups.map(g => {
        const rows = g.items.map(errorRow).join('');
        // Post-level "dismiss all" checkbox when a post has >1 failed file (mirrors
        // the pending panel's check-all): one click clears every file in the post,
        // in addition to the per-file checkboxes.
        const checkAll = g.items.length > 1
            ? `<input type="checkbox" class="pending-check pending-check-all"
                      title="Dismiss all ${g.items.length} errors in this post"
                      onchange="dismissPostErrors(this, '${encodeURIComponent(JSON.stringify(g.items.map(x => x.entry)))}')">`
            : '';
        // The date+site name prefix (trailing ' - ' trimmed) reads as a post heading;
        // fall back to the platform label when there's no prefix.
        const label = g.prefix ? escapeHtml(g.prefix.replace(/\s*-\s*$/, ''))
                               : escapeHtml(g.link || 'Failed files');
        const head = g.page_url
            ? `<a class="pending-posttitle" href="#" onclick="openLink('${encodeURIComponent(g.page_url)}');return false;"
                  title="Open the post page: ${escapeHtml(g.page_url)}">${label}</a>
               <button class="btn-tiny" onclick="copyText('${encodeURIComponent(g.page_url)}')" title="Copy the post-page URL">Copy post page</button>`
            : `<span class="pending-posttitle">${label}</span>`;
        const prefixBtn = g.prefix
            ? `<button class="btn-tiny" onclick="copyText('${encodeURIComponent(g.prefix)}')" title="Paste in front of the manually-downloaded file's own name">Copy name prefix</button>` : '';
        // Re-check every file in this post against the site (downloads any pawchive
        // has since imported; leaves the rest flagged).
        const retryPostBtn = `<button class="btn-tiny" onclick="retryErrorSet('${encodeURIComponent(JSON.stringify(g.items.map(x => x.entry)))}', 'post')" title="Re-check this post on the site and download anything now available">Retry post</button>`;
        const hint = g.pending
            ? '<span class="field-hint">not imported on pawchive yet — retries automatically on the next download</span>' : '';
        return `<div class="pending-group">
            <div class="pending-post">${checkAll}${head}${prefixBtn}${retryPostBtn}${hint}</div>
            <div class="pending-rows">${rows}</div>
          </div>`;
    }).join('');

    const caret = errorsCollapsed ? '▸' : '▾';
    // Count + breakdown reflect what's shown (the filtered subset when a year is picked).
    const shownCount = errorsYearFilter ? items.length : currentErrorCount;
    const gcShown = items.filter(i => i.state === 'gone').length;
    const fcShown = items.length - gcShown;
    const hint = errorsYearFilter
        ? `${fcShown} retryable · ${gcShown} gone · ${errorsYearFilter} only`
        : `${res.failed || 0} retryable · ${res.gone || 0} gone (grab manually via the URL / post page)`;
    // Sort toggle — only meaningful with more than one post shown.
    const sortBtn = groups.length > 1
        ? `<button class="btn-tiny err-sort" onclick="event.stopPropagation(); toggleErrorsSort()"
                   title="Toggle order by year">${errorsSortDir === 'desc' ? 'Newest ↓' : 'Oldest ↑'}</button>` : '';
    // Year dropdown = filter + target for Retry/Dismiss year. Only when 2+ years exist.
    const yearCtl = years.length > 1
        ? `<select class="err-year" onclick="event.stopPropagation()" onchange="onErrorYearChange(this.value)">
             <option value=""${errorsYearFilter ? '' : ' selected'}>All years</option>${years.map(y => `<option value="${y}"${y === errorsYearFilter ? ' selected' : ''}>${y}</option>`).join('')}
           </select>
           <button class="btn-tiny" onclick="event.stopPropagation(); retryErrorYear()"
                   title="Re-check every error from the selected year">Retry year</button>
           <button class="btn-tiny" onclick="event.stopPropagation(); dismissErrorYear()"
                   title="Dismiss every error from the selected year (Ctrl+Z to undo)">Dismiss year</button>` : '';
    // "Scan folder" — adopt files hand-placed into the (filtered) year folder(s).
    const scanBtn = `<button class="btn-tiny" onclick="event.stopPropagation(); scanErrorFolder()"
                   title="Find files you downloaded elsewhere and dropped into the folder, rename them to the archive name, and clear those errors">${errorsYearFilter ? 'Scan ' + errorsYearFilter : 'Scan folder'}</button>`;
    panel.style.display = '';
    panel.innerHTML = `<div class="pending-head" onclick="toggleErrors()">
          <span class="pending-caret">${caret}</span>
          <span>Errors needing attention</span>
          <span class="pending-count">${shownCount}</span>
          <span class="field-hint pending-hint">${hint}</span>
          ${sortBtn}
          ${yearCtl}
          ${scanBtn}
          <button class="btn-tiny pending-clear-all" onclick="event.stopPropagation(); dismissAllErrors()"
                  title="Dismiss every error here (Ctrl+Z to undo)">Dismiss all</button>
        </div>
        <div class="pending-list" ${errorsCollapsed ? 'style="display:none"' : ''}>${groupsHtml}</div>`;
}

// One failed-file row inside a post group: dismiss checkbox, status badge, filename,
// HTTP status, and the direct file URL + copy. The post page + name-prefix actions
// live in the group header (top-left). reason 'not_imported' reads as amber
// "not imported" (pawchive hasn't scraped the full file yet — auto-retried later).
function errorRow(i) {
    const gone = i.state === 'gone';
    const notImported = i.reason === 'not_imported';
    const badge = gone ? '<span class="badge badge-gone">gone</span>'
        : notImported ? '<span class="badge badge-gone">not imported</span>'
        : '<span class="badge badge-failed">failed</span>';
    const status = i.status ? `<span class="pending-missing">HTTP ${i.status}</span>` : '';
    const fileLink = i.url
        ? `<a class="pending-url" href="#" onclick="openLink('${encodeURIComponent(i.url)}');return false;" title="${escapeHtml(i.url)}">file&nbsp;url</a>
           <button class="btn-tiny" onclick="copyText('${encodeURIComponent(i.url)}')">Copy</button>` : '';
    // Re-check just this one file against the site.
    const retryBtn = `<button class="btn-tiny err-retry" onclick="retryErrorSet('${encodeURIComponent(JSON.stringify([i.entry]))}', 'file')" title="Re-check this file on the site">↻</button>`;
    return `<div class="pending-row">
        <input type="checkbox" class="pending-check" title="Dismiss — hide this error (Ctrl+Z to undo)"
               onchange="dismissError(this, '${encodeURIComponent(i.entry)}')">
        ${badge}
        <span class="pending-fname" title="${escapeHtml(i.filename || i.entry || '')}">${escapeHtml(i.filename || i.entry || '(unknown)')}</span>
        ${status}
        ${fileLink}
        ${retryBtn}
    </div>`;
}

// "Dismiss all" — hide every listed error at once (undoable via Ctrl+Z).
async function dismissAllErrors() {
    const id = currentCreatorId;
    const res = await pywebview.api.dismiss_all_creator_errors(id, currentScope);
    if (res && res.error) { showToast(res.error, 'error'); return; }
    const entries = (res && res.entries) || [];
    if (entries.length) {
        errorsUndo.push({ creatorId: id, entries, seq: ++undoSeq });
        showToast(`Dismissed ${entries.length} error${entries.length === 1 ? '' : 's'} — Ctrl+Z to undo`, 'info');
    }
    if (currentCreatorId === id) renderErrorLinks();
}

// Ctrl+Z for the errors panel: restore the most recently dismissed error(s) — one
// file, a post, a year, or a "Dismiss all" batch (whichever was last), newest first.
function undoErrorsDismiss() {
    while (errorsUndo.length && errorsUndo[errorsUndo.length - 1].creatorId !== currentCreatorId) {
        errorsUndo.pop();
    }
    if (!errorsUndo.length) return false;
    const action = errorsUndo.pop();
    pywebview.api.restore_creator_errors(action.creatorId, action.entries).then(r => {
        if (r && r.error) { showToast(r.error, 'error'); return; }
        if (currentCreatorId === action.creatorId) renderErrorLinks();
        showToast(`Restored ${action.entries.length} error${action.entries.length === 1 ? '' : 's'}`, 'info');
    });
    return true;
}

function toggleErrors() {
    errorsCollapsed = !errorsCollapsed;
    const list = document.querySelector('#errorLinks .pending-list');
    const caret = document.querySelector('#errorLinks .pending-caret');
    if (list) list.style.display = errorsCollapsed ? 'none' : '';
    if (caret) caret.textContent = errorsCollapsed ? '▸' : '▾';
}

async function dismissError(cb, entryEnc) {
    const entry = decodeURIComponent(entryEnc);
    const id = currentCreatorId;
    cb.disabled = true;
    try {
        const res = await pywebview.api.dismiss_creator_error(id, entry);
        if (res && res.error) {
            showToast(res.error, 'error');
            cb.checked = false; cb.disabled = false;
            return;
        }
    } catch (e) {
        cb.checked = false; cb.disabled = false;
        return;
    }
    if (currentCreatorId !== id) return;
    // Undoable via Ctrl+Z, like resolving a single link in the pending panel.
    errorsUndo.push({ creatorId: id, entries: [entry], seq: ++undoSeq });
    // Remove the row (and its now-empty post group) IN PLACE so the panel isn't
    // rebuilt — a full re-render resets the scroll to the top on every check. Only
    // fall back to a full render once the panel empties (to hide it + reset button).
    const group = cb.closest('.pending-group');
    const row = cb.closest('.pending-row');
    if (row) row.remove();
    if (group && !group.querySelector('.pending-row')) group.remove();
    const panel = document.getElementById('errorLinks');
    if (!panel || !panel.querySelector('.pending-row')) { renderErrorLinks(); return; }
    currentErrorCount = Math.max(0, currentErrorCount - 1);
    updateErrorCounts(panel);
}

// Refresh the panel's shown count (rows still on screen — correct whether or not a
// year filter is active) and the Redownload Errors button's total, without a rebuild.
function updateErrorCounts(panel) {
    const hc = panel.querySelector('.pending-count');
    if (hc) hc.textContent = panel.querySelectorAll('.pending-row').length;
    const btn = document.getElementById('btnRedownloadErrors');
    const bc = btn && btn.querySelector('.err-count');
    if (bc) bc.textContent = currentErrorCount;
    updateActionAvailability();
}

// Post-level checkbox in the errors panel: dismiss every error in one post at once
// (undoable via Ctrl+Z), mirroring resolvePostLinks in the pending panel. Removes the
// group in place so the scroll position is preserved.
async function dismissPostErrors(cb, encEntries) {
    let entries;
    try { entries = JSON.parse(decodeURIComponent(encEntries)); } catch (e) { return; }
    const id = currentCreatorId;
    cb.disabled = true;
    let res;
    try { res = await pywebview.api.dismiss_creator_errors(id, entries); }
    catch (e) { cb.disabled = false; cb.checked = false; return; }
    if (res && res.error) { showToast(res.error, 'error'); cb.disabled = false; cb.checked = false; return; }
    if (currentCreatorId !== id) return;
    const done = (res && res.entries) || entries;
    if (done.length) {
        errorsUndo.push({ creatorId: id, entries: done, seq: ++undoSeq });
        showToast(`Dismissed ${done.length} error${done.length === 1 ? '' : 's'} — Ctrl+Z to undo`, 'info');
    }
    const group = cb.closest('.pending-group');
    if (group) group.remove();
    const panel = document.getElementById('errorLinks');
    if (!panel || !panel.querySelector('.pending-row')) { renderErrorLinks(); return; }
    currentErrorCount = Math.max(0, currentErrorCount - done.length);
    updateErrorCounts(panel);
}

function resolvePendingLink(cb, encKey) {
    const key = decodeURIComponent(encKey);
    const id = currentCreatorId;
    cb.disabled = true;
    pywebview.api.set_link_resolved(id, key, true).then(r => {
        if (r && r.error) { cb.disabled = false; cb.checked = false; showToast(r.error, 'error'); return; }
        pendingUndo.push({ creatorId: id, keys: [key], seq: ++undoSeq });
        const group = cb.closest('.pending-group');
        const row = cb.closest('.pending-row');
        if (row) row.remove();
        // Drop the whole post block once its last link is resolved.
        if (group && !group.querySelector('.pending-row')) group.remove();
        const panel = document.getElementById('pendingLinks');
        if (panel && !panel.querySelector('.pending-row')) renderPendingLinks();
        else updatePendingCount();
    });
}

// Recompute the "Links needing attention" header count from the rows still shown, so
// it ticks down as links are checked off (not just when the panel fully empties).
function updatePendingCount() {
    const panel = document.getElementById('pendingLinks');
    if (!panel) return;
    const c = panel.querySelector('.pending-count');
    if (c) c.textContent = panel.querySelectorAll('.pending-row').length;
}

// Post-level checkbox: resolve every link in one post in a single batch.
function resolvePostLinks(cb, encKeys) {
    let keys;
    try { keys = JSON.parse(decodeURIComponent(encKeys)); } catch (e) { return; }
    const id = currentCreatorId;
    cb.disabled = true;
    pywebview.api.set_links_resolved(id, keys, true).then(r => {
        if (r && r.error) { cb.disabled = false; cb.checked = false; showToast(r.error, 'error'); return; }
        pendingUndo.push({ creatorId: id, keys, seq: ++undoSeq });
        const group = cb.closest('.pending-group');
        if (group) group.remove();
        const panel = document.getElementById('pendingLinks');
        if (panel && !panel.querySelector('.pending-row')) renderPendingLinks();
        else updatePendingCount();
    });
}

// Ctrl+Z: bring back the most recently resolved link (or whole post), one per press.
function undoPendingResolve() {
    // Drop history that belongs to a different creator (selection changed).
    while (pendingUndo.length && pendingUndo[pendingUndo.length - 1].creatorId !== currentCreatorId) {
        pendingUndo.pop();
    }
    if (!pendingUndo.length) return false;
    const action = pendingUndo.pop();
    pywebview.api.set_links_resolved(action.creatorId, action.keys, false).then(r => {
        if (r && r.error) { showToast(r.error, 'error'); return; }
        lastPendingSig = null;            // force the panel to rebuild
        renderPendingLinks();
        showToast(action.keys.length > 1
            ? `Restored ${action.keys.length} links` : 'Restored link', 'info');
    });
    return true;
}

// Top action for the current creator in an undo stack (or null).
function _topUndo(stack) {
    for (let i = stack.length - 1; i >= 0; i--) {
        if (stack[i].creatorId === currentCreatorId) return stack[i];
    }
    return null;
}

document.addEventListener('keydown', (e) => {
    if ((e.key === 'z' || e.key === 'Z') && (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey) {
        const el = document.activeElement;
        // Let real text fields keep their native undo.
        if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT')
            && el.type !== 'checkbox') return;
        // Undo whichever panel action was the most recent (across both stacks).
        const p = _topUndo(pendingUndo), er = _topUndo(errorsUndo);
        let handled = false;
        if (p && (!er || p.seq > er.seq)) handled = undoPendingResolve();
        else if (er) handled = undoErrorsDismiss();
        if (handled) e.preventDefault();
    }
});

function openLink(encoded) {
    pywebview.api.open_url(decodeURIComponent(encoded));
}

function copyText(encoded) {
    const text = decodeURIComponent(encoded);
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
            () => showToast('Copied', 'info'),
            () => showToast('Copy failed', 'error'));
    } else {
        const ta = document.createElement('textarea');
        ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); showToast('Copied', 'info'); }
        catch (e) { showToast('Copy failed', 'error'); }
        document.body.removeChild(ta);
    }
}

// ── Profile avatar ──────────────────────────────────────────────

function initials(name) {
    return (name || '?').replace(/[^A-Za-z0-9]+/g, ' ').trim().split(' ')
        .slice(0, 2).map(w => w[0]).join('').toUpperCase() || '?';
}

function cfAccounts(creator) {
    return (creator.links || [])
        .filter(l => l.platform === 'coomerfans')
        .map(l => ({
            key: `${l.service}_${l.user_id}`,
            label: (l.service === 'onlyfans' ? 'OF' : l.service === 'fansly' ? 'Fansly' : l.service)
                + ' ' + (l.name || l.user_id),
        }));
}

function setCreatorAvatar(creator) {
    const el = document.getElementById('creatorAvatar');
    if (!creator) { el.style.display = 'none'; el.textContent = ''; el.classList.remove('has-img', 'swappable'); el.onclick = null; return; }
    el.style.display = '';
    el.classList.remove('has-img');
    el.textContent = initials(creator.name);   // placeholder while loading

    const accts = cfAccounts(creator);
    const swappable = accts.length > 1;
    el.classList.toggle('swappable', swappable);
    el.title = swappable ? 'Click to switch icon' : '';
    el.onclick = swappable ? cycleCreatorAvatar : null;

    const id = creator.id;
    pywebview.api.get_creator_avatar(id).then(r => {
        if (currentCreatorId !== id) return;    // selection changed mid-fetch
        if (r && r.data) {
            el.classList.add('has-img');
            el.innerHTML = `<img src="${r.data}" alt="">`;
        } else {
            el.classList.remove('has-img');
            el.textContent = initials(creator.name);
        }
    });
}

async function cycleCreatorAvatar() {
    if (!currentCreator) return;
    const accts = cfAccounts(currentCreator);
    if (accts.length < 2) return;
    const idx = accts.findIndex(a => a.key === (currentCreator.avatar || ''));
    const next = accts[(idx + 1) % accts.length];   // idx -1 (none set) → first
    currentCreator.avatar = next.key;
    await pywebview.api.set_creator_avatar(currentCreator.id, next.key);
    if (typeof _avatarCache === 'object') delete _avatarCache[currentCreator.id];  // gallery tile cache
    setCreatorAvatar(currentCreator);
    showToast('Icon: ' + next.label, 'info');
}

// ── View switching (Creator | Gallery) ──────────────────────────

function switchView(name) {
    const views = { creator: 'creatorView', gallery: 'galleryView', media: 'mediaView',
                    dupes: 'dupesView', albums: 'albumsView' };
    Object.entries(views).forEach(([n, id]) => {
        const el = document.getElementById(id);
        if (el) el.style.display = (n === name) ? 'flex' : 'none';
    });
    // The header toggle only offers Creator/Gallery/Duplicates; the media browser is
    // a sub-page of the gallery, so keep "Gallery" lit while viewing media.
    const lit = (name === 'media') ? 'gallery' : name;
    document.querySelectorAll('#viewToggle .seg')
        .forEach(b => b.classList.toggle('active', b.dataset.view === lit));
    if (name === 'gallery' && typeof renderGallery === 'function') renderGallery();
    if (name === 'dupes' && typeof renderDupeHeader === 'function') renderDupeHeader();
    if (name === 'albums' && typeof onAlbumViewShown === 'function') onAlbumViewShown();
}

function persistLastCreator(id) {
    pywebview.api.save_state({ last_creator: id || '' });
}

// ── Composition chips ───────────────────────────────────────────

function renderChips() {
    const el = document.getElementById('creatorChips');
    if (!currentCreator) {
        el.innerHTML = '<span class="field-hint">No creator selected.</span>';
        return;
    }
    if (!currentCreator.links.length) {
        el.innerHTML = '<span class="field-hint">No links yet — click Configure Links to add some.</span>';
        return;
    }
    const s = summarize(currentCreator.links);
    const chips = [];
    if (s.onlyfans) chips.push(chipHtml('OF', s.onlyfans));
    if (s.fansly) chips.push(chipHtml('Fansly', s.fansly));
    if (s.patreon) chips.push(chipHtml('Patreon', s.patreon));
    if (s.fanbox) chips.push(chipHtml('Fanbox', s.fanbox));
    if (s.twitter) chips.push('<span class="chip chip-twitter">Twitter</span>');
    if (s.derpibooru) chips.push(chipHtml('Derpibooru', s.derpibooru));
    if (s.discord) chips.push('<span class="chip chip-discord">Discord</span>');
    el.innerHTML = chips.join('');
}

function chipHtml(label, n) {
    const count = n > 1 ? ` <span class="chip-count">×${n}</span>` : '';
    return `<span class="chip">${label}${count}</span>`;
}

// ── Scope control (dynamic — only shows what the creator has) ────

function buildScopeControl() {
    const el = document.getElementById('scopeControl');
    el.innerHTML = '';
    currentScope = 'everything';

    if (!currentCreator || !currentCreator.links.length) {
        el.innerHTML = '<span class="field-hint">No links to fetch — add some in Configure Links.</span>';
        return;
    }

    const s = summarize(currentCreator.links);
    const opts = [['everything', 'Everything']];
    if (s.onlyfans && s.fansly) opts.push(['coomerfans', 'All Coomerfans']);
    if (s.onlyfans) opts.push(['onlyfans', 'All OnlyFans']);
    if (s.fansly) opts.push(['fansly', 'All Fansly']);
    if (s.patreon && s.fanbox) opts.push(['pawchive', 'All Pawchive']);
    if (s.patreon) opts.push(['patreon', 'Patreon']);
    if (s.fanbox) opts.push(['fanbox', 'Fanbox']);
    if (s.twitter) opts.push(['twitter', 'Twitter']);
    if (s.derpibooru) opts.push(['derpibooru', 'Derpibooru']);
    if (s.discord) opts.push(['discord', 'Discord']);

    opts.forEach(([val, label]) => {
        const b = document.createElement('button');
        b.className = 'seg' + (val === currentScope ? ' active' : '');
        b.textContent = label;
        b.dataset.scope = val;
        b.onclick = () => setScope(val);
        el.appendChild(b);
    });

    buildLinkScope();
}

function buildLinkScope() {
    const sel = document.getElementById('linkScope');
    sel.innerHTML = '<option value="">Use the scope above</option>';
    if (!currentCreator) return;
    currentCreator.links.forEach(l => {
        const o = document.createElement('option');
        o.value = l.url;
        o.textContent = (l.platform === 'twitter')
            ? `Twitter @${l.username}`
            : (l.platform === 'derpibooru')
                ? `Derpibooru ${l.name || l.query}`
                : (l.platform === 'discord')
                    ? `Discord ${l.name || l.channel_id}`
                    : `${svcLabel(l.service)} ${l.name || l.user_id}`;
        sel.appendChild(o);
    });
}

function setScope(scope) {
    currentScope = scope;
    document.getElementById('linkScope').value = '';   // group scope overrides single-link
    document.querySelectorAll('#scopeControl .seg').forEach(b =>
        b.classList.toggle('active', b.dataset.scope === scope));
    // The errors panel mirrors the scope — show only the selected site's errors.
    if (typeof renderErrorLinks === 'function') renderErrorLinks();
}

function onLinkScopeChange() {
    const url = document.getElementById('linkScope').value;
    if (url) {
        currentScope = 'link:' + url;
        // a single link overrides the group buttons — clear their highlight
        document.querySelectorAll('#scopeControl .seg').forEach(b => b.classList.remove('active'));
        if (typeof renderErrorLinks === 'function') renderErrorLinks();
    } else {
        setScope('everything');
    }
}

// ── Action availability ─────────────────────────────────────────

function updateActionAvailability() {
    const hasCreator = !!currentCreator;
    const hasLinks = hasCreator && currentCreator.links.length > 0;
    const hasCf = hasCreator && currentCreator.links.some(l => l.platform === 'coomerfans');
    // The "refresh external links" opt-out only means anything for platforms that
    // keep a "Links needing attention" manifest (pawchive/discord) — hide it otherwise.
    const hasLinkManifest = hasCreator && currentCreator.links.some(
        l => l.platform === 'pawchive' || l.platform === 'discord');
    const refreshRow = document.getElementById('refreshLinksRow');
    if (refreshRow) refreshRow.style.display = hasLinkManifest ? '' : 'none';

    document.getElementById('btnConfigure').disabled = !hasCreator || isBusy;
    document.getElementById('btnOpenFolder').disabled = !hasCreator;
    ['btnDownloadAll', 'btnFetchLatest', 'btnRedownload'].forEach(id =>
        document.getElementById(id).disabled = !hasLinks || isBusy);
    document.getElementById('btnVerify').disabled = !hasCf || isBusy;
    const btnErr = document.getElementById('btnRedownloadErrors');
    if (btnErr) btnErr.disabled = isBusy || currentErrorCount === 0;
}

// ── Years (redownload dropdown) ─────────────────────────────────

async function refreshYears() {
    const sel = document.getElementById('redownloadYear');
    const rs = document.getElementById('latestRangeStart');
    const re = document.getElementById('latestRangeEnd');
    sel.innerHTML = '<option value="">Year…</option>';
    if (rs) rs.innerHTML = '<option value="">From…</option>';
    if (re) re.innerHTML = '<option value="">…to</option>';
    if (!currentCreatorId) return;
    const years = await pywebview.api.get_creator_years(currentCreatorId);
    years.forEach(y => {
        [sel, rs, re].forEach(el => {
            if (!el) return;
            const o = document.createElement('option');
            o.value = y;
            o.textContent = y;
            el.appendChild(o);
        });
    });
}

// Show the creator's saved Fetch Latest range (if any) so "set once & forget" is visible.
function updateLatestRangeInfo() {
    const el = document.getElementById('latestRangeInfo');
    if (!el) return;
    const r = currentCreator && currentCreator.latest_range;
    if (!r || (r.start == null && r.end == null)) {
        el.style.display = 'none';
        el.textContent = '';
        return;
    }
    let label;
    if (r.start != null && r.end != null) label = (r.start === r.end) ? `${r.start}` : `${r.start}–${r.end}`;
    else if (r.start != null) label = `${r.start} onward`;
    else label = `up to ${r.end}`;
    el.innerHTML = `🗓️ <b>Fetch Latest</b> is limited to <b>${label}</b> for this creator (change in Configure Links).`;
    el.style.display = '';
}

// One-off Fetch Latest scoped to a year range (not persisted). Blank side = open-ended.
function fetchLatestRange() {
    const start = document.getElementById('latestRangeStart').value;
    const end = document.getElementById('latestRangeEnd').value;
    if (!start && !end) {
        showToast('Pick a From and/or To year (or use plain Fetch Latest)', 'error');
        return;
    }
    startDownload('latest', { start: start || null, end: end || null });
}

// ── Downloads ───────────────────────────────────────────────────

function requireCreator() {
    if (!currentCreatorId) { showToast('Select a creator first', 'error'); return false; }
    if (isBusy) { showToast('An operation is already running', 'error'); return false; }
    return true;
}

function startDownload(mode, yearRange) {
    if (!requireCreator()) return;
    let year = null;
    if (mode === 'redownload_year') {
        year = document.getElementById('redownloadYear').value;
        if (!year) { showToast('Select a year to download', 'error'); return; }
    }
    // yearRange (one-off) applies only to Fetch Latest; null falls back to the
    // creator's saved range on the backend.
    const range = (mode === 'latest' && yearRange) ? yearRange : null;
    const refreshLinks = document.getElementById('refreshLinks').checked;
    setDownloadingState(true);
    resetStats();
    pywebview.api.start_creator_download(
        currentCreatorId, currentScope, mode, year, refreshLinks, null, range
    ).then(res => {
        if (res && res.error) {
            setDownloadingState(false);
            showToast(res.error, 'error');
            Logger.error(res.error);
        }
    });
}

function redownloadYear() {
    startDownload('redownload_year');
}

function redownloadErrors() {
    if (!requireCreator()) return;
    if (currentErrorCount === 0) { showToast('No recorded errors for this creator', 'error'); return; }
    setDownloadingState(true);
    resetStats();
    Logger.info('Redownloading errored files for this creator…');
    pywebview.api.start_creator_download(currentCreatorId, currentScope, 'errors', null).then(res => {
        if (res && res.error) {
            setDownloadingState(false);
            showToast(res.error, 'error');
            Logger.error(res.error);
        }
    });
}

// Re-check a specific set of recorded errors against the site (per-file ↻, per-post
// "Retry post", or per-year). Pawchive re-fetches each post fresh: anything it has
// since imported downloads now; the rest stay flagged. Runs pawchive links only.
function retryErrors(entries, label) {
    if (!requireCreator()) return;
    if (!entries || !entries.length) { showToast('Nothing to re-check', 'info'); return; }
    setDownloadingState(true);
    resetStats();
    Logger.info(`Re-checking ${label} on the site (${entries.length} file${entries.length === 1 ? '' : 's'})…`);
    pywebview.api.start_creator_download(currentCreatorId, currentScope, 'errors', null, false, entries).then(res => {
        if (res && res.error) {
            setDownloadingState(false);
            showToast(res.error, 'error');
            Logger.error(res.error);
        }
    });
}

// Decode a URI-encoded JSON entry list from a button and re-check it.
function retryErrorSet(encEntries, kind) {
    let entries;
    try { entries = JSON.parse(decodeURIComponent(encEntries)); } catch (e) { return; }
    retryErrors(entries, kind === 'file' ? 'this file' : kind === 'post' ? 'this post' : kind);
}

// Filter the panel to one year (or clear back to all), then re-render.
function onErrorYearChange(y) {
    errorsYearFilter = y || '';
    renderErrorLinks();
}

// Re-check every recorded error from the year selected in the panel header.
function retryErrorYear() {
    if (!errorsYearFilter) { showToast('Pick a year first', 'info'); return; }
    const y = errorsYearFilter;
    const entries = lastErrorItems.filter(i => errorYear(i) === y).map(i => i.entry);
    retryErrors(entries, `${y} errors`);
}

// Flip the errors panel between newest-first and oldest-first, then re-render.
function toggleErrorsSort() {
    errorsSortDir = errorsSortDir === 'desc' ? 'asc' : 'desc';
    renderErrorLinks();
}

// Adopt files hand-placed into the year folder(s): rename bare off-site copies to the
// archive name and clear their errors. Scans the shown errors (the filtered year, or
// all). Non-destructive — only renames exact original-name matches.
function scanErrorFolder() {
    if (!requireCreator()) return;
    const shown = errorsYearFilter
        ? lastErrorItems.filter(i => errorYear(i) === errorsYearFilter) : lastErrorItems;
    const entries = shown.map(i => i.entry);
    if (!entries.length) { showToast('No errors to scan', 'info'); return; }
    const where = errorsYearFilter || 'all years';
    Logger.info(`Scanning the folder for placed files (${where}, ${entries.length} error${entries.length === 1 ? '' : 's'})…`);
    pywebview.api.resolve_errors_from_disk(currentCreatorId, entries).then(res => {
        if (res && res.error) { showToast(res.error, 'error'); Logger.error(res.error); return; }
        const r = res || {};
        const matched = (r.renamed || 0) + (r.already_present || 0);
        const msg = `Matched ${matched} file${matched === 1 ? '' : 's'} `
            + `(${r.renamed || 0} renamed, ${r.already_present || 0} already named) · ${r.not_found || 0} not found`;
        showToast(msg, matched ? 'success' : 'info');
        Logger.info(msg);
        renderErrorLinks();
    });
}

// Dismiss every recorded error from the year selected in the panel header (undoable).
function dismissErrorYear() {
    if (!errorsYearFilter) { showToast('Pick a year first', 'info'); return; }
    const y = errorsYearFilter;
    const entries = lastErrorItems.filter(i => errorYear(i) === y).map(i => i.entry);
    if (!entries.length) return;
    const id = currentCreatorId;
    pywebview.api.dismiss_creator_errors(id, entries).then(res => {
        if (res && res.error) { showToast(res.error, 'error'); return; }
        const done = (res && res.entries) || entries;
        if (done.length) {
            errorsUndo.push({ creatorId: id, entries: done, seq: ++undoSeq });
            showToast(`Dismissed ${done.length} error${done.length === 1 ? '' : 's'} from ${y} — Ctrl+Z to undo`, 'info');
        }
        // Clearing a whole year: drop the filter (that year's gone) and rebuild.
        errorsYearFilter = '';
        if (currentCreatorId === id) renderErrorLinks();
    });
}

function verifyRepair() {
    if (!requireCreator()) return;
    const deep = !!(document.getElementById('deepVerify') || {}).checked;
    setDownloadingState(true);
    resetStats();
    Logger.info(deep
        ? 'Deep-verifying — decode-scanning videos (slower)…'
        : 'Verifying coomerfans files for this creator…');
    pywebview.api.start_creator_verify(currentCreatorId, deep).then(res => {
        if (res && res.error) {
            setDownloadingState(false);
            showToast(res.error, 'error');
            Logger.error(res.error);
        }
    });
}

function cancelDownload() {
    pywebview.api.cancel_creator_download();
    Logger.info('Cancelling…');
}

function openCreatorFolder() {
    if (!currentCreator) return;
    pywebview.api.open_folder(currentCreator.destination).then(r => {
        if (r && r.error) showToast(r.error, 'error');
    });
}

// ── Busy state (buttons + live indicator) ───────────────────────

let pendingPollTimer = null;

function setDownloadingState(downloading) {
    isBusy = downloading;
    document.getElementById('btnCancel').style.display = downloading ? '' : 'none';
    document.getElementById('creatorSelect').disabled = downloading;
    document.getElementById('runningIndicator').classList.toggle('visible', downloading);
    if (!downloading) {
        document.getElementById('runFile').textContent = '';
        if (typeof clearActiveDownloads === 'function') clearActiveDownloads();
    }
    // While downloading, surface external links as soon as the crawl writes them
    // (that happens before the file downloads finish) so there's something to do
    // in parallel. The signature guard keeps this from disrupting active clicks.
    if (downloading) {
        if (!pendingPollTimer) pendingPollTimer = setInterval(() => renderPendingLinks(true), 5000);
    } else if (pendingPollTimer) {
        clearInterval(pendingPollTimer);
        pendingPollTimer = null;
    }
    updateActionAvailability();
}
