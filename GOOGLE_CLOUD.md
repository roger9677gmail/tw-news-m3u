# Google Cloud 快速部署

## 一鍵開啟部署教學

[![Open in Cloud Shell](https://gstatic.com/cloudssh/images/open-btn.svg)](https://shell.cloud.google.com/cloudshell/editor?cloudshell_git_repo=https%3A%2F%2Fgithub.com%2Froger9677gmail%2Ftw-news-m3u.git&cloudshell_workspace=.&cloudshell_tutorial=gcp%2Ftutorial.md&show=ide%2Cterminal)

開啟後，Google 第一次執行 Cloud 指令時會顯示 **Authorize Cloud Shell**。按下 **Authorize** 後，在右側教學執行一鍵部署指令即可。

完整說明請看 [`gcp/README.md`](gcp/README.md)。

## 已經在 Cloud Shell／專案資料夾中

執行：

```bash
git pull --ff-only
bash gcp/create-and-deploy.sh
```

腳本會尋找你有權使用的開啟中帳單帳戶，自動建立一個獨立的 `tw-news-m3u-...` Google Cloud 專案，連結帳單，再部署到 Cloud Run 台灣 `asia-east1` 區域。

若帳號中有多個帳單帳戶，腳本會列出清單並要求輸入 `ACCOUNT_ID`。如果公司政策禁止建立專案，可指定一個你有 Owner 權限且已啟用計費的既有專案：

```bash
PROJECT_ID=你的既有專案ID bash gcp/create-and-deploy.sh
```

腳本完成後會直接顯示：

```text
https://你的-cloud-run-網址/live.m3u?key=隨機播放權杖
```

這就是貼進途播的網址。網址與 Project ID 也會保存在：

```bash
cat ~/tw-news-m3u-url.txt
cat ~/tw-news-m3u-project-id.txt
```

部署採用 HTTPS、Secret Manager、單一最大執行個體。單一執行個體是為了避免目前存放在記憶體中的 HLS 短期權杖被分散到不同執行個體。

要讓 GitHub 後續自動部署，再執行：

```bash
bash gcp/setup-github-actions.sh
```

然後在 GitHub Actions 執行 **Deploy to Google Cloud Run**。

> 預設 8 台使用 4GTV 官方行動直播來源，不需要 YouTube Cookie，也不需要家中 NAS 或電腦。影片會經過 Cloud Run 轉送，可能產生對外資料傳輸費。
