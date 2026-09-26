/* ── Tracked links (Creator tab) ─────────────────────────────────────
   Extra download sources a creator posts elsewhere — Terabox, gofile, bunkr…,
   and MEGA/Drive (stored, not yet downloadable). Kept apart from the creator's
   crawled links: Fetch Latest never touches them; each is fetched on its own. */

let xlBusy = false;
let xlCollapsed = false;

function renderExtraLinks() {
    const panel = document.getElementById('extraLinks');
    if (!panel) return;
    const c = typeof currentCreator !== 'undefined' ? currentCreator : null;
    if (!c) { panel.style.display = 'none'; panel.innerHTML = ''; return; }
    const links = c.extra_links || [];
    panel.style.display = '';
    const rows = links.map((l, i) => xlRow(l, i)).join('');
    panel.innerHTML = `
        <div class="pending-head" onclick="xlToggle()" style="cursor: pointer;">
            <span class="pmv-caret">${xlCollapsed ? '▸' : '▾'}</span>
            <span>Tracked links</span>
            <span class="badge badge-skipped">${links.length}</span>
            <span class="field-hint" style="margin-left: 8px;">Terabox / gofile / bunkr… fetched one at a time — never by Fetch Latest</span>
            <span style="margin-left: auto; display: flex; gap: 6px;" onclick="event.stopPropagation()">
                ${links.length ? `<button class="btn-tiny" ${xlBusy ? 'disabled' : ''} onclick="xlFetchSelected()">Fetch selected</button>` : ''}
                ${xlBusy ? '<button class="btn-tiny" onclick="xlCancel()">Cancel</button>' : ''}
            </span>
        </div>
        ${xlCollapsed ? '' : rows + `
        <div class="xl-add">
            <textarea id="xlNewLinks" class="input-field" rows="1"
                      placeholder="Paste link(s), one per line — optional ' | password' for a locked share"></textarea>
            <input id="xlNewFolder" class="input-field xl-folder" placeholder="Folder (default: site name)"
                   title="Where these links download to: a folder inside ${escapeHtml(c.destination || 'the creator folder')}, e.g. Images\\terabox — or an absolute path">
            <label class="checkbox-option" title="Record what each link downloaded so a file you delete later is never fetched again">
                <input type="checkbox" id="xlNewTrack" checked><span class="checkbox-label">Track</span>
            </label>
            <button class="btn btn-secondary btn-sm" onclick="xlAdd()">Add</button>
        </div>`}`;
}

function xlRow(l, i) {
    const enc = encodeURIComponent(l.url);
    const last = l.last_run
        ? `${(l.last_result || {}).downloaded || 0} new · ${(l.last_result || {}).skipped || 0} had`
          + (((l.last_result || {}).errors) ? ` · ${(l.last_result || {}).errors} failed` : '')
          + ` — ${new Date(l.last_run).toLocaleDateString()}`
        : 'never fetched';
    const note = !l.downloadable ? `${escapeHtml(l.site_label)} links can't be downloaded by the app yet — open in browser`
               : (l.dest_problem ? escapeHtml(l.dest_problem) : `Downloads to ${escapeHtml(l.dest)}`);
    return `<div class="xl-row${l.downloadable ? '' : ' xl-off'}" title="${note}">
        <input type="checkbox" class="pending-check xl-check" data-url="${enc}" ${l.downloadable ? '' : 'disabled'}>
        <span class="link-badge">${escapeHtml(l.site_label)}</span>
        ${l.has_password ? '<span class="badge badge-skipped" title="Has a share password">🔒</span>' : ''}
        <a class="xl-url" href="#" onclick="openLink('${enc}'); return false;">${escapeHtml(l.url)}</a>
        <input class="input-field xl-folder" value="${escapeHtml(l.folder || '')}" ${l.downloadable ? '' : 'disabled'}
               title="Folder inside the creator folder (or an absolute path)" onchange="xlUpdate('${enc}', {folder: this.value})">
        <label class="checkbox-option" title="Remember downloaded files so deleted ones never come back">
            <input type="checkbox" ${l.track ? 'checked' : ''} ${l.downloadable ? '' : 'disabled'}
                   onchange="xlUpdate('${enc}', {track: this.checked})"><span class="checkbox-label">Track</span>
        </label>
        <span class="xl-last">${last}</span>
        <button class="btn-tiny" ${(!l.downloadable || l.dest_problem || xlBusy) ? 'disabled' : ''} onclick="xlFetch(['${enc}'])"
                title="Download what's new from this link only">Fetch</button>
        <button class="btn-tiny" onclick="openLink('${enc}')" title="Open in browser">Open ↗</button>
        <button class="btn-tiny" onclick="xlRemove('${enc}')" title="Stop tracking this link (files stay)">✕</button>
    </div>`;
}

function xlToggle() { xlCollapsed = !xlCollapsed; renderExtraLinks(); }

function xlApply(res) {
    if (!res || res.error) { showToast((res && res.error) || 'Could not save', 'error'); return false; }
    if (currentCreator) currentCreator.extra_links = res.extra_links || [];
    renderExtraLinks();
    return true;
}

async function xlAdd() {
    if (!currentCreatorId) return;
    const text = document.getElementById('xlNewLinks').value;
    const folder = document.getElementById('xlNewFolder').value;
    const track = document.getElementById('xlNewTrack').checked;
    let res;
    try { res = await pywebview.api.add_extra_links(currentCreatorId, text, folder, track); }
    catch (e) { res = { error: String(e) }; }
    if (xlApply(res)) showToast(`${res.added} link${res.added === 1 ? '' : 's'} added`, 'success');
}

async function xlUpdate(enc, patch) {
    let res;
    try { res = await pywebview.api.update_extra_link(currentCreatorId, decodeURIComponent(enc), patch); }
    catch (e) { res = { error: String(e) }; }
    xlApply(res);
}

async function xlRemove(enc) {
    const url = decodeURIComponent(enc);
    if (!confirm(`Stop tracking this link?\n\n${url}\n\nDownloaded files stay where they are.`)) return;
    let res;
    try { res = await pywebview.api.remove_extra_link(currentCreatorId, url); }
    catch (e) { res = { error: String(e) }; }
    xlApply(res);
}

function xlFetchSelected() {
    const encs = [...document.querySelectorAll('#extraLinks .xl-check:checked')].map(b => b.dataset.url);
    if (!encs.length) { showToast('Tick one or more links first', 'info'); return; }
    xlFetch(encs);
}

async function xlFetch(encs) {
    if (xlBusy || !currentCreatorId) return;
    let res;
    try { res = await pywebview.api.fetch_extra_links(currentCreatorId, encs.map(decodeURIComponent)); }
    catch (e) { res = { error: String(e) }; }
    if (!res || res.error) { showToast((res && res.error) || 'Could not start', 'error'); return; }
    (res.skipped || []).forEach(m => Logger.info('[Links] skipped: ' + m));
    xlBusy = true;
    renderExtraLinks();
    Logger.info(`[Links] Fetching ${res.links} tracked link${res.links === 1 ? '' : 's'}…`);
}

function xlCancel() {
    pywebview.api.cancel_album_download();
    Logger.info('[Links] Cancelling…');
}

window.onExtraLinkProgress = function (d) {
    if (!d || !d.message) return;
    if (d.type === 'error') Logger.error('[Links] ' + d.message);
    else if (d.type === 'download' || d.type === 'info') Logger.info('[Links] ' + d.message);
};

window.onExtraLinkComplete = async function (r) {
    xlBusy = false;
    r = r || {};
    const msg = `Tracked links: ${r.downloaded || 0} downloaded, ${r.skipped || 0} already had`
              + (r.errors ? `, ${r.errors} failed` : '') + (r.cancelled ? ' (cancelled)' : '');
    Logger.info('[Links] ' + msg);
    showToast(msg, r.errors || r.cancelled ? 'info' : 'success');
    if (r.needs_terabox_auth) {
        showToast('Terabox needs you to sign in — opening Settings…', 'error');
        if (typeof openSettings === 'function') openSettings();
    }
    if (currentCreatorId && r.creator_id === currentCreatorId) {
        try { currentCreator = await pywebview.api.get_creator(currentCreatorId); } catch (e) { /* ignore */ }
    }
    renderExtraLinks();
};
