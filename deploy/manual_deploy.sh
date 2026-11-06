#!/usr/bin/env bash
set -Eeuo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Manual Deployment Script for Consumer (Neo Kotak Order Processor)
# Matches GitHub Actions (.github/workflows/deploy.yml)
# Usage:
#   ./deploy/manual_deploy.sh [target]
# Targets:
#   yuva
#   pavan
#   sahi
#   all     (deploys to all configured VMs: yuva, pavan, sahi)
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load configuration file if present
for env_file in "$ROOT_DIR/.env.deploy" "$SCRIPT_DIR/.env.deploy" "$ROOT_DIR/.deploy.env" "$SCRIPT_DIR/deploy.env"; do
  if [[ -f "$env_file" ]]; then
    echo "Loading deployment config from $env_file"
    # shellcheck disable=SC1090
    set -a
    source "$env_file"
    set +a
    break
  fi
done

TARGET="${1:-all}"
if [[ "$TARGET" == "--"* ]]; then
  TARGET="all"
fi

deploy_to_target() {
  local target_name="$1"
  local host="$2"
  local user="$3"
  local key="$4"
  local port="${5:-22}"
  local remote_path="${6:-}"

  if [[ -z "$host" || -z "$user" ]]; then
    echo "Skipping [$target_name]: Host or User is not configured."
    return 0
  fi

  if [[ -z "$remote_path" || "$remote_path" == "/home/" ]]; then
    remote_path="/home/$user/apps/neo-tel-order-processor"
  fi

  local ssh_opts=(-p "$port" -o "StrictHostKeyChecking=accept-new")
  if [[ -n "$key" && -f "$key" ]]; then
    ssh_opts+=(-i "$key")
  fi

  echo "================================================================="
  echo ">>> Deploying Consumer to [$target_name] at $user@$host:$port"
  echo ">>> Remote Path: $remote_path"
  echo "================================================================="

  # 1. Ensure target directory exists on remote VM
  ssh "${ssh_opts[@]}" "$user@$host" "mkdir -p '$remote_path'"

  # 2. Sync repository files (excluding git, venv, secrets, logs)
  echo ">>> Syncing repository files to $user@$host..."
  if command -v rsync >/dev/null 2>&1; then
    local rsync_ssh="ssh -p $port -o StrictHostKeyChecking=accept-new"
    if [[ -n "$key" && -f "$key" ]]; then
      rsync_ssh="$rsync_ssh -i $key"
    fi

    rsync -az --delete \
      --exclude='.git/' \
      --exclude='.env' --include='.env.example' --exclude='.env.*' \
      --exclude='.venv/' --exclude='venv/' \
      --exclude='logs/' --exclude='*.log' \
      --exclude='__pycache__/' \
      -e "$rsync_ssh" \
      "$ROOT_DIR/" "$user@$host:$remote_path/"
  else
    echo ">>> rsync not found locally; streaming via tar over ssh..."
    tar --exclude=".git" \
        --exclude=".env" \
        --exclude=".env.*" \
        --exclude=".venv" \
        --exclude="venv" \
        --exclude="logs" \
        --exclude="*.log" \
        --exclude="__pycache__" \
        -C "$ROOT_DIR" -czf - . | ssh "${ssh_opts[@]}" "$user@$host" "tar -xzf - -C '$remote_path'"
  fi

  # 3. Execute remote deploy.sh
  echo ">>> Running remote deploy.sh on $host..."
  local cleanup_cron="${LOG_CLEANUP_CRON:-0 8 * * *}"
  ssh "${ssh_opts[@]}" "$user@$host" \
    "DEPLOY_PATH='$remote_path' LOG_CLEANUP_CRON='$cleanup_cron' bash '$remote_path/deploy/deploy.sh'"

  echo ">>> Deployment to [$target_name] completed successfully!"
  echo ""
}

# Resolve shared defaults
DEFAULT_USER="${GCP_VM_USER:-${VM_USER:-}}"
DEFAULT_KEY="${GCP_VM_SSH_KEY:-${SSH_KEY_PATH:-}}"
DEFAULT_PORT="${GCP_VM_SSH_PORT:-${VM_PORT:-22}}"
DEFAULT_PATH="${CONSUMER_DEPLOY_PATH:-${DEPLOY_PATH:-}}"

# Resolve Yuva VM configuration
YUVA_HOST="${GCP_VM_HOST_YUVA:-${VM_HOST_YUVA:-}}"
YUVA_USER="${GCP_VM_USER_YUVA:-${VM_USER_YUVA:-$DEFAULT_USER}}"
YUVA_KEY="${GCP_VM_SSH_KEY_YUVA:-${SSH_KEY_PATH_YUVA:-$DEFAULT_KEY}}"
YUVA_PORT="${GCP_VM_SSH_PORT_YUVA:-${VM_PORT_YUVA:-$DEFAULT_PORT}}"
YUVA_PATH="${CONSUMER_DEPLOY_PATH_YUVA:-${DEPLOY_PATH_YUVA:-$DEFAULT_PATH}}"

# Resolve Pavan VM configuration
PAVAN_HOST="${GCP_VM_HOST_PAVAN:-${VM_HOST_PAVAN:-}}"
PAVAN_USER="${GCP_VM_USER_PAVAN:-${VM_USER_PAVAN:-$DEFAULT_USER}}"
PAVAN_KEY="${GCP_VM_SSH_KEY_PAVAN:-${SSH_KEY_PATH_PAVAN:-$DEFAULT_KEY}}"
PAVAN_PORT="${GCP_VM_SSH_PORT_PAVAN:-${VM_PORT_PAVAN:-$DEFAULT_PORT}}"
PAVAN_PATH="${CONSUMER_DEPLOY_PATH_PAVAN:-${DEPLOY_PATH_PAVAN:-$DEFAULT_PATH}}"

# Resolve Sahi VM configuration
SAHI_HOST="${GCP_VM_HOST_SAHI:-${VM_HOST_SAHI:-}}"
SAHI_USER="${GCP_VM_USER_SAHI:-${VM_USER_SAHI:-$DEFAULT_USER}}"
SAHI_KEY="${GCP_VM_SSH_KEY_SAHI:-${SSH_KEY_PATH_SAHI:-$DEFAULT_KEY}}"
SAHI_PORT="${GCP_VM_SSH_PORT_SAHI:-${VM_PORT_SAHI:-$DEFAULT_PORT}}"
SAHI_PATH="${CONSUMER_DEPLOY_PATH_SAHI:-${DEPLOY_PATH_SAHI:-$DEFAULT_PATH}}"

case "$TARGET" in
  yuva)
    deploy_to_target "Yuva VM" "$YUVA_HOST" "$YUVA_USER" "$YUVA_KEY" "$YUVA_PORT" "$YUVA_PATH"
    ;;
  pavan)
    deploy_to_target "Pavan VM" "$PAVAN_HOST" "$PAVAN_USER" "$PAVAN_KEY" "$PAVAN_PORT" "$PAVAN_PATH"
    ;;
  sahi)
    deploy_to_target "Sahi VM" "$SAHI_HOST" "$SAHI_USER" "$SAHI_KEY" "$SAHI_PORT" "$SAHI_PATH"
    ;;
  all)
    count=0
    if [[ -n "$YUVA_HOST" ]]; then
      deploy_to_target "Yuva VM" "$YUVA_HOST" "$YUVA_USER" "$YUVA_KEY" "$YUVA_PORT" "$YUVA_PATH"
      ((count++))
    fi
    if [[ -n "$PAVAN_HOST" ]]; then
      deploy_to_target "Pavan VM" "$PAVAN_HOST" "$PAVAN_USER" "$PAVAN_KEY" "$PAVAN_PORT" "$PAVAN_PATH"
      ((count++))
    fi
    if [[ -n "$SAHI_HOST" ]]; then
      deploy_to_target "Sahi VM" "$SAHI_HOST" "$SAHI_USER" "$SAHI_KEY" "$SAHI_PORT" "$SAHI_PATH"
      ((count++))
    fi
    if [[ $count -eq 0 ]]; then
      echo "Error: No consumer VM hosts configured (set GCP_VM_HOST_YUVA, GCP_VM_HOST_PAVAN, or GCP_VM_HOST_SAHI)." >&2
      exit 1
    fi
    ;;
  *)
    echo "Unknown target '$TARGET'. Use: yuva, pavan, sahi, or all." >&2
    exit 1
    ;;
esac
