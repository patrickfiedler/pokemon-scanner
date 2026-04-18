#!/usr/bin/env python3
"""
Migration tracker for update.sh.

Commands:
  status          — print all applied migrations
  check <id>      — print "pending" or "applied:<timestamp>"
  mark  <id> <desc> — record a migration as successfully applied
"""
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).parent.parent / "data" / "cards.db"


def _conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id          TEXT PRIMARY KEY,
            description TEXT,
            applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def cmd_status() -> None:
    conn = _conn()
    rows = conn.execute(
        "SELECT id, applied_at, description FROM schema_migrations ORDER BY applied_at"
    ).fetchall()
    conn.close()
    if rows:
        for r in rows:
            print(f"  ✓  {r[0]:45s}  {r[1]}  {r[2]}")
    else:
        print("  (no migrations recorded yet)")


def cmd_check(migration_id: str) -> None:
    conn = _conn()
    row = conn.execute(
        "SELECT applied_at FROM schema_migrations WHERE id = ?", (migration_id,)
    ).fetchone()
    conn.close()
    print("applied:" + row[0] if row else "pending")


def cmd_mark(migration_id: str, description: str) -> None:
    conn = _conn()
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (id, description) VALUES (?, ?)",
        (migration_id, description),
    )
    conn.commit()
    conn.close()


COMMANDS = {"status": cmd_status, "check": cmd_check, "mark": cmd_mark}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: migrate.py <{'|'.join(COMMANDS)}>", file=sys.stderr)
        sys.exit(1)
    cmd = sys.argv[1]
    args = sys.argv[2:]
    try:
        COMMANDS[cmd](*args)
    except TypeError as e:
        print(f"migrate.py {cmd}: wrong arguments — {e}", file=sys.stderr)
        sys.exit(1)
