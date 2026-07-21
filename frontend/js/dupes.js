/* ── Duplicates / similar-media panel ──────────────────────────────
 * Scans the selected creator (or two arbitrary folders) for exact + perceptually
 * similar images/videos, shows matched groups for manual sign-off, and applies the
 * chosen actions reversibly. Mirrors czkawka: nothing is deleted without review.
 */

let dupeMode = 'within';               // within | new | ref_check
let dupeBase = null;                   // { base, token } for thumbnails
let dupeGroups = [];                   // last scan's groups
let dupeUnmatched = [];                // _new files with no match
let dupeAllItems = [];                 // flat list of every shown item (for the lightbox)
let dupeRefDir = '';
let dupeCheckDir = '';
let dupeLastJournal = null;            // basename of the last apply journal (for Undo)
let dupePendingGroup = null;           // group id when applying a single group (vs all)
let dupeGroupMode = {};                // gid -> active action mode
let dupeBusy = false;

// czkawka similarity presets for hash size 16 (Hamming out of 256).
function dupePresetLabel(v) {
    v = parseInt(v, 10);
    if (v <= 2) return 'Very High';
    if (v <= 5) return 'High';
    if (v <= 10) return 'Krokiet default';
    if (v <= 15) return 'Medium';
    if (v <= 30) return 'Small';
    return 'Minimal';
}

function dupeDistInput(v) {
    document.getElementById('dupeDistVal').textContent = v;
    const el = document.getElementById('dupeDistPreset');
    if (el) el.textContent = dupePresetLabel(v);
}

const DUPE_MODE_HINTS = {
    within: 'Find duplicate & near-duplicate files inside this creator’s own library.',
    new: 'Process this creator’s <code>_new</code> folder: de-dupe the dropped files, then match them against the library. Higher-quality matches adopt the proper name; the rest you place yourself.',
    ref_check: 'Match a Reference folder against a Check folder. Matched Check files can be renamed to the Reference name, or retired to <code>_deleted</code>.',
};

function renderDupeHeader() {
    const el = document.getElementById('dupeCreatorName');
    if (dupeMode === 'ref_check') {
        el.textContent = 'Reference vs Check';
    } else if (typeof currentCreator === 'object' && currentCreator) {
        el.textContent = currentCreator.name || currentCreatorId || 'Creator';
    } else {
        el.textContent = 'No creator selected';
    }
    document.getElementById('dupeModeHint').innerHTML = DUPE_MODE_HINTS[dupeMode] || '';
}

function setDupeMode(mode) {
    dupeMode = mode;
    document.querySelectorAll('#dupeMode .seg')
        .forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
    document.getElementById('dupeRefCheck').style.display =
        (mode === 'ref_check') ? 'flex' : 'none';
    // Rotation matching is on by default for the messy _new dump.
    document.getElementById('dupeOrient').checked = (mode === 'new');
    renderDupeHeader();
}

async function pickDupeFolder(which) {
    const path = await pywebview.api.select_folder();
    if (!path) return;
    if (which === 'ref') {
        dupeRefDir = path;
        document.getElementById('dupeRefPath').textContent = path;
    } else {
        dupeCheckDir = path;
        document.getElementById('dupeCheckPath').textContent = path;
    }
}

function dupeThumb(path) {
    if (!dupeBase) return '';
    return `${dupeBase.base}/thumb?p=${encodeURIComponent(path)}&t=${encodeURIComponent(dupeBase.token)}`;
}

function setDupeBusy(busy, label) {
    dupeBusy = busy;
    document.getElementById('dupeScanBtn').style.display = busy ? 'none' : '';
    document.getElementById('dupeCancelBtn').style.display = busy ? '' : 'none';
    document.getElementById('dupeStatus').textContent = label || '';
}

async function startDupeScan() {
    if (dupeBusy) return;
    const params = {
        image_distance: parseInt(document.getElementById('dupeDistance').value, 10),
        orientations: document.getElementById('dupeOrient').checked,
    };
    if (dupeMode === 'ref_check') {
        if (!dupeRefDir || !dupeCheckDir) { showToast('Pick both folders first', 'error'); return; }
        params.ref_dir = dupeRefDir;
        params.check_dir = dupeCheckDir;
    } else if (!currentCreatorId) {
        showToast('Select a creator first', 'error');
        return;
    }
    document.getElementById('dupeResults').innerHTML = '';
    document.getElementById('dupeApplyBar').style.display = 'none';
    setDupeBusy(true, 'Scanning…');
    const res = await pywebview.api.start_dupe_scan(currentCreatorId, dupeMode, params);
    if (res && res.error) { setDupeBusy(false); showToast(res.error, 'error'); }
}

function cancelDupe() {
    pywebview.api.cancel_dupe();
    document.getElementById('dupeStatus').textContent = 'Cancelling…';
}

// ── progress + results (pushed from Python) ─────────────────────────
window.onDupeProgress = function (data) {
    if (data.message) document.getElementById('dupeStatus').textContent = data.message;
};

window.onDupeScanResult = function (data) {
    setDupeBusy(false, '');
    if (data.error) { showToast('Scan failed: ' + data.error, 'error'); return; }
    if (!data.reachable) {
        document.getElementById('dupeResults').innerHTML =
            '<div class="links-empty">Folder isn’t reachable — is the NAS connected?</div>';
        return;
    }
    dupeBase = { base: data.base, token: data.token };
    dupeGroups = data.groups || [];
    dupeUnmatched = data.unmatched_new || [];
    renderDupeResults(data);
};

function renderDupeResults(data) {
    const root = document.getElementById('dupeResults');
    const applyBar = document.getElementById('dupeApplyBar');
    if (!dupeGroups.length && !dupeUnmatched.length) {
        root.innerHTML = `<div class="links-empty">No duplicates or similar files found${data.note ? ' (' + data.note + ')' : ''}.</div>`;
        applyBar.style.display = 'none';
        return;
    }
    dupeAllItems = [];                  // rebuilt as groups render (lightbox source)
    const parts = [];
    parts.push(`<div class="dupe-summary" id="dupeSummary">${dupeGroups.length} group(s) · ${data.file_count || 0} file(s)`
        + (dupeUnmatched.length ? ` · ${dupeUnmatched.length} unmatched new` : '') + '</div>');
    dupeGroups.forEach(g => parts.push(renderDupeGroup(g)));
    if (dupeUnmatched.length) parts.push(renderUnmatched());
    root.innerHTML = parts.join('');
    attachDupeItemHandlers();
    applyBar.style.display = dupeGroups.length ? 'flex' : 'none';
    document.getElementById('dupeApplyHint').textContent =
        'Click a thumbnail to view large (arrow keys cycle). Right-click → Reveal in Explorer. Apply per group or all at once — retired files go to _deleted (undoable).';
}

// Register an item in the flat lightbox list; returns its index.
function registerDupeItem(it) {
    return dupeAllItems.push({
        path: it.path, kind: it.kind, filename: it.filename,
        date: it.year ? String(it.year) : '', source: it.source || '',
    }) - 1;
}

function tierBadge(g) {
    const cls = g.tier === 'identical' ? 'dupe-badge-identical' : 'dupe-badge-similar';
    const label = g.tier === 'identical' ? 'IDENTICAL' : 'SIMILAR — verify';
    return `<span class="dupe-badge ${cls}">${label}</span>`;
}

function bucketBadge(b) {
    if (!b || b === 'existing') return '';
    const map = { new: 'NEW', ref: 'REFERENCE', check: 'CHECK' };
    return `<span class="dupe-bucket dupe-bucket-${b}">${map[b] || b}</span>`;
}

function fmtSize(n) {
    if (!n) return '';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(0) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
}

function fmtDuration(secs) {
    if (!secs && secs !== 0) return '';
    secs = Math.round(secs);
    const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
    const mm = h ? String(m).padStart(2, '0') : String(m);
    return (h ? h + ':' : '') + mm + ':' + String(s).padStart(2, '0');
}

function itemMeta(it) {
    const dim = (it.width && it.height) ? `${it.width}×${it.height}` : (it.kind === 'video' ? 'video' : '');
    const dur = it.kind === 'video' ? fmtDuration(it.duration) : '';
    return [dim, dur, fmtSize(it.size)].filter(Boolean).join(' · ');
}

// Mode set offered per group, derived from the suggested action. `picks` means the
// items carry keep-checkboxes (keep-one / keep-multiple / delete-all selection).
function groupModes(g) {
    switch (g.suggested.action) {
        case 'keep_one':
            return { modes: [['keep_one', 'Keep 1'], ['keep_multi', 'Keep multiple'],
                             ['delete_all', 'Delete all'], ['skip', 'Skip']],
                     def: 'keep_one', picks: true };
        case 'rename_to_ref':
            return { modes: [['rename_to_ref', 'Rename → reference'],
                             ['delete_check', 'Delete Check'], ['skip', 'Skip']],
                     def: 'rename_to_ref', picks: false };
        case 'adopt':
            return { modes: [['adopt', 'Use higher-quality _new'],
                             ['keep_existing', 'Keep existing'], ['skip', 'Skip']],
                     def: 'adopt', picks: false };
        default:
            return { modes: [['skip', 'Skip (manual — see reason)']], def: 'skip', picks: false };
    }
}

function bucketDeletable(bucket) {
    if (dupeMode === 'within') return true;
    if (dupeMode === 'ref_check') return bucket === 'check';
    if (dupeMode === 'new') return bucket === 'new';
    return false;
}

function renderDupeGroup(g) {
    const cfg = groupModes(g);
    dupeGroupMode[g.id] = cfg.def;
    const items = g.items.map((it) => {
        const isBest = it.is_best;
        const di = registerDupeItem(it);
        const pick = cfg.picks
            ? `<label class="dupe-keep"><input type="checkbox" class="dupe-pick" data-path="${encodeURIComponent(it.path)}" ${isBest ? 'checked' : ''} onchange="onDupePick('${g.id}', this)"> keep</label>`
            : '';
        return `
            <div class="dupe-item ${isBest ? 'is-best' : ''}" data-di="${di}" data-path="${encodeURIComponent(it.path)}">
                <div class="dupe-thumb-wrap" title="Click to view · right-click for options">
                    <img class="dupe-thumb" loading="lazy" src="${dupeThumb(it.path)}" alt="" onerror="this.classList.add('broken')">
                    ${it.kind === 'video' ? '<span class="media-badge">▶</span>' : ''}
                    ${isBest ? '<span class="dupe-best">BEST</span>' : ''}
                    <span class="dupe-del-tag">→ _deleted</span>
                </div>
                <div class="dupe-item-meta">
                    ${bucketBadge(it.bucket)}
                    <span class="dupe-fn" title="${it.filename}">${it.filename}</span>
                    <span class="dupe-dims">${itemMeta(it)}</span>
                    ${pick}
                </div>
            </div>`;
    }).join('');
    const segs = cfg.modes.map(([v, label]) =>
        `<button class="seg ${v === cfg.def ? 'active' : ''}" data-mode="${v}" onclick="setDupeGroupMode('${g.id}', '${v}')">${label}</button>`).join('');
    return `
        <div class="dupe-group" data-gid="${g.id}">
            <div class="dupe-group-head">
                ${tierBadge(g)}
                <span class="dupe-kind">${g.kind}</span>
                <div class="segmented dupe-modes" id="modes-${g.id}">${segs}</div>
                <button class="btn btn-secondary btn-sm dupe-group-apply" onclick="applyDupeGroup('${g.id}')">Apply</button>
                <span class="dupe-reason">${g.suggested.reason || ''}</span>
            </div>
            <div class="dupe-items">${items}</div>
        </div>`;
}

// ── per-group mode + keep selection ─────────────────────────────────
function setDupeGroupMode(gid, mode) {
    dupeGroupMode[gid] = mode;
    const card = document.querySelector(`.dupe-group[data-gid="${gid}"]`);
    if (!card) return;
    card.querySelectorAll(`#modes-${CSS.escape(gid)} .seg`)
        .forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
    const picks = [...card.querySelectorAll('.dupe-pick')];
    if (mode === 'delete_all') {
        picks.forEach(p => { p.checked = false; p.disabled = true; });
    } else if (mode === 'keep_one' || mode === 'keep_multi') {
        picks.forEach(p => { p.disabled = false; });
        if (mode === 'keep_one') {                 // collapse to a single keeper
            const checked = picks.filter(p => p.checked);
            const keep = checked[0] || picks[0];
            picks.forEach(p => { p.checked = (p === keep); });
        }
    } else {
        picks.forEach(p => { p.disabled = true; });  // rename/adopt/skip: selection n/a
    }
    refreshGroupMarks(gid);
}

function onDupePick(gid, cb) {
    const mode = dupeGroupMode[gid];
    const card = document.querySelector(`.dupe-group[data-gid="${gid}"]`);
    if (!card) return;
    const picks = [...card.querySelectorAll('.dupe-pick')];
    if (mode === 'keep_one') {
        if (cb.checked) picks.forEach(p => { if (p !== cb) p.checked = false; });
        else if (!picks.some(p => p.checked)) cb.checked = true;   // keep-one needs exactly one
    }
    refreshGroupMarks(gid);
}

// Toggle the "→ _deleted" tag on items that the current mode will retire.
function refreshGroupMarks(gid) {
    const g = dupeGroups.find(x => x.id === gid);
    const card = document.querySelector(`.dupe-group[data-gid="${gid}"]`);
    if (!g || !card) return;
    const mode = dupeGroupMode[gid];
    const checked = new Set([...card.querySelectorAll('.dupe-pick:checked')]
        .map(p => decodeURIComponent(p.dataset.path)));
    g.items.forEach(it => {
        const el = card.querySelector(`.dupe-item[data-path="${cssAttr(it.path)}"]`);
        if (!el) return;
        let del = false;
        if (mode === 'keep_one' || mode === 'keep_multi' || mode === 'delete_all') {
            del = bucketDeletable(it.bucket) && !checked.has(it.path);
        } else if (mode === 'delete_check') {
            del = it.bucket === 'check';
        } else if (mode === 'keep_existing') {
            del = it.bucket === 'new';
        } else if (mode === 'adopt') {
            del = it.bucket !== 'new';        // the existing copy is retired
        }
        el.classList.toggle('marked-delete', del);
    });
}

function cssAttr(path) {
    return encodeURIComponent(path).replace(/"/g, '\\"');
}

function renderUnmatched() {
    const items = dupeUnmatched.map(it => {
        const di = registerDupeItem(it);
        return `
        <div class="dupe-item" data-di="${di}" data-path="${encodeURIComponent(it.path)}">
            <div class="dupe-thumb-wrap" title="Click to view · right-click for options">
                <img class="dupe-thumb" loading="lazy" src="${dupeThumb(it.path)}" alt="" onerror="this.classList.add('broken')">
                ${it.kind === 'video' ? '<span class="media-badge">▶</span>' : ''}
            </div>
            <div class="dupe-item-meta">
                <span class="dupe-fn" title="${it.filename}">${it.filename}</span>
                <span class="dupe-dims">${itemMeta(it)}</span>
            </div>
        </div>`;
    }).join('');
    return `
        <div class="dupe-group dupe-unmatched">
            <div class="dupe-group-head">
                <span class="dupe-badge dupe-badge-new">NEW · UNMATCHED</span>
                <span class="dupe-reason">No match in the library — left in _new for you to name & place.</span>
            </div>
            <div class="dupe-items">${items}</div>
        </div>`;
}

// ── item interactions: large viewer + right-click menu ──────────────
function attachDupeItemHandlers() {
    document.querySelectorAll('#dupeResults .dupe-item').forEach(el => {
        const di = parseInt(el.dataset.di, 10);
        const path = decodeURIComponent(el.dataset.path || '');
        const wrap = el.querySelector('.dupe-thumb-wrap');
        if (wrap) wrap.addEventListener('click', () => openDupeViewer(di));
        el.addEventListener('contextmenu', e => {
            e.preventDefault();
            showDupeCtxMenu(e.pageX, e.pageY, path, di);
        });
    });
    dupeGroups.forEach(g => refreshGroupMarks(g.id));   // show initial "→ _deleted" tags
}

// Reuse the media browser's lightbox (large overlay + arrow-key cycling).
function openDupeViewer(index) {
    if (index < 0 || index >= dupeAllItems.length) return;
    _mediaBase = dupeBase;            // shared binding from media.js
    mediaFiltered = dupeAllItems;     // arrow keys / prev-next index into this
    openViewer(index);
}

// ── minimal right-click context menu ────────────────────────────────
function ensureCtxMenu() {
    let m = document.getElementById('dupeCtxMenu');
    if (!m) {
        m = document.createElement('div');
        m.id = 'dupeCtxMenu';
        m.className = 'dupe-ctx';
        document.body.appendChild(m);
        document.addEventListener('click', hideDupeCtxMenu);
        document.addEventListener('scroll', hideDupeCtxMenu, true);
        window.addEventListener('blur', hideDupeCtxMenu);
    }
    return m;
}

function showDupeCtxMenu(x, y, path, di) {
    const m = ensureCtxMenu();
    m.innerHTML = '';
    const add = (label, fn) => {
        const b = document.createElement('button');
        b.className = 'dupe-ctx-item';
        b.textContent = label;
        b.onclick = () => { hideDupeCtxMenu(); fn(); };
        m.appendChild(b);
    };
    add('Open file (reveal in Explorer)', () => pywebview.api.show_in_explorer(path));
    add('View large', () => openDupeViewer(di));
    m.style.left = Math.min(x, window.innerWidth - 240) + 'px';
    m.style.top = Math.min(y, window.innerHeight - 90) + 'px';
    m.style.display = 'block';
}

function hideDupeCtxMenu() {
    const m = document.getElementById('dupeCtxMenu');
    if (m) m.style.display = 'none';
}

// ── build decisions from the DOM and apply ──────────────────────────
function buildDecisionForGroup(g) {
    const mode = dupeGroupMode[g.id] || 'skip';
    const s = g.suggested || {};
    const card = document.querySelector(`.dupe-group[data-gid="${g.id}"]`);
    const checked = card
        ? [...card.querySelectorAll('.dupe-pick:checked')].map(p => decodeURIComponent(p.dataset.path))
        : [];
    if (mode === 'keep_one' || mode === 'keep_multi')
        return { group_id: g.id, action: 'keep_many', keep_paths: checked };
    if (mode === 'delete_all')
        return { group_id: g.id, action: 'keep_many', keep_paths: [] };
    if (mode === 'delete_check')       // ref/check: retire check items, keep reference
        return { group_id: g.id, action: 'keep_many', keep_paths: [] };
    if (mode === 'keep_existing')      // _new: retire the _new copy, keep existing
        return { group_id: g.id, action: 'keep_many', keep_paths: [s.target_path] };
    if (mode === 'rename_to_ref')
        return { group_id: g.id, action: 'rename_to_ref', reference_name: s.reference_name };
    if (mode === 'adopt')
        return { group_id: g.id, action: 'adopt', new_path: s.new_path,
                 target_path: s.target_path, db_path: s.db_path, entry: s.entry };
    return { group_id: g.id, action: 'skip' };
}

function collectDecisions() {
    return dupeGroups.map(buildDecisionForGroup);
}

async function applyDupe() {
    if (dupeBusy) return;
    const decisions = collectDecisions().filter(d => d.action !== 'skip');
    if (!decisions.length) { showToast('Nothing selected to apply', 'info'); return; }
    if (!confirm(`Apply ${decisions.length} action(s)? Retired files go to _deleted and can be undone.`)) return;
    dupePendingGroup = null;
    setDupeBusy(true, 'Applying…');
    const res = await pywebview.api.start_dupe_apply(decisions);
    if (res && res.error) { setDupeBusy(false); showToast(res.error, 'error'); }
}

async function applyDupeGroup(gid) {
    if (dupeBusy) return;
    const g = dupeGroups.find(x => x.id === gid);
    if (!g) return;
    const d = buildDecisionForGroup(g);
    if (d.action === 'skip') { showToast('This group is set to Skip', 'info'); return; }
    dupePendingGroup = gid;
    setDupeBusy(true, 'Applying group…');
    const res = await pywebview.api.start_dupe_apply([d]);
    if (res && res.error) { setDupeBusy(false); dupePendingGroup = null; showToast(res.error, 'error'); }
}

function removeGroupCard(gid) {
    const card = document.querySelector(`.dupe-group[data-gid="${gid}"]`);
    if (card) card.remove();
    dupeGroups = dupeGroups.filter(g => g.id !== gid);
    const sum = document.getElementById('dupeSummary');
    if (sum) sum.textContent = `${dupeGroups.length} group(s) remaining`;
    if (!dupeGroups.length) {
        document.getElementById('dupeApplyBar').style.display = 'none';
    }
}

window.onDupeApplyProgress = function (data) {
    if (data.message) document.getElementById('dupeStatus').textContent = data.message;
};

window.onDupeApplyComplete = function (data) {
    setDupeBusy(false, '');
    const wasGroup = dupePendingGroup;
    dupePendingGroup = null;
    if (data.error) { showToast('Apply failed: ' + data.error, 'error'); return; }
    const summary = `Done — deleted ${data.deleted}, renamed ${data.renamed}, adopted ${data.adopted}`
        + (data.errors && data.errors.length ? `, ${data.errors.length} error(s)` : '');
    showToast(summary, data.errors && data.errors.length ? 'info' : 'success');
    dupeLastJournal = data.journal || null;
    document.getElementById('dupeUndoBtn').style.display = dupeLastJournal ? '' : 'none';
    document.getElementById('dupeStatus').textContent = summary + '.';

    if (wasGroup) {
        // Per-group apply: drop just that card, keep reviewing the rest.
        removeGroupCard(wasGroup);
    } else {
        // Whole-batch apply: the on-disk state changed, so clear the stale result.
        document.getElementById('dupeResults').innerHTML =
            '<div class="links-empty">Applied. Re-scan to see the updated state, or Undo to roll back.</div>';
        document.getElementById('dupeApplyBar').style.display = 'none';
    }
};

async function undoDupe() {
    if (!dupeLastJournal) return;
    if (!confirm('Undo the last apply? Restores files and archive to before the apply.')) return;
    const res = await pywebview.api.dupe_revert(dupeLastJournal);
    if (res && res.error) { showToast(res.error, 'error'); return; }
    showToast(`Undone — restored ${res.restored} item(s)`, 'success');
    document.getElementById('dupeUndoBtn').style.display = 'none';
    dupeLastJournal = null;
    document.getElementById('dupeStatus').textContent = 'Reverted. Re-scan to refresh.';
}
