#!/usr/bin/env python3
"""
Import PokemonTCG/pokemon-tcg-data JSON files into a local SQLite database.

Usage:
    python import_cards.py

Expects pokemon-tcg-data to be cloned at data/pokemon-tcg-data/.
Run `git clone https://github.com/PokemonTCG/pokemon-tcg-data data/pokemon-tcg-data` first.
"""

import json
import sqlite3
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data" / "pokemon-tcg-data"
DB_PATH = Path(__file__).parent / "data" / "cards.db"
CARDS_DIR = DATA_DIR / "cards" / "en"


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sets (
            id      TEXT PRIMARY KEY,
            name    TEXT NOT NULL,
            series  TEXT,
            total   INTEGER
        );

        CREATE TABLE IF NOT EXISTS cards (
            id          TEXT PRIMARY KEY,
            set_id      TEXT NOT NULL,
            number      TEXT NOT NULL,
            name        TEXT NOT NULL,
            hp          TEXT,
            types       TEXT,
            rarity      TEXT,
            image_small TEXT,
            image_large TEXT,
            FOREIGN KEY (set_id) REFERENCES sets(id)
        );

        CREATE INDEX IF NOT EXISTS idx_cards_set_number ON cards(set_id, number);
        CREATE INDEX IF NOT EXISTS idx_cards_number ON cards(number);
    """)


def import_sets(conn: sqlite3.Connection) -> None:
    sets_dir = DATA_DIR / "sets" / "en"
    if not sets_dir.exists():
        print(f"  Sets directory not found: {sets_dir}")
        return
    rows = []
    for f in sorted(sets_dir.glob("*.json")):
        s = json.loads(f.read_text())
        rows.append((s["id"], s["name"], s.get("series"), s.get("total")))
    conn.executemany(
        "INSERT OR REPLACE INTO sets VALUES (?,?,?,?)", rows
    )
    print(f"  Imported {len(rows)} sets")


def import_cards(conn: sqlite3.Connection) -> None:
    if not CARDS_DIR.exists():
        print(f"Cards directory not found: {CARDS_DIR}")
        print("Run: git clone https://github.com/PokemonTCG/pokemon-tcg-data data/pokemon-tcg-data")
        return

    total = 0
    for set_file in sorted(CARDS_DIR.glob("*.json")):
        cards = json.loads(set_file.read_text())
        rows = []
        for c in cards:
            images = c.get("images", {})
            rows.append((
                c["id"],
                c["set"]["id"],
                c["number"],
                c["name"],
                c.get("hp"),
                json.dumps(c.get("types", [])),
                c.get("rarity"),
                images.get("small"),
                images.get("large"),
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?,?)", rows
        )
        total += len(rows)
        print(f"  {set_file.stem}: {len(rows)} cards")

    print(f"\nTotal: {total} cards imported")


def main() -> None:
    if not DATA_DIR.exists():
        print("ERROR: data/pokemon-tcg-data not found.")
        print("Run: git clone https://github.com/PokemonTCG/pokemon-tcg-data data/pokemon-tcg-data")
        return

    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        create_schema(conn)
        print("Importing sets...")
        import_sets(conn)
        print("Importing cards...")
        import_cards(conn)
        conn.commit()
        print(f"\nDatabase written to {DB_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
