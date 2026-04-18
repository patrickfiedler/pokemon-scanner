#!/usr/bin/env bash
# Run once on the VPS to set up the pokemon-scanner service.
# Run as root.
set -e

APP_DIR=/opt/pokemon-scanner
APP_USER=pokemon-scanner

echo "=== Creating system user ==="
useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"

echo "=== Installing app ==="
git clone https://github.com/patrickfiedler/pokemon-scanner "$APP_DIR"
cd "$APP_DIR"

echo "=== Setting up Python venv ==="
python3 -m venv venv
venv/bin/pip install -r requirements.txt

echo "=== Importing card data (fetches from TCGdex API, ~5-10 min) ==="
venv/bin/python import_cards.py

echo "=== Setting passphrase ==="
cp .env.example .env
echo "Edit $APP_DIR/.env now and set SCANNER_PASSWORD, then press Enter to continue."
read -r
# Verify it's set
grep -q 'SCANNER_PASSWORD=.' .env || { echo "ERROR: SCANNER_PASSWORD is empty in .env"; exit 1; }

echo "=== Setting permissions ==="
chown -R root:root "$APP_DIR"          # app files owned by root (read-only for service)
chown -R "$APP_USER":"$APP_USER" "$APP_DIR/data"  # service user writes only here
chmod 640 "$APP_DIR/.env"              # only root + group can read the passphrase
chgrp "$APP_USER" "$APP_DIR/.env"

echo "=== Installing systemd service ==="
cp "$APP_DIR/deploy/pokemon-scanner.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pokemon-scanner

echo "=== Installing nginx config ==="
cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/pokemon-scanner
ln -sf /etc/nginx/sites-available/pokemon-scanner /etc/nginx/sites-enabled/pokemon-scanner
nginx -t && systemctl reload nginx

echo ""
echo "=== Done! Next steps: ==="
echo "1. Run: certbot --nginx -d pokemon.domain.de"
echo "2. Check service: systemctl status pokemon-scanner"
