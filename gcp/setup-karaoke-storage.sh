#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-asia-east1}"
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-tw-news-runtime}"
GITHUB_REPO="${GITHUB_REPO:-roger9677gmail/tw-news-m3u}"
KARAOKE_BUCKET="${KARAOKE_BUCKET:-${PROJECT_ID}-karaoke}"

[[ -n "$PROJECT_ID" && "$PROJECT_ID" != "(unset)" ]] || {
  printf 'ERROR: 請設定 PROJECT_ID。\n' >&2
  exit 1
}
[[ -f gcp/karaoke-cors.json ]] || {
  printf 'ERROR: 請在專案根目錄執行。\n' >&2
  exit 1
}

RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud services enable storage.googleapis.com --project "$PROJECT_ID" --quiet

if ! gcloud storage buckets describe "gs://${KARAOKE_BUCKET}" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${KARAOKE_BUCKET}" \
    --project "$PROJECT_ID" \
    --location "$REGION" \
    --uniform-bucket-level-access \
    --public-access-prevention
fi

gcloud storage buckets update "gs://${KARAOKE_BUCKET}" \
  --public-access-prevention \
  --uniform-bucket-level-access \
  --cors-file=gcp/karaoke-cors.json \
  --project "$PROJECT_ID"

gcloud storage buckets add-iam-policy-binding "gs://${KARAOKE_BUCKET}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role=roles/storage.objectAdmin \
  --project "$PROJECT_ID" >/dev/null

if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  gh variable set KARAOKE_BUCKET --repo "$GITHUB_REPO" --body "$KARAOKE_BUCKET"
fi

printf 'Karaoke bucket ready: gs://%s\n' "$KARAOKE_BUCKET"
