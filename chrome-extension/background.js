// Cross-origin JSON fetches for the content script (iwara's API lives on
// api.iwara.tv; the service worker has host permission for it).
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== 'fetchJson') return false;
  (async () => {
    try {
      const r = await fetch(msg.url, { headers: msg.headers || {}, credentials: 'omit' });
      if (!r.ok) { sendResponse({ ok: false, status: r.status }); return; }
      sendResponse({ ok: true, data: await r.json() });
    } catch (e) {
      sendResponse({ ok: false, error: String(e) });
    }
  })();
  return true;   // keep the channel open for the async reply
});
