# 將台灣新聞直播部署到 Cloud Run

<walkthrough-tutorial-duration duration="10"></walkthrough-tutorial-duration>

這份教學會把目前 GitHub 專案部署到 Google Cloud Run 台灣區域，最後產生可貼進途播的固定 HTTPS M3U 網址。

部署會建立一個專用 Google Cloud Project、Cloud Run、Cloud Build、Secret Manager 與服務帳戶資源，可能產生 Google Cloud 費用。

## 1. 授權 Cloud Shell

第一次執行 Google Cloud 指令時會出現 **Authorize Cloud Shell**，請按 **Authorize**。這是 Google 要求帳號本人完成的授權。

先確認目前登入帳號與可用帳單帳戶：

```bash
gcloud auth list --filter=status:ACTIVE --format='value(account)'
gcloud billing accounts list --filter='open=true'
```

## 2. 建立專用專案並部署到 Cloud Run

先取得最新程式，再執行一鍵腳本：

```bash
git pull --ff-only
bash gcp/create-and-deploy.sh
```

腳本會：

- 從目前 Google 帳號找出開啟中的帳單帳戶。
- 自動建立類似 `tw-news-m3u-260903-a1b2c3` 的全新專用 Project ID。
- 將新專案連結到帳單帳戶。
- 啟用必要 API。
- 建立播放權杖。
- 建置 Docker image 並部署 Cloud Run。
- 檢查 `/healthz`。

若帳號有多個帳單帳戶，終端機會列出清單並要求輸入要使用的 `ACCOUNT_ID`。

如果公司政策禁止建立專案，可改用你有 Owner 權限且已啟用計費的既有專案：

```bash
PROJECT_ID=你的既有專案ID bash gcp/create-and-deploy.sh
```

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

查看實際建立的 Google Cloud Project ID：

```bash
cat ~/tw-news-m3u-project-id.txt
```

將顯示的完整 `https://.../live.m3u?key=...` 網址貼進途播。

<walkthrough-conclusion-trophy></walkthrough-conclusion-trophy>

部署完成。請先用瀏覽器打開 Cloud Run 網站並測試一個頻道；YouTube 有時仍會限制雲端資料中心 IP。
