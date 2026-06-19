/* ── Twitter Tab Logic ────────────────────────────────────────── */

let currentAuthMethod = 'file';
let cookiesPath = '';
let cookiesBrowser = '';
let archiveDir = '';
let libraryRoot = '';
let hasVideos = false;
let isDownloading = false;

// True while the Destination field holds an auto-resolved path the user hasn't
// overridden. Lets us refresh the suggestion without clobbering a manual choice.
let destAutoFilled = false;

// ── Auth method switching ──────────────────────────────────────

function setAuthMethod(method) {
    currentAuthMethod = method;

    document.querySelectorAll('.radio-option').forEach(el => {
        el.classList.toggle('selected', el.dataset.auth === method);
    });

    document.getElementById('authFile').classList.toggle('active', method === 'file');
    document.getElementById('authBrowser').classList.toggle('active', method === 'browser');

    saveState();
}

// ── Cookie file selection ──────────────────────────────────────

async function browseCookies() {
    const path = await pywebview.api.select_cookies_file();
    if (path) {
        document.getElementById('cookiesPath').value = path;
        cookiesPath = path;

        const result = await pywebview.api.validate_cookies(path);
        updateCookieStatus(result);
        saveState();
    }
}

function updateCookieStatus(result) {
    const badge = document.getElementById('cookieStatus');
    badge.textContent = result.message;

    badge.className = 'status-badge';
    if (result.valid) {
        badge.classList.add('status-ok');
    } else {
        badge.classList.add('status-error');
    }
}

// ── Browser cookie extraction ──────────────────────────────────

async function extractBrowserCookies() {
    const browser = document.getElementById('browserSelect').value;
    const btn = document.getElementById('extractBtn');
    const badge = document.getElementById('browserStatus');

    btn.disabled = true;
    btn.textContent = 'Extracting...';
    badge.textContent = 'Extracting...';
    badge.className = 'status-badge status-warn';

    const result = await pywebview.api.extract_cookies(browser);

    btn.disabled = false;
    btn.textContent = 'Extract';

    badge.textContent = result.message;
    badge.className = 'status-badge';
    if (result.valid) {
        badge.classList.add('status-ok');
        cookiesBrowser = browser;
        saveState();
    } else {
        badge.classList.add('status-error');
        cookiesBrowser = '';
    }
}

// ── Destination folder selection ───────────────────────────────

async function browseDestination() {
    const path = await pywebview.api.select_folder();
    if (path) {
        document.getElementById('destFolder').value = path;
        destAutoFilled = false;  // explicit manual choice
        saveState();
        refreshYearDropdown();

        // Remember this folder for the current artist so it auto-fills next time.
        const url = document.getElementById('accountUrl').value.trim();
        if (validateUrl(url)) {
            await pywebview.api.record_artist(url, path, archiveDir);
            refreshRecentArtists();
        }
    }
}

async function openDestination() {
    const dest = document.getElementById('destFolder').value.trim();
    if (!dest) {
        showToast('No destination set', 'error');
        return;
    }
    const result = await pywebview.api.open_folder(dest);
    if (result.error) {
        showToast(result.error, 'error');
    }
}

// ── Library root selection ──────────────────────────────────────

async function browseLibraryRoot() {
    const path = await pywebview.api.select_folder();
    if (path) {
        libraryRoot = path;
        document.getElementById('libraryRoot').value = path;
        saveState();
        // Re-resolve the destination now that a root exists.
        resolveDestinationFromUrl();
    }
}

// ── URL → destination auto-fill ─────────────────────────────────

let _urlDebounce = null;

function handleUrlInput() {
    showUrlError(false);
    clearTimeout(_urlDebounce);
    _urlDebounce = setTimeout(resolveDestinationFromUrl, 300);
}

async function resolveDestinationFromUrl() {
    const url = document.getElementById('accountUrl').value.trim();
    if (!validateUrl(url)) return;

    const destEl = document.getElementById('destFolder');

    // Don't overwrite a folder the user picked manually.
    if (destEl.value.trim() && !destAutoFilled) return;

    const result = await pywebview.api.resolve_destination(url);
    if (result && result.destination) {
        destEl.value = result.destination;
        destAutoFilled = true;
        saveState();
        refreshYearDropdown();
    }
}

// ── Recent artists ──────────────────────────────────────────────

async function refreshRecentArtists() {
    const select = document.getElementById('recentArtists');
    const artists = await pywebview.api.list_artists();

    select.innerHTML = '<option value="">Select a recent artist...</option>';
    for (const artist of artists) {
        const opt = document.createElement('option');
        opt.value = artist.username;
        opt.dataset.destination = artist.destination;
        opt.textContent = artist.username;
        select.appendChild(opt);
    }
}

async function loadRecentArtist() {
    const select = document.getElementById('recentArtists');
    const username = select.value;
    if (!username) return;

    const opt = select.options[select.selectedIndex];
    document.getElementById('accountUrl').value = `https://x.com/${username}`;
    document.getElementById('destFolder').value = opt.dataset.destination || '';
    destAutoFilled = false;
    showUrlError(false);
    saveState();
    refreshYearDropdown();

    // Reset the picker so the same artist can be reselected later.
    select.value = '';
}

// ── Archive directory selection ─────────────────────────────────

async function browseArchiveDir() {
    const path = await pywebview.api.select_folder();
    if (path) {
        const oldDir = archiveDir;
        archiveDir = path;
        document.getElementById('archiveDir').value = path;

        // Migrate existing archives from old location
        if (oldDir && oldDir !== path) {
            const result = await pywebview.api.move_archives(oldDir, path);
            if (result.moved > 0) {
                showToast(`Moved ${result.moved} archive(s) to new location`, 'success');
            }
        }

        saveState();
    }
}

// ── Has videos toggle ──────────────────────────────────────────

function toggleHasVideos() {
    hasVideos = !hasVideos;
    document.getElementById('hasVideosBox').classList.toggle('checked', hasVideos);
    saveState();
}

// ── URL validation ─────────────────────────────────────────────

function validateUrl(url) {
    if (!url) return false;
    return /^https?:\/\/(www\.)?(x\.com|twitter\.com)\/.+/i.test(url.trim());
}

function showUrlError(show) {
    const el = document.getElementById('urlError');
    const input = document.getElementById('accountUrl');
    el.classList.toggle('visible', show);
    input.classList.toggle('error', show);
}

// ── Form state helpers ─────────────────────────────────────────

function getFormData() {
    return {
        url: document.getElementById('accountUrl').value.trim(),
        destination: document.getElementById('destFolder').value.trim(),
        cookiesPath: currentAuthMethod === 'file' ? cookiesPath : '',
        cookiesBrowser: currentAuthMethod === 'browser' ? cookiesBrowser : '',
    };
}

function hasAuth() {
    if (currentAuthMethod === 'file') return !!cookiesPath;
    if (currentAuthMethod === 'browser') return !!cookiesBrowser;
    return false;
}

function setDownloadingState(downloading) {
    isDownloading = downloading;

    document.getElementById('btnGetArtist').disabled = downloading;
    document.getElementById('btnFetchLatest').disabled = downloading;
    document.getElementById('btnRedownload').disabled = downloading;
    document.getElementById('redownloadYear').disabled = downloading;
    document.getElementById('btnCancel').style.display = downloading ? '' : 'none';
    document.getElementById('runningIndicator').classList.toggle('visible', downloading);

    // Disable inputs during download
    document.getElementById('accountUrl').disabled = downloading;
    document.getElementById('destFolder').disabled = downloading;
    document.getElementById('recentArtists').disabled = downloading;

    // Reset the live-progress strip
    if (!downloading) {
        document.getElementById('runFile').textContent = '';
        document.getElementById('runCount').textContent = '0';
    }
}

function resetStats() {
    document.getElementById('statDownloaded').textContent = '0';
    document.getElementById('statSkipped').textContent = '0';
    document.getElementById('statErrors').textContent = '0';
}

// ── Download actions ───────────────────────────────────────────

async function getArtistPosts() {
    showUrlError(false);
    const data = getFormData();

    if (!validateUrl(data.url)) {
        showUrlError(true);
        return;
    }

    if (!data.destination) {
        showToast('Select a destination folder first', 'error');
        return;
    }

    if (!hasAuth()) {
        showToast('Set up authentication first', 'error');
        return;
    }

    Logger.clear();
    resetStats();
    setDownloadingState(true);
    Logger.info('Starting full artist download...');
    Logger.info(`URL: ${data.url}`);
    Logger.info(`Destination: ${data.destination}`);

    const result = await pywebview.api.start_full_download(
        data.url, data.destination, data.cookiesPath, data.cookiesBrowser, hasVideos, archiveDir
    );

    if (result.error) {
        Logger.error(result.error);
        showToast(result.error, 'error');
        setDownloadingState(false);
    }
}

async function fetchLatestPosts() {
    showUrlError(false);
    const data = getFormData();

    if (!validateUrl(data.url)) {
        showUrlError(true);
        return;
    }

    if (!data.destination) {
        showToast('Select a destination folder first', 'error');
        return;
    }

    if (!hasAuth()) {
        showToast('Set up authentication first', 'error');
        return;
    }

    Logger.clear();
    resetStats();
    setDownloadingState(true);
    Logger.info('Fetching latest posts...');
    Logger.info(`URL: ${data.url}`);

    const result = await pywebview.api.start_latest_download(
        data.url, data.destination, data.cookiesPath, data.cookiesBrowser, hasVideos, archiveDir
    );

    if (result.error) {
        Logger.error(result.error);
        showToast(result.error, 'error');
        setDownloadingState(false);
    } else if (result.latest_year) {
        Logger.info(`Scanning for posts from ${result.latest_year} onwards...`);
    }
}

async function redownloadYear() {
    showUrlError(false);
    const data = getFormData();
    const year = document.getElementById('redownloadYear').value;

    if (!validateUrl(data.url)) {
        showUrlError(true);
        return;
    }

    if (!data.destination) {
        showToast('Select a destination folder first', 'error');
        return;
    }

    if (!hasAuth()) {
        showToast('Set up authentication first', 'error');
        return;
    }

    if (!year) {
        showToast('Select a year to redownload', 'error');
        return;
    }

    Logger.clear();
    resetStats();
    setDownloadingState(true);
    Logger.info(`Redownloading missing files for ${year}...`);
    Logger.info(`URL: ${data.url}`);
    Logger.info(`Destination: ${data.destination}`);
    Logger.info('Archive bypassed — only files missing from disk will be downloaded.');

    const result = await pywebview.api.start_redownload_year(
        data.url, data.destination, data.cookiesPath, data.cookiesBrowser, hasVideos, archiveDir, year
    );

    if (result.error) {
        Logger.error(result.error);
        showToast(result.error, 'error');
        setDownloadingState(false);
    }
}

async function refreshYearDropdown() {
    const dest = document.getElementById('destFolder').value.trim();
    const select = document.getElementById('redownloadYear');

    // Clear existing options
    select.innerHTML = '<option value="">Year...</option>';

    if (!dest) return;

    const years = await pywebview.api.get_destination_years(dest);
    for (const year of years) {
        const opt = document.createElement('option');
        opt.value = year;
        opt.textContent = year;
        select.appendChild(opt);
    }
}

async function cancelDownload() {
    Logger.info('Cancelling download...');
    await pywebview.api.cancel_download();
}
