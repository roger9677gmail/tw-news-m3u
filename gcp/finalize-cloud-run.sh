#!/usr/bin/env bash
set -Eeuo pipefail

# Finish a Cloud Run deployment that has already created the service.
# This version also repairs common Cloud Run 404 causes before checking the app:
# disabled run.app URL, restricted ingress, or missing public invocation access.

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

log "修復 Cloud Run 公開網址與 ingress"
gcloud run services update "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --ingress=all \
  --default-url \
  --quiet >/dev/null

# Tubo cannot attach a Google identity token, so the service must accept public
# requests. The application itself still protects playlists with ACCESS_KEY.
if ! gcloud run services add-iam-policy-binding "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --member="allUsers" \
    --role="roles/run.invoker" \
    --condition=None \
    --quiet >/dev/null 2>&1; then
  log "IAM 公開綁定未成功，改用停用 Invoker IAM 檢查"
  gcloud run services update "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --no-invoker-iam-check \
    --quiet >/dev/null \
    || fail "Google Cloud 組織政策不允許公開 Cloud Run；途播將無法直接連線。"
fi

SERVICE_URL="$(gcloud run services describe "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --format='value(status.url)' 2>/dev/null || true)"
[[ -n "$SERVICE_URL" ]] || fail "Cloud Run 沒有回傳服務網址。"

TMP_BODY="$(mktemp)"
TMP_HEADERS="$(mktemp)"
trap 'rm -f "$TMP_BODY" "$TMP_HEADERS"' EXIT
HEALTH_URL="${SERVICE_URL}/healthz"
HTTP_CODE=""
HEALTH_OK=false

log "確認 Cloud Run 公開路由與應用程式"
for attempt in $(seq 1 20); do
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

  printf '尚未就緒（第 %s 次，HTTP %s）\n' "$attempt" "${HTTP_CODE:-連線失敗}"
  sleep 3
done

if [[ "$HEALTH_OK" != "true" ]]; then
  printf '\n最後回應標頭：\n' >&2
  sed -n '1,30p' "$TMP_HEADERS" >&2 || true
  printf '\n最後回應內容（HTTP %s）：\n' "${HTTP_CODE:-連線失敗}" >&2
  sed -n '1,30p' "$TMP_BODY" >&2 || true

  printf '\nCloud Run 有效設定：\n' >&2
  gcloud run services describe "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --format='yaml(metadata.name,status.url,status.conditions,spec.template.spec.containers,spec.traffic)' >&2 || true

  printf '\n最近的 Cloud Run 記錄：\n' >&2
  gcloud run services logs read "$SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --limit 100 >&2 || true

  fail "服務已部署，但 /healthz 仍未通過。"
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
