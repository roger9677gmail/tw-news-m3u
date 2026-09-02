# 將台灣新聞直播部署到 Cloud Run

<walkthrough-tutorial-duration duration="10"></walkthrough-tutorial-duration>

這份教學會把目前 GitHub 專案部署到 Google Cloud Run 台灣區域，最後產生可貼進途播的固定 HTTPS M3U 網址。

部署會建立 Cloud Run、Cloud Build、Secret Manager 與服務帳戶資源，可能產生 Google Cloud 費用。

## 1. 選擇並授權 Google Cloud 專案

本次預設使用既有專案：

```text
infinite-mantra-458303-p8
```

先選擇有啟用計費的專案：

<walkthrough-project-setup billing="true"></walkthrough-project-setup>

接著把 Cloud Shell 的目前專案固定為上述 Project ID。點程式碼區塊旁的複製按鈕，貼到終端機執行：

```bash
gcloud config set project infinite-mantra-458303-p8
```

第一次執行 Google Cloud 指令時會出現 **Authorize Cloud Shell**，請按 **Authorize**。這是 Google 要求帳號本人完成的授權。

## 2. 一鍵部署到 Cloud Run

執行下列指令：

```bash
PROJECT_ID=infinite-mantra-458303-p8 REGION=asia-east1 bash gcp/deploy-cloud-run.sh
```

腳本會自動啟用必要 API、建立播放權杖、建置 Docker image、部署 Cloud Run，並檢查 `/healthz`。

部署完成後，終端機會顯示：

```text
給途播的 M3U 網址：
https://...a.run.app/live.m3u?key=...
```

## 3. 顯示途播網址

之後隨時可執行：

```bash
cat ~/tw-news-m3u-url.txt
```

將顯示的完整 `https://.../live.m3u?key=...` 網址貼進途播。

<walkthrough-conclusion-trophy></walkthrough-conclusion-trophy>

部署完成。請先用瀏覽器打開 Cloud Run 網站並測試一個頻道；YouTube 有時仍會限制雲端資料中心 IP。
