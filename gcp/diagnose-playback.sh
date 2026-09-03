#!/usr/bin/env bash
set -Eeuo pipefail

# Diagnose why Tubo can import the playlist but a selected channel keeps loading.
# Usage: bash gcp/diagnose-playback.sh [channel-id]
# The ACCESS_KEY is read from Secret Manager and is never printed.

REGION="${REGION:-asia-east1}"
SERVICE="${SERVICE:-tw-news-m3u}"
SECRET_NAME="${SECRET_NAME:-tw-news-access-key}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
CHANNEL_ID="${1:-tvbs-news}"

log() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
cleanup() { rm -rf "${TMP_DIR:-}" 2>/dev/null || true; }
trap cleanup EXIT

command -v gcloud >/dev/null 2>&1 || fail "找不到 gcloud；請在 Google Cloud Shell 執行。"
command -v curl >/dev/null 2>&1 || fail "找不到 curl。"
command -v python3 >/dev/null 2>&1 || fail "找不到 python3。"
[[ -n "$PROJECT_ID" && "$PROJECT_ID" != "(unset)" ]] || fail "尚未設定 Google Cloud Project ID。"

gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1 \
  || fail "目前帳號無法存取專案：${PROJECT_ID}"
gcloud config set project "$PROJECT_ID" >/dev/null

TMP_DIR="$(mktemp -d)"
SERVICE_JSON="$TMP_DIR/service.json"
gcloud run services describe "$SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --format=json >"$SERVICE_JSON"

mapfile -t CANDIDATE_URLS < <(python3 - "$SERVICE_JSON" <<'PY'
import json, re, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
urls = []
def add(value):
    if isinstance(value, str):
        value = value.strip().rstrip("/")
        if value.startswith("https://") and value not in urls:
            urls.append(value)
add((data.get("status") or {}).get("url"))
annotation = ((data.get("metadata") or {}).get("annotations") or {}).get("run.googleapis.com/urls")
if isinstance(annotation, str):
    try:
        values = json.loads(annotation)
    except json.JSONDecodeError:
        values = re.findall(r"https://[^\s\"']+", annotation)
    if isinstance(values, list):
        for value in values:
            add(value)
for value in urls:
    print(value)
PY
)
[[ "${#CANDIDATE_URLS[@]}" -gt 0 ]] || fail "Cloud Run 沒有回報服務網址。"

SERVICE_URL=""
for base_url in "${CANDIDATE_URLS[@]}"; do
  body="$TMP_DIR/config.json"
  code="$(curl --silent --show-error --http1.1 --connect-timeout 10 --max-time 30 \
    --output "$body" --write-out '%{http_code}' \
    "${base_url}/api/config?diagnose=$(date +%s)" 2>/dev/null || true)"
  if [[ "$code" == "200" ]] && grep -q '"channel_count"' "$body"; then
    SERVICE_URL="$base_url"
    break
  fi
done
[[ -n "$SERVICE_URL" ]] || fail "Cloud Run 網址無法連到 /api/config。"
printf 'Cloud Run URL: %s\n' "$SERVICE_URL"

ACCESS_KEY="$(gcloud secrets versions access latest \
  --secret "$SECRET_NAME" --project "$PROJECT_ID" 2>/dev/null || true)"
[[ -n "$ACCESS_KEY" ]] || fail "無法讀取播放權杖。"
AUTH_HEADER="X-Access-Key: ${ACCESS_KEY}"

log "讀取目前各頻道解析狀態"
STATUS_FILE="$TMP_DIR/status.json"
STATUS_CODE="$(curl --silent --show-error --http1.1 --connect-timeout 10 --max-time 30 \
  --header "$AUTH_HEADER" --output "$STATUS_FILE" --write-out '%{http_code}' \
  "${SERVICE_URL}/api/status" 2>/dev/null || true)"
if [[ "$STATUS_CODE" == "200" ]]; then
  python3 - "$STATUS_FILE" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
print("狀態摘要：", data.get("summary", {}))
for item in data.get("channels", []):
    state = item.get("state")
    error = item.get("error") or ""
    if state != "idle" or error:
        print(f"- {item.get('id')}: {state}")
        if error:
            print("  錯誤：" + error[:1000])
PY
else
  printf '狀態 API HTTP %s\n' "${STATUS_CODE:-連線失敗}"
fi

log "強制解析頻道 ${CHANNEL_ID}"
PROBE_FILE="$TMP_DIR/probe.json"
PROBE_CODE="$(curl --silent --show-error --http1.1 --connect-timeout 10 --max-time 130 \
  --request POST --header "$AUTH_HEADER" \
  --output "$PROBE_FILE" --write-out '%{http_code}' \
  "${SERVICE_URL}/api/channels/${CHANNEL_ID}/probe" 2>/dev/null || true)"
printf 'Probe HTTP: %s\n' "${PROBE_CODE:-連線失敗}"
python3 - "$PROBE_FILE" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    print(open(sys.argv[1], encoding="utf-8", errors="replace").read()[:3000])
    raise SystemExit
safe = {k: v for k, v in data.items() if k not in {"key", "access_key"}}
print(json.dumps(safe, ensure_ascii=False, indent=2)[:5000])
PY

if [[ "$PROBE_CODE" == "200" ]]; then
  log "測試 HLS 主清單"
  MASTER_FILE="$TMP_DIR/master.m3u8"
  MASTER_HEADERS="$TMP_DIR/master.headers"
  MASTER_CODE="$(curl --silent --show-error --http1.1 --connect-timeout 10 --max-time 60 \
    --header "$AUTH_HEADER" --dump-header "$MASTER_HEADERS" \
    --output "$MASTER_FILE" --write-out '%{http_code}' \
    "${SERVICE_URL}/hls/${CHANNEL_ID}/master.m3u8" 2>/dev/null || true)"
  printf 'Master playlist HTTP: %s\n' "${MASTER_CODE:-連線失敗}"
  sed -n '1,20p' "$MASTER_HEADERS"
  if [[ "$MASTER_CODE" == "200" ]]; then
    python3 - "$MASTER_FILE" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
text = re.sub(r"([?&]key=)[^&\s\"']+", r"\1***", text)
print("主清單前 20 行：")
print("\n".join(text.splitlines()[:20]))
PY
  else
    sed -n '1,30p' "$MASTER_FILE"
  fi
fi

log "最近的解析與上游錯誤記錄"
gcloud logging read \
  "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE}\"" \
  --project "$PROJECT_ID" \
  --freshness=30m \
  --limit=120 \
  --format='value(timestamp,textPayload,jsonPayload.message)' 2>/dev/null \
  | grep -Ei 'resolve|yt-dlp|youtube|upstream|error|warning|403|429|bot|token|format|直播|解析|失敗' \
  | tail -80 || true

log "初步判斷"
DIAG_TEXT="$(cat "$PROBE_FILE" 2>/dev/null || true)"
if grep -Eqi "confirm you.?re not a bot|sign in to confirm|not a bot" <<<"$DIAG_TEXT"; then
  cat <<'EOF'
診斷：YouTube 封鎖了 Cloud Run 的資料中心出口 IP，yt-dlp 無法取得直播網址。
這不是途播或 M3U 清單問題。改用住宅網路上的 NAS／電腦最可靠；個人 YouTube cookies 放上雲端仍有帳號與 IP 綁定風險。
EOF
elif grep -Eqi "po.?token|proof of origin|http error 403|403 forbidden" <<<"$DIAG_TEXT"; then
  cat <<'EOF'
診斷：YouTube 播放格式需要 PO Token 或上游回傳 403。可評估加入 yt-dlp PO Token provider；先不要上傳個人 cookies。
EOF
elif grep -Eqi "不是直播|not live|live_status" <<<"$DIAG_TEXT"; then
  cat <<'EOF'
診斷：目前設定的來源不是直播或官方直播網址已更換，需要更新 channels.json。
EOF
elif [[ "$PROBE_CODE" == "200" && "${MASTER_CODE:-}" == "200" ]]; then
  cat <<'EOF'
解析與 HLS 主清單均正常。下一步應檢查子清單／影音分段、Cloud Run 回應時間，或途播對轉送網址的相容性。
EOF
else
  cat <<'EOF'
尚未能自動分類。請提供本工具從「強制解析頻道」開始到「初步判斷」的輸出；播放權杖不會被印出。
EOF
fi
