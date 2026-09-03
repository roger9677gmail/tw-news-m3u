# 新聞直播無法播放診斷（2026-09-03）

## 結論

途播、M3U 匯入、Cloud Run 公開端點與播放權杖均正常。故障發生在上游直播來源。

## 實測結果

使用目前正式環境的 Docker image、Cloud Run 執行身分，以及 `tw-news-fourgtv-streams:latest` 快取，建立隔離診斷服務並測試目前設定的 8 個新聞頻道：

- 8/8 頻道解析 API 回 HTTP 200；程式能從快取讀到一條直播網址。
- 8/8 頻道在要求第一層 HLS master manifest 時回 HTTP 400。
- 上游回應為 `text/plain`、長度 4 bytes，內容是 4 個空白字元。
- 因為連 master manifest 都沒有取得，所以途播只會持續 Loading，不可能取得子清單或影音分段。

診斷工作：GitHub Actions run `33741884277`。

## 為什麼畫面仍顯示頻道可用

`app/fourgtv.py` 的 `_cached_stream_url()` 只確認：

1. 快取中有 HTTPS 網址；
2. 網址查詢參數中的時間尚未過期。

它沒有實際下載 HLS manifest 驗證上游是否接受該網址。因此解析狀態可顯示成功，但真正播放時才收到 HTTP 400。

## 為什麼無法自動換新網址

重新呼叫目前的 4GTV 行動 API `https://api2.4gtv.tv/App/GetChannelUrl2` 時，API 回傳應用程式錯誤碼 `02`，快取更新在取得第一個頻道時即失敗。診斷工作：GitHub Actions run `33742650313`。

程式原先送出 `fsVERSION: 3.2.1`；官方 iOS App 已更新到 `3.2.17`。將程式升到 `3.2.17` 後再次實測，API 仍回 `02`，所以不是只改版本號就能恢復，表示 App 認證密鑰、簽章或請求協定至少有一部分已改變。

## 備援來源

YouTube 備援在 GitHub hosted runner 與 Cloud Run 類型的資料中心網路測試時，主要新聞直播收到 `Sign in to confirm you're not a bot`。因此 4GTV 快取失效後，現有 YouTube 備援無法在公有雲可靠接手。

## 根因判定

已確認的直接根因是：

1. 目前快取的 4GTV 簽名 HLS URL 已不再被上游 CDN 接受，全部回 HTTP 400；
2. 目前使用的 4GTV 行動 API 認證／請求協定已失效，回錯誤碼 `02`，無法產生新快取；
3. YouTube 備援又受到資料中心網路的機器人驗證限制。

僅從 HTTP 400 空白回應，無法進一步證明是 IP 綁定、工作階段綁定，或 CDN 簽名格式變更；但可以排除途播、M3U、Cloud Run 路由和播放權杖。

## 建議修正

- 更新 4GTV 的現行官方請求協定，或改用另一個允許伺服器端播放的合法來源。
- 在把快取 URL 標記為 online 前，先實際驗證 HLS manifest；HTTP 400 時立即淘汰快取。
- 上游全數失效時，不要把頻道繼續列為可用，避免途播無限 Loading。
- 不建議把個人 YouTube cookies 放入公有雲。
