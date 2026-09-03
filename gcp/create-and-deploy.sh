#!/usr/bin/env bash
set -Eeuo pipefail

# Create a dedicated Google Cloud project when needed, link an open billing
# account, and then run deploy-cloud-run.sh.
#
# Optional overrides:
#   PROJECT_ID, PROJECT_NAME, BILLING_ACCOUNT, REGION, FOLDER_ID, ORG_ID

REGION="${REGION:-asia-east1}"
PROJECT_NAME="${PROJECT_NAME:-TW News M3U}"
PROJECT_ID="${PROJECT_ID:-}"
BILLING_ACCOUNT="${BILLING_ACCOUNT:-}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_ID_FILE="${PROJECT_ID_FILE:-$HOME/tw-news-m3u-project-id.txt}"

log() {
  printf '\n==> %s\n' "$*"
}

fail() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

cd "$ROOT_DIR"
command -v gcloud >/dev/null 2>&1 || fail "找不到 gcloud；請在 Google Cloud Shell 執行。"
command -v python3 >/dev/null 2>&1 || fail "找不到 python3。"
[[ -f Dockerfile && -f gcp/deploy-cloud-run.sh ]] || fail "請在 tw-news-m3u 專案中執行。"

ACTIVE_ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
[[ -n "$ACTIVE_ACCOUNT" && "$ACTIVE_ACCOUNT" != "(unset)" ]] \
  || fail "Cloud Shell 尚未登入 Google 帳號。"
log "目前 Google 帳號：${ACTIVE_ACCOUNT}"

if [[ -z "$BILLING_ACCOUNT" ]]; then
  mapfile -t OPEN_BILLING_ACCOUNTS < <(
    gcloud billing accounts list \
      --filter='open=true' \
      --format='value(name)' 2>/dev/null \
      | sed 's#^billingAccounts/##' \
      | sed '/^[[:space:]]*$/d'
  )

  case "${#OPEN_BILLING_ACCOUNTS[@]}" in
    0)
      fail "此帳號看不到可用的開啟中帳單帳戶。請先在 Google Cloud Console 啟用帳單，或切換到有帳單權限的 Google 帳號。"
      ;;
    1)
      BILLING_ACCOUNT="${OPEN_BILLING_ACCOUNTS[0]}"
      ;;
    *)
      printf '\n可用帳單帳戶：\n'
      gcloud billing accounts list \
        --filter='open=true' \
        --format='table(name.basename():label=ACCOUNT_ID,displayName:label=NAME,open:label=OPEN)'
      read -r -p "請輸入要使用的 ACCOUNT_ID：" BILLING_ACCOUNT
      ;;
  esac
fi

[[ -n "$BILLING_ACCOUNT" ]] || fail "沒有取得帳單帳戶 ID。"
log "使用帳單帳戶：${BILLING_ACCOUNT}"

if [[ -z "$PROJECT_ID" ]]; then
  RANDOM_SUFFIX="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(3))
PY
)"
  PROJECT_ID="tw-news-m3u-$(date -u +%y%m%d)-${RANDOM_SUFFIX}"
fi

if ! [[ "$PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]; then
  fail "PROJECT_ID 格式不正確：${PROJECT_ID}。需為 6–30 個小寫英數字或連字號，且以英文字母開頭。"
fi

if gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  log "使用既有專案：${PROJECT_ID}"
else
  log "建立專用 Google Cloud 專案：${PROJECT_ID}"
  CREATE_ARGS=(
    gcloud projects create "$PROJECT_ID"
    "--name=${PROJECT_NAME}"
    --quiet
  )
  if [[ -n "${FOLDER_ID:-}" ]]; then
    CREATE_ARGS+=("--folder=${FOLDER_ID}")
  elif [[ -n "${ORG_ID:-}" ]]; then
    CREATE_ARGS+=("--organization=${ORG_ID}")
  fi

  if ! "${CREATE_ARGS[@]}"; then
    fail "建立專案失敗。公司 Google Workspace 可能限制建立專案；可改用你有 Owner 權限的既有專案：PROJECT_ID=既有專案ID bash gcp/create-and-deploy.sh"
  fi
fi

log "等待專案可以使用"
PROJECT_READY=false
for _ in $(seq 1 30); do
  STATE="$(gcloud projects describe "$PROJECT_ID" --format='value(lifecycleState)' 2>/dev/null || true)"
  if [[ "$STATE" == "ACTIVE" ]]; then
    PROJECT_READY=true
    break
  fi
  sleep 3
done
[[ "$PROJECT_READY" == "true" ]] || fail "專案尚未進入 ACTIVE 狀態，請稍後重新執行。"

log "將專案連結到帳單帳戶"
BILLING_LINKED=false
for _ in $(seq 1 20); do
  if gcloud billing projects link "$PROJECT_ID" \
      --billing-account="$BILLING_ACCOUNT" \
      --quiet >/dev/null 2>&1; then
    BILLING_LINKED=true
    break
  fi
  sleep 3
done
[[ "$BILLING_LINKED" == "true" ]] \
  || fail "無法連結帳單帳戶。需要專案建立權限及 Billing Account User 權限。"

BILLING_ENABLED="$(
  gcloud billing projects describe "$PROJECT_ID" \
    --format='value(billingEnabled)' 2>/dev/null || true
)"
[[ "$BILLING_ENABLED" == "True" || "$BILLING_ENABLED" == "true" ]] \
  || fail "專案已建立，但帳單尚未啟用。"

gcloud config set project "$PROJECT_ID" >/dev/null
umask 077
printf '%s\n' "$PROJECT_ID" > "$PROJECT_ID_FILE"

cat <<EOF

專案與帳單已準備完成：
  Project ID: ${PROJECT_ID}
  Region:     ${REGION}

接著開始部署 Cloud Run。
EOF

PROJECT_ID="$PROJECT_ID" REGION="$REGION" bash gcp/deploy-cloud-run.sh
