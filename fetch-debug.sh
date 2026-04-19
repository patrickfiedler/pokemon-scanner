#!/usr/bin/env bash
# Fetch debug images and service journal from VPS to local data/debug/
set -euo pipefail

REMOTE="root@pokemon.mrfiedler.de"
REMOTE_DIR="/opt/pokemon-scanner/data/debug/"
LOCAL_DIR="$(dirname "$0")/data/debug/"

mkdir -p "$LOCAL_DIR"

echo "Syncing debug images from $REMOTE …"
rsync -avz --progress "$REMOTE:$REMOTE_DIR" "$LOCAL_DIR"

echo ""
echo "Fetching service journal …"
ssh "$REMOTE" "journalctl -u pokemon-scanner --no-pager -o short-iso" \
  > "$LOCAL_DIR/journal.log"
echo "Journal saved to data/debug/journal.log ($(wc -l < "$LOCAL_DIR/journal.log") lines)"

echo ""
echo "Done. Latest files in data/debug/:"
ls -lht "$LOCAL_DIR" | head -20
