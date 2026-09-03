#!/usr/bin/env bash
set -Eeuo pipefail

# Run this script from Google Cloud Shell in the repository root.
# Optional overrides: PROJECT_ID, REGION, SERVICE, ACCESS_KEY.

REGION="${REGION:-asia-east1}"
SERVICE="${SERVICE:-tw-news-m3u}"
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-tw-news-runtime}"
SECRET_NAME="${SECRET_NAME:-tw-news-access-key}"
URL_FILE="${URL_FILE:-$HOME/tw-news-m3u-url.txt}"

log() {
  printf '\n==> %s\n' "$*"
}

fail() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

command -v gcloud >/dev/null 2>&1 || fail "找不到 gcloud；請在 Google Cloud Shell 執行。"
command -v curl >/dev/null 2>&1 || fail "找不到 curl。"
[[ -f Dockerfile && -f channels.json ]] || fail "請在 tw-news-m3u 專案根目錄執行。"

ACTIVE_ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
[[ -n "$ACTIVE_ACCOUNT" && "$ACTIVE_ACCOUNT" != "(unset)" ]] || fail "尚未登入 Google Cloud。"

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "$PROJECT_ID" || "$PROJECT_ID" == "(unset)" ]]; then
  read -r -p "Google Cloud Project ID: " PROJECT_ID
fi
[[ -n "$PROJECT_ID" ]] || fail "必須提供 Google Cloud Project ID。"

gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1 \
  || fail "找不到專案 $PROJECT_ID，請先建立專案並啟用計費。"

gcloud config set project "$PROJECT_ID" >/dev/null

log "啟用 Cloud Run、Cloud Build、Artifact Registry、Secret Manager"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  compute.googleapis.com \
  iam.googleapis.com \
  logging.googleapis.com \
  --project "$PROJECT_ID" >/dev/null

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
BUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

for _ in $(seq 1 20); do
  if gcloud iam service-accounts describe "$BUILD_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
    break
  fi
  sleep 3
done

gcloud iam service-accounts describe "$BUILD_SA" --project "$PROJECT_ID" >/dev/null 2>&1 \
  || fail "尚未建立 Cloud Build 使用的預設服務帳戶，請稍後重新執行。"

log "授權 Cloud Build 建置來源程式"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/run.builder" \
  --condition=None \
  --quiet >/dev/null

log "建立最小權限的 Cloud Run 執行身分"
if ! gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
    --display-name="Taiwan News M3U runtime" \
    --project "$PROJECT_ID" >/dev/null
fi

log "建立或讀取播放權杖"
SECRET_EXISTS=false
if gcloud secrets describe "$SECRET_NAME" --project "$PROJECT_ID" >/dev/null 2>&1; then
  SECRET_EXISTS=true
else
  gcloud secrets create "$SECRET_NAME" \
    --replication-policy=automatic \
    --project "$PROJECT_ID" >/dev/null
fi

ACCESS_KEY="${ACCESS_KEY:-}"
if [[ -z "$ACCESS_KEY" && "$SECRET_EXISTS" == "true" ]]; then
  ACCESS_KEY="$(gcloud secrets versions access latest \
    --secret "$SECRET_NAME" \
    --project "$PROJECT_ID" 2>/dev/null || true)"
fi

if [[ -z "$ACCESS_KEY" ]]; then
  ACCESS_KEY="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
  printf '%s' "$ACCESS_KEY" | gcloud secrets versions add "$SECRET_NAME" \
    --data-file=- \
    --project "$PROJECT_ID" >/dev/null
elif [[ "$SECRET_EXISTS" == "false" ]]; then
  printf '%s' "$ACCESS_KEY" | gcloud secrets versions add "$SECRET_NAME" \
    --data-file=- \
    --project "$PROJECT_ID" >/dev/null
fi

gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project "$PROJECT_ID" \
  --quiet >/dev/null

ACCOUNT_MEMBER="user:${ACTIVE_ACCOUNT}"
if [[ "$ACTIVE_ACCOUNT" == *.gserviceaccount.com ]]; then
  ACCOUNT_MEMBER="serviceAccount:${ACTIVE_ACCOUNT}"
fi

gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member="$ACCOUNT_MEMBER" \
  --role="roles/iam.serviceAccountUser" \
  --project "$PROJECT_ID" \
  --quiet >/dev/null

log "建置並部署到 Cloud Run 台灣區域 ${REGION}"
gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --no-invoker-iam-check \
  --no-iap \
  --default-url \
  --ingress all \
  --service-account "$RUNTIME_SA" \
  --port 8080 \
  --cpu 1 \
  --memory 1Gi \
  --concurrency 20 \
  --min-instances 0 \
  --max-instances 1 \
  --timeout 3600 \
  --execution-environment gen2 \
  --cpu-boost \
  --set-secrets="ACCESS_KEY=${SECRET_NAME}:latest" \
  --set-env-vars="APP_NAME=台灣新聞直播 M3U,MAX_HEIGHT=720,RESOLVER_TTL_SECONDS=900,RESOLVER_FAILURE_TTL_SECONDS=90,RESOLVER_TIMEOUT_SECONDS=100,MAX_RESOLVER_CONCURRENCY=2,MEDIA_TOKEN_TTL_SECONDS=21600,MAX_TOKEN_ENTRIES=30000,UPSTREAM_TIMEOUT_SECONDS=25,LOG_LEVEL=INFO,TZ=Asia/Taipei" \
  --quiet

PROJECT_ID="$PROJECT_ID" REGION="$REGION" SERVICE="$SERVICE" \
  SECRET_NAME="$SECRET_NAME" URL_FILE="$URL_FILE" \
  bash gcp/finalize-cloud-run.sh
