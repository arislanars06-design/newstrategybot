#!/usr/bin/env bash
# One-shot installer for a fresh Ubuntu/Debian VPS.
# Run as root:
#   curl -fsSL https://raw.githubusercontent.com/<owner>/newstrategybot/main/deploy/install_vps.sh | bash
# Or after cloning:
#   sudo bash deploy/install_vps.sh
set -euo pipefail

APP_USER="botuser"
APP_DIR="/opt/newstrategybot"
REPO_URL="${REPO_URL:-https://github.com/arislanars06-design/newstrategybot.git}"

echo "==> Updating apt and installing base packages…"
apt-get update
apt-get install -y --no-install-recommends \
    git python3 python3-venv python3-pip ca-certificates

echo "==> Creating user $APP_USER (idempotent)…"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$APP_USER"

echo "==> Cloning repository to $APP_DIR …"
if [[ -d "$APP_DIR/.git" ]]; then
    git -C "$APP_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Creating virtualenv and installing dependencies…"
sudo -u "$APP_USER" -H bash -c "
    cd '$APP_DIR'
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -e .
    mkdir -p data logs
"

if [[ ! -f "$APP_DIR/.env" ]]; then
    echo "==> Creating .env from template (you MUST edit it before starting)…"
    sudo -u "$APP_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
fi

echo "==> Installing systemd unit…"
install -m 0644 "$APP_DIR/deploy/newstrategybot.service" /etc/systemd/system/newstrategybot.service
systemctl daemon-reload

echo "==> Done. Next steps:"
echo "  1. Edit secrets:    sudo -u $APP_USER nano $APP_DIR/.env"
echo "  2. Enable & start:  sudo systemctl enable --now newstrategybot"
echo "  3. View logs:       sudo journalctl -u newstrategybot -f"
