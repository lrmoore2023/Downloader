/* ── Shared UI behaviour ─────────────────────────────────────────
   Dialog open/close (animation, focus in, Tab trap, focus return), busy buttons,
   and keyboard support for tab strips. Loaded before the feature scripts. */

const UI = (() => {
    const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), '
        + 'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    const openers = new Map();      // overlay id → element focused before it opened

    function visible(el) {
        return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    }

    function focusables(root) {
        return [...root.querySelectorAll(FOCUSABLE)].filter(visible);
    }

    // Open a .modal-overlay / .lightbox. `focusSel` picks the element to focus;
    // otherwise the first text field, else the first control, else the dialog.
    function openOverlay(id, focusSel) {
        const ov = document.getElementById(id);
        if (!ov) return;
        const wasOpen = ov.classList.contains('visible') && !ov.classList.contains('closing');
        ov.classList.remove('closing');
        ov.classList.add('visible');
        if (wasOpen) return;
        if (document.activeElement && document.activeElement !== document.body) {
            openers.set(id, document.activeElement);
        }
        // The overlay is display:flex as of the line above, so focus can move now.
        const dlg = ov.querySelector('.modal') || ov;
        let target = focusSel ? ov.querySelector(focusSel) : null;
        if (!target) target = dlg.querySelector('.modal-body input:not([readonly]):not([type="checkbox"]):not([type="range"]), .modal-body textarea');
        if (!target) target = focusables(dlg)[0];
        if (!target) { dlg.setAttribute('tabindex', '-1'); target = dlg; }
        try { target.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
    }

    // Close with the exit animation, then hide and hand focus back.
    function closeOverlay(id) {
        const ov = typeof id === 'string' ? document.getElementById(id) : id;
        if (!ov || !ov.classList.contains('visible') || ov.classList.contains('closing')) return;
        const key = ov.id;
        let done = false;
        const finish = () => {
            if (done) return;
            done = true;
            ov.classList.remove('visible', 'closing');
            const back = openers.get(key);
            openers.delete(key);
            if (back && document.contains(back) && visible(back)) {
                try { back.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
            }
        };
        ov.classList.add('closing');
        ov.addEventListener('animationend', function onEnd(e) {
            if (e.target !== ov) return;
            ov.removeEventListener('animationend', onEnd);
            finish();
        });
        setTimeout(finish, 400);    // fallback if no animation runs
    }

    function isOpen(id) {
        const ov = document.getElementById(id);
        return !!ov && ov.classList.contains('visible') && !ov.classList.contains('closing');
    }

    function topOverlay() {
        const open = [...document.querySelectorAll('.modal-overlay.visible:not(.closing), .lightbox.visible:not(.closing)')];
        return open[open.length - 1] || null;
    }

    // Keep Tab inside the top-most open dialog.
    document.addEventListener('keydown', e => {
        if (e.key !== 'Tab') return;
        const ov = topOverlay();
        if (!ov) return;
        const items = focusables(ov);
        if (!items.length) { e.preventDefault(); return; }
        const first = items[0], last = items[items.length - 1];
        if (!ov.contains(document.activeElement)) { e.preventDefault(); first.focus(); return; }
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });

    // Busy button: spinner + disabled + aria-busy. Safe to call with null.
    function busy(btn, on) {
        if (typeof btn === 'string') btn = document.getElementById(btn);
        if (!btn) return;
        btn.classList.toggle('is-loading', !!on);
        btn.disabled = !!on;
        if (on) btn.setAttribute('aria-busy', 'true');
        else btn.removeAttribute('aria-busy');
    }

    // Replay the view entrance animation on a just-revealed element.
    function enter(el) {
        if (!el) return;
        el.classList.remove('view-enter');
        void el.offsetWidth;        // restart the animation
        el.classList.add('view-enter');
    }

    // Roving focus for a tab strip: ←/→ (and Home/End) move between tabs and
    // activate them, matching the WAI-ARIA tabs pattern.
    function tablist(container, tabSel) {
        if (!container) return;
        container.addEventListener('keydown', e => {
            const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
            if (!keys.includes(e.key)) return;
            const tabs = [...container.querySelectorAll(tabSel)].filter(visible);
            const i = tabs.indexOf(document.activeElement);
            if (i < 0) return;
            e.preventDefault();
            let n = i;
            if (e.key === 'ArrowLeft') n = (i - 1 + tabs.length) % tabs.length;
            if (e.key === 'ArrowRight') n = (i + 1) % tabs.length;
            if (e.key === 'Home') n = 0;
            if (e.key === 'End') n = tabs.length - 1;
            tabs[n].focus();
            tabs[n].click();
        });
    }

    // Reflect an "active" class as aria-selected + roving tabindex.
    function syncTabs(container, tabSel) {
        if (!container) return;
        container.querySelectorAll(tabSel).forEach(t => {
            const on = t.classList.contains('active');
            t.setAttribute('aria-selected', String(on));
            t.tabIndex = on ? 0 : -1;
        });
    }

    return { openOverlay, closeOverlay, isOpen, topOverlay, busy, enter, tablist, syncTabs };
})();
