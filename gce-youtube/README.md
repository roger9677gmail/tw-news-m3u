# 途播授權串流管理站

這個服務只供使用者已取得使用及轉播權利的內容使用。它提供手機管理頁、受權杖保護的 M3U、HLS/M3U8 原樣代理，以及 YouTube 單一並行 HLS 封裝。

## Cloud Run（建議用於授權 HLS）

Cloud Run 版使用本目錄的 `Dockerfile`，並以私人 Cloud Storage object 永久保存節目清單。容器重新部署或縮至零都不會遺失新增項目。

必要環境變數：

- `ACCESS_KEY`：至少 20 個字元，正式環境應由 Secret Manager 提供。
- `PUBLIC_BASE_URL`：Cloud Run 服務的正式 HTTPS 網址。
- `CATALOG_BUCKET`：保存私人節目清單的 Cloud Storage bucket。
- `CATALOG_OBJECT`：預設為 `catalog/catalog.json`。

為避免多個執行個體同時修改同一份小型清單，部署時將最大執行個體設為 1。公開 HLS 來源由 Cloud Run 即時代理，影片本身不寫入容器的暫存檔案。

YouTube 即時封裝仍使用暫存分段，可能遇到 YouTube 對公有雲 IP 的限制；此模式以 Compute Engine 版較穩定。

## HLS／M3U8 管理

- 每行使用 `節目名稱 | https://來源/playlist.m3u8`，一次最多 50 筆。
- 加入前會實際確認來源以 `#EXTM3U` 開頭。
- 支援主清單、子清單、分段、初始化檔、AES-128 key 與 Range 請求的代理。
- 可選填來源正式提供的 `Referer`、`Origin` 和 `User-Agent`。
- 不轉碼、不下載完整影片；刪除項目後會同步從輸出的 M3U 移除。
- 上游與重新導向網址必須是公開 HTTPS 位址，並以簽章綁定已加入項目。

## VM 規格

- Debian 13
- 測試最低規格：e2-micro，2 GB swap
- 正式多路播放：至少 2 vCPU / 4 GB RAM，並另行做容量與流量評估

## 安裝

將本目錄複製到 VM 後，以 root 執行：

```bash
DOMAIN=你的固定IP.sslip.io bash install.sh
```

服務會執行於 `127.0.0.1:8788`，由 Caddy 提供 HTTPS。播放權杖存放於 `/etc/youtube-m3u.env`，目錄資料存放於 `/var/lib/youtube-m3u`。安裝程式也會在本機 `4416` 連接埠啟動 bgutil PO Token provider；該連接埠不對網際網路開放。

## 安全限制

- HLS 只接受公開 HTTPS 網址；拒絕本機、內網、雲端 metadata 位址及任意代理網址。
- YouTube 只接受 `youtube.com` 或 `youtu.be` HTTPS 網址。
- 所有管理、M3U、HLS 和影片分段均需要播放權杖。
- e2-micro 同時只允許一部影片封裝；切台時會停止上一部。
- 150 秒沒有播放請求就停止 FFmpeg。
- 不接受 Cookie／帳密、不擷取一般影音網頁，也不繞過 DRM。
