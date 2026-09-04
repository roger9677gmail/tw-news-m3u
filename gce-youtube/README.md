# YouTube 轉 M3U：Compute Engine 測試站

這個服務只供使用者已取得使用及轉播權利的 YouTube 內容使用。它提供手機管理頁、受權杖保護的 M3U、單一並行 HLS 封裝，以及閒置自動停止。

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

- 只接受 YouTube HTTPS 網址。
- 所有管理、M3U、HLS 和影片分段均需要播放權杖。
- e2-micro 同時只允許一部影片封裝；切台時會停止上一部。
- 150 秒沒有播放請求就停止 FFmpeg。
- 不儲存 YouTube Cookie，也不繞過 DRM。
