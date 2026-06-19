/* ── Main App Controller ──────────────────────────────────────── */

// ── pywebview progress callbacks (called from Python backend) ──

window.onDownloadProgress = function(data) {
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
        case 'url':
            Logger.info(data.message);
            break;
        case 'info':
            Logger.info(data.message);
            break;
        default:
            Logger.info(data.message || JSON.stringify(data));
    }
};

// ── Live progress strip ────────────────────────────────────────

function updateLiveProgress(filename) {
    document.getElementById('runFile').textContent = filename || '';
    document.getElementById('runCount').textContent =
        document.getElementById('statDownloaded').textContent;
}

window.onDownloadComplete = function(data) {
    setDownloadingState(false);
    refreshYearDropdown();
    refreshRecentArtists();

    const msg = `Download complete. Downloaded: ${data.downloaded} | Skipped: ${data.skipped} | Errors: ${data.errors}`;

    if (data.cancelled) {
        Logger.info('Download was cancelled.');
    }

    Logger.success(msg);

    if (data.errors === 0 && !data.cancelled) {
        showToast(`Done! ${data.downloaded} files downloaded`, 'success');
    } else if (data.cancelled) {
        showToast('Download cancelled', 'info');
    }
};

window.onDownloadError = function(data) {
    Logger.error(data.message);
    incrementStat('statErrors');

    if (data.subtype === 'auth') {
        showToast('Authentication failed - cookies may have expired', 'error');
    }
};

// ── Sidebar tab switching ──────────────────────────────────────

function setupTabs() {
    document.querySelectorAll('.sidebar-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            const name = tab.dataset.tab;
            document.querySelectorAll('.sidebar-tab').forEach(t =>
                t.classList.toggle('active', t === tab));
            document.querySelectorAll('.tab-panel').forEach(p =>
                p.classList.toggle('active', p.id === `${name}Tab`));
            document.getElementById('headerSubtitle').textContent =
                tab.getAttribute('title') || name;
        });
    });
}

// ── Stat counter helper ────────────────────────────────────────

function incrementStat(id) {
    const el = document.getElementById(id);
    el.textContent = parseInt(el.textContent, 10) + 1;
}

// ── Toast notifications ────────────────────────────────────────

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

// ── Clear log ──────────────────────────────────────────────────

function clearLog() {
    Logger.clear();
    resetStats();
}

// ── State persistence ──────────────────────────────────────────

async function saveState() {
    const state = {
        cookies_path: cookiesPath,
        cookies_browser: cookiesBrowser,
        auth_method: currentAuthMethod,
        archive_dir: archiveDir,
        library_root: libraryRoot,
        has_videos: hasVideos,
        last_url: document.getElementById('accountUrl').value,
        last_destination: document.getElementById('destFolder').value,
    };
    await pywebview.api.save_state(state);
}

async function loadState() {
    const state = await pywebview.api.load_state();

    if (state.auth_method) {
        setAuthMethod(state.auth_method);
    }

    if (state.cookies_path) {
        cookiesPath = state.cookies_path;
        document.getElementById('cookiesPath').value = state.cookies_path;
        // Re-validate on load
        const result = await pywebview.api.validate_cookies(state.cookies_path);
        updateCookieStatus(result);
    }

    if (state.cookies_browser) {
        cookiesBrowser = state.cookies_browser;
        document.getElementById('browserSelect').value = state.cookies_browser;
        document.getElementById('browserStatus').textContent = `${state.cookies_browser} cookies ready`;
        document.getElementById('browserStatus').className = 'status-badge status-ok';
    }

    if (state.archive_dir) {
        archiveDir = state.archive_dir;
        document.getElementById('archiveDir').value = state.archive_dir;
    }

    if (state.library_root) {
        libraryRoot = state.library_root;
        document.getElementById('libraryRoot').value = state.library_root;
    }

    if (state.has_videos) {
        hasVideos = true;
        document.getElementById('hasVideosBox').classList.add('checked');
    }

    if (state.last_url) {
        document.getElementById('accountUrl').value = state.last_url;
    }

    if (state.last_destination) {
        document.getElementById('destFolder').value = state.last_destination;
        refreshYearDropdown();
    }
}

// ── URL field: live auto-resolve destination + save ────────────

document.getElementById('accountUrl').addEventListener('input', handleUrlInput);

document.getElementById('accountUrl').addEventListener('blur', () => {
    showUrlError(false);
    resolveDestinationFromUrl();
    saveState();
});

// ── Initialize ─────────────────────────────────────────────────

window.addEventListener('pywebviewready', async function() {
    Logger.init();
    Logger.info('Downloader initialized. Ready.');
    setupTabs();
    await loadState();
    await refreshRecentArtists();

    // Coomerfans tab init
    const fullState = await pywebview.api.load_state();
    await cfLoadState(fullState);

    // Clean up .db files for artist directories that no longer exist
    if (archiveDir) {
        const cleanup = await pywebview.api.cleanup_orphaned_archives(archiveDir);
        if (cleanup.removed > 0) {
            Logger.info(`Cleaned up ${cleanup.removed} orphaned archive(s): ${cleanup.usernames.join(', ')}`);
        }
    }
});
