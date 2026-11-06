#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="neo-tel-order-processor"
APP_DIR="${DEPLOY_PATH:-$HOME/apps/$APP_NAME}"
SERVICE_NAME="$APP_NAME.service"

if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "Missing $APP_DIR/.env. Create it on the VM before deploying." >&2
  exit 1
fi

# Ensure python3-venv and ensurepip are available
if ! python3 -c "import ensurepip" >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1; then
  echo "python3-venv / ensurepip missing on host. Installing packages..."
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y python3-venv python3-pip
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y python3 python3-pip
  fi
fi

if [[ ! -f "$APP_DIR/.venv/bin/python" ]]; then
  rm -rf "$APP_DIR/.venv"
  python3 -m venv "$APP_DIR/.venv"
fi
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

# Configure journalctl log cleanup cron job
CRON_SCHEDULE="${LOG_CLEANUP_CRON:-0 8 * * *}"
cron_file="/etc/cron.d/journal-log-cleanup"
echo "Configuring journalctl cleanup cron job ($CRON_SCHEDULE) in $cron_file..."
sudo tee "$cron_file" >/dev/null <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

$CRON_SCHEDULE root journalctl --rotate && journalctl --vacuum-time=1s >/dev/null 2>&1
EOF
sudo chmod 644 "$cron_file"
sudo systemctl enable --now cron 2>/dev/null || sudo systemctl enable --now crond 2>/dev/null || true
echo "Journalctl cleanup cron job successfully installed."
