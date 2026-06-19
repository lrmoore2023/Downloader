/* ── Coomerfans Tab Logic ─────────────────────────────────────────
 * Independent of the Twitter tab. All identifiers are cf-prefixed to avoid
 * colliding with twitter_tab.js (they share one global scope). Reuses the
 * shared helpers showToast() and incrementStat() from app.js.
 */

let cfArchiveDir = '';
let cfLibraryRoot = '';
let cfConcurrency = 5;
let cfIsDownloading = false;
let cfDestAutoFilled = false;
let cfUrlDebounce = null;

// ── Dedicated logger for the coomerfans output panel ────────────
const CfLogger = {
    _c: null,
    _userScrolled: false,
    init() {
        this._c = document.getElementById('cfLogContainer');
        this._c.addEventListener('scroll', () => {
            const el = this._c;
            this._userScrolled = (el.scrollHeight - el.scrollTop - el.clientHeight) > 30;
        });
    },
    add(message, type = 'info') {
        if (!this._c) this.init();
        const entry = document.createElement('div');
        entry.className = `log-entry log-${type}`;
        entry.textContent = message;
        this._c.appendChild(entry);
        while (this._c.children.length > 1000) this._c.removeChild(this._c.firstChild);
        if (!this._userScrolled) this._c.scrollTop = this._c.scrollHeight;
    },
    download(m) { this.add(m, 'download'); },
    skip(m) { this.add(m, 'skip'); },
    error(m) { this.add(m, 'error'); },
    info(m) { this.add(m, 'info'); },
    success(m) { this.add(m, 'success'); },
    clear() { if (!this._c) this.init(); this._c.innerHTML = ''; this._userScrolled = false; },
};

// ── Progress callbacks (called from the Python backend) ─────────

window.onCfProgress = function(data) {
    switch (data.type) {
        case 'download':
            CfLogger.download(data.message);
            incrementStat('cfStatDownloaded');
            cfUpdateLiveProgress(data.message);
            break;
        case 'skip':
            CfLogger.skip(data.message);
            incrementStat('cfStatSkipped');
            break;
        case 'error':
            CfLogger.error(data.message);
            incrementStat('cfStatErrors');
            break;
        default:
            CfLogger.info(data.message || JSON.stringify(data));
    }
};

window.onCfComplete = function(data) {
    cfSetDownloadingState(false);
    cfRefreshYearDropdown();
    cfRefreshRecentArtists();

    const msg = `Done. Downloaded: ${data.downloaded} | Skipped: ${data.skipped} | Errors: ${data.errors}`;
    if (data.cancelled) CfLogger.info('Download was cancelled.');
    CfLogger.success(msg);

    if (data.cancelled) {
        showToast('Download cancelled', 'info');
    } else if (data.errors === 0) {
        showToast(`Done! ${data.downloaded} files downloaded`, 'success');
    } else {
        showToast(`Finished with ${data.errors} error(s)`, 'error');
    }
};

window.onCfError = function(data) {
    CfLogger.error(data.message);
    incrementStat('cfStatErrors');
};

function cfUpdateLiveProgress(filename) {
    document.getElementById('runFile').textContent = filename || '';
    document.getElementById('runCount').textContent =
        document.getElementById('cfStatDownloaded').textContent;
}

// ── Folder pickers ──────────────────────────────────────────────

async function cfBrowseArchiveDir() {
    const path = await pywebview.api.select_folder();
    if (path) {
        cfArchiveDir = path;
        document.getElementById('cfArchiveDir').value = path;
        cfSaveState();
    }
}

async function cfBrowseLibraryRoot() {
    const path = await pywebview.api.select_folder();
    if (path) {
        cfLibraryRoot = path;
        document.getElementById('cfLibraryRoot').value = path;
        cfSaveState();
        cfResolveDestinationFromUrl();
    }
}

async function cfBrowseDestination() {
    const path = await pywebview.api.select_folder();
    if (path) {
        document.getElementById('cfDestFolder').value = path;
        cfDestAutoFilled = false;
        cfSaveState();
        cfRefreshYearDropdown();

        const url = document.getElementById('cfCreatorUrl').value.trim();
        if (cfValidateUrl(url)) {
            await pywebview.api.cf_record_artist(url, path);
            cfRefreshRecentArtists();
        }
    }
}

async function cfOpenDestination() {
    const dest = document.getElementById('cfDestFolder').value.trim();
    if (!dest) { showToast('No destination set', 'error'); return; }
    const result = await pywebview.api.open_folder(dest);
    if (result.error) showToast(result.error, 'error');
}

// ── URL → destination auto-fill ─────────────────────────────────

function cfHandleUrlInput() {
    cfShowUrlError(false);
    clearTimeout(cfUrlDebounce);
    cfUrlDebounce = setTimeout(cfResolveDestinationFromUrl, 300);
}

async function cfResolveDestinationFromUrl() {
    const url = document.getElementById('cfCreatorUrl').value.trim();
    if (!cfValidateUrl(url)) return;

    const destEl = document.getElementById('cfDestFolder');
    if (destEl.value.trim() && !cfDestAutoFilled) return;

    const result = await pywebview.api.cf_resolve_destination(url);
    if (result && result.destination) {
        destEl.value = result.destination;
        cfDestAutoFilled = true;
        cfSaveState();
        cfRefreshYearDropdown();
    }
}

// ── Recent creators ─────────────────────────────────────────────

async function cfRefreshRecentArtists() {
    const select = document.getElementById('cfRecentArtists');
    const artists = await pywebview.api.cf_list_artists();
    select.innerHTML = '<option value="">Select a recent creator...</option>';
    for (const a of artists) {
        const opt = document.createElement('option');
        opt.value = a.url;
        opt.dataset.destination = a.destination;
        opt.textContent = a.name;
        select.appendChild(opt);
    }
}

function cfLoadRecentArtist() {
    const select = document.getElementById('cfRecentArtists');
    const url = select.value;
    if (!url) return;
    const opt = select.options[select.selectedIndex];
    document.getElementById('cfCreatorUrl').value = url;
    document.getElementById('cfDestFolder').value = opt.dataset.destination || '';
    cfDestAutoFilled = false;
    cfShowUrlError(false);
    cfSaveState();
    cfRefreshYearDropdown();
    select.value = '';
}

// ── URL validation ──────────────────────────────────────────────

function cfValidateUrl(url) {
    if (!url) return false;
    return /^https?:\/\/(www\.)?coomerfans\.com\/u\/[^/]+\/[^/]+\/[^/?#]+/i.test(url.trim());
}

function cfShowUrlError(show) {
    document.getElementById('cfUrlError').classList.toggle('visible', show);
    document.getElementById('cfCreatorUrl').classList.toggle('error', show);
}

// ── Form state helpers ──────────────────────────────────────────

function cfSetDownloadingState(downloading) {
    cfIsDownloading = downloading;
    document.getElementById('cfBtnDownloadAll').disabled = downloading;
    document.getElementById('cfBtnFetchLatest').disabled = downloading;
    document.getElementById('cfBtnVerify').disabled = downloading;
    document.getElementById('cfBtnRedownload').disabled = downloading;
    document.getElementById('cfRedownloadYear').disabled = downloading;
    document.getElementById('cfBtnCancel').style.display = downloading ? '' : 'none';
    document.getElementById('runningIndicator').classList.toggle('visible', downloading);

    document.getElementById('cfCreatorUrl').disabled = downloading;
    document.getElementById('cfRecentArtists').disabled = downloading;

    if (!downloading) {
        document.getElementById('runFile').textContent = '';
        document.getElementById('runCount').textContent = '0';
    }
}

function cfResetStats() {
    document.getElementById('cfStatDownloaded').textContent = '0';
    document.getElementById('cfStatSkipped').textContent = '0';
    document.getElementById('cfStatErrors').textContent = '0';
}

function cfClearLog() {
    CfLogger.clear();
    cfResetStats();
}

function cfUpdateConcurrency() {
    cfConcurrency = parseInt(document.getElementById('cfConcurrency').value, 10) || 5;
    document.getElementById('cfConcurrencyVal').textContent = `${cfConcurrency} downloads at once`;
    cfSaveState();
}

async function cfRefreshYearDropdown() {
    const dest = document.getElementById('cfDestFolder').value.trim();
    const select = document.getElementById('cfRedownloadYear');
    select.innerHTML = '<option value="">Year...</option>';
    if (!dest) return;
    const years = await pywebview.api.cf_get_destination_years(dest);
    for (const year of years) {
        const opt = document.createElement('option');
        opt.value = year;
        opt.textContent = year;
        select.appendChild(opt);
    }
}

// ── Download actions ────────────────────────────────────────────

function cfGetData() {
    return {
        url: document.getElementById('cfCreatorUrl').value.trim(),
        destination: document.getElementById('cfDestFolder').value.trim(),
    };
}

function cfPreflight(data) {
    if (!cfValidateUrl(data.url)) { cfShowUrlError(true); return false; }
    if (!data.destination) { showToast('Select a destination folder first', 'error'); return false; }
    return true;
}

async function cfStart(mode, year) {
    const data = cfGetData();
    cfShowUrlError(false);
    if (!cfPreflight(data)) return;
    if (mode === 'redownload_year' && !year) { showToast('Select a year to redownload', 'error'); return; }

    CfLogger.clear();
    cfResetStats();
    cfSetDownloadingState(true);
    CfLogger.info(`Starting (${mode})...`);
    CfLogger.info(`URL: ${data.url}`);
    CfLogger.info(`Destination: ${data.destination}`);

    const result = await pywebview.api.start_cf_download(
        data.url, data.destination, mode, year || null, cfArchiveDir, cfConcurrency
    );
    if (result.error) {
        CfLogger.error(result.error);
        showToast(result.error, 'error');
        cfSetDownloadingState(false);
    }
}

function cfDownloadAll() { cfStart('full', null); }
function cfFetchLatest() { cfStart('latest', null); }
function cfRedownloadYear() {
    const year = document.getElementById('cfRedownloadYear').value;
    cfStart('redownload_year', year);
}

async function cfCancel() {
    CfLogger.info('Cancelling...');
    await pywebview.api.cancel_cf_download();
}

// ── Verify & Repair ─────────────────────────────────────────────

async function cfVerifyRepair() {
    const data = cfGetData();
    cfShowUrlError(false);
    if (!cfPreflight(data)) return;

    CfLogger.clear();
    cfResetStats();
    cfSetDownloadingState(true);
    CfLogger.info('Verifying downloaded files for this creator...');

    const result = await pywebview.api.cf_start_verify(data.url, data.destination, cfArchiveDir);
    if (result.error) {
        CfLogger.error(result.error);
        showToast(result.error, 'error');
        cfSetDownloadingState(false);
    }
}

window.onCfVerifyResult = function(data) {
    if (data.error) {
        CfLogger.error(data.error);
        showToast(data.error, 'error');
        cfSetDownloadingState(false);
        return;
    }
    CfLogger.success(`Verify complete — checked ${data.checked}, present ${data.present}, broken ${data.count}.`);

    if (data.count === 0) {
        showToast('All good — no broken files found', 'success');
        cfSetDownloadingState(false);
        return;
    }

    const preview = data.items.slice(0, 20).map(i => `• ${i.filename}  (${i.reason})`).join('\n');
    const more = data.count > 20 ? `\n…and ${data.count - 20} more` : '';
    const ok = window.confirm(
        `Found ${data.count} broken file(s):\n\n${preview}${more}\n\nRe-download them now?`
    );

    if (!ok) {
        CfLogger.info('Repair declined — no changes made.');
        cfSetDownloadingState(false);
        return;
    }

    const data2 = cfGetData();
    CfLogger.info(`Repairing ${data.count} file(s)...`);
    pywebview.api.cf_start_repair(data2.url, data2.destination, cfArchiveDir).then(r => {
        if (r.error) {
            CfLogger.error(r.error);
            showToast(r.error, 'error');
            cfSetDownloadingState(false);
        }
    });
};

window.onCfRepairComplete = function(data) {
    cfSetDownloadingState(false);
    cfRefreshYearDropdown();
    if (data.error) CfLogger.error(data.error);
    CfLogger.success(`Repair done — repaired ${data.repaired}, still bad ${data.still_bad}.`);
    if (data.still_bad === 0 && !data.error) {
        showToast(`Repaired ${data.repaired} file(s)`, 'success');
    } else {
        showToast(`Repaired ${data.repaired}, ${data.still_bad} still bad`, data.repaired ? 'info' : 'error');
    }
};

// ── State persistence ───────────────────────────────────────────

async function cfSaveState() {
    await pywebview.api.save_state({
        cf_archive_dir: cfArchiveDir,
        cf_library_root: cfLibraryRoot,
        cf_concurrency: cfConcurrency,
        cf_last_url: document.getElementById('cfCreatorUrl').value,
        cf_last_destination: document.getElementById('cfDestFolder').value,
    });
}

async function cfLoadState(state) {
    if (state.cf_archive_dir) {
        cfArchiveDir = state.cf_archive_dir;
        document.getElementById('cfArchiveDir').value = state.cf_archive_dir;
    }
    if (state.cf_library_root) {
        cfLibraryRoot = state.cf_library_root;
        document.getElementById('cfLibraryRoot').value = state.cf_library_root;
    }
    if (state.cf_concurrency) {
        cfConcurrency = state.cf_concurrency;
        document.getElementById('cfConcurrency').value = state.cf_concurrency;
    }
    cfUpdateConcurrency();
    if (state.cf_last_url) {
        document.getElementById('cfCreatorUrl').value = state.cf_last_url;
    }
    if (state.cf_last_destination) {
        document.getElementById('cfDestFolder').value = state.cf_last_destination;
        cfRefreshYearDropdown();
    }
    await cfRefreshRecentArtists();
}

// ── Wire up URL field listeners ─────────────────────────────────

document.getElementById('cfCreatorUrl').addEventListener('input', cfHandleUrlInput);
document.getElementById('cfCreatorUrl').addEventListener('blur', () => {
    cfShowUrlError(false);
    cfResolveDestinationFromUrl();
    cfSaveState();
});
