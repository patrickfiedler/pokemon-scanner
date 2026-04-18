#!/usr/bin/env bash
# Update the pokemon-scanner app from GitHub.
# Run as root from anywhere.
set -euo pipefail

APP_DIR=/opt/pokemon-scanner
APP_USER=pokemon-scanner
SERVICE=pokemon-scanner
PYTHON="$APP_DIR/venv/bin/python"
DB="$APP_DIR/data/cards.db"
MIGRATE="$PYTHON $APP_DIR/deploy/migrate.py"

# ---------------------------------------------------------------------------
# Migration runner
#
# Usage:  run_migration <id> <description> <bash_function_name>
#
# - Checks schema_migrations table; skips if already applied.
# - Calls the named function to perform the migration.
# - Marks as applied only on success; aborts deployment on failure.
# - Migrations must be defined as bash functions below.
# ---------------------------------------------------------------------------

run_migration() {
    local id="$1"
    local desc="$2"
    local fn="$3"

    local status
    status=$($MIGRATE check "$id")

    if [[ "$status" == pending ]]; then
        echo "  ▶  $id"
        echo "     $desc"
        if "$fn"; then
            $MIGRATE mark "$id" "$desc"
            echo "     ✓ applied"
        else
            echo "     ✗ FAILED — deployment aborted"
            echo "     Fix the issue and re-run update.sh."
            exit 1
        fi
    else
        local applied_at="${status#applied:}"
        echo "  ✓  $id  ($applied_at)"
    fi
}

# ---------------------------------------------------------------------------
# Migration definitions
# Add new migrations at the bottom — never rename or reorder existing ones.
# ---------------------------------------------------------------------------

_m001_collection_table() {
    "$PYTHON" - "$DB" <<'PYEOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("""
    CREATE TABLE IF NOT EXISTS collection (
        user_id   TEXT NOT NULL,
        card_id   TEXT NOT NULL,
        quantity  INTEGER NOT NULL DEFAULT 1,
        added_at  TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, card_id)
    )
""")
conn.commit()
conn.close()
PYEOF
}

_m002_name_fr_columns() {
    "$PYTHON" - "$DB" <<'PYEOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
for table in ("sets", "cards"):
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if "name_fr" not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN name_fr TEXT")
conn.commit()
conn.close()
PYEOF
}

_m003_card_detail_columns() {
    "$PYTHON" - "$DB" <<'PYEOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
existing = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
for col, typedef in [
    ("category",       "TEXT"),
    ("hp",             "INTEGER"),
    ("types",          "TEXT"),
    ("rarity",         "TEXT"),
    ("stage",          "TEXT"),
    ("description",    "TEXT"),
    ("dex_id",         "TEXT"),
    ("attacks",        "TEXT"),
    ("variants",       "TEXT"),
    ("detail_fetched", "INTEGER DEFAULT 0"),
]:
    if col not in existing:
        conn.execute(f"ALTER TABLE cards ADD COLUMN {col} {typedef}")
conn.commit()
conn.close()
PYEOF
}

_m004_card_images_dir() {
    mkdir -p "$APP_DIR/data/card_images"
    chown "$APP_USER":"$APP_USER" "$APP_DIR/data/card_images"
}

run_migrations() {
    echo "=== Migrations ==="
    $MIGRATE status
    echo "  ---"
    run_migration "001-collection-table" \
        "Create per-user card collection table" \
        _m001_collection_table
    run_migration "002-name-fr-columns" \
        "Add name_fr column to sets and cards tables" \
        _m002_name_fr_columns
    run_migration "003-card-detail-columns" \
        "Add hp/types/rarity/stage/category/description/dex_id/attacks/variants/detail_fetched to cards" \
        _m003_card_detail_columns
    run_migration "004-card-images-dir" \
        "Create data/card_images cache directory with correct ownership" \
        _m004_card_images_dir
}

# ---------------------------------------------------------------------------
# Deployment steps
# ---------------------------------------------------------------------------

BEFORE_COMMIT=$(git -C "$APP_DIR" rev-parse HEAD)

echo "=== Pulling latest code ==="
git -C "$APP_DIR" pull --ff-only

# Re-exec if update.sh itself changed in this pull.
# --restarted flag prevents a second re-exec after the self-update.
if [[ "${1:-}" != "--restarted" ]]; then
    if ! git -C "$APP_DIR" diff --quiet "$BEFORE_COMMIT" HEAD -- deploy/update.sh; then
        echo ""
        echo "=== update.sh was updated — re-running new version ==="
        exec bash "${BASH_SOURCE[0]}" --restarted
    fi
fi

echo "=== Installing new dependencies (if any) ==="
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "=== Fixing permissions ==="
chown -R root:root "$APP_DIR"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR/data"
chmod 640 "$APP_DIR/.env"
chgrp "$APP_USER" "$APP_DIR/.env"

run_migrations

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
