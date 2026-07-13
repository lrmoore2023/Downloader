/* ── Overlays: Configure Links + Settings ──────────────────────── */

// ── Global settings (mirrors app_state.json) ───────────────────

const Settings = {
    archiveDir: '',
    libraryRoot: '',
    concurrency: 5,
    pawConcurrency: 6,
    pawExtract: true,
    cookiesPath: '',
    cookiesBrowser: '',
    authMethod: 'file',
    derpibooruApiKey: '',
    derpibooruFilterId: '56027',
    discordToken: '',
    discordTokenType: 'user',
};

function loadSettingsFromState(state) {
    Settings.archiveDir = state.archive_dir || '';
    Settings.libraryRoot = state.library_root || '';
    Settings.concurrency = state.cf_concurrency || 5;
    Settings.pawConcurrency = state.pawchive_concurrency || 6;
    Settings.pawExtract = state.pawchive_extract !== false;
    Settings.cookiesPath = state.cookies_path || '';
    Settings.cookiesBrowser = state.cookies_browser || '';
    Settings.authMethod = state.auth_method || 'file';
    Settings.derpibooruApiKey = state.derpibooru_api_key || '';
    Settings.derpibooruFilterId = state.derpibooru_filter_id || '56027';
    Settings.discordToken = state.discord_token || '';
    Settings.discordTokenType = state.discord_token_type || 'user';

    document.getElementById('setArchiveDir').value = Settings.archiveDir;
    document.getElementById('setLibraryRoot').value = Settings.libraryRoot;
    document.getElementById('setConcurrency').value = Settings.concurrency;
    document.getElementById('setConcurrencyVal').textContent = `${Settings.concurrency} downloads at once`;
    const pawEl = document.getElementById('setPawConcurrency');
    if (pawEl) {
        pawEl.value = Settings.pawConcurrency;
        document.getElementById('setPawConcurrencyVal').textContent = `${Settings.pawConcurrency} downloads at once`;
    }
    const pawExtractEl = document.getElementById('setPawExtract');
    if (pawExtractEl) pawExtractEl.checked = Settings.pawExtract;
    const dbKeyEl = document.getElementById('setDerpiApiKey');
    if (dbKeyEl) dbKeyEl.value = Settings.derpibooruApiKey;
    const dcTokEl = document.getElementById('setDiscordToken');
    if (dcTokEl) dcTokEl.value = Settings.discordToken;
    const dcTypeEl = document.getElementById('setDiscordTokenType');
    if (dcTypeEl) dcTypeEl.value = Settings.discordTokenType;

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
        pawchive_concurrency: Settings.pawConcurrency,
        pawchive_extract: Settings.pawExtract,
        cookies_path: Settings.cookiesPath,
        cookies_browser: Settings.cookiesBrowser,
        auth_method: Settings.authMethod,
        derpibooru_api_key: Settings.derpibooruApiKey,
        derpibooru_filter_id: Settings.derpibooruFilterId,
        discord_token: Settings.discordToken,
        discord_token_type: Settings.discordTokenType,
    });
}

// Derpibooru API key — read from the input on change and persist.
function setUpdateDerpiApiKey() {
    const el = document.getElementById('setDerpiApiKey');
    Settings.derpibooruApiKey = (el.value || '').trim();
    persistSettings();
}

// Discord token + type — read from the inputs on change and persist.
function setUpdateDiscordToken() {
    const el = document.getElementById('setDiscordToken');
    Settings.discordToken = (el.value || '').trim();
    persistSettings();
}

function setUpdateDiscordTokenType() {
    const el = document.getElementById('setDiscordTokenType');
    Settings.discordTokenType = el.value === 'bot' ? 'bot' : 'user';
    persistSettings();
}

// ── Settings overlay ────────────────────────────────────────────

async function openSettings() {
    document.getElementById('settingsOverlay').classList.add('visible');
    loadLinkFilters();
    try {
        const roots = await pywebview.api.list_library_roots();
        document.getElementById('setRootsInfo').textContent = roots.length
            ? 'Will scan: ' + roots.join('   ·   ')
            : 'No library roots detected yet — add a creator first, then scan.';
    } catch (e) { /* ignore */ }
}

// ── Pawchive link filters (user-editable junk list) ─────────────
Settings.linkFilters = [];
Settings.linkFilterDefaults = [];

async function loadLinkFilters() {
    try {
        const r = await pywebview.api.get_link_filters();
        Settings.linkFilters = (r && r.filters) || [];
        Settings.linkFilterDefaults = (r && r.defaults) || [];
    } catch (e) { Settings.linkFilters = []; }
    renderLinkFilters();
}

function renderLinkFilters() {
    const box = document.getElementById('linkFilterList');
    if (!box) return;
    if (!Settings.linkFilters.length) {
        box.innerHTML = '<span class="field-hint">No filters — every external link is shown.</span>';
        return;
    }
    box.innerHTML = Settings.linkFilters.map(p =>
        `<span class="filter-chip">${escapeHtml(p)}<button class="filter-chip-x" title="Remove"
             onclick="removeLinkFilter('${encodeURIComponent(p)}')">&times;</button></span>`
    ).join('');
}

// Persist the current list and refresh the pending panel so edits show at once.
function saveLinkFilters() {
    pywebview.api.set_link_filters(Settings.linkFilters).then(() => {
        if (typeof renderPendingLinks === 'function') renderPendingLinks();
    });
}

function addLinkFilter() {
    const input = document.getElementById('linkFilterInput');
    const val = (input.value || '').trim().toLowerCase();
    if (!val) return;
    if (!Settings.linkFilters.includes(val)) {
        Settings.linkFilters.push(val);
        saveLinkFilters();
        renderLinkFilters();
    }
    input.value = '';
    input.focus();
}

function removeLinkFilter(encoded) {
    const p = decodeURIComponent(encoded);
    Settings.linkFilters = Settings.linkFilters.filter(x => x !== p);
    saveLinkFilters();
    renderLinkFilters();
}

function resetLinkFilters() {
    Settings.linkFilters = [...Settings.linkFilterDefaults];
    saveLinkFilters();
    renderLinkFilters();
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

function setUpdatePawConcurrency() {
    const v = parseInt(document.getElementById('setPawConcurrency').value, 10);
    Settings.pawConcurrency = v;
    document.getElementById('setPawConcurrencyVal').textContent = `${v} downloads at once`;
}

function setUpdatePawExtract() {
    Settings.pawExtract = document.getElementById('setPawExtract').checked;
    persistSettings();
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

function openConfigure() {
    if (!currentCreator) { showToast('Select a creator first', 'error'); return; }
    cfgEditingId = currentCreator.id;
    cfgLinksData = currentCreator.links.map(l => ({ ...l }));
    document.getElementById('configureTitle').textContent = 'Configure Creator';
    document.getElementById('cfgName').value = currentCreator.name || '';
    document.getElementById('cfgDest').value = currentCreator.destination || '';
    setCfgType(currentCreator.category, currentCreator.subcategory);
    document.getElementById('cfgDeleteBtn').style.display = '';
    openConfigureCommon();
}

function openNewCreator() {
    cfgEditingId = '';
    cfgLinksData = [];
    document.getElementById('configureTitle').textContent = 'New Creator';
    document.getElementById('cfgName').value = '';
    document.getElementById('cfgDest').value = '';
    setCfgType('', '');
    document.getElementById('cfgDeleteBtn').style.display = 'none';
    openConfigureCommon();
}

// Render the read-only Type chip ("Major · Sub") — the type is dictated by the
// destination folder, so it's shown, not entered.
function setCfgType(major, sub) {
    const el = document.getElementById('cfgCategoryDerived');
    if (!el) return;
    const parts = [major, sub].filter(Boolean);
    el.textContent = parts.length ? parts.join(' · ') : '—';
}

function openConfigureCommon() {
    document.getElementById('cfgNewLink').value = '';
    document.getElementById('cfgLinkPreview').textContent = '';
    renderCfgLinks();
    document.getElementById('configureOverlay').classList.add('visible');
}

function closeConfigure() {
    document.getElementById('configureOverlay').classList.remove('visible');
}

async function cfgBrowseDest() {
    const path = await pywebview.api.select_folder();
    if (!path) return;
    document.getElementById('cfgDest').value = path;
    const [major, sub] = (typeof deriveCategoryPair === 'function')
        ? deriveCategoryPair(path) : ['', ''];
    setCfgType(major, sub);
    const nameEl = document.getElementById('cfgName');
    if (!nameEl.value.trim()) {
        nameEl.value = path.replace(/\\/g, '/').replace(/\/+$/, '').split('/').pop();
    }
}

function cfgOpenDest() {
    const dest = document.getElementById('cfgDest').value;
    if (dest) pywebview.api.open_folder(dest);
}

// Filename site-code choices for a Discord link ('' = default "Discord").
const DISCORD_TAG_OPTS = [
    ['', 'Discord'], ['patreon', 'Patreon'], ['fanbox', 'Fanbox'],
    ['onlyfans', 'OnlyFans'], ['fansly', 'Fansly'],
];

function cfgSetTag(index, value) {
    if (cfgLinksData[index]) cfgLinksData[index].tag = value;
}

function platformLabel(link) {
    if (link.platform === 'twitter') return { badge: 'Twitter', cls: 'badge-twitter', name: '@' + (link.username || '?') };
    if (link.platform === 'derpibooru') return { badge: 'Derpibooru', cls: 'badge-derpibooru', name: link.name || link.query || '?' };
    if (link.platform === 'discord') return { badge: 'Discord', cls: 'badge-discord', name: link.name || link.channel_id || '?' };
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
        // "Reset history" only makes sense for a link that's already saved (i.e.
        // may have a download archive). Newly-pasted links have no history yet.
        const saved = cfgEditingId && currentCreator
            && currentCreator.links.some(x => (x.url || '') === l.url);
        const resetBtn = saved
            ? `<button class="link-reset" onclick="cfgResetLink(${i})"
                 title="Clear this site's download history so the next download re-fetches everything. Your files are kept.">Reset history</button>`
            : '';
        // Discord channels often mirror a Patreon/Fanbox — let each pick the site
        // tag its files are named with, so they blend with the creator's others.
        const tagSel = l.platform === 'discord'
            ? `<label class="link-tag" title="Name this channel's downloaded files with this site tag (e.g. tag a Patreon-mirror channel as Patreon so its files sit alongside your Patreon downloads).">
                 <span>Label as</span>
                 <select onchange="cfgSetTag(${i}, this.value)">${DISCORD_TAG_OPTS.map(
                    ([v, lab]) => `<option value="${v}"${(l.tag || '') === v ? ' selected' : ''}>${lab}</option>`
                 ).join('')}</select>
               </label>`
            : '';
        return `<div class="link-row">
            <span class="link-badge ${p.cls}">${p.badge}</span>
            <div class="link-meta">
                <span class="link-name">${escapeHtml(p.name)}</span>
                <span class="link-url">${escapeHtml(l.url)}</span>
            </div>
            ${tagSel}
            ${resetBtn}
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

// Clear one site's download history (its archive DB) so the next download
// re-fetches everything. Media files on disk are untouched.
async function cfgResetLink(index) {
    const l = cfgLinksData[index];
    if (!l || !cfgEditingId) return;
    const p = platformLabel(l);
    if (!confirm(`Clear the download history for ${p.badge} — ${p.name}?\n\n`
        + `The next download will re-fetch everything for this site. `
        + `Your downloaded files are NOT deleted.`)) return;
    const res = await pywebview.api.reset_link_archive(cfgEditingId, l.url);
    if (res && res.error) { showToast(res.error, 'error'); return; }
    if (res.existed) {
        showToast(`History cleared for ${p.badge} — next download re-fetches everything`, 'success');
    } else {
        showToast(`No history to clear for ${p.badge}`, 'info');
    }
}

async function cfgPreviewLink() {
    const url = document.getElementById('cfgNewLink').value.trim();
    const preview = document.getElementById('cfgLinkPreview');
    if (!url) { preview.textContent = ''; return; }
    const info = await pywebview.api.resolve_link(url);
    if (!info.valid) {
        preview.textContent = 'Unrecognized URL — expected coomerfans.com/u/…, pawchive.st/{service}/user/…, derpibooru.org/search?q=…, discord.com/channels/…, or x.com/…';
        return;
    }
    if (info.platform === 'coomerfans') {
        const svc = info.service === 'onlyfans' ? 'OnlyFans' : (info.service === 'fansly' ? 'Fansly' : info.service);
        preview.textContent = `Detected: ${svc} — ${info.name}  ·  click Add Link`;
    } else if (info.platform === 'pawchive') {
        const svc = info.service ? info.service.charAt(0).toUpperCase() + info.service.slice(1) : 'Pawchive';
        preview.textContent = `Detected: ${svc} (pawchive) — ${info.name || info.user_id}  ·  click Add Link`;
    } else if (info.platform === 'derpibooru') {
        preview.textContent = `Detected: Derpibooru — ${info.query}  ·  click Add Link`;
    } else if (info.platform === 'discord') {
        preview.textContent = `Detected: Discord — ${info.name || ('channel ' + info.channel_id)}  ·  click Add Link`;
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
        // Type is derived from the destination folder by the backend — not sent.
        destination,
        links: cfgLinksData.map(l => ({ url: l.url, tag: l.tag || '' })),
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
    if (!confirm(`Forget "${name}"? This removes it from the list. Your downloaded media files are NOT deleted.`)) {
        return;
    }
    const wipeArchive = confirm(
        `Also delete the download archive(s) for "${name}"?\n\n` +
        `OK — delete the archive database(s) AND reset the pawchive links list (so a future re-download starts fresh: re-downloads everything, applies current naming, and shows every external link again).\n\n` +
        `Cancel — keep the archive and your resolved-link checkmarks (re-adding the creator resumes where it left off; media files are untouched either way).`);
    const res = await pywebview.api.delete_creator(cfgEditingId, wipeArchive);
    closeConfigure();
    const n = (res && res.deleted_archives) ? res.deleted_archives.length : 0;
    showToast(wipeArchive
        ? `Creator removed; ${n} archive database(s) deleted`
        : 'Creator removed from list', 'info');
    currentCreatorId = '';
    document.getElementById('creatorSelect').value = '';
    await refreshCreators('');
}

// Live link preview + Enter-to-add
document.getElementById('cfgNewLink').addEventListener('input', cfgPreviewLink);
document.getElementById('cfgNewLink').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); cfgAddLink(); }
});
