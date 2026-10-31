// vidgrab browser extension: notices media the page loads and sends links to the local app.
// Nothing leaves the browser except the links you choose to send, and only to your own vidgrab.

const DEFAULT_SERVER = 'http://127.0.0.1:8765';
const MEDIA_PATH = /\.(mp4|m4v|webm|mov|mkv|flv|m3u8|mpd|mp3|m4a|aac|ogg|opus)(\?|$)/i;
// Pieces of a stream (HLS/DASH segments), not whole files
const SEGMENT_PATH = /\.(ts|m4s|m4f|cmfv|cmfa)(\?|$)|\/seg[-_]?\d+|\/chunk[-_]?\d+|[?&](range|bytestart)=/i;
const SEGMENT_TYPES = /^(video\/mp2t|video\/iso\.segment|audio\/mp2t)/i;
// Sites whose media requests are byte-range chunks; the page link is what vidgrab needs there
const PAGE_ONLY_HOSTS = /(^|\.)(googlevideo\.com|youtube\.com|ytimg\.com)$/i;
const MAX_PER_TAB = 30;

async function serverUrl() {
  const { server } = await chrome.storage.sync.get({ server: DEFAULT_SERVER });
  return server.replace(/\/$/, '');
}

const YOUTUBE_LINK = /^https?:\/\/([a-z0-9-]+\.)*(youtube\.com|youtu\.be)\//i;

// Opt-in (popup switch, off by default): YouTube links carry your YouTube session, so
// YouTube sees the same signed-in viewer as this browser instead of an anonymous request.
// Only youtube.com cookies are read, and vidgrab keeps them in memory for that one download.
async function youtubeSession(url) {
  const { useYoutubeLogin } = await chrome.storage.sync.get({ useYoutubeLogin: false });
  if (!useYoutubeLogin || !YOUTUBE_LINK.test(url)) return [];
  const cookies = await chrome.cookies.getAll({ domain: 'youtube.com' });
  return cookies.map(({ name, value, domain, path, secure, expirationDate }) =>
    ({ name, value, domain, path, secure, expirationDate }));
}

async function sendToVidgrab(url, referer = '') {
  const cookies = await youtubeSession(url);
  const res = await fetch(`${await serverUrl()}/api/downloads`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, referer, cookies }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `vidgrab answered HTTP ${res.status}`);
  }
  return res.json();
}

function notify(title, message) {
  chrome.notifications.create({ type: 'basic', iconUrl: 'icons/128.png', title, message });
}

async function sendAndNotify(url, referer) {
  try {
    await sendToVidgrab(url, referer);
    notify('Sent to vidgrab', url);
  } catch (err) {
    const offline = err instanceof TypeError;  // fetch failed: app not running
    notify('vidgrab', offline ? 'vidgrab is not running. Start the desktop app first.' : err.message);
  }
}

// ---- detected media, per tab (session storage survives the service worker sleeping) ----

const key = (tabId) => `tab:${tabId}`;

async function getFound(tabId) {
  const data = await chrome.storage.session.get(key(tabId));
  return data[key(tabId)] || [];
}

async function setFound(tabId, items) {
  await chrome.storage.session.set({ [key(tabId)]: items });
  chrome.action.setBadgeBackgroundColor({ tabId, color: '#7c3aed' });
  chrome.action.setBadgeText({ tabId, text: items.length ? String(items.length) : '' });
}

chrome.webRequest.onResponseStarted.addListener(
  async (details) => {
    if (details.tabId < 0) return;
    let url;
    try { url = new URL(details.url); } catch { return; }
    if (!/^https?:$/.test(url.protocol) || PAGE_ONLY_HOSTS.test(url.hostname)) return;
    const type = (details.responseHeaders || [])
      .find((h) => h.name.toLowerCase() === 'content-type')?.value?.toLowerCase() || '';
    const looksMedia = MEDIA_PATH.test(url.pathname) || /^(video|audio)\//.test(type) || /mpegurl|dash\+xml/.test(type);
    if (!looksMedia || SEGMENT_PATH.test(details.url) || SEGMENT_TYPES.test(type)) return;

    const found = await getFound(details.tabId);
    const id = url.origin + url.pathname;  // the same file with different query strings
    if (found.some((f) => f.id === id)) return;
    const size = Number((details.responseHeaders || [])
      .find((h) => h.name.toLowerCase() === 'content-length')?.value) || null;
    const name = decodeURIComponent(url.pathname.split('/').pop() || url.hostname);
    found.unshift({ id, url: details.url, name, type, size });
    await setFound(details.tabId, found.slice(0, MAX_PER_TAB));
  },
  { urls: ['<all_urls>'], types: ['media', 'xmlhttprequest', 'other', 'object'] },
  ['responseHeaders'],
);

chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (change.status === 'loading' && change.url) setFound(tabId, []);
});
chrome.tabs.onRemoved.addListener((tabId) => chrome.storage.session.remove(key(tabId)));

// ---- right-click menu ----------------------------------------------------------------

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: 'link', title: 'Download link with vidgrab', contexts: ['link'] });
  chrome.contextMenus.create({ id: 'media', title: 'Download this video with vidgrab', contexts: ['video', 'audio'] });
  chrome.contextMenus.create({ id: 'page', title: "Download this page's video with vidgrab", contexts: ['page'] });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const page = tab?.url || info.pageUrl || '';
  if (info.menuItemId === 'link') sendAndNotify(info.linkUrl, page);
  // blob: URLs only exist inside the page; vidgrab finds the stream from the page link
  if (info.menuItemId === 'media') {
    const src = info.srcUrl && /^https?:/.test(info.srcUrl) ? info.srcUrl : page;
    sendAndNotify(src, page);
  }
  if (info.menuItemId === 'page') sendAndNotify(page, '');
});

// ---- popup messages -----------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (async () => {
    if (msg.type === 'found') return reply(await getFound(msg.tabId));
    if (msg.type === 'send') {
      try {
        await sendToVidgrab(msg.url, msg.referer || '');
        reply({ ok: true });
      } catch (err) {
        reply({ ok: false, error: err instanceof TypeError ? 'vidgrab is not running.' : err.message });
      }
    }
    if (msg.type === 'ping') {
      try {
        const res = await fetch(`${await serverUrl()}/api/info`);
        reply({ ok: res.ok });
      } catch {
        reply({ ok: false });
      }
    }
  })();
  return true;  // reply asynchronously
});
