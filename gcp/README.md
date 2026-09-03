# 部署到 Google Cloud Run

這個目錄提供兩種方式：

1. `deploy-cloud-run.sh`：在 Google Cloud Shell 直接完成第一次部署。
2. `setup-github-actions.sh`：建立 Workload Identity Federation，之後由 GitHub Actions 自動更新 Cloud Run，不保存長效 JSON 金鑰。

> 目前預設的 8 個頻道使用 4GTV 官方行動直播來源，已避開 YouTube 對 Google 雲端 IP 的機器人驗證。自行新增的 YouTube 頻道仍可能受此限制。

4GTV 會限制雲端主機直接更新短期網址。若沒有 NAS 或電腦，請依 [iPhone 更新工具](https://roger9677gmail.github.io/tw-news-m3u/iphone-refresh.html) 設定 Scriptable；需要看新聞時先執行一次，iPhone 不必常駐。

## 需求

- 一個已啟用計費的 Google Cloud Project。
- 你在該 Project 具有 Owner，或至少能啟用 API、設定 IAM、建立 Secret Manager 與 Cloud Run。
- 不需要把 Google 密碼、GitHub 密碼或服務帳戶 JSON 金鑰交給本專案。

## 最快上線方式

在 Google Cloud Console 右上角開啟 **Cloud Shell**，執行：

```bash
git clone https://github.com/roger9677gmail/tw-news-m3u.git
cd tw-news-m3u
bash gcp/deploy-cloud-run.sh
```

如果 Cloud Shell 尚未選定 Project，腳本會要求輸入 **Project ID**。腳本會：

- 啟用 Cloud Run、Cloud Build、Artifact Registry、Secret Manager。
- 建立最小權限的執行服務帳戶。
- 產生隨機 `ACCESS_KEY` 並保存於 Secret Manager。
- 從本 repo 的 Dockerfile 建置映像。
- 部署到台灣 `asia-east1`。
- 設定公開 HTTPS 入口，但仍由應用程式的 `ACCESS_KEY` 保護播放清單。
- 限制 `max-instances=1`，避免 HLS 臨時權杖落到不同執行個體。
- 將途播網址保存於 Cloud Shell 的：

```text
~/tw-news-m3u-url.txt
```

部署完成後，畫面會顯示類似：

```text
網站：
https://tw-news-m3u-xxxxxxxxxx-de.a.run.app

給途播的 M3U 網址：
https://tw-news-m3u-xxxxxxxxxx-de.a.run.app/live.m3u?key=你的播放權杖
```

先用瀏覽器打開網站，輸入播放權杖並測試頻道，再將完整 M3U 網址貼進途播。預設 8 台應在數秒內完成測試。

## 啟用 GitHub Actions 自動部署

第一次 Cloud Run 部署成功後，在 Cloud Shell 的專案目錄執行：

```bash
bash gcp/setup-github-actions.sh
```

它會建立：

- Artifact Registry Docker repository。
- GitHub 專用部署服務帳戶。
- 只信任 `roger9677gmail/tw-news-m3u` 的 `main` 分支的 Workload Identity Provider。
- 必要的最小 IAM 權限。

若 Cloud Shell 中的 GitHub CLI 已登入，腳本會自動建立 GitHub repository variables。否則請到：

```text
GitHub repo → Settings → Secrets and variables → Actions → Variables
```

依腳本輸出的內容建立：

```text
GCP_PROJECT_ID
GCP_REGION
GCP_WORKLOAD_IDENTITY_PROVIDER
GCP_DEPLOY_SERVICE_ACCOUNT
GCP_RUNTIME_SERVICE_ACCOUNT
```

之後到 GitHub **Actions → Deploy to Google Cloud Run → Run workflow**。設定完成後，`main` 的應用程式、Dockerfile 或頻道設定變更也會自動部署。

## Cloud Run 設定理由

```text
region: asia-east1
port: 8080
cpu: 1
memory: 1Gi
concurrency: 20
min-instances: 0
max-instances: 1
request timeout: 3600 seconds
```

`max-instances=1` 是必要保守設定：目前媒體 URL 的短期權杖保存於單一 Python 程序記憶體中。多執行個體可能導致後續 HLS 分段被路由到沒有該權杖的執行個體。

`min-instances=0` 可降低閒置費用，但第一次開台可能需要等待冷啟動。若更重視速度，可改成 `1`，但即使無人使用也可能產生費用。

## 費用提醒

影片資料會經過 Cloud Run 轉送，所以除了 CPU、記憶體和請求費用，還可能產生大量對外資料傳輸費。720p 直播長時間播放時，流量通常會比一般網站高很多。請在 Google Cloud 設定預算與通知。

## YouTube 限制

即使部署位於台灣區域，Google Cloud 的出口仍屬資料中心網路。YouTube 可能要求 Cookie、登入或機器人驗證。請勿將個人 YouTube Cookie 直接提交到公開 GitHub repo。

預設 8 台不依賴 YouTube 解析。只有自行加入、且沒有 `fourgtv` 官方來源設定的頻道，才會走 YouTube 備援並可能遇到機器人驗證。

## 常用指令

查看服務：

```bash
gcloud run services describe tw-news-m3u --region asia-east1
```

查看最近記錄：

```bash
gcloud run services logs read tw-news-m3u --region asia-east1 --limit 100
```

重新讀取途播網址：

```bash
cat ~/tw-news-m3u-url.txt
```

刪除 Cloud Run 服務：

```bash
gcloud run services delete tw-news-m3u --region asia-east1
```

刪除服務不會自動刪除 Secret Manager、Artifact Registry 或已建立的 IAM 設定。
