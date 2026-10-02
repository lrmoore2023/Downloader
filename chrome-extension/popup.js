// Hands Chrome's own rule34video session to the Downloader app's localhost
// receiver (backend/ext_bridge.py). Only rule34video.com cookies are read, and
// they go only to 127.0.0.1 — the app verifies them before keeping anything.

const APP = 'http://127.0.0.1:47834';

function setStatus(text, cls) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = cls || '';
}

async function send() {
  const btn = document.getElementById('send');
  btn.disabled = true;
  setStatus('Sending…');
  try {
    const all = await chrome.cookies.getAll({ domain: 'rule34video.com' });
    if (!all.length) {
      setStatus('No rule34video cookies — sign in on rule34video.com first.', 'err');
      return;
    }
    const cookies = all.map(c => ({
      name: c.name, value: c.value, domain: c.domain, path: c.path, secure: c.secure,
    }));
    let r;
    try {
      r = await fetch(`${APP}/pmv/session/rule34video`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cookies }),
      });
    } catch (e) {
      setStatus("Couldn't reach the Downloader — is the app open?", 'err');
      return;
    }
    const res = await r.json().catch(() => ({}));
    setStatus(res.message || (res.ok ? 'Done' : `Failed (HTTP ${r.status})`), res.ok ? 'ok' : 'err');
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('send').addEventListener('click', send);
