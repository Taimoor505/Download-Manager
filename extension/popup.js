const $ = (id) => document.getElementById(id);
const human = (n) => {
  if (!n) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
};
const ask = (msg) => chrome.runtime.sendMessage(msg);

function say(text, kind = '') {
  $('msg').textContent = text;
  $('msg').className = `msg ${kind}`;
}

async function send(url, referer) {
  const res = await ask({ type: 'send', url, referer });
  say(res.ok ? 'Sent to vidgrab.' : res.error, res.ok ? 'ok' : 'err');
}

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

  ask({ type: 'ping' }).then((res) => {
    $('dot').className = `dot ${res.ok ? 'on' : 'off'}`;
    $('status').textContent = res.ok ? 'Connected' : 'Not running';
    if (!res.ok) say('Start the vidgrab desktop app, then try again.', 'err');
  });

  $('send-page').onclick = () => send(tab.url, '');

  const found = await ask({ type: 'found', tabId: tab.id });
  const list = $('found');
  if (!found.length) {
    list.innerHTML = '<li class="empty">Nothing yet. Play the video, then open this again.</li>';
  } else {
    for (const item of found) {
      const li = document.createElement('li');
      const label = document.createElement('div');
      label.className = 'name';
      label.title = item.url;
      label.textContent = item.name;
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = [item.type.split(';')[0], human(item.size)].filter(Boolean).join(' · ');
      label.appendChild(meta);
      const button = document.createElement('button');
      button.textContent = 'Download';
      button.onclick = () => send(item.url, tab.url);
      li.append(label, button);
      list.appendChild(li);
    }
  }

  const { server } = await chrome.storage.sync.get({ server: 'http://127.0.0.1:8765' });
  $('server').value = server;
  $('server').onchange = () => chrome.storage.sync.set({ server: $('server').value.trim() || 'http://127.0.0.1:8765' });
}

init();
