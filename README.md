# 台灣新聞直播 M3U Relay

將台灣新聞台的**官方公開 YouTube 直播**，即時轉成途播與其他 HLS/M3U 播放器可使用的固定清單。

專案入口：<https://roger9677gmail.github.io/tw-news-m3u/>（GitHub Pages 僅提供靜態說明；直播 Relay 仍需依下方步驟部署。）

## 為什麼不是只放一個 GitHub Pages 網址？

YouTube 直播的實際 HLS 網址會過期，而且部分網址必須由「解析它的同一個網路出口」取回。GitHub Pages 只能放靜態檔案，不能在途播點台時現場解析與轉送，因此本專案採用：

```text
GitHub：保存程式碼
Synology NAS／Docker：即時解析 + HLS 轉送
途播：永久匯入 NAS 的固定 live.m3u
```

途播看到的每個頻道網址固定不變，例如：

```text
https://你的網域/hls/tvbs-news/master.m3u8?key=你的存取密碼
```

NAS 在第一次點台時才解析當下的官方直播，之後短暫快取結果。

## 已預設的新聞台

- TVBS NEWS
- 三立新聞
- 東森新聞
- 民視新聞
- 台視新聞
- 中視新聞
- 寰宇新聞
- 公視網路直播
- 非凡財經新聞
- 東森財經新聞
- 鏡新聞

來源都集中在 [`channels.json`](channels.json)，可自行增刪官方公開直播。

---

## A. 先把程式放進 GitHub

### 方法 1：Windows 一鍵推送

1. 解壓縮整個專案。
2. 安裝 Git for Windows，並先登入 GitHub。
3. 雙擊 `publish-to-github.cmd`。
4. 瀏覽器若跳出 GitHub 授權，完成登入。

預設會推送到：

```text
https://github.com/roger9677gmail/tw-news-m3u.git
```

### 方法 2：GitHub 網頁上傳

1. 進入 `roger9677gmail/tw-news-m3u`。
2. 選 **Add file → Upload files**。
3. 把解壓後的所有檔案與資料夾拖入；要包含 `.github`、`app`、`tests`。
4. Commit message 可填 `Create Taiwan news M3U relay`，再按 **Commit changes**。

GitHub 只負責保存程式與執行測試；直播服務仍要在 NAS 啟動。

---

## B. 在 Synology NAS 啟動

### 1. 放置專案

在 File Station 建立資料夾，例如：

```text
/docker/tw-news-m3u
```

將本專案全部檔案放進去。

### 2. 建立 `.env`

把 `.env.example` 複製為 `.env`，先修改專用播放權杖：

```dotenv
PUBLIC_BASE_URL=
ACCESS_KEY=請換成至少24個隨機英數字元的專用播放權杖
```

`PUBLIC_BASE_URL` 通常保持空白即可，程式會依實際連線網址自動產生 M3U。絕對不要把 NAS、Google、GitHub 或其他帳號密碼拿來當 `ACCESS_KEY`。

不要把 `.env` 上傳到 GitHub；專案已透過 `.gitignore` 排除它。

### 3. 用 Container Manager 建立專案

1. 開啟 **Container Manager → 專案**。
2. 選 **新增／建立**。
3. 專案名稱填 `tw-news-m3u`。
4. 路徑選剛才的 `/docker/tw-news-m3u`。
5. Compose 檔選 `compose.yml`。
6. 建置並啟動。

也可透過 NAS SSH 執行：

```bash
cd /volume1/docker/tw-news-m3u
cp .env.example .env
# 編輯 .env 後：
docker compose up -d --build
```

查看狀態：

```bash
docker compose ps
docker compose logs -f --tail=100
```

### 4. 打開管理頁

在同一個區網瀏覽：

```text
http://NAS區網IP:8787
```

輸入 `.env` 裡的專用 `ACCESS_KEY` 播放權杖，即可：

- 複製途播用 M3U 網址
- 以 `tubo://` 專屬連結直接開啟途播
- 個別測試新聞台是否可解析
- 查看解析畫質與錯誤訊息

---

## C. 在途播匯入

管理頁會自動產生類似：

```text
http://NAS區網IP:8787/live.m3u?key=你的存取密碼
```

在途播新增遠端播放來源，貼上這個網址即可。第一次點某個頻道時，NAS 需要現場解析，通常會比一般 IPTV 多等一下；同一頻道後續會使用快取。

「直接開啟途播」使用官方 `tubo://import` App 連結，不會先把完整清單網址送到網頁中轉站。若瀏覽器阻擋自訂連結，改用「複製 M3U 網址」並在途播內手動貼上。

> 完整 M3U 網址含有專用播放權杖，請勿公開貼到社群、Issue 或公開網頁。

### 離開家中後要在車上使用

區網 IP 只在家中 Wi-Fi 有效。車外使用時，需要讓 iPhone 能安全連回 NAS，常見做法有：

- 你既有的 VPN；或
- Synology 反向代理 + 自有網域/DDNS + HTTPS。

設定完成後，先用外部 HTTPS 網址開啟管理頁並重新複製 M3U。若 Synology 反向代理傳遞的主機名稱不正確，才在 `.env` 固定填入：

```dotenv
PUBLIC_BASE_URL=https://你的安全網域
```

修改 `.env` 後重新啟動：

```bash
docker compose up -d
```

不要直接把 8787 埠裸露到網際網路；至少保留專用 `ACCESS_KEY` 播放權杖，外部使用時也應採 HTTPS。

---

## 設定項目

| 變數 | 預設值 | 說明 |
|---|---:|---|
| `PUBLIC_BASE_URL` | 空白 | 清單中要寫入的固定外部網址；空白時使用目前請求網址 |
| `ACCESS_KEY` | 空白 | 建議必填；本服務專用播放權杖，不可使用 NAS 或其他帳號密碼 |
| `MAX_HEIGHT` | `720` | 最高解析度，可改 `480` 或 `1080` |
| `RESOLVER_TTL_SECONDS` | `900` | 成功解析結果的快取秒數 |
| `RESOLVER_FAILURE_TTL_SECONDS` | `90` | 失敗後暫停重試秒數 |
| `RESOLVER_TIMEOUT_SECONDS` | `75` | 單次點台嘗試所有備援來源的整體解析上限 |
| `MAX_RESOLVER_CONCURRENCY` | `2` | 同時執行的 yt-dlp 解析數，避免 NAS 瞬間過載 |
| `MEDIA_TOKEN_TTL_SECONDS` | `21600` | 內部媒體權杖有效時間 |
| `UPSTREAM_TIMEOUT_SECONDS` | `25` | 上游連線逾時 |
| `LOG_LEVEL` | `INFO` | 日誌層級 |

## 運作方式

1. 途播讀取 `/live.m3u`。
2. 點選頻道後，途播要求 `/hls/<頻道>/master.m3u8`。
3. NAS 使用 yt-dlp 解析官方公開直播。
4. NAS 下載 HLS manifest，將裡面的播放清單、分段與金鑰網址改寫成短期內部權杖。
5. 途播後續所有影音請求都由 NAS 代為取回。

伺服器只允許 YouTube/GoogleVideo 相關媒體主機，不提供任意網址代理，避免被濫用成 open proxy。

## 新增或修改頻道

編輯 `channels.json`：

```json
{
  "id": "example-news",
  "name": "範例新聞",
  "group": "綜合新聞",
  "short_name": "範例",
  "sources": [
    "https://www.youtube.com/@官方頻道/live",
    "https://www.youtube.com/live/固定直播ID"
  ]
}
```

注意：

- `id` 只能使用小寫英數字與連字號。
- 把最穩定的官方 `/live` 或固定直播網址放前面。
- 本版本的安全白名單只允許 YouTube/GoogleVideo 媒體來源。
- 修改後執行 `docker compose up -d --build`。

## 疑難排解

### 管理頁能開，但全部頻道都解析失敗

先看日誌：

```bash
docker compose logs --tail=200
```

常見原因：

- YouTube 暫時要求額外驗證。
- NAS DNS 或外網連線有問題。
- 官方直播已換網址、休播或有地區限制。
- yt-dlp 或 YouTube 端規則剛變更。

可先重新建置取得最新版 yt-dlp：

```bash
docker compose build --no-cache
docker compose up -d
```

### 途播清單讀得到，但頻道轉圈

1. 先到管理頁按該頻道的「測試」。
2. 確認 `PUBLIC_BASE_URL` 是 iPhone 當下可連線的網址。
3. 車外使用時，不能填 NAS 區網 IP。
4. 確認反向代理允許串流連線，沒有太短的逾時。
5. 把 `MAX_HEIGHT` 暫時改成 `480` 測試頻寬。

### 網站顯示 401

輸入 `.env` 的專用 `ACCESS_KEY` 播放權杖。途播清單網址也必須保留 `?key=...`。

### 更新程式

在專案資料夾拉取 GitHub 更新後重建：

```bash
git pull
docker compose up -d --build
```

## 安全與使用範圍

- 僅加入內容提供者公開發布的直播來源。
- 不處理付費、私人、DRM 或需要竊取憑證的內容。
- 不保存、錄製或重新編碼影音。
- `ACCESS_KEY` 會出現在 M3U 網址中，因此它只能是本服務專用權杖，不可重複使用 NAS、Google 或其他帳號密碼。Uvicorn access log 已預設關閉；外部連線仍應使用 HTTPS。
- 所有影音都經過 NAS，會消耗 NAS 的下載與上傳頻寬。
- 請只在安全停車時操作與觀看影像，並遵守所在地法規。

## 本機測試

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
uvicorn app.main:app --reload
```

MIT License
