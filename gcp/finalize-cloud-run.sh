#!/usr/bin/env bash
set -Eeuo pipefail

# Finish an existing Cloud Run deployment and choose the Cloud Run URL that
# actually reaches the service. Cloud Run reserves some URL paths ending in z,
# so the public smoke test uses /api/config instead of /healthz.

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
  rm -rf "${TMP_DIR:-}" 2>/dev/null || true
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

log "確認公開存取設定"
gcloud run services update "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --ingress=all \
  --default-url \
  --no-invoker-iam-check \
  --no-iap \
  --quiet >/dev/null \
  || fail "無法把 Cloud Run 設為公開；可能受到組織政策限制。"

TMP_DIR="$(mktemp -d)"
SERVICE_JSON="$TMP_DIR/service.json"

# Give the routing control plane a moment, then read a fresh service object.
sleep 5
gcloud run services describe "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --format=json >"$SERVICE_JSON"

mapfile -t CANDIDATE_URLS < <(python3 - "$SERVICE_JSON" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)

urls = []

def add(value):
    if isinstance(value, str):
        value = value.strip().rstrip("/")
        if value.startswith("https://") and value not in urls:
            urls.append(value)

# Prefer the service URL selected by the Cloud Run API.
add((data.get("status") or {}).get("url"))

annotation = ((data.get("metadata") or {}).get("annotations") or {}).get(
    "run.googleapis.com/urls"
)
if isinstance(annotation, str):
    try:
        parsed = json.loads(annotation)
    except json.JSONDecodeError:
        parsed = re.findall(r"https://[^\s\"']+", annotation)
    if isinstance(parsed, list):
        for item in parsed:
            add(item)

for url in urls:
    print(url)
PY
)

[[ "${#CANDIDATE_URLS[@]}" -gt 0 ]] || fail "Cloud Run 沒有回報任何服務網址。"

printf '\nCloud Run 回報的網址：\n'
printf '  %s\n' "${CANDIDATE_URLS[@]}"

SERVICE_URL=""
LAST_CODE=""
LAST_URL=""

log "逐一測試 /api/config，選出可用網址"
for round in $(seq 1 8); do
  for base_url in "${CANDIDATE_URLS[@]}"; do
    safe_name="$(printf '%s' "$base_url" | sha256sum | cut -c1-12)"
    body_file="$TMP_DIR/body-$safe_name"
    header_file="$TMP_DIR/header-$safe_name"
    probe_url="${base_url}/api/config?probe=$(date +%s)-${round}"

    result="$(curl \
      --silent \
      --show-error \
      --http1.1 \
      --connect-timeout 10 \
      --max-time 30 \
      --header 'Cache-Control: no-cache' \
      --dump-header "$header_file" \
      --output "$body_file" \
      --write-out '%{http_code}|%{url_effective}' \
      "$probe_url" 2>/dev/null || true)"

    code="${result%%|*}"
    effective_url="${result#*|}"
    LAST_CODE="$code"
    LAST_URL="$effective_url"
    printf '第 %s 輪：%s -> HTTP %s\n' "$round" "$base_url" "${code:-連線失敗}"

    if [[ "$code" == "200" ]] \
        && grep -Eq '"channel_count"[[:space:]]*:[[:space:]]*[0-9]+' "$body_file"; then
      SERVICE_URL="$base_url"
      break 2
    fi
  done
  sleep 3
done

if [[ -z "$SERVICE_URL" ]]; then
  printf '\nCloud Run status.url：\n' >&2
  python3 - "$SERVICE_JSON" <<'PY' >&2
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
print((data.get("status") or {}).get("url") or "(none)")
PY

  printf '\n最後測試：HTTP %s，URL %s\n' "${LAST_CODE:-連線失敗}" "${LAST_URL:-未知}" >&2
  printf '\n最近的 Cloud Run 請求記錄：\n' >&2
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

  fail "Cloud Run 已 Ready，但所有回報網址仍無法連到 /api/config。"
fi

printf '\n採用可用網址：%s\n' "$SERVICE_URL"

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

PLAYLIST_BODY="$TMP_DIR/playlist.m3u"
PLAYLIST_CODE="$(curl \
  --silent \
  --show-error \
  --http1.1 \
  --connect-timeout 10 \
  --max-time 30 \
  --header 'Cache-Control: no-cache' \
  --output "$PLAYLIST_BODY" \
  --write-out '%{http_code}' \
  "$PLAYLIST_URL" 2>/dev/null || true)"

if [[ "$PLAYLIST_CODE" != "200" ]] || ! grep -q '^#EXTM3U' "$PLAYLIST_BODY"; then
  printf '\nM3U 檢查失敗：HTTP %s\n' "${PLAYLIST_CODE:-連線失敗}" >&2
  sed -n '1,20p' "$PLAYLIST_BODY" >&2 || true
  fail "服務可以連線，但 M3U 清單無法讀取。"
fi

umask 077
printf '%s\n' "$PLAYLIST_URL" > "$URL_FILE"
printf '%s\n' "$PROJECT_ID" > "$HOME/tw-news-m3u-project-id.txt"
printf '%s\n' "$SERVICE_URL" > "$HOME/tw-news-m3u-service-url.txt"

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
