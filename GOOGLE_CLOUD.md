# Google Cloud 快速部署

完整說明請看 [`gcp/README.md`](gcp/README.md)。

在 Google Cloud Console 建立並選定一個已啟用計費的 Project，打開右上角 **Cloud Shell**，執行：

```bash
git clone https://github.com/roger9677gmail/tw-news-m3u.git
cd tw-news-m3u
bash gcp/deploy-cloud-run.sh
```

腳本完成後會直接顯示：

```text
https://你的-cloud-run-網址/live.m3u?key=隨機播放權杖
```

這就是貼進途播的網址。

部署採用台灣 `asia-east1`、HTTPS、Secret Manager、單一最大執行個體。單一執行個體是為了避免目前存放在記憶體中的 HLS 短期權杖被分散到不同執行個體。

要讓 GitHub 後續自動部署，再執行：

```bash
bash gcp/setup-github-actions.sh
```

然後在 GitHub Actions 執行 **Deploy to Google Cloud Run**。

> 公有雲出口仍可能被 YouTube 要求機器人驗證；請在部署後先從管理頁測試頻道。影片會經過 Cloud Run 轉送，也可能產生對外資料傳輸費。
