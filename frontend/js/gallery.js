/* ── Gallery: tile grid of creators (avatar + name + chips) ─────── */

const _avatarCache = {};   // creator id -> data URI ('' = no avatar)

function chipsFromSummary(s) {
    const out = [];
    if (s.onlyfans) out.push(chipHtml('OF', s.onlyfans));
    if (s.fansly) out.push(chipHtml('Fansly', s.fansly));
    if (s.patreon) out.push(chipHtml('Patreon', s.patreon));
    if (s.fanbox) out.push(chipHtml('Fanbox', s.fanbox));
    if (s.twitter) out.push('<span class="chip chip-twitter">Twitter</span>');
    if (s.derpibooru) out.push(chipHtml('Derpibooru', s.derpibooru));
    return out.join('');
}

function renderGallery() {
    const grid = document.getElementById('tileGrid');
    if (!grid) return;

    const q = (document.getElementById('gallerySearch').value || '').trim().toLowerCase();
    const list = applySort(creatorsCache.filter(c =>
        (currentCategory === 'All' || c.category === currentCategory) &&
        (!q || (c.name || '').toLowerCase().includes(q))));

    const count = document.getElementById('galleryCount');
    if (count) count.textContent = `${list.length} creator${list.length === 1 ? '' : 's'}`;

    if (!list.length) {
        grid.innerHTML = '<div class="links-empty">No creators match this filter.</div>';
        return;
    }

    grid.innerHTML = list.map((c, i) => `
        <div class="creator-tile" data-id="${escapeHtml(c.id)}" title="${escapeHtml(c.name)}">
            <div class="tile-avatar" id="tileav${i}">${escapeHtml(initials(c.name))}</div>
            <div class="tile-name">${escapeHtml(c.name)}</div>
            <div class="tile-chips">${chipsFromSummary(c.summary || {})}</div>
        </div>`).join('');

    grid.querySelectorAll('.creator-tile').forEach(t =>
        t.addEventListener('click', () => openMediaBrowser(t.dataset.id)));

    list.forEach((c, i) => loadTileAvatar(c.id, 'tileav' + i));
}

function loadTileAvatar(id, elId) {
    const apply = (uri) => {
        const el = document.getElementById(elId);
        if (el && uri) { el.classList.add('has-img'); el.innerHTML = `<img src="${uri}" alt="">`; }
    };
    if (id in _avatarCache) { apply(_avatarCache[id]); return; }
    pywebview.api.get_creator_avatar(id).then(r => {
        _avatarCache[id] = (r && r.data) ? r.data : '';
        apply(_avatarCache[id]);
    });
}

function openCreatorFromGallery(id) {
    const sel = document.getElementById('creatorSelect');
    // The dropdown is filtered by the same category, so the option should exist;
    // rebuild targeting this id just in case, then select + load.
    if (![...sel.options].some(o => o.value === id)) populateCreatorDropdown(id);
    sel.value = id;
    onCreatorChange();
    switchView('creator');
}
