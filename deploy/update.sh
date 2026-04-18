#!/usr/bin/env bash
# Update the pokemon-scanner app from GitHub.
# Run as root from anywhere.
set -euo pipefail

APP_DIR=/opt/pokemon-scanner
APP_USER=pokemon-scanner
SERVICE=pokemon-scanner

echo "=== Pulling latest code ==="
git -C "$APP_DIR" pull --ff-only

echo "=== Installing new dependencies (if any) ==="
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "=== Fixing permissions ==="
chown -R root:root "$APP_DIR"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR/data"
chmod 640 "$APP_DIR/.env"
chgrp "$APP_USER" "$APP_DIR/.env"

echo "=== Restarting service ==="
systemctl restart "$SERVICE"

echo "=== Checking service status ==="
sleep 2
if systemctl is-active --quiet "$SERVICE"; then
    echo "OK: $SERVICE is running."
else
    echo "ERROR: $SERVICE failed to start. Check logs with: journalctl -u $SERVICE -n 50"
    exit 1
fi
