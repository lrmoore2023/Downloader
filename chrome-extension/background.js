// Cross-origin JSON fetches for the content script (iwara's API lives on
// api.iwara.tv; the service worker has host permission for it).
//
// iwara: many videos are only visible to a logged-in account and 404
// anonymously. The site stores the user's long-lived token in localStorage
// ("token") and exchanges it for a ~1h access token via POST /user/token before
// each API call. The content script passes that user token along and this
// worker performs the same exchange, caching the access token in memory.

const IWARA_API = 'https://api.iwara.tv';
let iwaraAccess = { token: '', exp: 0, forUser: '' };

function jwtExp(token) {
  try {
    const payload = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    return (JSON.parse(atob(payload)).exp || 0) * 1000;
  } catch (e) {
    return 0;
  }
}

async function iwaraAccessToken(userToken) {
  if (!userToken) return '';
  const now = Date.now();
  if (iwaraAccess.token && iwaraAccess.forUser === userToken && iwaraAccess.exp - 60000 > now) {
    return iwaraAccess.token;
  }
  try {
    const r = await fetch(`${IWARA_API}/user/token`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${userToken}`, 'Content-Type': 'application/json' },
      credentials: 'omit',
    });
    if (!r.ok) return '';
    const data = await r.json();
    const tok = data && data.accessToken;
    if (!tok) return '';
    iwaraAccess = { token: tok, exp: jwtExp(tok) || (now + 55 * 60000), forUser: userToken };
    return tok;
  } catch (e) {
    return '';
  }
}

async function getJson(url, headers) {
  const r = await fetch(url, { headers: headers || {}, credentials: 'omit' });
  if (!r.ok) return { ok: false, status: r.status };
  return { ok: true, data: await r.json() };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== 'fetchJson') return false;
  (async () => {
    try {
      const headers = Object.assign({}, msg.headers || {});
      if (msg.iwaraUserToken && msg.url.startsWith(IWARA_API)) {
        const access = await iwaraAccessToken(msg.iwaraUserToken);
        if (access) headers.Authorization = `Bearer ${access}`;
        headers['X-Site'] = 'www.iwara.tv';
      }
      let res = await getJson(msg.url, headers);
      // A stale cached access token → refresh once and retry.
      if (!res.ok && (res.status === 401 || res.status === 403) && msg.iwaraUserToken) {
        iwaraAccess = { token: '', exp: 0, forUser: '' };
        const access = await iwaraAccessToken(msg.iwaraUserToken);
        if (access) {
          headers.Authorization = `Bearer ${access}`;
          res = await getJson(msg.url, headers);
        }
      }
      sendResponse(res);
    } catch (e) {
      sendResponse({ ok: false, error: String(e) });
    }
  })();
  return true;   // keep the channel open for the async reply
});
