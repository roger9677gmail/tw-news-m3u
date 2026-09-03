#!/usr/bin/env bash
set -Eeuo pipefail

# Finish an existing Cloud Run deployment, force a public run.app endpoint,
# distinguish a Cloud Run edge 404 from an application 404, and print the
# complete Tubo playlist URL after verification.

REGION="${REGION:-asia-east1}"
SERVICE="${SERVICE:-tw-news-m3u}"
SECRET_NAME="${SECRET_NAME:-tw-news-access-key}"
URL_FILE="${URL_FILE:-$HOME/tw-news-m3u-url.txt}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"

log() {
  printf '\n==> %s\n' "$*"
}

fail() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  rm -f "${TMP_BODY:-}" "${TMP_HEADERS:-}" "${TMP_AUTH_BODY:-}" "${TMP_AUTH_HEADERS:-}" "${TMP_PROXY_LOG:-}" 2>/dev/null || true
  if [[ -n "${PROXY_PID:-}" ]]; then
    kill "$PROXY_PID" >/dev/null 2>&1 || true
    wait "$PROXY_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

command -v gcloud >/dev/null 2>&1 || fail "找不到 gcloud；請在 Google Cloud Shell 執行。"
command -v curl >/dev/null 2>&1 || fail "找不到 curl。"
command -v python3 >/dev/null 2>&1 || fail "找不到 python3。"
[[ -n "$PROJECT_ID" && "$PROJECT_ID" != "(unset)" ]] || fail "尚未設定 Google Cloud Project ID。"

gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1 \
  || fail "目前帳號無法存取專案：${PROJECT_ID}"
gcloud config set project "$PROJECT_ID" >/dev/null

gcloud run services describe "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" >/dev/null 2>&1 \
  || fail "找不到 Cloud Run 服務 ${SERVICE}（${PROJECT_ID}/${REGION}）。"

log "強制啟用公開 run.app 端點"
# Disable both Invoker IAM checks and IAP unconditionally. Tubo cannot attach
# Google identity tokens; the application still protects playlists with its
# own ACCESS_KEY.
gcloud run services update "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --ingress=all \
  --default-url \
  --no-invoker-iam-check \
  --no-iap \
  --quiet >/dev/null \
  || fail "無法把 Cloud Run 設為公開；可能受到組織政策限制。"

SERVICE_URL="$(gcloud run services describe "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --format='value(status.url)' 2>/dev/null || true)"
[[ -n "$SERVICE_URL" ]] || fail "Cloud Run 沒有回傳服務網址。"
printf 'Service URL: %s\n' "$SERVICE_URL"

TMP_BODY="$(mktemp)"
TMP_HEADERS="$(mktemp)"
TMP_AUTH_BODY="$(mktemp)"
TMP_AUTH_HEADERS="$(mktemp)"
TMP_PROXY_LOG="$(mktemp)"
HEALTH_URL="${SERVICE_URL}/healthz"
HTTP_CODE=""
HEALTH_OK=false

log "檢查未登入的 /healthz"
for attempt in $(seq 1 5); do
  : >"$TMP_BODY"
  : >"$TMP_HEADERS"
  HTTP_CODE="$(curl \
    --silent \
    --show-error \
    --location \
    --connect-timeout 10 \
    --max-time 30 \
    --dump-header "$TMP_HEADERS" \
    --output "$TMP_BODY" \
    --write-out '%{http_code}' \
    "$HEALTH_URL" 2>/dev/null || true)"

  if [[ "$HTTP_CODE" == "200" ]] && grep -q '"ok"[[:space:]]*:[[:space:]]*true' "$TMP_BODY"; then
    HEALTH_OK=true
    break
  fi

  printf '第 %s 次：HTTP %s\n' "$attempt" "${HTTP_CODE:-連線失敗}"
  sleep 2
done

if [[ "$HEALTH_OK" != "true" ]]; then
  printf '\n未登入回應標頭：\n' >&2
  sed -n '1,30p' "$TMP_HEADERS" >&2 || true
  printf '\n未登入回應內容（HTTP %s）：\n' "${HTTP_CODE:-連線失敗}" >&2
  sed -n '1,30p' "$TMP_BODY" >&2 || true

  log "使用 Google 身分權杖測試同一網址"
  ID_TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
  AUTH_CODE=""
  if [[ -n "$ID_TOKEN" ]]; then
    AUTH_CODE="$(curl \
      --silent \
      --show-error \
      --location \
      --connect-timeout 10 \
      --max-time 30 \
      --header "Authorization: Bearer ${ID_TOKEN}" \
      --dump-header "$TMP_AUTH_HEADERS" \
      --output "$TMP_AUTH_BODY" \
      --write-out '%{http_code}' \
      "$HEALTH_URL" 2>/dev/null || true)"
    printf '登入測試：HTTP %s\n' "${AUTH_CODE:-連線失敗}" >&2
    sed -n '1,20p' "$TMP_AUTH_HEADERS" >&2 || true
    sed -n '1,20p' "$TMP_AUTH_BODY" >&2 || true
  else
    printf '無法取得 Google identity token。\n' >&2
  fi

  log "透過 Cloud Run 本機認證代理測試應用程式"
  PROXY_PORT="${PROXY_PORT:-18080}"
  gcloud run services proxy "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --port "$PROXY_PORT" >"$TMP_PROXY_LOG" 2>&1 &
  PROXY_PID=$!
  PROXY_CODE=""
  PROXY_BODY=""
  for _ in $(seq 1 10); do
    PROXY_BODY="$(curl --silent --show-error --max-time 10 \
      --write-out $'\n%{http_code}' \
      "http://127.0.0.1:${PROXY_PORT}/healthz" 2>/dev/null || true)"
    PROXY_CODE="${PROXY_BODY##*$'\n'}"
    PROXY_BODY="${PROXY_BODY%$'\n'*}"
    [[ "$PROXY_CODE" =~ ^[0-9]{3}$ ]] && break
    sleep 1
  done
  printf '認證代理測試：HTTP %s\n' "${PROXY_CODE:-連線失敗}" >&2
  printf '%s\n' "$PROXY_BODY" >&2
  printf '\n代理記錄：\n' >&2
  sed -n '1,40p' "$TMP_PROXY_LOG" >&2 || true

  printf '\nCloud Run 有效設定：\n' >&2
  gcloud run services describe "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --format='yaml(metadata.name,metadata.annotations,status.url,status.conditions,status.traffic,spec.template.spec.containers)' >&2 || true

  printf '\n最近 20 分鐘的 Cloud Run HTTP 請求記錄：\n' >&2
  gcloud logging read \
    "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE}\" AND logName=\"projects/${PROJECT_ID}/logs/run.googleapis.com%2Frequests\"" \
    --project "$PROJECT_ID" \
    --freshness=20m \
    --limit=30 \
    --format='table(timestamp,httpRequest.status,httpRequest.requestMethod,httpRequest.requestUrl,resource.labels.revision_name)' >&2 || true

  printf '\n最近的容器記錄：\n' >&2
  gcloud run services logs read "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --limit=100 >&2 || true

  if [[ "$PROXY_CODE" == "200" ]] && [[ "$PROXY_BODY" == *'"ok"'* ]]; then
    fail "應用程式本身正常，但公開 run.app 路由被 Google Cloud 邊緣或組織政策阻擋。"
  fi
  if [[ "$AUTH_CODE" == "200" ]]; then
    fail "服務需要 Google 登入身分；公開設定仍被組織政策阻擋。"
  fi
  fail "公開與認證測試都未通過；請提供上方診斷輸出，但遮住任何 key= 後的內容。"
fi

ACCESS_KEY="$(gcloud secrets versions access latest \
  --secret "$SECRET_NAME" \
  --project "$PROJECT_ID" 2>/dev/null || true)"
[[ -n "$ACCESS_KEY" ]] || fail "無法讀取 Secret Manager 的 ${SECRET_NAME}。"

ENCODED_KEY="$(ACCESS_KEY="$ACCESS_KEY" python3 - <<'PY'
import os
import urllib.parse
print(urllib.parse.quote(os.environ["ACCESS_KEY"], safe=""))
PY
)"
PLAYLIST_URL="${SERVICE_URL}/live.m3u?key=${ENCODED_KEY}"

: >"$TMP_BODY"
PLAYLIST_CODE="$(curl \
  --silent \
  --show-error \
  --location \
  --connect-timeout 10 \
  --max-time 30 \
  --output "$TMP_BODY" \
  --write-out '%{http_code}' \
  "$PLAYLIST_URL" 2>/dev/null || true)"

if [[ "$PLAYLIST_CODE" != "200" ]] || ! grep -q '^#EXTM3U' "$TMP_BODY"; then
  printf '\nM3U 檢查失敗：HTTP %s\n' "${PLAYLIST_CODE:-連線失敗}" >&2
  sed -n '1,20p' "$TMP_BODY" >&2 || true
  fail "服務健康，但 M3U 清單無法讀取。"
fi

umask 077
printf '%s\n' "$PLAYLIST_URL" > "$URL_FILE"
printf '%s\n' "$PROJECT_ID" > "$HOME/tw-news-m3u-project-id.txt"

cat <<EOF

部署確認完成。

Project ID：
${PROJECT_ID}

網站：
${SERVICE_URL}

給途播的 M3U 網址：
${PLAYLIST_URL}

網址已另存於：
${URL_FILE}

完整網址含有專用播放權杖，請勿公開貼到 GitHub Issue 或社群。
EOF
