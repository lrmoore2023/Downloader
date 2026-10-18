/* ── SimpCity threads + tracked albums panel (Creator view, Real creators) ──
   Three parts, all fed by pywebview.api.list_simpcity(creatorId):
     • each tracked thread's walk summary (pages, posts, media fetched/failed);
     • "Links to review" — every non-embedded link found in the thread's posts,
       newest post first, with Download / Download & track / Dismiss;
     • "Tracked albums" — albums re-checked for new files on every run, flagged
       when they disappear.
*/

let simpcityCollapsed = false;
let simpcityShowAll = false;
let simpcityLastSig = null;
let simpcityPollTimer = null;
let simpcityData = null;
let simpcityChecking = false;

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
    if (alive === 'live') return '<span class="badge badge-sc-live" title="Reachable at the last check">live</span>';
    if (sinceUnreach && streak > 1) {
        return `<span class="badge badge-gone" title="Couldn't reach the host on the last ${streak} checks — it may be down, or the album may be gone">unreachable since ${scDate(sinceUnreach)}</span>`;
    }
    return '';
}

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
    const sig = id + '::' + JSON.stringify([res.threads, res.review.map(r => [r.url, r.status, r.alive]),
        res.albums.map(a => [a.url, a.file_count, a.alive]), res.busy]);
    if (fromPoll && sig === simpcityLastSig) return;
    simpcityLastSig = sig;
    simpcityData = res;

    const pending = res.review.filter(r => r.status === 'pending');
    const shown = simpcityShowAll ? res.review : pending;
    const caret = simpcityCollapsed ? '▸' : '▾';
    const busy = !!res.busy || isBusy;

    const threadsHtml = res.threads.map(t => {
        if (!t.walked) {
            return `<div class="sc-thread"><span class="badge badge-simpcity">SimpCity</span>
                <a class="pending-posttitle" href="#" onclick="openLink('${encodeURIComponent(t.url)}');return false;">${escapeHtml(t.title || t.name || t.url)}</a>
                <span class="field-hint">not read yet — run Download Everything or Fetch Latest</span></div>`;
        }
        const m = t.media || {};
        const got = m.downloaded || 0, failed = m.failed || 0, gone = m.gone || 0;
        const wait = (m.new || 0);
        return `<div class="sc-thread"><span class="badge badge-simpcity">SimpCity</span>
            <a class="pending-posttitle" href="#" onclick="openLink('${encodeURIComponent(t.url)}');return false;"
               title="Open the thread">${escapeHtml(t.title || t.name)}</a>
            <span class="sc-meta">${t.pages_total || '?'} pages · ${t.posts} posts · ${got} file${got === 1 ? '' : 's'} downloaded`
            + (wait ? ` · ${wait} waiting` : '')
            + (failed ? ` · <span class="sc-bad">${failed} failed</span>` : '')
            + (gone ? ` · <span class="sc-warn">${gone} gone</span>` : '')
            + ` · read ${scDate(t.last_walk)}</span></div>`;
    }).join('');

    // Group review rows by post (newest first).
    const groups = [];
    const byPost = {};
    shown.forEach(r => {
        const k = r.thread_id + ':' + r.post_id;
        if (!byPost[k]) { byPost[k] = { post_url: r.post_url, date: r.post_date, rows: [] }; groups.push(byPost[k]); }
        byPost[k].rows.push(r);
    });
    const reviewHtml = groups.length ? groups.map(g => `<div class="pending-group">
            <div class="pending-post">
              <a class="pending-posttitle" href="#" onclick="openLink('${encodeURIComponent(g.post_url)}');return false;"
                 title="Open this post on SimpCity">Post · ${escapeHtml(scDate(g.date))}</a>
            </div>
            <div class="pending-rows">${g.rows.map(r => scReviewRow(r, busy)).join('')}</div>
        </div>`).join('')
        : `<div class="field-hint sc-empty">${res.review.length ? 'Nothing left to review.' : 'No external links found yet.'}</div>`;

    const albumsHtml = res.albums.length ? res.albums.map(a => `<div class="pending-row sc-album">
            <span class="badge badge-album">${escapeHtml(a.label)}</span>
            <a class="pending-url" href="#" onclick="openLink('${encodeURIComponent(a.url)}');return false;"
               title="${escapeHtml(a.url)}">${escapeHtml(a.title || a.album_id)}</a>
            ${scAliveBadge(a.alive, a.dead_since, a.unreachable_since, a.fail_streak || 0)}
            <span class="sc-meta">${a.last_run ? `${a.file_count || 0} file${a.file_count === 1 ? '' : 's'} · checked ${scDate(a.last_run)}` : 'not downloaded yet'}</span>
            <button class="btn-tiny" onclick="scUntrack('${encodeURIComponent(a.url)}')"
                    title="Stop checking this album for new files. Downloaded files and its record are kept.">Untrack</button>
        </div>`).join('')
        : '<div class="field-hint sc-empty">No tracked albums — use “Download &amp; track” on a link, or paste an album URL in Configure Links.</div>';

    const dismissedCount = res.review.length - pending.length;
    panel.style.display = '';
    panel.innerHTML = `<div class="pending-head" onclick="toggleSimpcity()">
          <span class="pending-caret">${caret}</span>
          <span>SimpCity &amp; albums</span>
          <span class="pending-count" title="Links waiting for review">${pending.length}</span>
          <span class="field-hint pending-hint">thread media downloads automatically · review the links below</span>
          <button class="btn-tiny pending-clear-all" ${simpcityChecking ? 'disabled' : ''}
                  onclick="event.stopPropagation(); scCheckLinks(this)"
                  title="Check whether the links still exist (a quick request each, oldest checks first)">${simpcityChecking ? 'Checking…' : 'Check links'}</button>
        </div>
        <div class="pending-list" ${simpcityCollapsed ? 'style="display:none"' : ''}>
          ${threadsHtml}
          <div class="sc-subhead"><span>Links to review</span>
            ${dismissedCount ? `<label class="checkbox-option sc-toggle"><input type="checkbox" ${simpcityShowAll ? 'checked' : ''}
                onchange="simpcityShowAll=this.checked; simpcityLastSig=null; renderSimpcityPanel()">
                show handled (${dismissedCount})</label>` : ''}
            ${busy ? '<span class="field-hint">— buttons wait for the current download to finish</span>' : ''}
          </div>
          ${reviewHtml}
          <div class="sc-subhead"><span>Tracked albums</span></div>
          ${albumsHtml}
        </div>`;
}

function scReviewRow(r, busy) {
    const kindBadge = r.kind === 'album' ? '<span class="badge badge-album">album</span>'
        : r.kind === 'file' ? '<span class="badge badge-skipped">file</span>'
        : r.kind === 'embed' ? '<span class="badge badge-gone" title="Embedded media from a site the app can\'t download — grab it by hand">embed</span>'
        : '';
    const statusBadge = r.status === 'tracked' ? '<span class="badge badge-sc-live">tracked</span>'
        : r.status === 'downloaded' ? '<span class="badge badge-sc-live">downloaded</span>'
        : r.status === 'dismissed' ? '<span class="badge badge-skipped">dismissed</span>' : '';
    const enc = encodeURIComponent(r.url);
    const dis = busy ? 'disabled' : '';
    const actions = [];
    if (r.status === 'pending' || r.status === 'downloaded') {
        if (r.can_download && r.status === 'pending') {
            actions.push(`<button class="btn-tiny" ${dis} onclick="scAction('${r.thread_id}','${enc}','download')"
                title="Download once into Simpcity\\${escapeHtml(r.host)}\\<year>">Download</button>`);
        }
        if (r.can_track) {
            actions.push(`<button class="btn-tiny" ${dis} onclick="scAction('${r.thread_id}','${enc}','track')"
                title="Download now and re-check it for new files on every run">Download &amp; track</button>`);
        }
    }
    if (r.status === 'pending') {
        actions.push(`<button class="btn-tiny" onclick="scSetStatus('${r.thread_id}','${enc}','dismissed')"
            title="Hide this link — it won't come back when the thread is re-read">Dismiss</button>`);
    } else if (r.status === 'dismissed') {
        actions.push(`<button class="btn-tiny" onclick="scSetStatus('${r.thread_id}','${enc}','pending')">Restore</button>`);
    }
    return `<div class="pending-row">
        <span class="badge badge-host">${escapeHtml(r.host)}</span>${kindBadge}${statusBadge}
        ${scAliveBadge(r.alive, r.dead_since, null, 0)}
        <a class="pending-url" href="#" onclick="openLink('${enc}');return false;" title="${escapeHtml(r.url)}">${escapeHtml(r.url)}</a>
        <span class="sc-actions">${actions.join('')}<button class="btn-tiny" onclick="copyText('${enc}')">Copy</button></span>
    </div>`;
}

function toggleSimpcity() {
    simpcityCollapsed = !simpcityCollapsed;
    simpcityLastSig = null;
    renderSimpcityPanel();
}

async function scSetStatus(threadId, encUrl, status) {
    const res = await pywebview.api.set_simpcity_link_status(currentCreatorId, threadId,
        decodeURIComponent(encUrl), status);
    if (res && res.error) { showToast(res.error, 'error'); return; }
    simpcityLastSig = null;
    renderSimpcityPanel();
}

async function scAction(threadId, encUrl, action) {
    const res = await pywebview.api.simpcity_link_action(currentCreatorId, threadId,
        decodeURIComponent(encUrl), action);
    if (res && res.error) { showToast(res.error, 'error'); return; }
    Logger.info(action === 'track' ? `Downloading and tracking ${res.url}…` : `Downloading ${res.url}…`);
    if (action === 'track' && typeof onCreatorChange === 'function') {
        // The album is now a creator link — refresh chips/scope, keep the run going.
        const c = await pywebview.api.get_creator(currentCreatorId);
        if (c) { currentCreator = c; renderChips(); buildScopeControl(); }
    }
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
