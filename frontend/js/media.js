/* ── Media browser + lightbox viewer ───────────────────────────── */

let mediaItems = [];          // raw items for the open creator (newest-first)
let mediaFiltered = [];       // current filtered+sorted view (lightbox indexes into this)
let mediaCreatorId = '';
let mediaKind = 'all';        // all | image | video
let mediaSource = 'all';      // all | OF | Fansly | Twitter | Other | …
let mediaSort = 'newest';     // newest | oldest
let viewerIndex = -1;
let _mediaBase = null;        // { base, token }

async function mediaBase() {
    if (!_mediaBase) _mediaBase = await pywebview.api.media_base_url();
    return _mediaBase;
}

function mediaUrl(endpoint, path) {   // endpoint: 'media' | 'thumb'
    return `${_mediaBase.base}/${endpoint}?p=${encodeURIComponent(path)}&t=${encodeURIComponent(_mediaBase.token)}`;
}

// ── Browser ─────────────────────────────────────────────────────

async function openMediaBrowser(creatorId) {
    mediaCreatorId = creatorId;
    await mediaBase();
    const res = await pywebview.api.list_creator_media(creatorId);
    document.getElementById('mediaTitle').textContent = res.name || '';
    switchView('media');

    if (!res.reachable) {
        mediaItems = [];
        mediaFiltered = [];
        document.getElementById('mediaYear').innerHTML = '<option value="">All years</option>';
        document.getElementById('mediaCount').textContent = '';
        document.getElementById('mediaGrid').innerHTML =
            '<div class="links-empty">This folder isn’t reachable — is the NAS connected?</div>';
        return;
    }
    mediaItems = res.items || [];
    buildYearFilter();
    buildSourceFilter();
    renderMedia();
}

function buildYearFilter() {
    const years = Array.from(new Set(mediaItems.map(i => i.year))).sort((a, b) => b - a);
    document.getElementById('mediaYear').innerHTML =
        '<option value="">All years</option>' + years.map(y => `<option value="${y}">${y}</option>`).join('');
}

function buildSourceFilter() {
    mediaSource = 'all';   // reset per creator (sources differ)
    const el = document.getElementById('mediaSourceFilter');
    const srcs = Array.from(new Set(mediaItems.map(i => i.source || 'Other'))).sort();
    if (srcs.length <= 1) { el.innerHTML = ''; return; }   // single source → no filter needed
    const opts = [['all', 'All'], ...srcs.map(s => [s, s])];
    el.innerHTML = '';
    opts.forEach(([val, label]) => {
        const b = document.createElement('button');
        b.className = 'seg' + (val === mediaSource ? ' active' : '');
        b.dataset.src = val;
        b.textContent = label;
        b.onclick = () => setMediaSource(val);
        el.appendChild(b);
    });
}

function setMediaSource(s) {
    mediaSource = s;
    document.querySelectorAll('#mediaSourceFilter .seg').forEach(b => b.classList.toggle('active', b.dataset.src === s));
    renderMedia();
}

function setMediaKind(k) {
    mediaKind = k;
    document.querySelectorAll('#mediaKind .seg').forEach(b => b.classList.toggle('active', b.dataset.kind === k));
    renderMedia();
}

function setMediaSort(s) {
    mediaSort = s;
    document.querySelectorAll('#mediaSort .seg').forEach(b => b.classList.toggle('active', b.dataset.sort === s));
    renderMedia();
}

function currentFiltered() {
    const yr = document.getElementById('mediaYear').value;
    const list = mediaItems.filter(i =>
        (mediaKind === 'all' || i.kind === mediaKind) &&
        (mediaSource === 'all' || (i.source || 'Other') === mediaSource) &&
        (!yr || String(i.year) === yr));
    const dir = mediaSort === 'newest' ? -1 : 1;
    return list.sort((a, b) => dir * ((a.date.localeCompare(b.date)) || a.filename.localeCompare(b.filename)));
}

function renderMedia() {
    mediaFiltered = currentFiltered();
    const grid = document.getElementById('mediaGrid');
    document.getElementById('mediaCount').textContent =
        `${mediaFiltered.length} item${mediaFiltered.length === 1 ? '' : 's'}`;

    if (!mediaFiltered.length) {
        grid.innerHTML = '<div class="links-empty">No media matches this filter.</div>';
        return;
    }
    grid.innerHTML = mediaFiltered.map((it, i) => `
        <div class="media-tile" data-i="${i}">
            <img class="media-thumb" loading="lazy" src="${mediaUrl('thumb', it.path)}" alt="" onerror="this.classList.add('broken')">
            ${it.kind === 'video' ? '<span class="media-badge">▶</span>' : ''}
        </div>`).join('');
    grid.querySelectorAll('.media-tile').forEach(t =>
        t.addEventListener('click', () => openViewer(parseInt(t.dataset.i, 10))));
}

function manageCurrentCreator() {
    const sel = document.getElementById('creatorSelect');
    if (![...sel.options].some(o => o.value === mediaCreatorId)) populateCreatorDropdown(mediaCreatorId);
    sel.value = mediaCreatorId;
    onCreatorChange();
    switchView('creator');
}

// ── Lightbox ─────────────────────────────────────────────────────

function openViewer(index) {
    if (index < 0 || index >= mediaFiltered.length) return;
    viewerIndex = index;
    document.getElementById('mediaViewer').classList.add('visible');
    showViewerItem();
}

function _teardownStageVideo() {
    const v = document.querySelector('#lightboxStage video');
    if (v) { try { v.pause(); v.removeAttribute('src'); v.load(); } catch (e) { /* ignore */ } }
}

function showViewerItem() {
    const it = mediaFiltered[viewerIndex];
    if (!it) return;
    _teardownStageVideo();
    const stage = document.getElementById('lightboxStage');
    stage.innerHTML = '';

    let el;
    if (it.kind === 'video') {
        el = document.createElement('video');
        el.src = mediaUrl('media', it.path);
        el.controls = true;
        el.autoplay = true;
        el.playsInline = true;
    } else {
        el = document.createElement('img');
        el.src = mediaUrl('media', it.path);
        el.alt = '';
    }
    el.className = 'lightbox-media';
    stage.appendChild(el);

    document.getElementById('lightboxCaption').textContent =
        `${it.filename}  ·  ${it.date}${it.source ? '  ·  ' + it.source : ''}  ·  ${viewerIndex + 1}/${mediaFiltered.length}`;
    preloadNeighbors();
}

function preloadNeighbors() {
    [viewerIndex - 1, viewerIndex + 1].forEach(i => {
        const it = mediaFiltered[i];
        if (it && it.kind === 'image') { const im = new Image(); im.src = mediaUrl('media', it.path); }
    });
}

function viewerPrev() { if (viewerIndex > 0) { viewerIndex--; showViewerItem(); } }
function viewerNext() { if (viewerIndex < mediaFiltered.length - 1) { viewerIndex++; showViewerItem(); } }

function closeViewer() {
    _teardownStageVideo();
    document.getElementById('lightboxStage').innerHTML = '';
    document.getElementById('mediaViewer').classList.remove('visible');
}

// Click the dim backdrop (not the media) to close.
document.getElementById('lightboxStage').addEventListener('click', e => {
    if (e.target.id === 'lightboxStage') closeViewer();
});

// Keyboard nav — capture phase so arrows navigate instead of seeking a focused video.
document.addEventListener('keydown', e => {
    if (!document.getElementById('mediaViewer').classList.contains('visible')) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); e.stopPropagation(); viewerPrev(); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); e.stopPropagation(); viewerNext(); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeViewer(); }
}, true);
