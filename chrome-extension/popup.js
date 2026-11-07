// Hands Chrome's own sign-ins to the Downloader app's localhost receiver
// (backend/ext_bridge.py):
//  * rule34video — only rule34video.com cookies are read;
//  * Discord — the token background.js saw on discord.com's own API calls.
// Both go only to 127.0.0.1, and the app checks them before keeping anything.

const APP = 'http://127.0.0.1:47834';

function setStatus(text, cls) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = cls || '';
}

async function post(platform, payload) {
  let r;
  try {
    r = await fetch(`${APP}/pmv/session/${platform}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    setStatus("Couldn't reach the Downloader — is the app open?", 'err');
    return;
  }
  const res = await r.json().catch(() => ({}));
  setStatus(res.message || (res.ok ? 'Done' : `Failed (HTTP ${r.status})`), res.ok ? 'ok' : 'err');
}

async function sendR34() {
  const all = await chrome.cookies.getAll({ domain: 'rule34video.com' });
  if (!all.length) {
    setStatus('No rule34video cookies — sign in on rule34video.com first.', 'err');
    return;
  }
  const cookies = all.map(c => ({
    name: c.name, value: c.value, domain: c.domain, path: c.path, secure: c.secure,
  }));
  await post('rule34video', { cookies });
}

async function sendDiscord() {
  const { discordToken } = await chrome.storage.session.get('discordToken');
  if (!discordToken) {
    setStatus('No Discord token seen yet — open (or reload) discord.com in a tab, signed in, then click again.', 'err');
    return;
  }
  await post('discord', { token: discordToken });
}

function wire(id, fn) {
  const btn = document.getElementById(id);
  btn.addEventListener('click', async () => {
    document.querySelectorAll('button').forEach(b => { b.disabled = true; });
    setStatus('Sending…');
    try {
      await fn();
    } catch (e) {
      setStatus(String(e), 'err');
    } finally {
      document.querySelectorAll('button').forEach(b => { b.disabled = false; });
    }
  });
}

wire('send', sendR34);
wire('sendDiscord', sendDiscord);
