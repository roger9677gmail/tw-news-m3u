#!/usr/bin/env bash
set -Eeuo pipefail

# Optional overrides:
# PROJECT_ID, REGION, GITHUB_REPO, POOL_ID, PROVIDER_ID,
# DEPLOY_SA_NAME, RUNTIME_SA_NAME, AR_REPOSITORY, SECRET_NAME.

REGION="${REGION:-asia-east1}"
GITHUB_REPO="${GITHUB_REPO:-roger9677gmail/tw-news-m3u}"
POOL_ID="${POOL_ID:-github-actions}"
PROVIDER_ID="${PROVIDER_ID:-tw-news-m3u}"
DEPLOY_SA_NAME="${DEPLOY_SA_NAME:-github-cloud-run-deployer}"
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-tw-news-runtime}"
AR_REPOSITORY="${AR_REPOSITORY:-tw-news-m3u}"
SECRET_NAME="${SECRET_NAME:-tw-news-access-key}"

log() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

command -v gcloud >/dev/null 2>&1 || fail "請在 Google Cloud Shell 執行。"

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "$PROJECT_ID" || "$PROJECT_ID" == "(unset)" ]]; then
  read -r -p "Google Cloud Project ID: " PROJECT_ID
fi
[[ -n "$PROJECT_ID" ]] || fail "必須提供 Google Cloud Project ID。"

gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1 \
  || fail "找不到專案 $PROJECT_ID。"
gcloud config set project "$PROJECT_ID" >/dev/null

log "啟用 GitHub Actions 部署所需 API"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  --project "$PROJECT_ID" >/dev/null

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
DEPLOY_SA="${DEPLOY_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

log "建立 Artifact Registry"
if ! gcloud artifacts repositories describe "$AR_REPOSITORY" \
  --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$AR_REPOSITORY" \
    --repository-format=docker \
    --location "$REGION" \
    --description="Taiwan News M3U images" \
    --project "$PROJECT_ID" >/dev/null
fi

log "建立部署及執行服務帳戶"
if ! gcloud iam service-accounts describe "$DEPLOY_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$DEPLOY_SA_NAME" \
    --display-name="GitHub Cloud Run deployer" \
    --project "$PROJECT_ID" >/dev/null
fi
if ! gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
    --display-name="Taiwan News M3U runtime" \
    --project "$PROJECT_ID" >/dev/null
fi

for role in \
  roles/run.admin \
  roles/artifactregistry.writer \
  roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${DEPLOY_SA}" \
    --role="$role" \
    --condition=None \
    --quiet >/dev/null
done

gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member="serviceAccount:${DEPLOY_SA}" \
  --role="roles/iam.serviceAccountUser" \
  --project "$PROJECT_ID" \
  --quiet >/dev/null

log "確認播放權杖 Secret"
if ! gcloud secrets describe "$SECRET_NAME" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud secrets create "$SECRET_NAME" \
    --replication-policy=automatic \
    --project "$PROJECT_ID" >/dev/null
  ACCESS_KEY="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
  printf '%s' "$ACCESS_KEY" | gcloud secrets versions add "$SECRET_NAME" \
    --data-file=- --project "$PROJECT_ID" >/dev/null
fi

gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project "$PROJECT_ID" \
  --quiet >/dev/null

gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
  --member="serviceAccount:${DEPLOY_SA}" \
  --role="roles/secretmanager.viewer" \
  --project "$PROJECT_ID" \
  --quiet >/dev/null

log "建立只信任本 GitHub repo main 分支的 Workload Identity"
if ! gcloud iam workload-identity-pools describe "$POOL_ID" \
  --location=global --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$POOL_ID" \
    --location=global \
    --display-name="GitHub Actions" \
    --description="OIDC identities for GitHub Actions" \
    --project "$PROJECT_ID" >/dev/null
fi

if ! gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --workload-identity-pool="$POOL_ID" \
  --location=global \
  --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
    --workload-identity-pool="$POOL_ID" \
    --location=global \
    --issuer-uri="https://token.actions.githubusercontent.com/" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository=='${GITHUB_REPO}' && assertion.ref=='refs/heads/main'" \
    --project "$PROJECT_ID" >/dev/null
fi

POOL_RESOURCE="$(gcloud iam workload-identity-pools describe "$POOL_ID" \
  --location=global --project "$PROJECT_ID" --format='value(name)')"
PROVIDER_RESOURCE="$(gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --workload-identity-pool="$POOL_ID" \
  --location=global \
  --project "$PROJECT_ID" \
  --format='value(name)')"

PRINCIPAL_SET="principalSet://iam.googleapis.com/${POOL_RESOURCE}/attribute.repository/${GITHUB_REPO}"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" \
  --member="$PRINCIPAL_SET" \
  --role="roles/iam.workloadIdentityUser" \
  --project "$PROJECT_ID" \
  --quiet >/dev/null

cat <<EOF

Google Cloud 端設定完成。

請在 GitHub repo Settings → Secrets and variables → Actions → Variables 新增：

GCP_PROJECT_ID=${PROJECT_ID}
GCP_REGION=${REGION}
GCP_WORKLOAD_IDENTITY_PROVIDER=${PROVIDER_RESOURCE}
GCP_DEPLOY_SERVICE_ACCOUNT=${DEPLOY_SA}
GCP_RUNTIME_SERVICE_ACCOUNT=${RUNTIME_SA}

EOF

# Cloud Shell may already have an authenticated GitHub CLI. Set variables automatically when possible.
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  log "偵測到已登入的 GitHub CLI，自動寫入 repository variables"
  gh variable set GCP_PROJECT_ID --repo "$GITHUB_REPO" --body "$PROJECT_ID"
  gh variable set GCP_REGION --repo "$GITHUB_REPO" --body "$REGION"
  gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --repo "$GITHUB_REPO" --body "$PROVIDER_RESOURCE"
  gh variable set GCP_DEPLOY_SERVICE_ACCOUNT --repo "$GITHUB_REPO" --body "$DEPLOY_SA"
  gh variable set GCP_RUNTIME_SERVICE_ACCOUNT --repo "$GITHUB_REPO" --body "$RUNTIME_SA"
  printf '\nGitHub variables 已自動設定。到 Actions 手動執行「Deploy to Google Cloud Run」。\n'
else
  printf 'GitHub CLI 尚未登入，因此請依上方內容手動新增 Variables。\n'
fi
