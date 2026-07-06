/* ── Main App Controller ──────────────────────────────────────── */

// ── Download progress callbacks (pushed from the Python backend) ──

window.onCreatorProgress = function (data) {
    switch (data.type) {
        case 'download':
            Logger.download(data.message);
            incrementStat('statDownloaded');
            updateLiveProgress(data.message);
            break;
        case 'skip':
            Logger.skip(data.message);
            incrementStat('statSkipped');
            break;
        case 'error':
            Logger.error(data.message);
            incrementStat('statErrors');
            break;
        case 'dl_start':
            addActiveDownload(data.id, data.message);
            break;
        case 'dl_stop':
            removeActiveDownload(data.id);
            break;
        case 'url':
        case 'info':
            Logger.info(data.message);
            break;
        default:
            Logger.info(data.message || JSON.stringify(data));
    }
};

window.onCreatorComplete = function (data) {
    setDownloadingState(false);
    refreshYears();
    if (typeof renderPendingLinks === 'function') renderPendingLinks();

    const msg = `Done. Downloaded: ${data.downloaded} | Skipped: ${data.skipped} | Errors: ${data.errors}`;
    if (data.cancelled) Logger.info('Operation cancelled.');
    Logger.success(msg);

    if (data.cancelled) {
        showToast('Cancelled', 'info');
    } else if (data.errors === 0) {
        showToast(`Done! ${data.downloaded} file(s) downloaded`, 'success');
    } else {
        showToast(`Finished with ${data.errors} error(s)`, 'info');
    }
};

window.onCreatorError = function (data) {
    Logger.error(data.message);
    incrementStat('statErrors');
    if (data.subtype === 'auth') {
        showToast('Twitter auth failed — cookies may have expired (Settings)', 'error');
    }
};

window.onCreatorVerifyResult = function (data) {
    setDownloadingState(false);
    if (data.error) {
        Logger.error('Verify failed: ' + data.error);
        showToast('Verify failed', 'error');
        return;
    }
    Logger.info(`Verify complete — checked ${data.checked}, present ${data.present}, broken ${data.count}.`);
    if (data.cancelled) { Logger.info('Verify cancelled.'); return; }
    if (data.count === 0) { showToast('All present files look good', 'success'); return; }

    data.items.slice(0, 50).forEach(it => Logger.error(`BROKEN: ${it.filename} (${it.reason})`));
    if (confirm(`${data.count} broken file(s) found. Re-download them now?`)) {
        setDownloadingState(true);
        pywebview.api.start_creator_repair(currentCreatorId).then(res => {
            if (res && res.error) { setDownloadingState(false); showToast(res.error, 'error'); }
        });
    }
};

window.onCreatorRepairComplete = function (data) {
    setDownloadingState(false);
    if (data.cancelled) Logger.info('Repair cancelled.');
    Logger.success(`Repair done — repaired ${data.repaired}, still bad ${data.still_bad}.`);
    if (data.error) Logger.error(data.error);
    showToast(`Repaired ${data.repaired} file(s)`, data.still_bad ? 'info' : 'success');
};

// ── Library import callbacks ────────────────────────────────────

window.onImportProgress = function (data) {
    Logger.info(data.message);
};

window.onImportComplete = function (data) {
    setDownloadingState(false);
    if (data.error) {
        Logger.error('Import failed: ' + data.error);
        showToast('Import failed', 'error');
        return;
    }
    Logger.success(
        `Import done — matched ${data.matched_cf} coomerfans + ${data.matched_tw} twitter `
        + `(resolved ${data.resolved_online} online), +${data.links_added} new link(s). `
        + `Now ${data.creators_total} creator(s).`);
    if (data.unmatched && data.unmatched.length) {
        Logger.info(`${data.unmatched.length} archive DB(s) had no folder match: ${data.unmatched.join(', ')}`);
    }
    showToast(`Imported — ${data.links_added} new link(s)`, data.cancelled ? 'info' : 'success');
    refreshCreators(currentCreatorId);
};

// ── Live progress strip (header) ────────────────────────────────

function updateLiveProgress(filename) {
    document.getElementById('runFile').textContent = filename || '';
    document.getElementById('runCount').textContent =
        document.getElementById('statDownloaded').textContent;
}

// ── Active downloads panel (live, per-file, skippable) ──────────

function addActiveDownload(id, filename) {
    if (!id) return;
    const list = document.getElementById('activeDownloads');
    const section = document.getElementById('activeSection');
    if (!list) return;
    if (document.getElementById('active-' + cssId(id))) return;   // dedupe
    const row = document.createElement('div');
    row.className = 'active-row';
    row.id = 'active-' + cssId(id);
    const name = document.createElement('span');
    name.className = 'active-name';
    name.textContent = filename || id;
    name.title = filename || id;
    const btn = document.createElement('button');
    btn.className = 'btn-tiny active-skip';
    btn.textContent = 'Skip';
    btn.title = "Skip this file (won't re-download on future runs)";
    btn.onclick = () => skipDownload(id, btn);
    row.appendChild(name);
    row.appendChild(btn);
    list.appendChild(row);
    if (section) section.style.display = '';
}

function removeActiveDownload(id) {
    const row = document.getElementById('active-' + cssId(id));
    if (row) row.remove();
    const list = document.getElementById('activeDownloads');
    const section = document.getElementById('activeSection');
    if (section && list && !list.children.length) section.style.display = 'none';
}

function clearActiveDownloads() {
    const list = document.getElementById('activeDownloads');
    const section = document.getElementById('activeSection');
    if (list) list.innerHTML = '';
    if (section) section.style.display = 'none';
}

function skipDownload(id, btn) {
    if (btn) { btn.disabled = true; btn.textContent = 'Skipping…'; }
    try { pywebview.api.skip_download(id); } catch (e) { /* ignore */ }
}

// Make an entry key safe for use in an element id.
function cssId(s) {
    return String(s).replace(/[^a-zA-Z0-9_-]/g, '_');
}

// ── Stats helpers ───────────────────────────────────────────────

function incrementStat(id) {
    const el = document.getElementById(id);
    el.textContent = parseInt(el.textContent, 10) + 1;
}

function resetStats() {
    document.getElementById('statDownloaded').textContent = '0';
    document.getElementById('statSkipped').textContent = '0';
    document.getElementById('statErrors').textContent = '0';
    document.getElementById('runCount').textContent = '0';
}

function clearLog() {
    Logger.clear();
    resetStats();
}

// ── Toast notifications ─────────────────────────────────────────

function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.3s ease';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

// ── Close overlays on backdrop click / Escape ───────────────────

function dismissOverlay(ov) {
    if (ov.id === 'settingsOverlay') persistSettings();
    ov.classList.remove('visible');
}

document.querySelectorAll('.modal-overlay').forEach(ov => {
    ov.addEventListener('mousedown', e => {
        if (e.target === ov) dismissOverlay(ov);
    });
});

document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal-overlay.visible').forEach(dismissOverlay);
    }
});

// ── Initialize ──────────────────────────────────────────────────

window.addEventListener('pywebviewready', async function () {
    Logger.init();
    Logger.info('Downloader initialized. Ready.');

    // Consolidate legacy per-tab state into the creator model (idempotent).
    try {
        const m = await pywebview.api.migrate_state();
        if (m && m.migrated) {
            Logger.info(`Migrated existing data into ${m.creators} creator(s). Backup saved as app_state.json.pre-migrate.bak.`);
        }
    } catch (e) {
        Logger.error('Migration note: ' + e);
    }

    const state = await pywebview.api.load_state();
    loadSettingsFromState(state);
    currentSort = state.creator_sort || 'recent';
    document.getElementById('creatorSort').value = currentSort;
    await refreshCreators(state.last_creator || '');
});
