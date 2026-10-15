/* ── Theme runtime ───────────────────────────────────────────────
   Themes are pure CSS (css/themes/<id>.css) keyed off two attributes on <html>:
   data-theme="<id>" and data-mode="light|dark". This file only flips those
   attributes, remembers the choice, and draws the picker in Settings ▸ Appearance.

   To add a theme: write its CSS file, link it in index.html, add an entry here.
   `swatches` drive the mini preview card: [ground, panel, ink, accent]. */

const THEMES = [
    {
        id: 'brutalist',
        label: 'Brutalist',
        blurb: 'Square, ruled, monospace. Hazard-yellow signal.',
        swatches: {
            light: ['#e9e6de', '#ffffff', '#0b0b0b', '#ffd400'],
            dark: ['#0a0a0a', '#141414', '#ece9e2', '#ffd400'],
        },
    },
    {
        id: 'studio',
        label: 'Studio',
        blurb: 'Soft slate panels, rounded corners, calm teal.',
        swatches: {
            light: ['#eef1f4', '#ffffff', '#1b2129', '#1a7b70'],
            dark: ['#0b0e13', '#161a21', '#e6e8ec', '#3fb6a8'],
        },
    },
];

const THEME_DEFAULT = { theme: 'brutalist', mode: 'system', motion: 'auto' };

const Theme = (() => {
    const root = document.documentElement;
    const darkQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
    let pref = { ...THEME_DEFAULT };

    function cacheGet() {
        try { return JSON.parse(localStorage.getItem('ui-theme') || 'null'); } catch (e) { return null; }
    }
    function cacheSet(p) {
        try { localStorage.setItem('ui-theme', JSON.stringify(p)); } catch (e) { /* private mode */ }
    }

    function resolveMode(mode) {
        if (mode === 'light' || mode === 'dark') return mode;
        return darkQuery && !darkQuery.matches ? 'light' : 'dark';
    }

    function valid(p) {
        const t = THEMES.find(x => x.id === p.theme) ? p.theme : THEME_DEFAULT.theme;
        const m = ['light', 'dark', 'system'].includes(p.mode) ? p.mode : THEME_DEFAULT.mode;
        const mo = ['auto', 'reduce'].includes(p.motion) ? p.motion : THEME_DEFAULT.motion;
        return { theme: t, mode: m, motion: mo };
    }

    function paint() {
        root.dataset.theme = pref.theme;
        root.dataset.mode = resolveMode(pref.mode);
        if (pref.motion === 'reduce') root.dataset.motion = 'reduce';
        else delete root.dataset.motion;
    }

    function motionAllowed() {
        if (pref.motion === 'reduce') return false;
        return !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    }

    // Apply a (partial) preference. `animate` crossfades via View Transitions when
    // available and motion is allowed; `persist` writes it to app_state.json.
    function set(partial, { animate = true, persist = true } = {}) {
        const next = valid({ ...pref, ...partial });
        const changed = next.theme !== pref.theme || next.mode !== pref.mode || next.motion !== pref.motion;
        pref = next;
        cacheSet(pref);
        if (animate && changed && document.startViewTransition && motionAllowed()) {
            // A newer switch aborts the running transition; that's expected.
            const vt = document.startViewTransition(paint);
            [vt.ready, vt.finished, vt.updateCallbackDone].forEach(p => p && p.catch(() => {}));
            // If the browser holds the transition (no frames, e.g. window hidden),
            // still apply the theme. paint() is idempotent.
            setTimeout(paint, 300);
        } else {
            paint();
        }
        if (persist && window.pywebview && pywebview.api && pywebview.api.save_state) {
            pywebview.api.save_state({ ui_theme: pref.theme, ui_mode: pref.mode, ui_motion: pref.motion });
        }
        renderPicker();
    }

    // Saved state from app_state.json (called once the backend is ready).
    function loadFromState(state) {
        if (!state) return;
        set({
            theme: state.ui_theme || pref.theme,
            mode: state.ui_mode || pref.mode,
            motion: state.ui_motion || pref.motion,
        }, { animate: false, persist: false });
    }

    // ── Settings ▸ Appearance picker ──
    function renderPicker() {
        const grid = document.getElementById('themeGrid');
        if (!grid) return;
        const mode = resolveMode(pref.mode);
        grid.innerHTML = THEMES.map(t => {
            const [ground, panel, ink, accent] = t.swatches[mode] || t.swatches.dark;
            const on = t.id === pref.theme;
            return `<button type="button" class="theme-card${on ? ' active' : ''}" role="radio"
                        aria-checked="${on}" tabindex="${on ? 0 : -1}" data-theme-id="${t.id}"
                        onclick="Theme.set({theme: '${t.id}'})">
                <span class="theme-preview theme-preview-${t.id}" style="--p-ground:${ground};--p-panel:${panel};--p-ink:${ink};--p-accent:${accent}" aria-hidden="true">
                    <span class="tp-bar"></span>
                    <span class="tp-card"><span class="tp-line"></span><span class="tp-line short"></span><span class="tp-btn"></span></span>
                </span>
                <span class="theme-meta">
                    <span class="theme-name">${t.label}</span>
                    <span class="theme-blurb">${t.blurb}</span>
                </span>
            </button>`;
        }).join('');
        document.querySelectorAll('#themeModeSeg .seg').forEach(b => {
            const on = b.dataset.mode === pref.mode;
            b.classList.toggle('active', on);
            b.setAttribute('aria-checked', String(on));
        });
        const rm = document.getElementById('themeReduceMotion');
        if (rm) rm.checked = pref.motion === 'reduce';
    }

    // Arrow keys move between theme cards (radiogroup pattern).
    function onGridKey(e) {
        if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) return;
        const i = THEMES.findIndex(t => t.id === pref.theme);
        const step = (e.key === 'ArrowLeft' || e.key === 'ArrowUp') ? -1 : 1;
        const next = THEMES[(i + step + THEMES.length) % THEMES.length];
        e.preventDefault();
        set({ theme: next.id });
        const btn = document.querySelector(`.theme-card[data-theme-id="${next.id}"]`);
        if (btn) btn.focus();
    }

    // First paint happens synchronously here, so there is never an unthemed frame.
    // (pywebview runs private, so localStorage does not survive a restart —
    // app_state.json is the source of truth.)
    // main.py writes the saved choice to js/theme-pref.js at launch; an in-session
    // change (cache) wins over it after a reload.
    pref = valid({ ...THEME_DEFAULT, ...(window.__THEME_PREF || {}), ...(cacheGet() || {}) });
    paint();

    if (darkQuery) {
        const follow = () => { if (pref.mode === 'system') { paint(); renderPicker(); } };
        if (darkQuery.addEventListener) darkQuery.addEventListener('change', follow);
        else if (darkQuery.addListener) darkQuery.addListener(follow);
    }

    document.addEventListener('DOMContentLoaded', () => {
        renderPicker();
        const grid = document.getElementById('themeGrid');
        if (grid) grid.addEventListener('keydown', onGridKey);
    });

    return { set, loadFromState, motionAllowed, get: () => ({ ...pref }) };
})();
