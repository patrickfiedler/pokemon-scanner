#!/usr/bin/env python3
"""
Import card data from the TCGdex public API into a local SQLite database.

Languages imported: de (German), en (English), it (Italian), ja (Japanese)

Strategy:
  - Fetch the set list per language to know coverage
  - For each set in English (most complete, 200+ sets), fetch per-language
    set details — each returns the card list with localised names
  - Construct image URLs from the series slug in the set symbol URL
  - Store all language variants; no individual card API calls needed

Usage:
    python import_cards.py
"""

import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://api.tcgdex.net/v2"
LANGUAGES = ["en", "de", "it", "ja"]
DB_PATH = Path(__file__).parent / "data" / "cards.db"


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def fetch_json(url: str, retries: int = 3) -> dict | list | None:
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt < retries - 1:
                time.sleep(1.5 ** attempt)
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1.5 ** attempt)
            else:
                print(f"  WARN: {url}: {e}")
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_series(data: dict) -> str | None:
    """Pull series slug from a symbol or logo URL in set data.

    URL pattern: https://assets.tcgdex.net/univ/{series}/{setId}/symbol
    """
    for key in ("symbol", "logo"):
        url = data.get(key, "")
        if url:
            parts = url.rstrip("/").split("/")
            # parts: ['https:', '', 'assets.tcgdex.net', 'univ', series, setId, ...]
            if len(parts) >= 6:
                return parts[-3]
    return None


def make_image_url(series: str, set_id: str, local_id: str) -> str:
    return f"https://assets.tcgdex.net/en/{series}/{set_id}/{local_id}"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        DROP TABLE IF EXISTS cards;
        DROP TABLE IF EXISTS sets;

        CREATE TABLE sets (
            id       TEXT PRIMARY KEY,
            name_en  TEXT,
            name_de  TEXT,
            name_it  TEXT,
            name_ja  TEXT,
            series   TEXT,
            total    INTEGER
        );

        CREATE TABLE cards (
            id       TEXT PRIMARY KEY,
            set_id   TEXT NOT NULL,
            number   TEXT NOT NULL,
            name_en  TEXT,
            name_de  TEXT,
            name_it  TEXT,
            name_ja  TEXT,
            image    TEXT,
            FOREIGN KEY (set_id) REFERENCES sets(id)
        );

        CREATE INDEX cards_set_number ON cards(set_id, number);
        CREATE INDEX cards_number     ON cards(number);
    """)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    # --- Step 1: collect set IDs available per language ---
    print("Fetching set lists per language…")
    lang_set_ids: dict[str, set] = {}
    for lang in LANGUAGES:
        sets = fetch_json(f"{BASE_URL}/{lang}/sets") or []
        lang_set_ids[lang] = {s["id"] for s in sets}
        print(f"  {lang}: {len(lang_set_ids[lang])} sets")

    en_set_ids = sorted(lang_set_ids["en"])
    total_sets = len(en_set_ids)

    # --- Step 2: for each EN set, collect multilingual data ---
    for idx, set_id in enumerate(en_set_ids, 1):
        print(f"[{idx}/{total_sets}] {set_id}", end="", flush=True)

        # Fetch EN set detail — authoritative for card list and series slug
        en_set = fetch_json(f"{BASE_URL}/en/sets/{set_id}")
        if not en_set or "cards" not in en_set:
            print(" — skipped (no EN data)")
            continue

        series = extract_series(en_set)
        total_cards = (
            en_set.get("cardCount", {}).get("official")
            or en_set.get("cardCount", {}).get("total")
            or 0
        )
        set_names = {"en": en_set.get("name"), "de": None, "it": None, "ja": None}

        # Localised card names: localId -> {lang: name}
        card_name_map: dict[str, dict[str, str]] = {}

        for lang in ["de", "it", "ja"]:
            if set_id not in lang_set_ids[lang]:
                continue
            lang_set = fetch_json(f"{BASE_URL}/{lang}/sets/{set_id}")
            if not lang_set:
                continue
            set_names[lang] = lang_set.get("name")
            for c in lang_set.get("cards", []):
                local_id = c["localId"]
                card_name_map.setdefault(local_id, {})[lang] = c.get("name")

        # Insert set
        conn.execute(
            "INSERT OR REPLACE INTO sets VALUES (?,?,?,?,?,?,?)",
            (set_id, set_names["en"], set_names["de"], set_names["it"],
             set_names["ja"], series, total_cards),
        )

        # Insert cards
        rows = []
        for card in en_set["cards"]:
            local_id = card["localId"]
            names = card_name_map.get(local_id, {})
            img = make_image_url(series, set_id, local_id) if series else None
            rows.append((
                card["id"], set_id, local_id,
                card.get("name"),
                names.get("de"), names.get("it"), names.get("ja"),
                img,
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?)", rows
        )
        conn.commit()
        print(f" — {len(rows)} cards")

    # --- Summary ---
    n_sets  = conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    n_cards = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    n_de    = conn.execute("SELECT COUNT(*) FROM cards WHERE name_de IS NOT NULL").fetchone()[0]
    n_it    = conn.execute("SELECT COUNT(*) FROM cards WHERE name_it IS NOT NULL").fetchone()[0]
    n_ja    = conn.execute("SELECT COUNT(*) FROM cards WHERE name_ja IS NOT NULL").fetchone()[0]
    print(f"\nDone: {n_sets} sets, {n_cards} cards")
    print(f"  DE names: {n_de} | IT: {n_it} | JA: {n_ja}")
    print(f"  DB: {DB_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
