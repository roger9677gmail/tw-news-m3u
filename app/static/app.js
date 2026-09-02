const els = {
  keyField: document.querySelector('#key-field'),
  accessKey: document.querySelector('#access-key'),
  playlistUrl: document.querySelector('#playlist-url'),
  copyPlaylist: document.querySelector('#copy-playlist'),
  tuboImport: document.querySelector('#tubo-import'),
  openPlaylist: document.querySelector('#open-playlist'),
  serverState: document.querySelector('#server-state'),
  grid: document.querySelector('#channel-grid'),
  notice: document.querySelector('#notice'),
  refresh: document.querySelector('#refresh-status'),
  countTotal: document.querySelector('#count-total'),
  countOnline: document.querySelector('#count-online'),
  countError: document.querySelector('#count-error'),
  maxHeight: document.querySelector('#max-height'),
  toast: document.querySelector('#toast'),
};

let config = null;
let statusTimer = null;
const STORAGE_KEY = 'tw-news-m3u-access-key';

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function keyValue() {
  return els.accessKey.value.trim();
}

function authQuery() {
  const key = keyValue();
  return key ? `?key=${encodeURIComponent(key)}` : '';
}

function playlistUrl() {
  const base = (config?.public_base_url || window.location.origin).replace(/\/$/, '');
  return `${base}/live.m3u${authQuery()}`;
}

function updateLinks() {
  const url = playlistUrl();
  els.playlistUrl.textContent = url;
  els.openPlaylist.href = url;
  const params = new URLSearchParams({
    url,
    name: config?.app_name || '台灣新聞直播 M3U',
  });
  // Use Tubo's direct app scheme so the playlist token is not sent through an
  // intermediary web page. Manual copy remains available if iOS blocks it.
  els.tuboImport.href = `tubo://import?${params.toString()}`;
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add('show');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => els.toast.classList.remove('show'), 1900);
}

function formatDate(value) {
  if (!value) return '尚未測試';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '時間未知';
  return new Intl.DateTimeFormat('zh-TW', {
    timeZone: 'Asia/Taipei',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

function stateLabel(channel) {
  if (channel.state === 'online') {
    return `${channel.height ? `${channel.height}p · ` : ''}${formatDate(channel.resolved_at)} 已解析`;
  }
  if (channel.state === 'resolving') return '正在解析官方直播…';
  if (channel.state === 'error') return `失敗 · ${channel.error || '來源暫時不可用'}`;
  return '尚未測試；途播點播時會自動解析';
}

function channelCard(channel) {
  return `
    <article class="channel-card ${escapeHtml(channel.state)}" data-channel-id="${escapeHtml(channel.id)}">
      <span class="dot" aria-hidden="true"></span>
      <div>
        <h3>${escapeHtml(channel.name)}</h3>
        <p title="${escapeHtml(channel.error || '')}">${escapeHtml(stateLabel(channel))}</p>
      </div>
      <button type="button" class="probe quiet">測試</button>
    </article>`;
}

async function requestJson(path, options = {}) {
  const response = await fetch(path, { cache: 'no-store', ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data.detail || data.error || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return data;
}

function setServerState(text, state) {
  els.serverState.textContent = text;
  els.serverState.className = `server-state ${state || ''}`.trim();
}

async function loadConfig() {
  config = await requestJson('/api/config');
  els.maxHeight.textContent = `${config.max_height}p`;
  els.countTotal.textContent = config.channel_count;
  els.keyField.hidden = !config.access_required;
  if (!config.access_required) els.accessKey.value = '';
  updateLinks();
}

async function loadStatus({ quiet = false } = {}) {
  if (config?.access_required && !keyValue()) {
    els.grid.innerHTML = '';
    els.notice.textContent = '請先輸入播放權杖，才能讀取與測試頻道。';
    els.notice.hidden = false;
    setServerState('等待密碼', '');
    return;
  }

  els.grid.setAttribute('aria-busy', 'true');
  if (!quiet) els.refresh.disabled = true;
  try {
    const data = await requestJson(`/api/status${authQuery()}`);
    els.notice.hidden = true;
    els.grid.innerHTML = data.channels.map(channelCard).join('');
    els.countTotal.textContent = data.channels.length;
    els.countOnline.textContent = data.summary.online || 0;
    els.countError.textContent = data.summary.error || 0;
    setServerState('伺服器正常', 'ok');
  } catch (error) {
    els.grid.innerHTML = '';
    els.notice.textContent = `無法讀取狀態：${error.message}`;
    els.notice.hidden = false;
    setServerState((error.message.includes('權杖') || error.message.includes('密碼')) ? '權杖錯誤' : '連線異常', 'bad');
  } finally {
    els.grid.setAttribute('aria-busy', 'false');
    els.refresh.disabled = false;
  }
}

async function probe(button) {
  const card = button.closest('[data-channel-id]');
  const channelId = card?.dataset.channelId;
  if (!channelId) return;
  if (config?.access_required && !keyValue()) {
    showToast('請先輸入播放權杖');
    els.accessKey.focus();
    return;
  }

  button.disabled = true;
  button.textContent = '解析中';
  card.className = 'channel-card resolving';
  const text = card.querySelector('p');
  text.textContent = '正在連線官方直播，請稍候…';
  try {
    const result = await requestJson(`/api/channels/${encodeURIComponent(channelId)}/probe${authQuery()}`, {
      method: 'POST',
    });
    showToast(`${card.querySelector('h3').textContent}：解析成功${result.height ? ` ${result.height}p` : ''}`);
  } catch (error) {
    showToast(`解析失敗：${error.message}`);
  } finally {
    await loadStatus({ quiet: true });
  }
}

els.accessKey.value = localStorage.getItem(STORAGE_KEY) || '';
els.accessKey.addEventListener('input', () => {
  localStorage.setItem(STORAGE_KEY, keyValue());
  updateLinks();
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => loadStatus({ quiet: true }), 450);
});
els.copyPlaylist.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(playlistUrl());
    showToast('已複製 M3U 網址');
  } catch {
    window.prompt('請複製以下網址：', playlistUrl());
  }
});
els.refresh.addEventListener('click', () => loadStatus());
els.grid.addEventListener('click', (event) => {
  const button = event.target.closest('.probe');
  if (button) probe(button);
});

(async () => {
  try {
    await loadConfig();
    await loadStatus();
  } catch (error) {
    els.notice.textContent = `伺服器初始化失敗：${error.message}`;
    els.notice.hidden = false;
    setServerState('無法連線', 'bad');
  }
})();
