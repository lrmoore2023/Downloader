/* ── PMV ▸ Latest ─────────────────────────────────────────────────
   Every new video across all PMV creators in one list, plus the
   "Check for new" job: it reads each site's followed feed and fetches only the
   creators that changed (backend/pmv_check.py). ✓/✗ here writes the same
   manifests as the creator page, so the two views can never disagree. */

let pmvMode = 'latest';
let pmvLatest = null;               // get_pmv_latest()
let pmvLatestSel = new Set();       // "creator_id|link_key|item_id"
let pmvLatestSite = '';
let pmvLatestSearch = '';
let pmvLatestHideNonMedia = true;
let pmvLatestSeq = 0;

const PMV_SITE_LABEL = { rule34video: 'rule34video', iwara: 'iwara', pmvhaven: 'PMVHaven',
                         pawchive: 'pawchive', hmvmania: 'HMVMania' };

function pmvRowKey(it) { return `${it.creator_id}|${it.link_key}|${it.id}`; }

// ── mode switch ─────────────────────────────────────────────────

function pmvSetMode(mode, quiet) {
    pmvMode = mode === 'creators' ? 'creators' : 'latest';
    document.querySelectorAll('.pmv-mode').forEach(b => {
        const on = b.dataset.mode === pmvMode;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    const latest = document.getElementById('pmvLatest');
    const creators = document.getElementById('pmvCreatorsPane');
    if (latest) latest.style.display = pmvMode === 'latest' ? '' : 'none';
    if (creators) creators.style.display = pmvMode === 'creators' ? '' : 'none';
    if (!quiet) pywebview.api.save_state({ pmv_mode: pmvMode });
    if (pmvMode === 'latest') refreshPmvLatest();
}

function initPmvLatest(state) {
    pmvSetMode(state.pmv_mode || 'latest', true);
}

// ── data ────────────────────────────────────────────────────────

async function refreshPmvLatest() {
    const seq = ++pmvLatestSeq;
    let d;
    try { d = await pywebview.api.get_pmv_latest(); }
    catch (e) { return; }
    if (seq !== pmvLatestSeq || !d) return;
    pmvLatest = d;
    const live = new Set((d.items || []).map(pmvRowKey));
    pmvLatestSel = new Set([...pmvLatestSel].filter(k => live.has(k)));
    renderPmvLatest();
}

function pmvLatestVisible() {
    if (!pmvLatest) return [];
    const words = pmvLatestSearch ? pmvLatestSearch.split(/\s+/) : [];
    return (pmvLatest.items || []).filter(it => {
        if (pmvLatestSite && it.platform !== pmvLatestSite) return false;
        if (pmvLatestHideNonMedia && it.media_post === false) return false;
        if (words.length) {
            const hay = `${it.creator_name} ${it.title} ${it.site_code}`.toLowerCase();
            if (!words.every(w => hay.includes(w))) return false;
        }
        return true;
    });
}

// ── render ──────────────────────────────────────────────────────

function renderPmvLatest() {
    const d = pmvLatest;
    if (!d) return;
    const total = (d.items || []).filter(it => !(pmvLatestHideNonMedia && it.media_post === false)).length;
    const cnt = document.getElementById('pmvLatestCount');
    if (cnt) cnt.textContent = total ? String(total) : '';

    // meta line
    const meta = document.getElementById('pmvLatestMeta');
    if (meta) {
        const parts = [];
        parts.push(d.last_check ? `Last check ${pmvFmtWhen(d.last_check)}` : 'Not checked yet');
        parts.push(d.last_sweep ? `last full sweep ${pmvFmtWhen(d.last_sweep)}` : 'no full sweep yet');
        if (d.since) parts.push(`watching uploads since ${pmvFmtWhen(d.since)}`);
        meta.textContent = parts.join(' · ');
    }
    renderPmvFeedChips(d);
    renderPmvCoverage(d);
    renderPmvLatestSiteSelect(d);

    const body = document.getElementById('pmvLatestBody');
    if (!body) return;
    const rows = pmvLatestVisible();
    const html = [];
    if (!rows.length) {
        html.push(`<div class="links-empty">${(d.items || []).length
            ? 'Nothing matches the filters.'
            : (d.last_check ? 'All caught up — nothing new to review.' : 'Click <b>Check for new</b> to look for new uploads from the creators you track.')}</div>`);
    } else {
        // Group by upload day, newest first (undated: by when it was found).
        let day = null;
        let group = [];
        const flush = () => {
            if (!group.length) return;
            html.push(`<div class="pmv-day"><div class="pmv-day-head">${escapeHtml(pmvDayLabel(day))}<span>${group.length}</span></div>
                <div class="pmv-table">${group.map(renderPmvLatestRow).join('')}</div></div>`);
            group = [];
        };
        rows.forEach(it => {
            const k = String(it.date || it.first_seen || '').slice(0, 10);
            if (k !== day) { flush(); day = k; }
            group.push(it);
        });
        flush();
    }
    html.push(renderPmvPending(d));
    html.push(renderPmvUntracked(d));
    body.innerHTML = html.join('');
    syncPmvLatestSelAll(rows);
}

function pmvDayLabel(day) {
    if (!day) return 'Undated';
    const d = new Date(day + 'T12:00:00');
    if (isNaN(d)) return day;
    const today = new Date();
    const y = new Date(); y.setDate(today.getDate() - 1);
    const name = d.toDateString() === today.toDateString() ? 'Today'
        : d.toDateString() === y.toDateString() ? 'Yesterday'
        : d.toLocaleDateString(undefined, { weekday: 'short' });
    return `${name} · ${day.replace(/-/g, '.')}`;
}

function renderPmvLatestRow(it) {
    const k = pmvRowKey(it);
    const encK = encodeURIComponent(k);
    const b = [];
    if (it.just_in) b.push('<span class="badge pmv-new" title="Found by the last check">just in</span>');
    if (it.quality) b.push(`<span class="badge badge-q${it.quality >= 2160 ? ' badge-4k' : ''}">${it.quality >= 2160 ? '4K' : it.quality + 'p'}</span>`);
    if (it.backfilled) b.push('<span class="badge badge-backfilled" title="Appeared after newer posts had already been numbered">backfilled</span>');
    if (it.private) b.push('<span class="badge badge-private">private</span>');
    if (it.detail_pending) b.push('<span class="badge badge-skipped" title="Date / quality lookup failed; retried on the next fetch">no detail</span>');
    if (it.preview_state === 'pending') b.push('<span class="badge badge-skipped" title="pawchive hasn\'t imported this post\'s files yet">not imported</span>');
    (it.media_kinds || []).forEach(kd => {
        if (kd === 'video' || kd === 'archive') b.push(`<span class="badge badge-media badge-media-${kd}">${kd}</span>`);
    });
    (it.link_hosts || []).forEach(h => b.push(`<span class="badge badge-host">${escapeHtml(h)}</span>`));
    const encUrl = encodeURIComponent(it.url || '');
    const encPrefix = encodeURIComponent(it.prefix || '');
    return `<div class="pmv-item pmv-latest-item is-${it.status}">
        <input type="checkbox" class="pmv-check" ${pmvLatestSel.has(k) ? 'checked' : ''}
               onchange="pmvLatestToggle('${encK}', this.checked)" title="Select">
        <span class="pmv-status">
            <button class="pmv-st" title="Downloaded" onclick="pmvLatestSet('${encK}', 'downloaded')">✓</button>
            <button class="pmv-st x" title="Not downloading this one" onclick="pmvLatestSet('${encK}', 'skipped')">✗</button>
        </span>
        <span class="pmv-latest-who">
            <a href="#" onclick="pmvOpenFromLatest('${encodeURIComponent(it.creator_id)}'); return false;"
               title="Open this creator's page">${escapeHtml(it.creator_name)}</a>
            <span class="pmv-site-chip">${escapeHtml(it.site_code)}</span>
        </span>
        <span class="pmv-title">
            <a href="#" onclick="openLink('${encUrl}'); return false;" title="Open in your browser">${escapeHtml(it.title || it.id)}</a>
        </span>
        <span class="pmv-date" title="${escapeHtml(it.date || '')}">${pmvFmtDate(it.date)}</span>
        <span class="pmv-dur">${pmvFmtDur(it.duration)}</span>
        <span class="pmv-badges">${b.join('')}</span>
        <span class="pmv-actions">
            <button class="btn-tiny" onclick="copyText('${encPrefix}')" title="Copy “${escapeHtml(it.prefix || '')}”">Copy prefix</button>
            <button class="btn-tiny" onclick="openLink('${encUrl}')" title="Open in browser">Open ↗</button>
        </span>
    </div>`;
}

function renderPmvFeedChips(d) {
    const el = document.getElementById('pmvFeedChips');
    if (!el) return;
    const res = d.last_result || {};
    const plats = res.platforms || {};
    const acc = d.accounts || {};
    const order = ['iwara', 'pmvhaven', 'rule34video', 'pawchive'];
    const chips = order.map(p => {
        const r = plats[p];
        const signedIn = acc[p] ? acc[p].signed_in : true;
        let cls = 'idle', txt, tip = '';
        if (!r) {
            txt = signedIn ? 'not checked yet' : 'not signed in';
            cls = signedIn ? 'idle' : 'warn';
        } else if (r.status === 'ok') {
            cls = 'ok';
            txt = p === 'pawchive' ? `${r.flagged} updated` : `feed ✓ · ${r.flagged} new`;
            if (r.unresolved) { cls = 'warn'; txt += ` · ${r.unresolved} to retry`; }
        } else if (r.status === 'off') {
            cls = 'warn'; txt = 'no feed → checked one by one';
        } else if (r.status === 'auth') {
            cls = 'bad'; txt = 'sign-in expired → checked one by one';
        } else if (r.status === 'gap') {
            cls = 'warn'; txt = 'feed too far behind → checked one by one';
        } else {
            cls = 'bad'; txt = 'feed error → checked one by one';
        }
        tip = (r && r.message) || '';
        const fix = (cls === 'bad' || (!signedIn && p !== 'pawchive'))
            ? ` <a href="#" onclick="openSettings(); return false;">Settings</a>` : '';
        return `<span class="pmv-feed-chip ${cls}" title="${escapeHtml(tip)}"><b>${PMV_SITE_LABEL[p]}</b> ${escapeHtml(txt)}${fix}</span>`;
    });
    chips.push('<span class="pmv-feed-chip idle"><b>HMVMania</b> no feed → checked one by one</span>');
    const extra = [];
    if (res.marked_read) extra.push(`${res.marked_read} PMVHaven notification${res.marked_read === 1 ? '' : 's'} marked read`);
    if (res.fetched_links !== undefined) extra.push(`last check fetched ${res.fetched_links} link${res.fetched_links === 1 ? '' : 's'}`);
    el.innerHTML = chips.join('') + (extra.length ? `<span class="pmv-feed-extra">${escapeHtml(extra.join(' · '))}</span>` : '');
}

function renderPmvCoverage(d) {
    const el = document.getElementById('pmvCoverage');
    if (!el) return;
    const un = ((d.last_result || {}).unfollowed) || [];
    if (!un.length) { el.innerHTML = ''; return; }
    const list = un.map(u => `${escapeHtml(u.creator)} (${escapeHtml(PMV_SITE_LABEL[u.platform] || u.platform)})`).join(', ');
    el.innerHTML = `<details class="pmv-coverage"><summary>${un.length} tracked link${un.length === 1 ? ' isn\'t' : 's aren\'t'} followed on the site — checked one by one every time</summary>
        <div class="field-hint">${list}. Follow them on the site to make checks faster; nothing is missed either way.</div></details>`;
}

function renderPmvLatestSiteSelect(d) {
    const sel = document.getElementById('pmvLatestSite');
    if (!sel) return;
    const counts = {};
    (d.items || []).forEach(it => { counts[it.platform] = (counts[it.platform] || 0) + 1; });
    const opts = [`<option value="">All sites</option>`].concat(
        Object.keys(PMV_SITE_LABEL).filter(p => counts[p] || p === pmvLatestSite)
            .map(p => `<option value="${p}">${PMV_SITE_LABEL[p]} (${counts[p] || 0})</option>`));
    sel.innerHTML = opts.join('');
    sel.value = pmvLatestSite;
}

function renderPmvPending(d) {
    const p = d.pending || [];
    if (!p.length) return '';
    const rows = p.map(x => {
        const enc = encodeURIComponent(x.url || '');
        return `<div class="pmv-side-row">
            <span class="pmv-site-chip">${escapeHtml(x.site_code || x.platform)}</span>
            <span class="pmv-side-who">${escapeHtml(x.creator_name || '')}</span>
            <a href="#" onclick="openLink('${enc}'); return false;">${escapeHtml(x.title || x.video_id)}</a>
            <span class="pmv-side-meta">seen ${pmvFmtWhen(x.first_seen)}</span>
            <button class="btn-tiny" onclick="pmvDismissPending('${encodeURIComponent(x.key)}')" title="Stop waiting for this one">Dismiss</button>
        </div>`;
    }).join('');
    return `<details class="pmv-side" open><summary>Waiting for the listing (${p.length})
        <span class="field-hint">— seen in a feed but not on the creator's page yet; looked for again on every check</span></summary>${rows}</details>`;
}

function renderPmvUntracked(d) {
    const u = d.untracked || [];
    if (!u.length) return '';
    const rows = u.map(x => {
        const vids = (x.items || []).slice(0, 3).map(v =>
            `<a href="#" onclick="openLink('${encodeURIComponent(v.url || '')}'); return false;">${escapeHtml(v.title || v.video_id)}</a>`).join(' · ');
        const more = (x.items || []).length > 3 ? ` +${x.items.length - 3}` : '';
        return `<div class="pmv-side-row">
            <span class="pmv-site-chip">${escapeHtml(PMV_SITE_LABEL[x.platform] || x.platform)}</span>
            <span class="pmv-side-who">${escapeHtml(x.name || x.uploader_id)}</span>
            <span class="pmv-side-vids">${vids}${more}</span>
            <button class="btn-tiny" onclick="pmvTrackUntracked('${encodeURIComponent(x.profile_url || '')}', '${encodeURIComponent(x.name || '')}')">Track</button>
            <button class="btn-tiny" onclick="pmvDismissUntracked('${encodeURIComponent(x.key)}')" title="Hide this uploader from Latest">Dismiss</button>
        </div>`;
    }).join('');
    return `<details class="pmv-side"><summary>Followed but not tracked (${u.length})
        <span class="field-hint">— new uploads from accounts you follow that aren't PMV creators here</span></summary>${rows}</details>`;
}

// ── selection + status ──────────────────────────────────────────

function pmvLatestToggle(encK, on) {
    const k = decodeURIComponent(encK);
    if (on) pmvLatestSel.add(k); else pmvLatestSel.delete(k);
    syncPmvLatestSelAll(pmvLatestVisible());
}

function pmvLatestSelectAll(on) {
    const rows = pmvLatestVisible();
    if (on) rows.forEach(it => pmvLatestSel.add(pmvRowKey(it)));
    else rows.forEach(it => pmvLatestSel.delete(pmvRowKey(it)));
    renderPmvLatest();
}

function syncPmvLatestSelAll(rows) {
    const box = document.getElementById('pmvLatestSelAll');
    const label = document.getElementById('pmvLatestSelLabel');
    const n = rows.length;
    const sel = rows.filter(it => pmvLatestSel.has(pmvRowKey(it))).length;
    if (box) { box.checked = n > 0 && sel === n; box.indeterminate = sel > 0 && sel < n; }
    if (label) label.textContent = sel ? `${sel} selected` : 'Select all';
}

async function pmvLatestApply(keys, status) {
    const entries = keys.map(k => {
        const [creator_id, link_key, ...rest] = k.split('|');
        return { creator_id, link_key, item_id: rest.join('|') };
    });
    if (!entries.length) return;
    let res;
    try { res = await pywebview.api.set_pmv_status_multi(entries, status); }
    catch (e) { res = { errors: [String(e)] }; }
    if (res && res.errors && res.errors.length) showToast(res.errors[0], 'error');
    keys.forEach(k => pmvLatestSel.delete(k));
    await refreshPmvLatest();
    refreshPmvCreators();
    if (pmvCurrentId && typeof openPmvCreator === 'function') openPmvCreator(pmvCurrentId, true);
}

function pmvLatestSet(encK, status) {
    pmvLatestApply([decodeURIComponent(encK)], status);
}

function pmvLatestBulk(status) {
    const visible = new Set(pmvLatestVisible().map(pmvRowKey));
    const keys = [...pmvLatestSel].filter(k => visible.has(k));
    if (!keys.length) { showToast('Select some videos first', 'info'); return; }
    pmvLatestApply(keys, status);
}

function pmvLatestSetSite(v) { pmvLatestSite = v || ''; renderPmvLatest(); }
function pmvLatestSetSearch(v) { pmvLatestSearch = (v || '').trim().toLowerCase(); renderPmvLatest(); }
function pmvLatestSetHideNonMedia(on) { pmvLatestHideNonMedia = !!on; renderPmvLatest(); }

function pmvOpenFromLatest(encId) {
    pmvSetMode('creators');
    openPmvCreator(decodeURIComponent(encId));
}

async function pmvDismissPending(encKey) {
    const r = await pywebview.api.dismiss_pmv_pending(decodeURIComponent(encKey));
    if (r && r.error) showToast(r.error, 'info');
    refreshPmvLatest();
}

async function pmvDismissUntracked(encKey) {
    const r = await pywebview.api.dismiss_pmv_untracked(decodeURIComponent(encKey));
    if (r && r.error) showToast(r.error, 'info');
    refreshPmvLatest();
}

async function pmvTrackUntracked(encUrl, encName) {
    const url = decodeURIComponent(encUrl);
    await openPmvEditor('');
    const name = document.getElementById('pmvEdName');
    if (name && !name.value) name.value = decodeURIComponent(encName);
    const inp = document.getElementById('pmvEdNewLink');
    if (inp && url) {
        inp.value = url;
        if (typeof pmvEdAdd === 'function') await pmvEdAdd();
    }
}

// ── the check job ───────────────────────────────────────────────

async function pmvStartCheck(mode) {
    if (pmvBusy) { showToast('A PMV fetch is already running', 'info'); return; }
    const sweepPaw = !!(document.getElementById('pmvSweepPaw') || {}).checked;
    setPmvBusy(true, mode === 'sweep' ? 'Starting full sweep…' : 'Checking feeds…');
    let res;
    try { res = await pywebview.api.start_pmv_check(mode, sweepPaw); }
    catch (e) { res = { error: String(e) }; }
    if (!res || res.error) {
        setPmvBusy(false);
        showToast((res && res.error) || 'Could not start', 'error');
    }
}

function pmvCheckForNew() { pmvStartCheck('check'); }
function pmvFullSweep() { pmvStartCheck('sweep'); }

// Summary toast for a finished check; returns true when it handled the toast.
function pmvCheckToast(r) {
    const c = r && r.check;
    if (!c) return false;
    if (r.cancelled) { showToast('Check cancelled', 'info'); return true; }
    const n = r.total_new || 0;
    const errs = (r.errors || []).length;
    const what = c.mode === 'sweep' ? 'Full sweep' : 'Check';
    const bits = [`${n} new video${n === 1 ? '' : 's'}`, `${c.fetched_links} link${c.fetched_links === 1 ? '' : 's'} fetched`];
    if (c.pending) bits.push(`${c.pending} waiting for the listing`);
    if (errs) bits.push(`${errs} error${errs === 1 ? '' : 's'}`);
    showToast(`${what} done: ${bits.join(', ')}`, errs ? 'info' : 'success');
    return true;
}
