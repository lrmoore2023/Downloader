/* ── Log Display Component ────────────────────────────────────── */

const Logger = {
    _container: null,
    _maxEntries: 1000,
    _userScrolled: false,

    init() {
        this._container = document.getElementById('logContainer');

        // Detect when the user scrolls up manually
        this._container.addEventListener('scroll', () => {
            const el = this._container;
            // "Pinned to bottom" = within 30px of the bottom edge
            this._userScrolled = (el.scrollHeight - el.scrollTop - el.clientHeight) > 30;
        });
    },

    add(message, type = 'info') {
        if (!this._container) this.init();

        const entry = document.createElement('div');
        entry.className = `log-entry log-${type}`;
        entry.textContent = message;
        this._container.appendChild(entry);

        // Trim old entries
        while (this._container.children.length > this._maxEntries) {
            this._container.removeChild(this._container.firstChild);
        }

        // Only auto-scroll if the user hasn't scrolled up
        if (!this._userScrolled) {
            this._container.scrollTop = this._container.scrollHeight;
        }
    },

    download(message) {
        this.add(message, 'download');
    },

    skip(message) {
        this.add(message, 'skip');
    },

    error(message) {
        this.add(message, 'error');
    },

    info(message) {
        this.add(message, 'info');
    },

    success(message) {
        this.add(message, 'success');
    },

    clear() {
        if (!this._container) this.init();
        this._container.innerHTML = '';
        this._userScrolled = false;
    }
};
