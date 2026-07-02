/* ── Overlays: Configure Links + Settings ──────────────────────── */

// ── Global settings (mirrors app_state.json) ───────────────────

const Settings = {
    archiveDir: '',
    libraryRoot: '',
    concurrency: 5,
    cookiesPath: '',
    cookiesBrowser: '',
    authMethod: 'file',
};

function loadSettingsFromState(state) {
    Settings.archiveDir = state.archive_dir || '';
    Settings.libraryRoot = state.library_root || '';
    Settings.concurrency = state.cf_concurrency || 5;
    Settings.cookiesPath = state.cookies_path || '';
    Settings.cookiesBrowser = state.cookies_browser || '';
    Settings.authMethod = state.auth_method || 'file';

    document.getElementById('setArchiveDir').value = Settings.archiveDir;
    document.getElementById('setLibraryRoot').value = Settings.libraryRoot;
    document.getElementById('setConcurrency').value = Settings.concurrency;
    document.getElementById('setConcurrencyVal').textContent = `${Settings.concurrency} downloads at once`;

    setAuthMethod(Settings.authMethod);
    if (Settings.cookiesPath) {
        document.getElementById('cookiesPath').value = Settings.cookiesPath;
        pywebview.api.validate_cookies(Settings.cookiesPath).then(updateCookieStatus);
    }
    if (Settings.cookiesBrowser) {
        document.getElementById('browserSelect').value = Settings.cookiesBrowser;
        const badge = document.getElementById('browserStatus');
        badge.textContent = `${Settings.cookiesBrowser} cookies ready`;
        badge.className = 'status-badge status-ok';
    }
}

function persistSettings() {
    pywebview.api.save_state({
        archive_dir: Settings.archiveDir,
        library_root: Settings.libraryRoot,
        cf_concurrency: Settings.concurrency,
        cookies_path: Settings.cookiesPath,
        cookies_browser: Settings.cookiesBrowser,
        auth_method: Settings.authMethod,
    });
}

// ── Settings overlay ────────────────────────────────────────────

async function openSettings() {
    document.getElementById('settingsOverlay').classList.add('visible');
    try {
        const roots = await pywebview.api.list_library_roots();
        document.getElementById('setRootsInfo').textContent = roots.length
            ? 'Will scan: ' + roots.join('   ·   ')
            : 'No library roots detected yet — add a creator first, then scan.';
    } catch (e) { /* ignore */ }
}

function closeSettings() {
    persistSettings();
    document.getElementById('settingsOverlay').classList.remove('visible');
}

// ── Rebuild library ─────────────────────────────────────────────

async function recoverFromBackup() {
    closeSettings();
    Logger.info('Recovering creators from backup state files…');
    const r = await pywebview.api.recover_from_backup();
    if (r && r.error) { showToast('Recovery failed', 'error'); Logger.error(r.error); return; }
    Logger.success(`Recovered +${r.creators_added} creator(s), +${r.links_added} link(s) `
        + `from ${r.sources.length ? r.sources.join(', ') : 'no backup found'}. Total: ${r.creators_total}.`);
    showToast(`Recovered ${r.creators_added} creator(s), ${r.links_added} link(s)`, 'success');
    await refreshCreators(currentCreatorId);
}

async function importFromDisk() {
    closeSettings();
    setDownloadingState(true);
    resetStats();
    Logger.info('Scanning library on disk — matching archive DBs to creator folders…');
    const res = await pywebview.api.import_from_disk(null);
    if (res && res.error) {
        setDownloadingState(false);
        showToast(res.error, 'error');
        Logger.error(res.error);
    } else if (res && res.roots) {
        Logger.info('Roots: ' + res.roots.join('   ·   '));
    }
}

async function setBrowseArchive() {
    const path = await pywebview.api.select_folder();
    if (path) {
        Settings.archiveDir = path;
        document.getElementById('setArchiveDir').value = path;
        persistSettings();
    }
}

async function setBrowseLibrary() {
    const path = await pywebview.api.select_folder();
    if (path) {
        Settings.libraryRoot = path;
        document.getElementById('setLibraryRoot').value = path;
        persistSettings();
    }
}

function setUpdateConcurrency() {
    const v = parseInt(document.getElementById('setConcurrency').value, 10);
    Settings.concurrency = v;
    document.getElementById('setConcurrencyVal').textContent = `${v} downloads at once`;
}

// ── Twitter auth (cookies) ──────────────────────────────────────

function setAuthMethod(method) {
    Settings.authMethod = method;
    document.querySelectorAll('.radio-option').forEach(opt =>
        opt.classList.toggle('selected', opt.dataset.auth === method));
    document.getElementById('authFile').classList.toggle('active', method === 'file');
    document.getElementById('authBrowser').classList.toggle('active', method === 'browser');
}

async function browseCookies() {
    const path = await pywebview.api.select_cookies_file();
    if (!path) return;
    Settings.cookiesPath = path;
    document.getElementById('cookiesPath').value = path;
    const result = await pywebview.api.validate_cookies(path);
    updateCookieStatus(result);
    persistSettings();
}

function updateCookieStatus(result) {
    const badge = document.getElementById('cookieStatus');
    if (result && result.valid) {
        badge.textContent = result.message || 'Cookies valid';
        badge.className = 'status-badge status-ok';
    } else {
        badge.textContent = (result && result.message) || 'Invalid cookies';
        badge.className = 'status-badge status-error';
    }
}

async function extractBrowserCookies() {
    const browser = document.getElementById('browserSelect').value;
    const badge = document.getElementById('browserStatus');
    badge.textContent = 'Extracting…';
    badge.className = 'status-badge status-idle';
    const result = await pywebview.api.extract_cookies(browser);
    if (result && result.valid) {
        Settings.cookiesBrowser = browser;
        badge.textContent = result.message || `${browser} cookies ready`;
        badge.className = 'status-badge status-ok';
        persistSettings();
    } else {
        Settings.cookiesBrowser = '';
        badge.textContent = (result && result.message) || 'Extraction failed';
        badge.className = 'status-badge status-error';
    }
}

// ── Configure Links overlay ─────────────────────────────────────

let cfgEditingId = '';      // '' = creating a new creator
let cfgLinksData = [];      // working copy of link records
let cfgHasVideos = false;

function openConfigure() {
    if (!currentCreator) { showToast('Select a creator first', 'error'); return; }
    cfgEditingId = currentCreator.id;
    cfgLinksData = currentCreator.links.map(l => ({ ...l }));
    cfgHasVideos = !!currentCreator.has_videos;
    document.getElementById('configureTitle').textContent = 'Configure Creator';
    document.getElementById('cfgName').value = currentCreator.name || '';
    document.getElementById('cfgCategory').value = currentCreator.category || '';
    document.getElementById('cfgDest').value = currentCreator.destination || '';
    document.getElementById('cfgDeleteBtn').style.display = '';
    openConfigureCommon();
}

function openNewCreator() {
    cfgEditingId = '';
    cfgLinksData = [];
    cfgHasVideos = false;
    document.getElementById('configureTitle').textContent = 'New Creator';
    document.getElementById('cfgName').value = '';
    document.getElementById('cfgCategory').value = '';
    document.getElementById('cfgDest').value = '';
    document.getElementById('cfgDeleteBtn').style.display = 'none';
    openConfigureCommon();
}

function openConfigureCommon() {
    if (typeof refreshCategoryDatalist === 'function') refreshCategoryDatalist();
    document.getElementById('cfgVideosBox').classList.toggle('checked', cfgHasVideos);
    document.getElementById('cfgNewLink').value = '';
    document.getElementById('cfgLinkPreview').textContent = '';
    renderCfgLinks();
    document.getElementById('configureOverlay').classList.add('visible');
}

function closeConfigure() {
    document.getElementById('configureOverlay').classList.remove('visible');
}

function cfgToggleVideos() {
    cfgHasVideos = !cfgHasVideos;
    document.getElementById('cfgVideosBox').classList.toggle('checked', cfgHasVideos);
}

async function cfgBrowseDest() {
    const path = await pywebview.api.select_folder();
    if (!path) return;
    document.getElementById('cfgDest').value = path;
    const nameEl = document.getElementById('cfgName');
    if (!nameEl.value.trim()) {
        nameEl.value = path.replace(/\\/g, '/').replace(/\/+$/, '').split('/').pop();
    }
}

function cfgOpenDest() {
    const dest = document.getElementById('cfgDest').value;
    if (dest) pywebview.api.open_folder(dest);
}

function platformLabel(link) {
    if (link.platform === 'twitter') return { badge: 'Twitter', cls: 'badge-twitter', name: '@' + (link.username || '?') };
    const svc = { onlyfans: 'OF', fansly: 'Fansly', patreon: 'Patreon', fanbox: 'Fanbox' }[link.service]
        || (link.service || '?');
    const cls = link.platform === 'pawchive' ? 'badge-pawchive' : '';
    return { badge: svc, cls, name: link.name || link.user_id || '?' };
}

function renderCfgLinks() {
    const el = document.getElementById('cfgLinks');
    if (!cfgLinksData.length) {
        el.innerHTML = '<div class="links-empty">No links yet. Paste a URL below to add one.</div>';
        return;
    }
    el.innerHTML = cfgLinksData.map((l, i) => {
        const p = platformLabel(l);
        return `<div class="link-row">
            <span class="link-badge ${p.cls}">${p.badge}</span>
            <div class="link-meta">
                <span class="link-name">${escapeHtml(p.name)}</span>
                <span class="link-url">${escapeHtml(l.url)}</span>
            </div>
            <button class="link-remove" onclick="cfgRemoveLink(${i})" title="Remove link">&times;</button>
        </div>`;
    }).join('');
}

function escapeHtml(s) {
    return (s || '').replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function cfgRemoveLink(index) {
    cfgLinksData.splice(index, 1);
    renderCfgLinks();
}

async function cfgPreviewLink() {
    const url = document.getElementById('cfgNewLink').value.trim();
    const preview = document.getElementById('cfgLinkPreview');
    if (!url) { preview.textContent = ''; return; }
    const info = await pywebview.api.resolve_link(url);
    if (!info.valid) {
        preview.textContent = 'Unrecognized URL — expected coomerfans.com/u/…, pawchive.st/{service}/user/…, or x.com/…';
        return;
    }
    if (info.platform === 'coomerfans') {
        const svc = info.service === 'onlyfans' ? 'OnlyFans' : (info.service === 'fansly' ? 'Fansly' : info.service);
        preview.textContent = `Detected: ${svc} — ${info.name}  ·  click Add Link`;
    } else if (info.platform === 'pawchive') {
        const svc = info.service ? info.service.charAt(0).toUpperCase() + info.service.slice(1) : 'Pawchive';
        preview.textContent = `Detected: ${svc} (pawchive) — ${info.name || info.user_id}  ·  click Add Link`;
    } else {
        preview.textContent = `Detected: Twitter — @${info.username}  ·  click Add Link`;
    }
}

async function cfgAddLink() {
    const input = document.getElementById('cfgNewLink');
    const url = input.value.trim();
    if (!url) return;
    const info = await pywebview.api.resolve_link(url);
    if (!info.valid) {
        showToast('Unrecognized URL', 'error');
        return;
    }
    if (cfgLinksData.some(l => (l.url || '').toLowerCase() === info.url.toLowerCase())) {
        showToast('That link is already added', 'info');
        input.value = '';
        document.getElementById('cfgLinkPreview').textContent = '';
        return;
    }
    cfgLinksData.push(info);
    input.value = '';
    document.getElementById('cfgLinkPreview').textContent = '';
    renderCfgLinks();
}

async function cfgSave() {
    // Commit a URL still sitting in the Add box — people paste a link, see the
    // "Detected…" preview, and click Save without clicking Add Link first.
    if (document.getElementById('cfgNewLink').value.trim()) {
        await cfgAddLink();
        // If it was unrecognized, cfgAddLink leaves it in the box + toasts; stop
        // so the user can fix it rather than silently saving without it.
        if (document.getElementById('cfgNewLink').value.trim()) return;
    }

    const destination = document.getElementById('cfgDest').value.trim();
    if (!destination) { showToast('Choose a destination folder', 'error'); return; }

    const creator = {
        id: cfgEditingId || undefined,
        name: document.getElementById('cfgName').value.trim(),
        category: document.getElementById('cfgCategory').value.trim(),
        destination,
        has_videos: cfgHasVideos,
        links: cfgLinksData.map(l => ({ url: l.url })),
    };
    const res = await pywebview.api.save_creator(creator);
    if (res && res.error) { showToast(res.error, 'error'); return; }

    closeConfigure();
    showToast('Creator saved', 'success');
    await refreshCreators(res.id);
}

async function cfgDelete() {
    if (!cfgEditingId) return;
    const name = document.getElementById('cfgName').value.trim() || 'this creator';
    if (!confirm(`Forget "${name}"? This only removes it from the list — your downloaded files and archives are not deleted.`)) {
        return;
    }
    await pywebview.api.delete_creator(cfgEditingId);
    closeConfigure();
    showToast('Creator removed from list', 'info');
    currentCreatorId = '';
    document.getElementById('creatorSelect').value = '';
    await refreshCreators('');
}

// Live link preview + Enter-to-add
document.getElementById('cfgNewLink').addEventListener('input', cfgPreviewLink);
document.getElementById('cfgNewLink').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); cfgAddLink(); }
});
