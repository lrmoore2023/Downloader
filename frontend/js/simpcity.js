/* ── SimpCity links + tracked albums panel (Creator view, Real creators) ──
   Same shape and habits as "Links needing attention" (creators.js):
     • a checkbox checks a link off (it's handled — hidden, never comes back on a
       re-read of the thread); a post's checkbox checks off all its links;
       "Resolve all" checks off everything listed; Ctrl+Z undoes, newest first;
     • "Download all" fetches every downloadable link in one run (a post has its
       own "Download all"); a downloaded link drops off the list by itself;
     • tracked albums sit underneath with their live / gone / unreachable state.
   Thread media downloads automatically — only links show up here.
*/

let simpcityCollapsed = false;
let simpcityShowAll = false;
let simpcityLastSig = null;
let simpcityPollTimer = null;
let simpcityData = null;
let simpcityChecking = false;
let scUndo = [];                // [{creatorId, items:[{thread_id,url}], seq}]

function simpcityRelevant() {
    return !!(currentCreator && currentCreator.links.some(
        l => l.platform === 'simpcity' || l.platform === 'album'));
}

function scDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return String(iso).slice(0, 10);
    return `${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, '0')}.${String(d.getDate()).padStart(2, '0')}`;
}

function scAliveBadge(alive, sinceDead, sinceUnreach, streak) {
    if (alive === 'dead') {
        return `<span class="badge badge-failed" title="The host says this no longer exists — files already downloaded are kept">gone${sinceDead ? ' since ' + scDate(sinceDead) : ''}</span>`;
    }
    if (sinceUnreach && streak > 1) {
        return `<span class="badge badge-gone" title="Couldn't reach the host on the last ${streak} checks — it may be down, or the link may be gone">unreachable since ${scDate(sinceUnreach)}</span>`;
    }
    return '';
}

function scKey(r) { return encodeURIComponent(JSON.stringify({ thread_id: r.thread_id, url: r.url })); }
function scItems(enc) { try { return JSON.parse(decodeURIComponent(enc)); } catch (e) { return []; } }

async function renderSimpcityPanel(fromPoll = false) {
    const panel = document.getElementById('simpcityPanel');
    if (!panel) return;
    const id = currentCreatorId;
    if (!id || !simpcityRelevant()) {
        panel.style.display = 'none'; panel.innerHTML = ''; simpcityData = null; return;
    }
    let res;
    try { res = await pywebview.api.list_simpcity(id); } catch (e) { return; }
    if (currentCreatorId !== id || !res) return;
    const sig = id + '::' + simpcityShowAll + '::' + JSON.stringify([res.threads,
        res.review.map(r => [r.url, r.status, r.alive]),
        res.albums.map(a => [a.url, a.file_count, a.alive, a.last_run]), res.busy]);
    if (fromPoll && sig === simpcityLastSig) return;
    simpcityLastSig = sig;
    simpcityData = res;

    const busy = !!res.busy || isBusy;
    const pending = res.review.filter(r => r.status === 'pending');
    const handled = res.review.filter(r => r.status !== 'pending');
    const shown = simpcityShowAll ? res.review : pending;
    const dlAll = pending.filter(r => r.can_download);
    const dis = busy ? 'disabled title="Waits for the current download to finish"' : '';

    const threadsHtml = res.threads.map(t => {
        const name = escapeHtml(t.title || t.name || t.url);
        const link = `<a class="pending-posttitle" href="#" onclick="openLink('${encodeURIComponent(t.url)}');return false;" title="Open the thread">${name}</a>`;
        if (!t.walked) {
            return `<div class="sc-thread"><span class="badge badge-simpcity">SimpCity</span>${link}
                <span class="sc-meta">not read yet — run Download Everything or Fetch Latest</span></div>`;
        }
        const m = t.media || {};
        const got = m.downloaded || 0, failed = m.failed || 0, gone = m.gone || 0, wait = m.new || 0;
        return `<div class="sc-thread"><span class="badge badge-simpcity">SimpCity</span>${link}
            <span class="sc-meta">${t.pages_total || '?'} pages · ${t.posts} posts · ${got} file${got === 1 ? '' : 's'} downloaded`
            + (wait ? ` · ${wait} waiting` : '')
            + (failed ? ` · <span class="sc-bad">${failed} failed</span>` : '')
            + (gone ? ` · <span class="sc-warn">${gone} gone</span>` : '')
            + ` · read ${scDate(t.last_walk)}</span></div>`;
    }).join('');

    // Group by post, newest post first (the API already sorts by post date).
    const groups = [];
    const byPost = {};
    shown.forEach(r => {
        const k = r.thread_id + ':' + r.post_id;
        if (!byPost[k]) { byPost[k] = { post_url: r.post_url, date: r.post_date, rows: [] }; groups.push(byPost[k]); }
        byPost[k].rows.push(r);
    });
    const groupsHtml = groups.map(g => {
        const open = g.rows.filter(r => r.status === 'pending');
        const dl = open.filter(r => r.can_download);
        const checkAll = open.length > 1
            ? `<input type="checkbox" class="pending-check pending-check-all"
                 title="Check off all ${open.length} links in this post (Ctrl+Z to undo)"
                 onchange="scCheckOff(this, '${encodeURIComponent(JSON.stringify(open.map(r => ({ thread_id: r.thread_id, url: r.url }))))}')">`
            : '';
        const dlBtn = dl.length > 1
            ? `<button class="btn-tiny" ${dis} onclick="scDownload('${encodeURIComponent(JSON.stringify(dl.map(r => ({ thread_id: r.thread_id, url: r.url }))))}')"
                 title="Download these ${dl.length} links one after another, in one run">Download all (${dl.length})</button>`
            : '';
        return `<div class="pending-group">
            <div class="pending-post">${checkAll}
              <a class="pending-posttitle" href="#" onclick="openLink('${encodeURIComponent(g.post_url)}');return false;"
                 title="Open this post on SimpCity">Post</a>
              <span class="pending-date">${escapeHtml(scDate(g.date))}</span>
              ${dlBtn}
            </div>
            <div class="pending-rows">${g.rows.map(r => scRow(r, dis)).join('')}</div>
        </div>`;
    }).join('');

    const albumsHtml = res.albums.map(a => `<div class="pending-row">
            <span class="badge badge-album">${escapeHtml(a.label)}</span>
            ${scAliveBadge(a.alive, a.dead_since, a.unreachable_since, a.fail_streak || 0)}
            <a class="pending-url" href="#" onclick="openLink('${encodeURIComponent(a.url)}');return false;"
               title="${escapeHtml(a.url)}">${escapeHtml(a.title || a.url)}</a>
            <span class="pending-missing">${a.last_run ? `${a.file_count || 0} file${a.file_count === 1 ? '' : 's'} · checked ${scDate(a.last_run)}` : 'not downloaded yet'}</span>
            <button class="btn-tiny" onclick="scUntrack('${encodeURIComponent(a.url)}')"
                    title="Stop checking this album for new files. Its files and history are kept.">Untrack</button>
        </div>`).join('');

    const caret = simpcityCollapsed ? '▸' : '▾';
    const emptyMsg = res.review.length ? 'Nothing left to review.' : 'No links found in the thread yet.';
    panel.style.display = '';
    panel.innerHTML = `<div class="pending-head" onclick="toggleSimpcity()">
          <span class="pending-caret">${caret}</span>
          <span>SimpCity links</span>
          <span class="pending-count">${pending.length}</span>
          <span class="field-hint pending-hint">thread media downloads automatically — check off links when handled (Ctrl+Z to undo)</span>
          ${dlAll.length ? `<button class="btn-tiny" ${dis} onclick="event.stopPropagation(); scDownload('${encodeURIComponent(JSON.stringify(dlAll.map(r => ({ thread_id: r.thread_id, url: r.url }))))}')"
                  title="Download every downloadable link listed, one after another, in one run">Download all (${dlAll.length})</button>` : ''}
          <button class="btn-tiny" ${simpcityChecking ? 'disabled' : ''} onclick="event.stopPropagation(); scCheckLinks(this)"
                  title="Check whether the links still exist (one quick request each)">${simpcityChecking ? 'Checking…' : 'Check links'}</button>
          ${pending.length ? `<button class="btn-tiny pending-clear-all" onclick="event.stopPropagation(); scResolveAll()"
                  title="Check off every link listed (Ctrl+Z to undo)">Resolve all</button>` : ''}
        </div>
        <div class="pending-list" ${simpcityCollapsed ? 'style="display:none"' : ''}>
          ${threadsHtml}
          ${groupsHtml || `<div class="field-hint sc-empty">${emptyMsg}</div>`}
          ${handled.length ? `<label class="checkbox-option sc-toggle"><input type="checkbox" ${simpcityShowAll ? 'checked' : ''}
                onchange="simpcityShowAll=this.checked; simpcityLastSig=null; renderSimpcityPanel()">
                Show handled links (${handled.length})</label>` : ''}
          ${res.albums.length ? `<div class="pending-group">
              <div class="pending-post"><span class="pending-posttitle sc-albums-title">Tracked albums</span>
                <span class="pending-date">re-checked for new files on every run</span></div>
              <div class="pending-rows">${albumsHtml}</div></div>` : ''}
        </div>`;
}

function scRow(r, dis) {
    const kind = r.kind === 'album' ? '<span class="badge badge-album">album</span>'
        : r.kind === 'embed' ? '<span class="badge badge-gone" title="Embedded media from a site the app can\'t download — grab it by hand">embed</span>'
        : '';
    const state = r.status === 'tracked' ? '<span class="badge badge-sc-live">tracked</span>'
        : r.status === 'downloaded' ? '<span class="badge badge-sc-live">downloaded</span>'
        : r.status === 'dismissed' ? '<span class="badge badge-skipped">checked off</span>' : '';
    const enc = encodeURIComponent(r.url);
    const alt = (typeof isHighlightHost === 'function' && isHighlightHost(r.host)) ? ' host-alt' : '';
    const one = scKey(r);
    const lead = r.status === 'pending'
        ? `<input type="checkbox" class="pending-check" title="Check off — hides this link; it won't come back when the thread is re-read"
                  onchange="scCheckOff(this, '${encodeURIComponent(JSON.stringify([{ thread_id: r.thread_id, url: r.url }]))}')">`
        : r.status === 'dismissed'
            ? `<button class="btn-tiny" onclick="scRestore('${one}')" title="Put it back on the list">Restore</button>`
            : '<span class="sc-check-spacer"></span>';
    const actions = [];
    if (r.status === 'pending' && r.can_download) {
        actions.push(`<button class="btn-tiny" ${dis} onclick="scDownload('${encodeURIComponent(JSON.stringify([{ thread_id: r.thread_id, url: r.url }]))}')"
            title="Download once into Unsorted\\<year> (images: Unsorted\\Images\\<year>)">Download</button>`);
    }
    if ((r.status === 'pending' || r.status === 'downloaded') && r.can_track) {
        actions.push(`<button class="btn-tiny" ${dis} onclick="scTrack('${r.thread_id}','${enc}')"
            title="Download now and re-check this album for new files on every run">Download &amp; track</button>`);
    }
    return `<div class="pending-row">
        ${lead}
        <span class="badge badge-host${alt}">${escapeHtml(r.host)}</span>${kind}${state}
        ${scAliveBadge(r.alive, r.dead_since, null, 0)}
        <a class="pending-url${alt}" href="#" onclick="openLink('${enc}');return false;" title="${escapeHtml(r.url)}">${escapeHtml(r.url)}</a>
        ${actions.join('')}
        <button class="btn-tiny" onclick="copyText('${enc}')">Copy</button>
    </div>`;
}

function toggleSimpcity() {
    simpcityCollapsed = !simpcityCollapsed;
    simpcityLastSig = null;
    renderSimpcityPanel();
}

// Checkbox (one link, or a post's check-all): check off, undoable.
async function scCheckOff(cb, encItems) {
    const items = scItems(encItems);
    const id = currentCreatorId;
    cb.disabled = true;
    let res;
    try { res = await pywebview.api.set_simpcity_links_status(id, items, 'dismissed'); }
    catch (e) { cb.disabled = false; cb.checked = false; return; }
    if (res && res.error) { showToast(res.error, 'error'); cb.disabled = false; cb.checked = false; return; }
    scUndo.push({ creatorId: id, items, seq: ++undoSeq });
    if (items.length > 1) showToast(`Checked off ${items.length} links — Ctrl+Z to undo`, 'info');
    simpcityLastSig = null;
    renderSimpcityPanel();
}

async function scResolveAll() {
    const id = currentCreatorId;
    const items = ((simpcityData && simpcityData.review) || []).filter(r => r.status === 'pending')
        .map(r => ({ thread_id: r.thread_id, url: r.url }));
    if (!items.length) return;
    const res = await pywebview.api.set_simpcity_links_status(id, items, 'dismissed');
    if (res && res.error) { showToast(res.error, 'error'); return; }
    scUndo.push({ creatorId: id, items, seq: ++undoSeq });
    showToast(`Checked off ${items.length} link${items.length === 1 ? '' : 's'} — Ctrl+Z to undo`, 'info');
    simpcityLastSig = null;
    renderSimpcityPanel();
}

async function scRestore(encOne) {
    const item = scItems(encOne);
    const res = await pywebview.api.set_simpcity_links_status(currentCreatorId, [item], 'pending');
    if (res && res.error) { showToast(res.error, 'error'); return; }
    simpcityLastSig = null;
    renderSimpcityPanel();
}

// Ctrl+Z (wired in creators.js): restore the most recent check-off.
function undoSimpcityCheckOff() {
    while (scUndo.length && scUndo[scUndo.length - 1].creatorId !== currentCreatorId) scUndo.pop();
    if (!scUndo.length) return false;
    const action = scUndo.pop();
    pywebview.api.set_simpcity_links_status(action.creatorId, action.items, 'pending').then(r => {
        if (r && r.error) { showToast(r.error, 'error'); return; }
        simpcityLastSig = null;
        renderSimpcityPanel();
        showToast(action.items.length > 1 ? `Restored ${action.items.length} links` : 'Restored link', 'info');
    });
    return true;
}

async function scDownload(encItems) {
    const items = scItems(encItems);
    const res = await pywebview.api.simpcity_download_links(currentCreatorId, items);
    if (res && res.error) { showToast(res.error, 'error'); return; }
    Logger.info(`Downloading ${res.count} link${res.count === 1 ? '' : 's'} from the review list…`);
    setDownloadingState(true);
    simpcityLastSig = null;
    renderSimpcityPanel();
}

async function scTrack(threadId, encUrl) {
    const res = await pywebview.api.simpcity_link_action(currentCreatorId, threadId,
        decodeURIComponent(encUrl), 'track');
    if (res && res.error) { showToast(res.error, 'error'); return; }
    Logger.info(`Downloading and tracking ${res.url}…`);
    const c = await pywebview.api.get_creator(currentCreatorId);
    if (c) { currentCreator = c; renderChips(); buildScopeControl(); }
    setDownloadingState(true);
    simpcityLastSig = null;
    renderSimpcityPanel();
}

async function scUntrack(encUrl) {
    const url = decodeURIComponent(encUrl);
    if (!confirm('Stop tracking this album?\n\nIts downloaded files and history are kept — adding it again later picks up where it left off.')) return;
    const res = await pywebview.api.untrack_album(currentCreatorId, url);
    if (res && res.error) { showToast(res.error, 'error'); return; }
    const c = await pywebview.api.get_creator(currentCreatorId);
    if (c) { currentCreator = c; renderChips(); buildScopeControl(); }
    simpcityLastSig = null;
    renderSimpcityPanel();
}

async function scCheckLinks(btn) {
    if (simpcityChecking) return;
    simpcityChecking = true;
    UI.busy(btn, true);
    try {
        const res = await pywebview.api.check_simpcity_links(currentCreatorId, null, 25);
        if (res && res.error) showToast(res.error, 'error');
        else showToast(`Checked ${res.checked} link${res.checked === 1 ? '' : 's'}`
            + (res.dead ? ` — ${res.dead} gone` : ''), res.dead ? 'info' : 'success');
    } finally {
        simpcityChecking = false;
        simpcityLastSig = null;
        renderSimpcityPanel();
    }
}

// Poll while a download runs, so thread media counts / new links show up live.
function simpcitySetPolling(on) {
    if (on && !simpcityPollTimer) {
        simpcityPollTimer = setInterval(() => renderSimpcityPanel(true), 5000);
    } else if (!on && simpcityPollTimer) {
        clearInterval(simpcityPollTimer);
        simpcityPollTimer = null;
    }
}
