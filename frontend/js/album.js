/* ── Albums panel ───────────────────────────────────────────────────
 * Ad-hoc + saved-creator album downloader (bunkr / cyberdrop / filester).
 * Two modes:
 *   quick   — pick a folder, paste links, download.
 *   creator — a registry (isolated from the regular downloader's creators):
 *             name + root dir + saved links, with per-album download history.
 * Before any download, already-downloaded albums trigger a prompt: scan for new
 * files / redownload whole / skip. Progress is pushed from Python (onAlbum*).
 */

let albumMode = 'quick';           // quick | creator
let albumDest = '';                // quick-mode destination folder
let albumBusy = false;
let albumActiveDest = '';          // destination of the last/active run (for errors+retry)
let albumLastFailedUrls = [];
let albumCurrentCreator = null;    // full record {id,name,root_dir,links[]}
let albumPending = null;           // { entries, forceUrls, creatorId, dest } awaiting dedup confirm

// Called by switchView when the Albums tab is shown.
function onAlbumViewShown() {
    refreshAlbumCreators();
    if (albumActiveDest) refreshAlbumErrors();
}

function setAlbumMode(mode) {
    if (albumBusy) return;
    albumMode = mode;
    document.querySelectorAll('#albumMode .seg')
        .forEach(b => b.classList.toggle('active', b.dataset.amode === mode));
    document.getElementById('albumQuick').style.display = (mode === 'quick') ? '' : 'none';
    document.getElementById('albumCreator').style.display = (mode === 'creator') ? '' : 'none';
    hideDedupPrompt();
}

function setAlbumBusy(busy, label) {
    albumBusy = busy;
    ['albumDownloadBtn', 'albumDownloadSelBtn'].forEach(id => {
        const el = document.getElementById(id); if (el) el.disabled = busy;
    });
    document.getElementById('albumCancelBtn').style.display = busy ? '' : 'none';
    document.getElementById('albumStatus').textContent = label || '';
    const ta = document.getElementById('albumLinks'); if (ta) ta.disabled = busy;
    const retry = document.getElementById('albumRetryBtn'); if (retry) retry.disabled = busy;
}

// ── quick mode ──────────────────────────────────────────────────────
async function pickAlbumDest() {
    const path = await pywebview.api.select_folder();
    if (!path) return;
    albumDest = path;
    document.getElementById('albumDestPath').textContent = path;
    albumActiveDest = path;
    refreshAlbumErrors();
}

function downloadQuick() {
    if (albumBusy) return;
    if (!albumDest) { showToast('Pick a destination folder first', 'error'); return; }
    const entries = document.getElementById('albumLinks').value
        .split('\n').map(s => s.trim()).filter(Boolean);
    if (!entries.length) { showToast('Paste at least one album link', 'error'); return; }
    beginDownload(entries, { dest: albumDest, creatorId: null });
}

// ── creator mode ────────────────────────────────────────────────────
async function refreshAlbumCreators(selectId) {
    let list = [];
    try { list = await pywebview.api.list_album_creators(); } catch (e) { return; }
    const sel = document.getElementById('albumCreatorSelect');
    const keep = selectId || sel.value;
    sel.innerHTML = '<option value="">— select a creator —</option>' +
        list.map(c => `<option value="${c.id}">${escapeHtml(c.name)} (${c.link_count})</option>`).join('');
    if (keep && list.some(c => c.id === keep)) { sel.value = keep; onAlbumCreatorChange(); }
    else { albumCurrentCreator = null; renderAlbumCreator(); }
}

async function onAlbumCreatorChange() {
    const id = document.getElementById('albumCreatorSelect').value;
    albumCurrentCreator = id ? await pywebview.api.get_album_creator(id) : null;
    renderAlbumCreator();
    hideDedupPrompt();
    if (albumCurrentCreator) {
        albumActiveDest = albumCurrentCreator.root_dir || '';
        refreshAlbumErrors();
    }
}

function renderAlbumCreator() {
    const has = !!albumCurrentCreator;
    document.getElementById('albumCreatorDelete').style.display = has ? '' : 'none';
    document.getElementById('albumCreatorRootRow').style.display = has ? '' : 'none';
    document.getElementById('albumAddRow').style.display = has ? '' : 'none';
    const c = albumCurrentCreator;
    if (has) {
        document.getElementById('albumCreatorRoot').textContent = c.root_dir || '';
    }
    renderAlbumLinks();
}

function renderAlbumLinks() {
    const list = document.getElementById('albumLinkList');
    const actions = document.getElementById('albumCreatorActions');
    const links = (albumCurrentCreator && albumCurrentCreator.links) || [];
    if (!albumCurrentCreator) { list.innerHTML = ''; actions.style.display = 'none'; return; }
    if (!links.length) {
        list.innerHTML = '<div class="links-empty">No links yet — add a bunkr/cyberdrop/filester album above.</div>';
        actions.style.display = 'none';
        return;
    }
    list.innerHTML = links.map(l => {
        const meta = l.last_run
            ? `${l.title ? escapeHtml(l.title) + ' · ' : ''}${l.file_count || 0} files · ${escapeHtml(l.last_run)}`
            : 'not downloaded yet';
        const lock = l.password ? ' 🔒' : '';
        return `
        <label class="album-link-row">
            <input type="checkbox" class="album-link-cb" value="${escapeHtml(l.url)}" checked>
            <span class="badge badge-host">${escapeHtml(l.site)}${lock}</span>
            <span class="album-link-url" title="${escapeHtml(l.url)}">${escapeHtml(l.url)}</span>
            <span class="album-link-meta">${meta}</span>
            <button class="album-link-x" title="Remove" onclick="removeAlbumLink(event, '${escapeHtml(l.url)}')">×</button>
        </label>`;
    }).join('');
    actions.style.display = '';
}

async function newAlbumCreator() {
    const root = await pywebview.api.select_folder();
    if (!root) return;
    // Name comes from the chosen folder (backend defaults name -> folder basename).
    const res = await pywebview.api.save_album_creator({ name: '', root_dir: root });
    if (res && res.error) { showToast(res.error, 'error'); return; }
    refreshAlbumCreators(res.id);
    showToast('Album creator created', 'success');
}

async function deleteAlbumCreator() {
    if (!albumCurrentCreator) return;
    if (!confirm(`Remove album creator “${albumCurrentCreator.name}”? (Downloaded files are NOT deleted.)`)) return;
    await pywebview.api.delete_album_creator(albumCurrentCreator.id);
    albumCurrentCreator = null;
    refreshAlbumCreators();
}

function openAlbumCreatorFolder() {
    if (albumCurrentCreator) pywebview.api.open_folder(albumCurrentCreator.root_dir);
}

async function addAlbumLink() {
    if (!albumCurrentCreator) return;
    const raw = document.getElementById('albumNewLink').value.trim();
    if (!raw) return;
    // The dedicated password field wins; otherwise still accept inline "url | password".
    const [urlPart, pwInline] = raw.split('|');
    const pwField = document.getElementById('albumNewPassword').value.trim();
    const password = pwField || (pwInline || '').trim();
    const res = await pywebview.api.add_album_link(
        albumCurrentCreator.id, (urlPart || '').trim(), password);
    if (res && res.error) { showToast(res.error, 'error'); return; }
    document.getElementById('albumNewLink').value = '';
    document.getElementById('albumNewPassword').value = '';
    albumCurrentCreator = await pywebview.api.get_album_creator(albumCurrentCreator.id);
    renderAlbumLinks();
    refreshAlbumCreators(albumCurrentCreator.id);
}

async function removeAlbumLink(ev, url) {
    ev.preventDefault();
    ev.stopPropagation();
    if (!albumCurrentCreator) return;
    await pywebview.api.remove_album_link(albumCurrentCreator.id, url);
    albumCurrentCreator = await pywebview.api.get_album_creator(albumCurrentCreator.id);
    renderAlbumLinks();
    refreshAlbumCreators(albumCurrentCreator.id);
}

function downloadSelectedCreator() {
    if (albumBusy || !albumCurrentCreator) return;
    const checked = Array.from(document.querySelectorAll('#albumLinkList .album-link-cb:checked'))
        .map(cb => cb.value);
    if (!checked.length) { showToast('Select at least one link', 'error'); return; }
    // Re-attach stored passwords as inline "url | password" so locked folders unlock.
    const byUrl = {};
    (albumCurrentCreator.links || []).forEach(l => { byUrl[l.url] = l.password; });
    const entries = checked.map(u => byUrl[u] ? `${u} | ${byUrl[u]}` : u);
    beginDownload(entries, { dest: '', creatorId: albumCurrentCreator.id });
}

// ── dedup pre-check + prompt ────────────────────────────────────────
// entries may carry inline "url | password"; the bare url is what we dedup on.
function bareUrl(entry) { return String(entry).split('|')[0].trim(); }

async function beginDownload(entries, ctx) {
    const urls = entries.map(bareUrl);
    let known = [];
    try {
        const res = await pywebview.api.check_album_links(urls);
        known = ((res && res.results) || []).filter(r => r.known);
    } catch (e) { /* proceed as if nothing is known */ }

    if (!known.length) {
        doStart(entries, [], ctx);
        return;
    }
    albumPending = { entries, ctx };
    renderDedupPrompt(known, urls.length - known.length);
}

function renderDedupPrompt(known, freshCount) {
    const el = document.getElementById('albumDedupPrompt');
    const rows = known.map(k => {
        const where = k.root_dir ? ` → ${escapeHtml(k.root_dir)}` : '';
        const cnt = k.file_count ? ` · ${k.file_count} files` : '';
        const when = k.last_run ? ` · ${escapeHtml(k.last_run)}` : '';
        const name = escapeHtml(k.title || k.url);
        return `
        <div class="album-dedup-row" data-url="${escapeHtml(k.url)}" data-choice="scan">
            <div class="album-dedup-name">${name}<span class="album-dedup-where">already downloaded${where}${cnt}${when}</span></div>
            <div class="segmented seg-sub album-dedup-choice">
                <button class="seg active" data-choice="scan" onclick="pickDedup(this)">Scan for new</button>
                <button class="seg" data-choice="redownload" onclick="pickDedup(this)">Redownload whole</button>
                <button class="seg" data-choice="skip" onclick="pickDedup(this)">Skip</button>
            </div>
        </div>`;
    }).join('');
    el.innerHTML = `
        <div class="album-dedup-head">${known.length} album(s) already downloaded${freshCount ? ` · ${freshCount} new` : ''}. Choose what to do:</div>
        ${rows}
        <div class="album-dedup-actions">
            <button class="btn btn-primary btn-sm" onclick="confirmDedup()">Continue</button>
            <button class="btn btn-ghost btn-sm" onclick="hideDedupPrompt()">Cancel</button>
        </div>`;
    el.style.display = '';
}

function pickDedup(btn) {
    const group = btn.parentElement;
    group.querySelectorAll('.seg').forEach(b => b.classList.toggle('active', b === btn));
    btn.closest('.album-dedup-row').dataset.choice = btn.dataset.choice;
}

function hideDedupPrompt() {
    const el = document.getElementById('albumDedupPrompt');
    el.style.display = 'none';
    el.innerHTML = '';
    albumPending = null;
}

function confirmDedup() {
    if (!albumPending) return;
    const choices = {};
    document.querySelectorAll('#albumDedupPrompt .album-dedup-row')
        .forEach(r => { choices[r.dataset.url] = r.dataset.choice; });
    const { entries, ctx } = albumPending;
    const finalEntries = [];
    const forceUrls = [];
    entries.forEach(e => {
        const u = bareUrl(e);
        const choice = choices[u] || 'new';   // fresh links have no choice -> include
        if (choice === 'skip') return;
        finalEntries.push(e);
        if (choice === 'redownload') forceUrls.push(u);
    });
    hideDedupPrompt();
    if (!finalEntries.length) { showToast('Nothing to download', 'info'); return; }
    doStart(finalEntries, forceUrls, ctx);
}

// ── start / progress ────────────────────────────────────────────────
async function doStart(entries, forceUrls, ctx) {
    albumActiveDest = ctx.creatorId
        ? (albumCurrentCreator ? albumCurrentCreator.root_dir : '')
        : ctx.dest;
    document.getElementById('albumLog').innerHTML = '';
    document.getElementById('albumStatsSection').style.display = '';
    resetAlbumStats();
    setAlbumBusy(true, 'Downloading…');
    albumLog(`Starting ${entries.length} album(s)` + (forceUrls.length ? ` (${forceUrls.length} full redownload)` : ''), 'info');
    const res = await pywebview.api.start_album_download(
        ctx.dest || '', entries, false, ctx.creatorId || null, forceUrls);
    if (res && res.error) { setAlbumBusy(false, ''); showToast(res.error, 'error'); albumLog('Error: ' + res.error, 'error'); }
}

function cancelAlbum() {
    pywebview.api.cancel_album_download();
    document.getElementById('albumStatus').textContent = 'Cancelling…';
}

function retryAlbumFailed() {
    if (albumBusy) return;
    if (!albumLastFailedUrls.length) { showToast('No failed files to retry', 'info'); return; }
    // Retry runs against the same destination; failures re-attempt via the engines.
    doStart(albumLastFailedUrls.slice(), [], { dest: albumActiveDest, creatorId: null });
}

function albumLog(msg, type) {
    const c = document.getElementById('albumLog');
    if (!c) return;
    const atBottom = c.scrollHeight - c.scrollTop - c.clientHeight < 40;
    const d = document.createElement('div');
    d.className = 'log-entry log-' + (type || 'info');
    d.textContent = msg;
    c.appendChild(d);
    while (c.childNodes.length > 1000) c.removeChild(c.firstChild);
    if (atBottom) c.scrollTop = c.scrollHeight;
}

function resetAlbumStats() {
    document.getElementById('albumStatDownloaded').textContent = '0';
    document.getElementById('albumStatSkipped').textContent = '0';
    document.getElementById('albumStatErrors').textContent = '0';
}

window.onAlbumProgress = function (data) {
    const t = data.type, msg = data.message || '';
    if (t === 'download') { albumLog('✓ ' + msg, 'download'); incrementStat('albumStatDownloaded'); }
    else if (t === 'skip') { albumLog('• ' + msg, 'skip'); incrementStat('albumStatSkipped'); }
    else if (t === 'error') { albumLog('✗ ' + msg, 'error'); }
    else { albumLog(msg, 'info'); }
    if (msg) document.getElementById('albumStatus').textContent = msg.slice(0, 90);
};

window.onAlbumError = function (data) {
    albumLog('✗ ' + (data.message || 'error'), 'error');
    incrementStat('albumStatErrors');
};

window.onAlbumComplete = function (data) {
    setAlbumBusy(false, '');
    document.getElementById('albumStatDownloaded').textContent = data.downloaded || 0;
    document.getElementById('albumStatSkipped').textContent = data.skipped || 0;
    document.getElementById('albumStatErrors').textContent = data.errors || 0;
    let summary = `Done — ${data.downloaded || 0} downloaded, ${data.skipped || 0} skipped, ${data.errors || 0} error(s)`;
    if (data.unsupported && data.unsupported.length) summary += `, ${data.unsupported.length} unsupported link(s)`;
    if (data.cancelled) summary = 'Cancelled — ' + summary;
    albumLog(summary, (data.errors || data.cancelled) ? 'info' : 'success');
    document.getElementById('albumStatus').textContent = summary;
    showToast(summary, (data.errors || data.cancelled) ? 'info' : 'success');
    refreshAlbumErrors();
    // In creator mode, reload to show updated per-link counts/dates.
    if (albumMode === 'creator' && albumCurrentCreator) {
        pywebview.api.get_album_creator(albumCurrentCreator.id).then(c => {
            albumCurrentCreator = c; renderAlbumLinks();
        });
    }
};

// ── failures panel + retry ──────────────────────────────────────────
async function refreshAlbumErrors() {
    if (!albumActiveDest) return;
    let res;
    try { res = await pywebview.api.list_album_errors(albumActiveDest); } catch (e) { return; }
    const fails = (res && res.failures) || [];
    albumLastFailedUrls = fails.map(f => f.url).filter(Boolean);
    const sec = document.getElementById('albumErrorsSection');
    const panel = document.getElementById('albumErrors');
    if (!fails.length) { sec.style.display = 'none'; panel.innerHTML = ''; return; }
    sec.style.display = '';
    panel.innerHTML = fails.map(f => `
        <div class="album-error-row">
            <input type="checkbox" class="album-error-cb" title="Tick off — hide this failure"
                   onchange="dismissAlbumError('${encodeURIComponent(f.entry || '')}')">
            <a class="album-error-name" href="#" title="${escapeHtml(f.url || '')} — open the file link"
               onclick="openLink('${encodeURIComponent(f.url || '')}');return false;">${escapeHtml(f.filename || f.url || 'file')}</a>
            <span class="album-error-reason">${escapeHtml(f.reason || '')}${f.attempts ? ` · ${f.attempts} attempt(s)` : ''}</span>
        </div>`).join('');
}

async function dismissAlbumError(encEntry) {
    const entry = decodeURIComponent(encEntry || '');
    if (!entry || !albumActiveDest) return;
    await pywebview.api.dismiss_album_error(albumActiveDest, entry);
    refreshAlbumErrors();
}

function copyAlbumLog() {
    const c = document.getElementById('albumLog');
    const text = c ? c.innerText : '';
    if (!text.trim()) { showToast('Log is empty', 'info'); return; }
    const done = () => showToast('Log copied to clipboard', 'success');
    // execCommand is the reliable path inside the webview; clipboard API is a bonus.
    const fallback = () => {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        let ok = false;
        try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        document.body.removeChild(ta);
        ok ? done() : showToast('Copy failed — select the text and Ctrl+C', 'error');
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, fallback);
    } else {
        fallback();
    }
}
