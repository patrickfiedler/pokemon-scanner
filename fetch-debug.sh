#!/usr/bin/env bash
# Fetch debug data from VPS to local data/debug/
set -euo pipefail

REMOTE="root@pokemon.mrfiedler.de"
REMOTE_DIR="/opt/pokemon-scanner/data/debug/"
LOCAL_DIR="$(dirname "$0")/data/debug/"

mkdir -p "$LOCAL_DIR"

echo "Syncing debug data from $REMOTE …"
rsync -avz --progress "$REMOTE:$REMOTE_DIR" "$LOCAL_DIR"

echo ""
echo "Done. Files in data/debug/:"
ls -lht "$LOCAL_DIR" | head -20
