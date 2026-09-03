// Taiwan News M3U Relay — iPhone refresh helper for Scriptable.
const BASE_URL = "https://tw-news-m3u-1079887872019.asia-east1.run.app";
const KEYCHAIN_ID = "tw-news-m3u-access-key";
const AUTOMATED = config.runsWithSiri || !config.runsInApp;

async function message(title, body) {
  const alert = new Alert();
  alert.title = title;
  alert.message = body;
  alert.addAction("好");
  await alert.presentAlert();
}

async function accessKey() {
  if (Keychain.contains(KEYCHAIN_ID)) return Keychain.get(KEYCHAIN_ID);
  if (AUTOMATED) {
    throw new Error("尚未設定播放權杖，請先在 Scriptable 手動執行一次。");
  }
  const alert = new Alert();
  alert.title = "輸入播放權杖";
  alert.message = "只需輸入一次，會安全保存在 iPhone Keychain。";
  alert.addSecureTextField("ACCESS_KEY", "");
  alert.addAction("儲存並更新");
  alert.addCancelAction("取消");
  const selected = await alert.presentAlert();
  if (selected < 0) throw new Error("已取消");
  const key = alert.textFieldValue(0).trim();
  if (!key) throw new Error("播放權杖不可空白");
  return key;
}

async function relayJSON(path, key, method = "GET", body = null) {
  const request = new Request(BASE_URL + path);
  request.method = method;
  request.headers = {
    "x-access-key": key,
    "Content-Type": "application/json",
    "Accept": "application/json",
  };
  if (body !== null) request.body = JSON.stringify(body);
  const payload = await request.loadJSON();
  if (request.response.statusCode === 401) {
    if (Keychain.contains(KEYCHAIN_ID)) Keychain.remove(KEYCHAIN_ID);
    throw new Error("播放權杖不正確，請重新執行後輸入。 ");
  }
  if (request.response.statusCode >= 400) {
    throw new Error(payload.error || `Relay 回傳 ${request.response.statusCode}`);
  }
  return payload;
}

async function reportSuccess(channels) {
  const body = `已更新 ${channels} 個頻道。`;
  if (AUTOMATED) {
    Script.setShortcutOutput(body);
    return;
  }
  await message("更新成功", `${body}現在回到途播重新點台即可。`);
}

async function reportFailure(error) {
  const body = String(error.message || error);
  if (!AUTOMATED) {
    await message("更新失敗", body);
    return;
  }
  Script.setShortcutOutput(`更新失敗：${body}`);
  try {
    const notification = new Notification();
    notification.title = "新聞直播更新失敗";
    notification.body = body;
    await notification.schedule();
  } catch (_) {
    // Shortcuts will still receive the error text as its action output.
  }
}

try {
  const key = await accessKey();
  const plan = await relayJSON("/api/fourgtv/refresh-plan", key);
  const responses = [];
  for (const item of plan.requests) {
    const request = new Request(item.url);
    request.method = "POST";
    request.headers = item.headers;
    request.body = JSON.stringify(item.body);
    request.timeoutInterval = 20;
    const payload = await request.loadJSON();
    if (request.response.statusCode >= 400 || payload.Success !== true) {
      throw new Error(`${item.channel_id} 取得官方直播失敗`);
    }
    responses.push({ channel_id: item.channel_id, payload });
  }
  const result = await relayJSON(
    "/api/fourgtv/refresh",
    key,
    "POST",
    { responses }
  );
  Keychain.set(KEYCHAIN_ID, key);
  await reportSuccess(result.channels);
} catch (error) {
  await reportFailure(error);
}

Script.complete();
