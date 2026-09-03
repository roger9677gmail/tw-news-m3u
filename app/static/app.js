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
  countKaraoke: document.querySelector('#count-karaoke'),
  maxHeight: document.querySelector('#max-height'),
  toast: document.querySelector('#toast'),
  karaokeSection: document.querySelector('#karaoke-section'),
  karaokeFile: document.querySelector('#karaoke-file'),
  karaokeRights: document.querySelector('#karaoke-rights'),
  karaokeUpload: document.querySelector('#upload-karaoke'),
  karaokeGrid: document.querySelector('#karaoke-grid'),
  karaokeNotice: document.querySelector('#karaoke-notice'),
  karaokeRefresh: document.querySelector('#refresh-karaoke'),
  karaokeProgressWrap: document.querySelector('#karaoke-progress-wrap'),
  karaokeProgress: document.querySelector('#karaoke-progress'),
  karaokeProgressText: document.querySelector('#karaoke-progress-text'),
};

let config = null;
let statusTimer = null;
let karaokeUploading = false;
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

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes < 1) return '大小未知';
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
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

function karaokeCard(song) {
  return `
    <article class="channel-card online karaoke-card" data-song-id="${escapeHtml(song.id)}">
      <span class="dot" aria-hidden="true"></span>
      <div>
        <h3>${escapeHtml(song.title)}</h3>
        <p>${escapeHtml(formatBytes(song.size_bytes))} · 已加入途播 KTV 點歌</p>
      </div>
      <button type="button" class="delete-song quiet">刪除</button>
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
  els.countKaraoke.textContent = config.karaoke_song_count || 0;
  els.keyField.hidden = !config.access_required;
  if (!config.access_required) els.accessKey.value = '';
  updateLinks();
}

async function loadKaraoke({ quiet = false } = {}) {
  if (!config?.karaoke_enabled) {
    els.karaokeGrid.innerHTML = '';
    els.karaokeNotice.textContent = '伺服器尚未啟用卡拉 OK 儲存空間。';
    els.karaokeUpload.disabled = true;
    els.countKaraoke.textContent = '0';
    return;
  }
  if (config?.access_required && !keyValue()) {
    els.karaokeGrid.innerHTML = '';
    els.karaokeNotice.textContent = '請先輸入上方播放權杖。';
    els.karaokeUpload.disabled = false;
    return;
  }
  els.karaokeGrid.setAttribute('aria-busy', 'true');
  if (!quiet) els.karaokeRefresh.disabled = true;
  try {
    const data = await requestJson(`/api/karaoke/songs${authQuery()}`);
    const songs = data.songs || [];
    els.karaokeGrid.innerHTML = songs.map(karaokeCard).join('');
    els.countKaraoke.textContent = songs.length;
    els.karaokeNotice.textContent = songs.length
      ? '新增或刪除後，請在途播重新整理這份遠端清單。'
      : '尚未加入歌曲。可從 iPhone「檔案」選擇 Google Drive 裡的 MP4。';
  } catch (error) {
    els.karaokeGrid.innerHTML = '';
    els.karaokeNotice.textContent = `無法讀取歌曲：${error.message}`;
  } finally {
    els.karaokeGrid.setAttribute('aria-busy', 'false');
    els.karaokeRefresh.disabled = false;
  }
}

function titleFromFileName(fileName) {
  return fileName.replace(/\.mp4$/i, '').trim() || fileName;
}

function uploadToStorage(uploadUrl, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', uploadUrl);
    xhr.setRequestHeader('Content-Type', 'video/mp4');
    xhr.setRequestHeader('Content-Range', `bytes 0-${file.size - 1}/${file.size}`);
    xhr.upload.addEventListener('progress', (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.round((event.loaded / event.total) * 100);
      onProgress(percent);
    });
    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(`影片上傳失敗（HTTP ${xhr.status}）`));
    });
    xhr.addEventListener('error', () => reject(new Error('影片上傳連線失敗')));
    xhr.send(file);
  });
}

async function uploadOneKaraoke(file, index, total) {
  const position = `第 ${index + 1}/${total} 首`;
  els.karaokeProgress.value = Math.round((index / total) * 100);
  els.karaokeProgressText.textContent = `${position}：${file.name}，建立安全上傳連結…`;
  const upload = await requestJson(`/api/karaoke/uploads${authQuery()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      file_name: file.name,
      size_bytes: file.size,
      rights_confirmed: true,
    }),
  });
  await uploadToStorage(upload.upload_url, file, (percent) => {
    const overall = Math.round(((index + (percent / 100)) / total) * 100);
    els.karaokeProgress.value = overall;
    els.karaokeProgressText.textContent = `${position}：${file.name}，上傳中 ${percent}%`;
  });
  els.karaokeProgress.removeAttribute('value');
  els.karaokeProgressText.textContent = `${position}：${file.name}，正在轉成 720p M3U8…`;
  const result = await requestJson(
    `/api/karaoke/uploads/${encodeURIComponent(upload.upload_id)}/complete${authQuery()}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: titleFromFileName(file.name),
        file_name: file.name,
      }),
    },
  );
  return result.song;
}

async function uploadKaraoke() {
  if (config?.access_required && !keyValue()) {
    showToast('請先輸入播放權杖');
    els.accessKey.focus();
    return;
  }
  const files = Array.from(els.karaokeFile.files || []);
  if (!files.length) {
    showToast('請先選擇一個或多個 MP4 影片');
    return;
  }
  const invalidFile = files.find((file) => !file.name.toLowerCase().endsWith('.mp4'));
  if (invalidFile) {
    showToast(`${invalidFile.name} 不是 MP4 影片`);
    return;
  }
  const oversizedFile = files.find(
    (file) => file.size > Number(config.karaoke_max_upload_bytes || 0),
  );
  if (oversizedFile) {
    showToast(`${oversizedFile.name} 超過單檔大小限制`);
    return;
  }
  if (!els.karaokeRights.checked) {
    showToast('請先確認影片使用權');
    return;
  }

  karaokeUploading = true;
  els.karaokeUpload.disabled = true;
  els.karaokeFile.disabled = true;
  els.karaokeRights.disabled = true;
  els.karaokeProgressWrap.hidden = false;
  els.karaokeProgress.value = 0;
  const succeeded = [];
  const failed = [];
  for (let index = 0; index < files.length; index += 1) {
    const file = files[index];
    try {
      succeeded.push(await uploadOneKaraoke(file, index, files.length));
    } catch (error) {
      failed.push({ file: file.name, message: error.message });
    }
  }
  karaokeUploading = false;
  els.karaokeProgress.value = 100;
  els.karaokeProgressText.textContent = failed.length
    ? `批次完成：${succeeded.length} 首成功，${failed.length} 首失敗`
    : `完成：${succeeded.length} 首已加入途播清單`;
  els.karaokeFile.value = '';
  els.karaokeRights.checked = false;
  els.karaokeUpload.disabled = false;
  els.karaokeFile.disabled = false;
  els.karaokeRights.disabled = false;
  updateKaraokeSelection();
  await loadKaraoke();
  if (failed.length) {
    els.karaokeNotice.textContent = `已成功 ${succeeded.length} 首。失敗：${failed.map((item) => `${item.file}（${item.message}）`).join('、')}`;
    showToast(`${succeeded.length} 首成功，${failed.length} 首失敗`);
  } else {
    showToast(`${succeeded.length} 首已全部加入途播清單`);
  }
}

function updateKaraokeSelection() {
  const count = els.karaokeFile.files?.length || 0;
  els.karaokeUpload.textContent = count
    ? `批次上傳並轉換 ${count} 個檔案`
    : '批次上傳並轉成 M3U8';
}

async function deleteKaraoke(button) {
  const card = button.closest('[data-song-id]');
  const songId = card?.dataset.songId;
  const title = card?.querySelector('h3')?.textContent || '這首歌曲';
  if (!songId || !window.confirm(`確定刪除「${title}」？M3U8 與所有影片分段也會一起刪除。`)) return;
  button.disabled = true;
  try {
    await requestJson(`/api/karaoke/songs/${encodeURIComponent(songId)}${authQuery()}`, {
      method: 'DELETE',
    });
    showToast(`${title} 已刪除`);
    await loadKaraoke();
  } catch (error) {
    showToast(`刪除失敗：${error.message}`);
    button.disabled = false;
  }
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
  statusTimer = setTimeout(() => {
    loadStatus({ quiet: true });
    loadKaraoke({ quiet: true });
  }, 450);
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
els.karaokeRefresh.addEventListener('click', () => loadKaraoke());
els.karaokeUpload.addEventListener('click', uploadKaraoke);
els.karaokeFile.addEventListener('change', updateKaraokeSelection);
els.grid.addEventListener('click', (event) => {
  const button = event.target.closest('.probe');
  if (button) probe(button);
});

window.addEventListener('beforeunload', (event) => {
  if (!karaokeUploading) return;
  event.preventDefault();
  event.returnValue = '';
});
els.karaokeGrid.addEventListener('click', (event) => {
  const button = event.target.closest('.delete-song');
  if (button) deleteKaraoke(button);
});

(async () => {
  try {
    await loadConfig();
    await Promise.all([loadStatus(), loadKaraoke()]);
  } catch (error) {
    els.notice.textContent = `伺服器初始化失敗：${error.message}`;
    els.notice.hidden = false;
    setServerState('無法連線', 'bad');
  }
})();
