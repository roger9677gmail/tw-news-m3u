# Google Cloud 快速部署

本次預設部署到既有 Google Cloud 專案：

```text
infinite-mantra-458303-p8
```

## 一鍵開啟部署教學

[![Open in Cloud Shell](https://gstatic.com/cloudssh/images/open-btn.svg)](https://shell.cloud.google.com/cloudshell/editor?cloudshell_git_repo=https%3A%2F%2Fgithub.com%2Froger9677gmail%2Ftw-news-m3u.git&cloudshell_workspace=.&cloudshell_tutorial=gcp%2Ftutorial.md&show=ide%2Cterminal)

開啟後，Google 第一次執行 Cloud 指令時會顯示 **Authorize Cloud Shell**。按下 **Authorize** 後，在右側教學依序複製並執行兩個指令即可。

完整說明請看 [`gcp/README.md`](gcp/README.md)。

## 手動執行方式

在 Google Cloud Console 打開 **Cloud Shell**，執行：

```bash
git clone https://github.com/roger9677gmail/tw-news-m3u.git
cd tw-news-m3u
PROJECT_ID=infinite-mantra-458303-p8 REGION=asia-east1 bash gcp/deploy-cloud-run.sh
```

腳本完成後會直接顯示：

```text
https://你的-cloud-run-網址/live.m3u?key=隨機播放權杖
```

這就是貼進途播的網址。網址也會保存在：

```bash
cat ~/tw-news-m3u-url.txt
```

部署採用台灣 `asia-east1`、HTTPS、Secret Manager、單一最大執行個體。單一執行個體是為了避免目前存放在記憶體中的 HLS 短期權杖被分散到不同執行個體。

要讓 GitHub 後續自動部署，再執行：

```bash
bash gcp/setup-github-actions.sh
```

然後在 GitHub Actions 執行 **Deploy to Google Cloud Run**。

> 公有雲出口仍可能被 YouTube 要求機器人驗證；請在部署後先從管理頁測試頻道。影片會經過 Cloud Run 轉送，也可能產生對外資料傳輸費。
