#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${DOMAIN:?DOMAIN is required}"
PUBLIC_BASE_URL="https://${DOMAIN}"
APP_DIR=/opt/youtube-m3u
DATA_DIR=/var/lib/youtube-m3u
ENV_FILE=/etc/youtube-m3u.env

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends caddy ffmpeg git nodejs npm python3-venv ca-certificates curl

if ! swapon --show --noheadings | grep -q .; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

if ! id -u ytm3u >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin ytm3u
fi
install -d -o ytm3u -g ytm3u -m 0750 "$DATA_DIR" "$APP_DIR"
install -o root -g root -m 0644 app.py index.html requirements.txt "$APP_DIR"/

python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir --upgrade pip
"$APP_DIR/venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir -r "$APP_DIR/requirements.txt"

PROVIDER_DIR=/opt/bgutil-ytdlp-pot-provider
if [[ ! -d "$PROVIDER_DIR/.git" ]]; then
  git clone --depth 1 --branch 1.3.2 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$PROVIDER_DIR"
fi
(
  cd "$PROVIDER_DIR/server"
  npm ci --no-audit --no-fund
  npx tsc
)
chown -R root:root "$PROVIDER_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  ACCESS_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(30))')"
  cat >"$ENV_FILE" <<EOF
ACCESS_KEY=${ACCESS_KEY}
PUBLIC_BASE_URL=${PUBLIC_BASE_URL}
DATA_DIR=${DATA_DIR}
MAX_HEIGHT=720
RESOLVER_TIMEOUT_SECONDS=50
STARTUP_TIMEOUT_SECONDS=35
IDLE_TIMEOUT_SECONDS=150
LOG_LEVEL=INFO
EOF
  chown root:ytm3u "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
else
  sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=${PUBLIC_BASE_URL}|" "$ENV_FILE"
fi

install -o root -g root -m 0644 youtube-m3u.service /etc/systemd/system/youtube-m3u.service
install -o root -g root -m 0644 bgutil-provider.service /etc/systemd/system/bgutil-provider.service
sed "s/__DOMAIN__/${DOMAIN}/g" Caddyfile.template >/etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile

systemctl daemon-reload
systemctl enable --now bgutil-provider.service
systemctl enable --now youtube-m3u.service
systemctl restart youtube-m3u.service
systemctl enable --now caddy
systemctl restart caddy

for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8788/healthz >/dev/null; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:8788/healthz
