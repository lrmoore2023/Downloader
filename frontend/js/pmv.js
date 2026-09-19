/* ── PMV tracker tab ──────────────────────────────────────────────
   Tracks PMV creators across rule34video / iwara / pawchive: a per-video ✓ / ✗
   checklist with catalogue numbers and copyable filename prefixes
   ("SadBernard - R34 - 02 - "). Metadata only — nothing here downloads. */

let pmvCreators = [];              // list_pmv_creators()
let pmvSelected = new Set();       // checked creator ids (multi-select)
let pmvCurrentId = '';
let pmvDetail = null;              // get_pmv_items() for the open creator
let pmvBusy = false;
let pmvSort = 'name';
let pmvSearch = '';                // title filter inside the open creator
let pmvListSearch = '';            // creator-list filter (name / tag / site)
const pmvFilters = { hideNonMedia: true, hideGone: false };
const pmvExpanded = new Set();     // link_keys whose reviewed (✓/✗) group is unfolded

// ── boot / list ─────────────────────────────────────────────────

async function initPmv(state) {
    pmvSort = state.pmv_sort || 'name';
    const sel = document.getElementById('pmvSort');
    if (sel) sel.value = pmvSort;
    try {
        const s = await pywebview.api.pmv_fetch_status();
        if (s && s.running) setPmvBusy(true, 'A PMV fetch is running…');
    } catch (e) { /* ignore */ }
    await refreshPmvCreators();
    const last = state.last_pmv_creator || '';
    if (last && pmvCreators.some(c => c.id === last)) openPmvCreator(last);
}

function onPmvViewShown() {
    refreshPmvCreators();
}

async function refreshPmvCreators() {
    try { pmvCreators = await pywebview.api.list_pmv_creators(); }
    catch (e) { return; }
    renderPmvList();
}

function pmvSorted() {
    const list = pmvCreators.slice();
    const k = c => c.counts || {};
    if (pmvSort === 'new') list.sort((a, b) => (k(b).new - k(a).new) || (k(b).unreviewed - k(a).unreviewed) || a.name.localeCompare(b.name));
    else if (pmvSort === 'unreviewed') list.sort((a, b) => (k(b).unreviewed - k(a).unreviewed) || a.name.localeCompare(b.name));
    else if (pmvSort === 'fetched') list.sort((a, b) => (b.last_fetch || '').localeCompare(a.last_fetch || '') || a.name.localeCompare(b.name));
    else list.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }));
    return list;
}

function pmvSetSort(v) {
    pmvSort = v;
    pywebview.api.save_state({ pmv_sort: v });
    renderPmvList();
}

function pmvSetListSearch(v) {
    pmvListSearch = (v || '').trim().toLowerCase();
    renderPmvList();
}

function pmvCreatorMatches(c) {
    if (!pmvListSearch) return true;
    const hay = [c.name, ...(c.tags || []),
                 ...(c.links || []).flatMap(l => [l.site_code, l.username, l.display_name, l.platform])]
        .filter(Boolean).join(' ').toLowerCase();
    return pmvListSearch.split(/\s+/).every(w => hay.includes(w));
}

function renderPmvList() {
    const el = document.getElementById('pmvList');
    const cnt = document.getElementById('pmvCreatorCount');
    if (cnt) cnt.textContent = pmvCreators.length ? `(${pmvCreators.length})` : '';
    if (!el) return;
    if (!pmvCreators.length) {
        el.innerHTML = '<div class="links-empty">No PMV creators yet. Click <b>Add creator</b> and paste a rule34video, iwara or pawchive profile link.</div>';
        syncPmvSelectAll();
        return;
    }
    const rows = pmvSorted().filter(pmvCreatorMatches);
    if (!rows.length) {
        el.innerHTML = '<div class="links-empty">No creator matches the filter.</div>';
        syncPmvSelectAll();
        return;
    }
    el.innerHTML = rows.map(c => {
        const k = c.counts || {};
        const badges = [];
        if (k.new) badges.push(`<span class="badge pmv-new" title="Posted since you started tracking and not yet reviewed">${k.new} new</span>`);
        if (k.unreviewed) badges.push(`<span class="badge badge-skipped" title="Unreviewed (including the initial backlog)">${k.unreviewed}</span>`);
        if (k.shifted) badges.push(`<span class="badge badge-shifted" title="✓ posts whose catalogue number has changed">${k.shifted} shifted</span>`);
        const hasErr = (c.links || []).some(l => l.last_error);
        const err = hasErr ? '<span class="badge badge-failed" title="Last fetch had an error on one of this creator\'s sites">!</span>' : '';
        const sites = (c.links || []).map(l => `<span class="pmv-site-chip">${escapeHtml(l.site_code)}</span>`).join('');
        return `<div class="pmv-row${c.id === pmvCurrentId ? ' active' : ''}" onclick="openPmvCreator('${c.id}')">
            <input type="checkbox" class="pmv-check" ${pmvSelected.has(c.id) ? 'checked' : ''}
                   onclick="event.stopPropagation()" onchange="pmvToggleSelect('${c.id}', this.checked)">
            <div class="pmv-row-main">
                <div class="pmv-row-name">${escapeHtml(c.name)} ${err}</div>
                <div class="pmv-row-meta">${sites}<span>${k.total || 0} videos</span></div>
            </div>
            <div class="pmv-row-badges">${badges.join('')}</div>
        </div>`;
    }).join('');
    syncPmvSelectAll();
}

function pmvToggleSelect(id, checked) {
    if (checked) pmvSelected.add(id); else pmvSelected.delete(id);
    syncPmvSelectAll();
}

function pmvToggleSelectAll(checked) {
    pmvSelected = checked ? new Set(pmvCreators.map(c => c.id)) : new Set();
    renderPmvList();
}

function syncPmvSelectAll() {
    const box = document.getElementById('pmvSelectAll');
    if (!box) return;
    const n = pmvCreators.length;
    const sel = pmvCreators.filter(c => pmvSelected.has(c.id)).length;
    box.checked = n > 0 && sel === n;
    box.indeterminate = sel > 0 && sel < n;
    const btn = document.getElementById('pmvFetchSelBtn');
    if (btn) btn.textContent = sel ? `Fetch selected (${sel})` : 'Fetch selected';
}

// ── fetch job ───────────────────────────────────────────────────

async function pmvFetch(ids, mode, linkKey) {
    if (pmvBusy) { showToast('A PMV fetch is already running', 'info'); return; }
    setPmvBusy(true, 'Starting…');
    let res;
    try { res = await pywebview.api.start_pmv_fetch(ids, mode || 'latest', linkKey || null); }
    catch (e) { res = { error: String(e) }; }
    if (!res || res.error) {
        setPmvBusy(false);
        showToast((res && res.error) || 'Could not start', 'error');
        return;
    }
    pmvStatusText(`Fetching ${res.jobs} site${res.jobs === 1 ? '' : 's'} across ${res.creators} creator${res.creators === 1 ? '' : 's'}…`);
}

function pmvFetchSelected() {
    const ids = pmvCreators.map(c => c.id).filter(id => pmvSelected.has(id));
    if (!ids.length) { showToast('Tick one or more creators first', 'info'); return; }
    pmvFetch(ids);
}

function pmvFetchAll() {
    if (!pmvCreators.length) { showToast('Add a creator first', 'info'); return; }
    pmvFetch([]);
}

function pmvFetchCurrent() {
    if (pmvCurrentId) pmvFetch([pmvCurrentId]);
}

function pmvRescanSite(linkKey) {
    if (pmvCurrentId) pmvFetch([pmvCurrentId], 'full', linkKey);
}

async function pmvCancel() {
    try { await pywebview.api.cancel_pmv_fetch(); } catch (e) { /* ignore */ }
    pmvStatusText('Cancelling — finishing the current page…');
}

function setPmvBusy(busy, label) {
    pmvBusy = busy;
    ['pmvFetchSelBtn', 'pmvFetchAllBtn'].forEach(id => {
        const b = document.getElementById(id);
        if (b) b.disabled = busy;
    });
    document.querySelectorAll('.pmv-fetch-btn').forEach(b => { b.disabled = busy; });
    const c = document.getElementById('pmvCancelBtn');
    if (c) c.style.display = busy ? '' : 'none';
    if (label !== undefined) pmvStatusText(label);
}

function pmvStatusText(t) {
    const el = document.getElementById('pmvStatus');
    if (el) el.textContent = t || '';
}

window.onPmvProgress = function (d) {
    if (!d) return;
    const who = d.creator_name ? `${d.creator_name} · ${d.site_code}: ` : '';
    pmvStatusText(who + (d.message || ''));
    if (d.type === 'error') Logger.error('[PMV] ' + who + d.message);
    else if (d.type === 'link_done') Logger.info('[PMV] ' + who + d.message);
};

window.onPmvComplete = async function (r) {
    r = r || {};
    setPmvBusy(false);
    await refreshPmvCreators();
    if (pmvCurrentId) await openPmvCreator(pmvCurrentId, true);
    const errs = (r.errors || []).length;
    const n = r.total_new || 0;
    if (r.needs_cf_auth) {
        showToast('Pawchive needs a Cloudflare reconnect — opening Settings…', 'error');
        if (typeof openSettings === 'function') openSettings();
        if (typeof refreshPawCfStatus === 'function') refreshPawCfStatus();
    } else if (r.cancelled) {
        showToast('PMV fetch cancelled', 'info');
    } else if (errs) {
        showToast(`PMV fetch finished: ${n} new, ${errs} site error${errs === 1 ? '' : 's'}`, 'info');
    } else {
        showToast(`PMV fetch done: ${n} new video${n === 1 ? '' : 's'}`, 'success');
    }
    if (r.iwara_auth_failed) {
        showToast('iwara login failed (' + (r.iwara_message || 'see Settings') + ') — listed anonymously', 'info');
    }
    pmvStatusText(r.cancelled ? 'Cancelled.'
        : `Done — ${n} new.` + (errs ? ` ${errs} site error(s): see the red bar in the creator's site header.` : ''));
};

// ── detail panel ────────────────────────────────────────────────

async function openPmvCreator(id, keepScroll) {
    if (!id) return;
    pmvCurrentId = id;
    pywebview.api.save_state({ last_pmv_creator: id });
    let data;
    try { data = await pywebview.api.get_pmv_items(id); }
    catch (e) { return; }
    if (!data || data.error) { showToast((data && data.error) || 'Could not load', 'error'); return; }
    pmvDetail = data;
    renderPmvList();
    renderPmvDetail(keepScroll);
}

function pmvSetFilter(key, on) {
    pmvFilters[key] = !!on;
    renderPmvDetail(true);
}

function pmvSetSearch(v) {
    pmvSearch = v || '';
    renderPmvDetail(true);
    const inp = document.querySelector('#pmvDetail .pmv-search');
    if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
}

function pmvItemVisible(it, s) {
    if (pmvFilters.hideGone && it.gone) return false;
    if (pmvFilters.hideNonMedia && s.platform === 'pawchive' && !it.media_post) return false;
    if (pmvSearch && !(it.title || '').toLowerCase().includes(pmvSearch.toLowerCase())) return false;
    return true;
}

function renderPmvDetail(keepScroll) {
    const el = document.getElementById('pmvDetail');
    if (!el) return;
    const d = pmvDetail;
    if (!d || !d.creator) {
        el.innerHTML = '<div class="links-empty">Select a creator to see its videos.</div>';
        return;
    }
    const scroll = keepScroll ? el.scrollTop : 0;
    const c = d.creator;
    const tags = (c.tags || []).map(t => `<span class="chip">${escapeHtml(t)}</span>`).join('');
    const titleMap = pmvBuildTitleMap(d.sites || []);
    const sites = (d.sites || []);
    el.innerHTML = `
        <div class="pmv-detail-head">
            <div class="pmv-detail-title"><span class="pmv-detail-name">${escapeHtml(c.name)}</span>${tags}</div>
            <div class="actions-row">
                <button class="btn btn-secondary btn-sm pmv-fetch-btn" ${pmvBusy ? 'disabled' : ''} onclick="pmvFetchCurrent()"
                        title="Fetch new videos on every site of this creator">Fetch latest</button>
                <button class="btn btn-ghost btn-sm" onclick="openPmvEditor('${c.id}')">Edit</button>
            </div>
        </div>
        ${c.notes ? `<div class="field-hint pmv-notes">${escapeHtml(c.notes)}</div>` : ''}
        <div class="pmv-filters">
            <label class="checkbox-option" title="pawchive posts with no video / archive attachment and no external link"><input type="checkbox" ${pmvFilters.hideNonMedia ? 'checked' : ''} onchange="pmvSetFilter('hideNonMedia', this.checked)"><span class="checkbox-label">Hide image/text-only posts</span></label>
            <label class="checkbox-option"><input type="checkbox" ${pmvFilters.hideGone ? 'checked' : ''} onchange="pmvSetFilter('hideGone', this.checked)"><span class="checkbox-label">Hide gone</span></label>
            <input type="search" class="input-field pmv-search" placeholder="Filter titles…" value="${escapeHtml(pmvSearch)}" oninput="pmvSetSearch(this.value)">
        </div>
        ${sites.length ? sites.map((s, i) => renderPmvSite(s, i, titleMap)).join('')
                       : '<div class="links-empty">No sites yet — click <b>Edit</b> and add a rule34video / iwara / pawchive link.</div>'}`;
    el.scrollTop = scroll;
}

function renderPmvSite(s, idx, titleMap) {
    const k = s.counts || {};
    const isPaw = s.platform === 'pawchive';
    const items = (s.items || []).filter(it => pmvItemVisible(it, s));
    const who = s.display_name || s.username || s.user_id || '';
    const rank = idx === 0
        ? '<span class="badge pmv-primary" title="Source of truth — download from here first">primary</span>'
        : `<span class="badge badge-skipped" title="Priority ${idx + 1}: check for videos the sites above don't have">#${idx + 1}</span>`;
    const meta = [
        `${k.total || 0} videos`,
        k.unreviewed ? `${k.unreviewed} unreviewed` : '',
        k.gone ? `${k.gone} gone` : '',
        s.last_fetch ? `fetched ${pmvFmtWhen(s.last_fetch)}` : 'never fetched',
    ].filter(Boolean).join(' · ');
    const numbering = isPaw
        ? '<span class="pmv-hint" title="pawchive back-fills old posts, so numbers are recomputed by date on every fetch. A ✓ post whose number moved shows a red “shifted” badge.">chronological numbering</span>'
        : '';
    const todo = items.filter(it => it.status === 'unreviewed');
    const done = items.filter(it => it.status !== 'unreviewed');
    const markAll = todo.length
        ? `<button class="btn-tiny" onclick="pmvMarkVisible('${s.link_key}')" title="Mark every visible unreviewed video ✗ (not wanted)">Mark visible ✗</button>` : '';
    const rescan = isPaw ? ''
        : `<button class="btn-tiny pmv-fetch-btn" ${pmvBusy ? 'disabled' : ''} onclick="pmvRescanSite('${s.link_key}')"
             title="Walk the whole listing again to spot deleted (gone) videos. Numbers never change — unless older videos turn up and nothing is ✓ yet, in which case the catalogue is re-sorted by date.">Full rescan</button>`;
    const renumber = (isPaw || !(s.items || []).length) ? ''
        : `<button class="btn-tiny" onclick="pmvRenumber('${s.link_key}')"
             title="Re-sort the whole catalogue by upload date (for a first scan that missed videos, e.g. iwara before logging in). ✓ videos whose number moves will show a red “was NN” badge.">Renumber</button>`;
    const profile = s.url
        ? `<button class="btn-tiny" onclick="openLink('${encodeURIComponent(s.url)}')" title="${escapeHtml(s.url)}">Open profile ↗</button>` : '';
    let warn = '';
    if (s.last_error) warn = `<div class="pmv-warn pmv-warn-error">${escapeHtml(s.last_error)}</div>`;
    else if (!isPaw && !s.initial_complete) warn = '<div class="pmv-warn">Not numbered yet — run <b>Fetch latest</b> once to walk the whole catalogue.</div>';
    if (s.platform === 'iwara' && pmvDetail && pmvDetail.iwara_configured === false) {
        warn += '<div class="pmv-warn">Not logged in to iwara — videos hidden from guests are missing. Add your login under Settings ▸ Iwara, then <b>Full rescan</b>' +
                (s.counts && s.counts.downloaded ? ' and <b>Renumber</b>' : '') + '.</div>';
    }
    let body;
    if (!items.length) {
        body = `<div class="links-empty" style="padding: 10px 12px;">${(s.items || []).length ? 'Nothing matches the current filters.' : 'No videos recorded yet.'}</div>`;
    } else {
        const open = pmvExpanded.has(s.link_key);
        const nOk = done.filter(it => it.status === 'downloaded').length;
        const nX = done.length - nOk;
        body = `<div class="pmv-table">${todo.map(it => renderPmvRow(it, s, titleMap)).join('')}</div>`
            + (todo.length ? '' : '<div class="links-empty" style="padding: 8px 12px;">Nothing left to review here.</div>')
            + (done.length ? `<div class="pmv-reviewed-toggle" onclick="pmvToggleReviewed('${s.link_key}')" title="${open ? 'Hide' : 'Show'} the videos you have already reviewed">
                    <span class="pmv-caret">${open ? '▾' : '▸'}</span>
                    Reviewed ${done.length} <span class="badge pmv-primary">✓ ${nOk}</span> <span class="badge badge-skipped">✗ ${nX}</span>
                </div>` + (open ? `<div class="pmv-table">${done.map(it => renderPmvRow(it, s, titleMap)).join('')}</div>` : '') : '');
    }
    return `<div class="pmv-site">
        <div class="pmv-site-head">
            <span class="link-badge pmv-code">${escapeHtml(s.site_code)}</span>${rank}
            <span class="pmv-site-who" title="${escapeHtml(s.url)}">${escapeHtml(who)}</span>
            <span class="pmv-site-meta">${meta}</span>${numbering}
            <span class="pmv-site-actions">${markAll}${rescan}${renumber}${profile}</span>
        </div>
        ${warn}${body}
    </div>`;
}

function pmvToggleReviewed(linkKey) {
    if (pmvExpanded.has(linkKey)) pmvExpanded.delete(linkKey); else pmvExpanded.add(linkKey);
    renderPmvDetail(true);
}

async function pmvRenumber(linkKey) {
    const s = pmvDetail && (pmvDetail.sites || []).find(x => x.link_key === linkKey);
    if (!s) return;
    const nOk = (s.counts || {}).downloaded || 0;
    const msg = `Re-sort ${s.site_code}'s whole catalogue by upload date?\n\n`
        + (nOk ? `${nOk} video${nOk === 1 ? ' is' : 's are'} marked ✓ — any whose number changes will show a red "was NN" badge so you can rename the file, then click the badge to accept.`
               : 'Nothing is marked ✓ yet, so no filenames depend on the current numbers.');
    if (!confirm(msg)) return;
    let res;
    try { res = await pywebview.api.renumber_pmv_site(pmvCurrentId, linkKey); }
    catch (e) { res = { error: String(e) }; }
    if (!res || res.error) { showToast((res && res.error) || 'Renumber failed', 'error'); return; }
    showToast(res.changed ? `${res.changed} number${res.changed === 1 ? '' : 's'} changed` + (res.shifted ? `, ${res.shifted} ✓ shifted` : '') : 'Already in order', res.shifted ? 'info' : 'success');
    await openPmvCreator(pmvCurrentId, true);
    refreshPmvCreators();
}

function pmvPad(n, w) {
    return (n === null || n === undefined) ? '—' : String(n).padStart(w || 2, '0');
}

function renderPmvRow(it, s, titleMap) {
    const cls = ['pmv-item', 'is-' + it.status, it.gone ? 'is-gone' : ''].join(' ');
    const b = [];
    if (it.quality) b.push(`<span class="badge badge-q${it.quality >= 2160 ? ' badge-4k' : ''}" title="Best quality offered">${it.quality >= 2160 ? '4K' : it.quality + 'p'}</span>`);
    else if (s.platform === 'iwara' && !it.gone) b.push('<span class="badge badge-q" style="opacity:.45" title="iwara only records dimensions for newer uploads; the API returns nothing for this one">? p</span>');
    if (it.gone) b.push('<span class="badge badge-gone" title="No longer listed on the site — its number is kept">gone</span>');
    if (it.backfilled) b.push('<span class="badge badge-backfilled" title="Appeared after newer posts had already been numbered">backfilled</span>');
    if (it.shifted) b.push(`<span class="badge badge-shifted" onclick="pmvAckShift('${s.link_key}', '${it.id}')"
        title="Was #${pmvPad(it.number_at_check, s.width)} when you marked it ✓; now #${pmvPad(it.number, s.width)}. Rename the file, then click to accept the new number.">was ${pmvPad(it.number_at_check, s.width)}</span>`);
    if (it.private) b.push('<span class="badge badge-private" title="Private on iwara — needs a logged-in account to view">private</span>');
    if (it.detail_pending) b.push('<span class="badge badge-skipped" title="Date / quality lookup failed; it is retried on the next fetch">no detail</span>');
    (it.media_kinds || []).forEach(kd => {
        if (kd === 'video' || kd === 'archive') b.push(`<span class="badge badge-media badge-media-${kd}">${kd}</span>`);
    });
    (it.link_hosts || []).forEach(h => {
        const alt = /mega|drive|dropbox|mediafire|pixeldrain/i.test(h) ? ' host-alt' : '';
        b.push(`<span class="badge badge-host${alt}">${escapeHtml(h)}</span>`);
    });
    const dup = titleMap ? pmvDupHint(it, s, titleMap) : '';
    const encUrl = encodeURIComponent(it.url || '');
    const encPrefix = encodeURIComponent(it.prefix || '');
    return `<div class="${cls}">
        <span class="pmv-num" title="Catalogue number on ${escapeHtml(s.site_code)}">${pmvPad(it.number, s.width)}</span>
        <span class="pmv-status">
            <button class="pmv-st${it.status === 'downloaded' ? ' on' : ''}" title="Downloaded — click again to clear"
                    onclick="pmvSetStatus('${s.link_key}', '${it.id}', 'downloaded')">✓</button>
            <button class="pmv-st x${it.status === 'skipped' ? ' on' : ''}" title="Not downloading this one — click again to clear"
                    onclick="pmvSetStatus('${s.link_key}', '${it.id}', 'skipped')">✗</button>
        </span>
        <span class="pmv-title">
            <a href="#" onclick="openLink('${encUrl}'); return false;" title="Open the post in your browser">${escapeHtml(it.title || it.id)}</a>${dup}
        </span>
        <span class="pmv-date" title="${escapeHtml(it.date || '')}">${pmvFmtDate(it.date)}</span>
        <span class="pmv-dur">${pmvFmtDur(it.duration)}</span>
        <span class="pmv-badges">${b.join('')}</span>
        <span class="pmv-actions">
            <button class="btn-tiny" onclick="copyText('${encPrefix}')" title="Copy “${escapeHtml(it.prefix || '')}” — paste it as the start of the filename">Copy prefix</button>
            <button class="btn-tiny" onclick="openLink('${encUrl}')" title="Open in browser">Open ↗</button>
        </span>
    </div>`;
}

// Same-title hint across the creator's other sites (naming differs between
// sites often enough that this is only a nudge, never a verdict).
function pmvNormTitle(t) {
    return (t || '').normalize('NFKC').toLowerCase()
        .replace(/[【】\[\]()（）{}]/g, ' ')
        .replace(/\b(hmv|pmv|fhd|hd|4k|60\s?fps|uhd)\b/g, ' ')
        .replace(/[^\p{L}\p{N}]+/gu, ' ')
        .trim();
}

function pmvBuildTitleMap(sites) {
    const map = new Map();
    sites.forEach(s => (s.items || []).forEach(it => {
        const key = pmvNormTitle(it.title);
        if (key.length < 4) return;
        if (!map.has(key)) map.set(key, []);
        map.get(key).push({ site: s, it });
    }));
    return map;
}

function pmvDupHint(it, s, map) {
    const others = (map.get(pmvNormTitle(it.title)) || []).filter(e => e.site.link_key !== s.link_key);
    if (!others.length) return '';
    const txt = others.map(o => `${escapeHtml(o.site.site_code)} #${pmvPad(o.it.number, o.site.width)}${o.it.status === 'downloaded' ? ' ✓' : ''}`).join(', ');
    return `<span class="pmv-hint" title="Same title on another of this creator's sites — probably the same video">≈ ${txt}</span>`;
}

function pmvFmtDate(iso) {
    if (!iso) return '';
    return String(iso).slice(0, 10);
}

function pmvFmtDur(sec) {
    if (!sec && sec !== 0) return '';
    sec = Math.round(sec);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h ? `${h}:${String(m).padStart(2, '0')}` : `${m}`) + ':' + String(s).padStart(2, '0');
}

function pmvFmtWhen(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return String(iso).slice(0, 16).replace('T', ' ');
    const today = new Date();
    const same = d.toDateString() === today.toDateString();
    const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return same ? `today ${hm}` : `${d.toLocaleDateString()} ${hm}`;
}

// ── status writes ───────────────────────────────────────────────

function pmvFindItem(linkKey, id) {
    const s = pmvDetail && (pmvDetail.sites || []).find(x => x.link_key === linkKey);
    const it = s && (s.items || []).find(x => x.id === id);
    return { s, it };
}

function pmvRecount(s) {
    const c = { total: 0, unreviewed: 0, new: 0, downloaded: 0, skipped: 0, shifted: 0, gone: 0 };
    (s.items || []).forEach(it => {
        c.total++;
        if (it.gone) c.gone++;
        if (it.status === 'downloaded') c.downloaded++;
        else if (it.status === 'skipped') c.skipped++;
        else if (!it.gone) { c.unreviewed++; if (!it.initial) c.new++; }
        if (it.shifted) c.shifted++;
    });
    s.counts = c;
    // Mirror into the left list so its badges track without re-reading manifests.
    const row = pmvCreators.find(x => x.id === pmvCurrentId);
    if (row && pmvDetail) {
        const tot = { total: 0, unreviewed: 0, new: 0, downloaded: 0, skipped: 0, shifted: 0, gone: 0 };
        pmvDetail.sites.forEach(x => Object.keys(tot).forEach(k => { tot[k] += (x.counts || {})[k] || 0; }));
        row.counts = tot;
    }
}

async function pmvSetStatus(linkKey, id, status) {
    const { s, it } = pmvFindItem(linkKey, id);
    if (!it) return;
    const next = it.status === status ? 'unreviewed' : status;
    let res;
    try { res = await pywebview.api.set_pmv_status(pmvCurrentId, linkKey, id, next); }
    catch (e) { res = { error: String(e) }; }
    if (!res || res.error) { showToast((res && res.error) || 'Could not save', 'error'); return; }
    it.status = next;
    it.number_at_check = next === 'downloaded' ? it.number : null;
    it.shifted = false;
    pmvRecount(s);
    renderPmvDetail(true);
    renderPmvList();
}

async function pmvMarkVisible(linkKey) {
    const s = pmvDetail && (pmvDetail.sites || []).find(x => x.link_key === linkKey);
    if (!s) return;
    const ids = (s.items || []).filter(it => it.status === 'unreviewed' && pmvItemVisible(it, s)).map(it => it.id);
    if (!ids.length) return;
    if (!confirm(`Mark ${ids.length} visible video${ids.length === 1 ? '' : 's'} on ${s.site_code} as ✗ (not wanted)?`)) return;
    let res;
    try { res = await pywebview.api.set_pmv_status_bulk(pmvCurrentId, linkKey, ids, 'skipped'); }
    catch (e) { res = { error: String(e) }; }
    if (!res || res.error) { showToast((res && res.error) || 'Could not save', 'error'); return; }
    s.items.forEach(it => { if (ids.includes(it.id)) { it.status = 'skipped'; it.number_at_check = null; it.shifted = false; } });
    pmvRecount(s);
    renderPmvDetail(true);
    renderPmvList();
    showToast(`${res.updated} marked ✗`, 'info');
}

async function pmvAckShift(linkKey, id) {
    const { s, it } = pmvFindItem(linkKey, id);
    if (!it) return;
    let res;
    try { res = await pywebview.api.ack_pmv_shift(pmvCurrentId, linkKey, id); }
    catch (e) { res = { error: String(e) }; }
    if (!res || res.error) { showToast((res && res.error) || 'Could not save', 'error'); return; }
    it.number_at_check = it.number;
    it.shifted = false;
    pmvRecount(s);
    renderPmvDetail(true);
    renderPmvList();
}

// ── creator editor overlay ──────────────────────────────────────

let pmvEdId = '';
let pmvEdLinks = [];
let pmvEdTimer = null;

async function openPmvEditor(id) {
    pmvEdId = id || '';
    let rec = null;
    if (pmvEdId) {
        try { rec = await pywebview.api.get_pmv_creator(pmvEdId); } catch (e) { rec = null; }
        if (!rec) { showToast('Creator not found', 'error'); return; }
    }
    document.getElementById('pmvEditorTitle').textContent = rec ? 'Edit PMV creator' : 'New PMV creator';
    document.getElementById('pmvEdName').value = rec ? rec.name : '';
    document.getElementById('pmvEdTags').value = rec ? (rec.tags || []).join(', ') : '';
    document.getElementById('pmvEdNotes').value = rec ? (rec.notes || '') : '';
    document.getElementById('pmvEdNewLink').value = '';
    document.getElementById('pmvEdPreview').textContent = '';
    document.getElementById('pmvEdDeleteBtn').style.display = rec ? '' : 'none';
    pmvEdLinks = rec ? (rec.links || []).map(l => ({ ...l })) : [];
    renderPmvEdLinks();
    document.getElementById('pmvEditorOverlay').classList.add('visible');
    setTimeout(() => document.getElementById(rec ? 'pmvEdNewLink' : 'pmvEdName').focus(), 50);
}

function closePmvEditor() {
    document.getElementById('pmvEditorOverlay').classList.remove('visible');
}

function pmvPlatformLabel(l) {
    return { rule34video: { badge: 'rule34video', cls: '' }, iwara: { badge: 'iwara', cls: 'badge-twitter' },
             pawchive: { badge: (l.service || 'pawchive'), cls: 'badge-pawchive' } }[l.platform]
        || { badge: l.platform || '?', cls: '' };
}

function renderPmvEdLinks() {
    const el = document.getElementById('pmvEdLinks');
    if (!pmvEdLinks.length) {
        el.innerHTML = '<div class="links-empty">No sites yet. Paste a profile URL below — the first site is the source of truth.</div>';
        return;
    }
    el.innerHTML = pmvEdLinks.map((l, i) => {
        const p = pmvPlatformLabel(l);
        const name = l.display_name || l.username || l.user_id || '?';
        return `<div class="link-row">
            <span class="pmv-ed-rank" title="Priority">${i + 1}</span>
            <span class="link-badge ${p.cls}">${escapeHtml(p.badge)}</span>
            <div class="link-meta">
                <span class="link-name">${escapeHtml(name)}</span>
                <span class="link-url">${escapeHtml(l.url || '')}</span>
            </div>
            <label class="link-tag" title="Site code used in the filename prefix, e.g. “SadBernard - R34 - 02 - ”">
                <span>Code</span>
                <input type="text" class="input-field pmv-ed-code" value="${escapeHtml(l.site_code || '')}" oninput="pmvEdCode(${i}, this.value)">
            </label>
            <span class="pmv-ed-move">
                <button class="btn-tiny" onclick="pmvEdMove(${i}, -1)" ${i === 0 ? 'disabled' : ''} title="Higher priority">▲</button>
                <button class="btn-tiny" onclick="pmvEdMove(${i}, 1)" ${i === pmvEdLinks.length - 1 ? 'disabled' : ''} title="Lower priority">▼</button>
            </span>
            <button class="link-remove" onclick="pmvEdRemove(${i})" title="Remove site (its checklist file is kept on disk)">&times;</button>
        </div>`;
    }).join('');
}

function pmvEdCode(i, v) {
    if (pmvEdLinks[i]) pmvEdLinks[i].site_code = v;
}

function pmvEdMove(i, delta) {
    const j = i + delta;
    if (j < 0 || j >= pmvEdLinks.length) return;
    [pmvEdLinks[i], pmvEdLinks[j]] = [pmvEdLinks[j], pmvEdLinks[i]];
    renderPmvEdLinks();
}

function pmvEdRemove(i) {
    pmvEdLinks.splice(i, 1);
    renderPmvEdLinks();
}

function pmvEdDescribe(info) {
    const who = info.display_name || info.username || info.user_id || '?';
    const site = { rule34video: 'rule34video', iwara: 'iwara', pawchive: `pawchive (${info.service || '?'})` }[info.platform] || info.platform;
    return `Detected: ${site} — ${who}` + (info.note ? ` (${info.note})` : '');
}

function pmvEdSchedulePreview() {
    clearTimeout(pmvEdTimer);
    pmvEdTimer = setTimeout(pmvEdPreview, 350);
}

async function pmvEdPreview() {
    const url = document.getElementById('pmvEdNewLink').value.trim();
    const preview = document.getElementById('pmvEdPreview');
    if (!url) { preview.textContent = ''; return; }
    preview.textContent = 'Checking…';
    let info;
    try { info = await pywebview.api.resolve_pmv_link(url); } catch (e) { info = { valid: false }; }
    if (document.getElementById('pmvEdNewLink').value.trim() !== url) return;   // typed more since
    preview.textContent = info && info.valid
        ? pmvEdDescribe(info) + '  ·  click Add site'
        : 'Unrecognized URL — expected rule34video.com/members/…, iwara.tv/profile/…, or pawchive.pw/{service}/user/…';
    if (info && info.valid && info.platform === 'iwara') {
        try {
            const st = await pywebview.api.iwara_login_status();
            if (st && !st.configured) {
                preview.textContent += '   ⚠ Not logged in to iwara: videos hidden from guests would be missed and numbered out of order later. Add your login under Settings ▸ Iwara first.';
            }
        } catch (e) { /* ignore */ }
    }
}

function pmvEdSameSite(a, b) {
    if (a.platform !== b.platform) return false;
    if (a.platform === 'pawchive') return a.service === b.service && a.user_id === b.user_id;
    if (a.platform === 'iwara') return (a.user_id && a.user_id === b.user_id)
        || (a.username || '').toLowerCase() === (b.username || '').toLowerCase();
    return a.user_id === b.user_id;
}

async function pmvEdAdd() {
    const input = document.getElementById('pmvEdNewLink');
    const url = input.value.trim();
    if (!url) return true;
    let info;
    try { info = await pywebview.api.resolve_pmv_link(url); } catch (e) { info = { valid: false }; }
    if (!info || !info.valid) { showToast('Unrecognized URL', 'error'); return false; }
    if (pmvEdLinks.some(l => pmvEdSameSite(l, info))) {
        showToast('That site is already added', 'info');
    } else {
        delete info.valid; delete info.note;
        pmvEdLinks.push(info);
    }
    input.value = '';
    document.getElementById('pmvEdPreview').textContent = '';
    renderPmvEdLinks();
    return true;
}

async function pmvEdSave() {
    // Commit a URL still sitting in the Add box (paste → Save is common).
    if (document.getElementById('pmvEdNewLink').value.trim()) {
        const ok = await pmvEdAdd();
        if (!ok) return;
    }
    const name = document.getElementById('pmvEdName').value.trim();
    if (!name) { showToast('Give the creator a name — it becomes the filename prefix', 'error'); return; }
    const payload = {
        id: pmvEdId || undefined,
        name,
        tags: document.getElementById('pmvEdTags').value,
        notes: document.getElementById('pmvEdNotes').value,
        links: pmvEdLinks.map(l => ({ ...l, site_code: (l.site_code || '').trim() })),
    };
    let res;
    try { res = await pywebview.api.save_pmv_creator(payload); }
    catch (e) { res = { error: String(e) }; }
    if (!res || res.error) { showToast((res && res.error) || 'Save failed', 'error'); return; }
    closePmvEditor();
    showToast('PMV creator saved', 'success');
    await refreshPmvCreators();
    await openPmvCreator(res.id);
    if (typeof switchView === 'function') switchView('pmv');
}

async function pmvEdDelete() {
    if (!pmvEdId) return;
    const name = document.getElementById('pmvEdName').value.trim() || 'this creator';
    if (!confirm(`Remove ${name} from the PMV tracker?`)) return;
    const wipe = confirm('Also delete its checklist files (which videos you marked ✓/✗)?\n\nOK = delete them too.  Cancel = keep them on disk.');
    let res;
    try { res = await pywebview.api.delete_pmv_creator(pmvEdId, wipe); }
    catch (e) { res = { error: String(e) }; }
    if (!res || res.error) { showToast((res && res.error) || 'Delete failed', 'error'); return; }
    if (pmvCurrentId === pmvEdId) { pmvCurrentId = ''; pmvDetail = null; renderPmvDetail(); }
    pmvSelected.delete(pmvEdId);
    closePmvEditor();
    showToast('Removed', 'info');
    refreshPmvCreators();
}

document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('pmvEdNewLink');
    if (!input) return;
    input.addEventListener('input', pmvEdSchedulePreview);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); pmvEdAdd(); } });
});
