/* ── Creator view: selection, scope, and download actions ──────── */

let creatorsCache = [];
let currentCreatorId = '';
let currentCreator = null;        // full object {id, name, destination, category, has_videos, links}
let currentScope = 'everything';
let currentCategory = 'All';      // shared type filter (Creator dropdown + Gallery)
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
    refreshCategoryDatalist();
    if (typeof renderGallery === 'function') renderGallery();
    await onCreatorChange();
}

function populateCreatorDropdown(selectId) {
    const sel = document.getElementById('creatorSelect');
    const prev = selectId || currentCreatorId || sel.value;
    const list = applySort(creatorsCache.filter(c => currentCategory === 'All' || c.category === currentCategory));

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

function buildCategoryFilter() {
    const cats = Array.from(new Set(creatorsCache.map(c => c.category).filter(Boolean))).sort();
    if (!cats.includes(currentCategory) && currentCategory !== 'All') currentCategory = 'All';
    const opts = ['All', ...cats];
    ['categoryFilter', 'galleryCategoryFilter'].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        el.innerHTML = '';
        opts.forEach(cat => {
            const b = document.createElement('button');
            b.className = 'seg' + (cat === currentCategory ? ' active' : '');
            b.textContent = cat;
            b.dataset.cat = cat;
            b.onclick = () => setCategory(cat);
            el.appendChild(b);
        });
    });
}

function setCategory(cat) {
    currentCategory = cat;
    document.querySelectorAll('#categoryFilter .seg, #galleryCategoryFilter .seg')
        .forEach(b => b.classList.toggle('active', b.dataset.cat === cat));
    populateCreatorDropdown();
    onCreatorChange();
    if (typeof renderGallery === 'function') renderGallery();
}

function refreshCategoryDatalist() {
    const dl = document.getElementById('cfgCategoryList');
    if (!dl) return;
    const cats = Array.from(new Set(['Real', 'Furry', ...creatorsCache.map(c => c.category).filter(Boolean)]));
    dl.innerHTML = cats.map(c => `<option value="${c}"></option>`).join('');
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
    await refreshYears();
    pendingUndo = [];          // undo history is per-creator
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

    const groupsHtml = groups.map(g => {
        const rows = g.links.map(i => {
            const failed = i.status === 'failed'
                ? '<span class="badge badge-failed">failed</span>' : '';
            const missing = (i.missing && i.missing.length)
                ? `<span class="pending-missing">missing: ${escapeHtml(i.missing.join(', '))}</span>` : '';
            // Highlight the hosts the user cares about spotting at a glance (mega,
            // google drive, tinyurl) in amber instead of the default colors.
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
        }).join('');
        const groupKeys = encodeURIComponent(JSON.stringify(g.links.map(l => l.key)));
        const multi = g.links.length > 1;
        const allBox = multi
            ? `<input type="checkbox" class="pending-check pending-check-all"
                      title="Mark all ${g.links.length} links in this post done"
                      onchange="resolvePostLinks(this, '${groupKeys}')">`
            : '';
        return `<div class="pending-group">
            <div class="pending-post">
              ${allBox}
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
        </div>
        <div class="pending-list" ${pendingCollapsed ? 'style="display:none"' : ''}>${groupsHtml}</div>`;
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
    try { res = await pywebview.api.list_creator_errors(id); }
    catch (e) { return; }
    if (currentCreatorId !== id) return;    // selection changed mid-fetch
    const items = (res && res.items) || [];
    currentErrorCount = res ? (res.count || 0) : 0;

    if (btn) {
        btn.style.display = currentErrorCount > 0 ? '' : 'none';
        const c = btn.querySelector('.err-count');
        if (c) c.textContent = currentErrorCount;
    }
    updateActionAvailability();

    if (!items.length) { panel.style.display = 'none'; panel.innerHTML = ''; return; }

    const rows = items.map(i => {
        const gone = i.state === 'gone';
        const badge = gone
            ? '<span class="badge badge-gone">gone</span>'
            : '<span class="badge badge-failed">failed</span>';
        const status = i.status ? `<span class="pending-missing">HTTP ${i.status}</span>` : '';
        const fileLink = i.url
            ? `<a class="pending-url" href="#" onclick="openLink('${encodeURIComponent(i.url)}');return false;" title="${escapeHtml(i.url)}">file&nbsp;url</a>
               <button class="btn-tiny" onclick="copyText('${encodeURIComponent(i.url)}')">Copy</button>` : '';
        const pageLink = i.page_url
            ? `<a class="pending-url" href="#" onclick="openLink('${encodeURIComponent(i.page_url)}');return false;" title="${escapeHtml(i.page_url)}">post&nbsp;page</a>
               <button class="btn-tiny" onclick="copyText('${encodeURIComponent(i.page_url)}')">Copy</button>` : '';
        const prefixBtn = i.prefix
            ? `<button class="btn-tiny" onclick="copyText('${encodeURIComponent(i.prefix)}')" title="Paste in front of the manually-downloaded file's own name">Copy name prefix</button>` : '';
        return `<div class="pending-row">
            <input type="checkbox" class="pending-check" title="Dismiss — hide this error (won't come back on re-download)"
                   onchange="dismissError(this, '${encodeURIComponent(i.entry)}')">
            ${badge}
            <span class="pending-fname" title="${escapeHtml(i.filename || i.entry || '')}">${escapeHtml(i.filename || i.entry || '(unknown)')}</span>
            ${status}
            ${fileLink} ${pageLink} ${prefixBtn}
            <span class="field-hint">${escapeHtml(i.link || '')}</span>
        </div>`;
    }).join('');

    const caret = errorsCollapsed ? '▸' : '▾';
    const fc = res.failed || 0, gc = res.gone || 0;
    panel.style.display = '';
    panel.innerHTML = `<div class="pending-head" onclick="toggleErrors()">
          <span class="pending-caret">${caret}</span>
          <span>Errors needing attention</span>
          <span class="pending-count">${currentErrorCount}</span>
          <span class="field-hint pending-hint">${fc} retryable · ${gc} gone (grab manually via the URL / post page)</span>
        </div>
        <div class="pending-list" ${errorsCollapsed ? 'style="display:none"' : ''}>${rows}</div>`;
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
    if (currentCreatorId === id) renderErrorLinks();   // refresh count/button/panel
}

function resolvePendingLink(cb, encKey) {
    const key = decodeURIComponent(encKey);
    const id = currentCreatorId;
    cb.disabled = true;
    pywebview.api.set_link_resolved(id, key, true).then(r => {
        if (r && r.error) { cb.disabled = false; cb.checked = false; showToast(r.error, 'error'); return; }
        pendingUndo.push({ creatorId: id, keys: [key] });
        const group = cb.closest('.pending-group');
        const row = cb.closest('.pending-row');
        if (row) row.remove();
        // Drop the whole post block once its last link is resolved.
        if (group && !group.querySelector('.pending-row')) group.remove();
        const panel = document.getElementById('pendingLinks');
        if (panel && !panel.querySelector('.pending-row')) renderPendingLinks();
    });
}

// Post-level checkbox: resolve every link in one post in a single batch.
function resolvePostLinks(cb, encKeys) {
    let keys;
    try { keys = JSON.parse(decodeURIComponent(encKeys)); } catch (e) { return; }
    const id = currentCreatorId;
    cb.disabled = true;
    pywebview.api.set_links_resolved(id, keys, true).then(r => {
        if (r && r.error) { cb.disabled = false; cb.checked = false; showToast(r.error, 'error'); return; }
        pendingUndo.push({ creatorId: id, keys });
        const group = cb.closest('.pending-group');
        if (group) group.remove();
        const panel = document.getElementById('pendingLinks');
        if (panel && !panel.querySelector('.pending-row')) renderPendingLinks();
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

document.addEventListener('keydown', (e) => {
    if ((e.key === 'z' || e.key === 'Z') && (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey) {
        const el = document.activeElement;
        // Let real text fields keep their native undo.
        if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT')
            && el.type !== 'checkbox') return;
        if (undoPendingResolve()) e.preventDefault();
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
    const views = { creator: 'creatorView', gallery: 'galleryView', media: 'mediaView' };
    Object.entries(views).forEach(([n, id]) => {
        const el = document.getElementById(id);
        if (el) el.style.display = (n === name) ? 'flex' : 'none';
    });
    // The header toggle only offers Creator/Gallery; the media browser is a
    // sub-page of the gallery, so keep "Gallery" lit while viewing media.
    const lit = (name === 'media') ? 'gallery' : name;
    document.querySelectorAll('#viewToggle .seg')
        .forEach(b => b.classList.toggle('active', b.dataset.view === lit));
    if (name === 'gallery' && typeof renderGallery === 'function') renderGallery();
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
}

function onLinkScopeChange() {
    const url = document.getElementById('linkScope').value;
    if (url) {
        currentScope = 'link:' + url;
        // a single link overrides the group buttons — clear their highlight
        document.querySelectorAll('#scopeControl .seg').forEach(b => b.classList.remove('active'));
    } else {
        setScope('everything');
    }
}

// ── Action availability ─────────────────────────────────────────

function updateActionAvailability() {
    const hasCreator = !!currentCreator;
    const hasLinks = hasCreator && currentCreator.links.length > 0;
    const hasCf = hasCreator && currentCreator.links.some(l => l.platform === 'coomerfans');

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
    sel.innerHTML = '<option value="">Year…</option>';
    if (!currentCreatorId) return;
    const years = await pywebview.api.get_creator_years(currentCreatorId);
    years.forEach(y => {
        const o = document.createElement('option');
        o.value = y;
        o.textContent = y;
        sel.appendChild(o);
    });
}

// ── Downloads ───────────────────────────────────────────────────

function requireCreator() {
    if (!currentCreatorId) { showToast('Select a creator first', 'error'); return false; }
    if (isBusy) { showToast('An operation is already running', 'error'); return false; }
    return true;
}

function startDownload(mode) {
    if (!requireCreator()) return;
    let year = null;
    if (mode === 'redownload_year') {
        year = document.getElementById('redownloadYear').value;
        if (!year) { showToast('Select a year to download', 'error'); return; }
    }
    setDownloadingState(true);
    resetStats();
    pywebview.api.start_creator_download(currentCreatorId, currentScope, mode, year).then(res => {
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

function verifyRepair() {
    if (!requireCreator()) return;
    setDownloadingState(true);
    resetStats();
    Logger.info('Verifying coomerfans files for this creator…');
    pywebview.api.start_creator_verify(currentCreatorId).then(res => {
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
