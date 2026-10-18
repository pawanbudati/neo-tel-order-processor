#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="neo-tel-order-processor"
APP_DIR="${DEPLOY_PATH:-$HOME/apps/$APP_NAME}"
SERVICE_NAME="$APP_NAME.service"

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "Missing $APP_DIR/.env. Create it on the VM before deploying." >&2
  exit 1
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

service_tmp="$(mktemp)"
trap 'rm -f "$service_tmp"' EXIT
cat > "$service_tmp" <<EOF
[Unit]
Description=Neo Kotak order processor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo install -o root -g root -m 644 "$service_tmp" "/etc/systemd/system/$SERVICE_NAME"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl --no-pager --full status "$SERVICE_NAME"
