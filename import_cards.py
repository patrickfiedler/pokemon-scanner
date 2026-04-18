#!/usr/bin/env python3
"""
Import card data from the TCGdex public API into a local SQLite database,
then augment missing Pokémon names from the PokeAPI.

Languages imported: de (German), en (English), fr (French), it (Italian), ja (Japanese)

Strategy:
  - Fetch the set list per language to know coverage
  - For each set in English (most complete, 200+ sets), fetch per-language
    set details — each returns the card list with localised names
  - Construct image URLs from the series slug in the set symbol URL
  - Store all language variants; no individual card API calls needed
  - After TCGdex import, call PokeAPI for cards still missing a DE/FR/IT name
    to get official species translations (e.g. Kilowattrel → Voltrean)

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
LANGUAGES = ["en", "de", "fr", "it", "ja"]
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


# TCG card name suffixes — order matters (longer first to avoid partial matches)
_SUFFIXES = [" VMAX", " VSTAR", " LV.X", " LEGEND", " EX", " GX", " ex", " V", " ◆"]

def parse_card_name(name_en: str) -> tuple[str, str]:
    """Split e.g. 'Kilowattrel ex' → ('kilowattrel', ' ex') for PokeAPI lookup.

    Returns (pokeapi_slug, suffix). For tag teams ('A & B-GX') returns
    the first Pokémon slug and the rest as suffix.
    """
    if " & " in name_en:
        first, rest = name_en.split(" & ", 1)
        return first.lower().replace(" ", "-"), " & " + rest
    for suffix in _SUFFIXES:
        if name_en.endswith(suffix):
            base = name_en[: -len(suffix)]
            return base.lower().replace(" ", "-"), suffix
    return name_en.lower().replace(" ", "-"), ""


def fetch_pokeapi_names(slug: str) -> dict[str, str]:
    """Return {lang_code: name} from PokeAPI for a species slug."""
    data = fetch_json(f"https://pokeapi.co/api/v2/pokemon-species/{slug}")
    if not data:
        return {}
    want = {"de", "fr", "it", "ja"}
    return {
        e["language"]["name"]: e["name"]
        for e in data.get("names", [])
        if e["language"]["name"] in want
    }


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
            name_fr  TEXT,
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
            name_fr  TEXT,
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
        set_names = {"en": en_set.get("name"), "de": None, "fr": None, "it": None, "ja": None}

        # Localised card names: localId -> {lang: name}
        card_name_map: dict[str, dict[str, str]] = {}

        for lang in ["de", "fr", "it", "ja"]:
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
            "INSERT OR REPLACE INTO sets VALUES (?,?,?,?,?,?,?,?)",
            (set_id, set_names["en"], set_names["de"], set_names["fr"],
             set_names["it"], set_names["ja"], series, total_cards),
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
                names.get("de"), names.get("fr"), names.get("it"), names.get("ja"),
                img,
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?,?)", rows
        )
        conn.commit()
        print(f" — {len(rows)} cards")

    # --- Step 3: Augment missing names from PokeAPI ---
    print("\nFetching Pokémon species names from PokeAPI for missing translations…")
    missing_rows = conn.execute(
        "SELECT DISTINCT name_en FROM cards "
        "WHERE name_en IS NOT NULL AND (name_de IS NULL OR name_fr IS NULL OR name_it IS NULL)"
    ).fetchall()

    # Build slug → pokeapi names cache (one request per unique species)
    slug_cache: dict[str, dict] = {}
    for idx, (name_en,) in enumerate(missing_rows):
        slug, _ = parse_card_name(name_en)
        if slug in slug_cache:
            continue
        names = fetch_pokeapi_names(slug)
        slug_cache[slug] = names
        if names:
            print(f"  [{idx+1}/{len(missing_rows)}] {name_en} → de:{names.get('de','?')}")
        time.sleep(0.07)  # be polite to PokeAPI

    # Update cards — COALESCE keeps existing TCGdex translations intact
    updates = []
    for (name_en,) in missing_rows:
        slug, suffix = parse_card_name(name_en)
        names = slug_cache.get(slug)
        if not names:
            continue
        def localized(lang: str) -> str | None:
            n = names.get(lang)
            return (n + suffix) if n else None
        updates.append((localized("de"), localized("fr"), localized("it"), name_en))

    conn.executemany(
        """UPDATE cards
           SET name_de = COALESCE(name_de, ?),
               name_fr = COALESCE(name_fr, ?),
               name_it = COALESCE(name_it, ?)
           WHERE name_en = ?""",
        updates,
    )
    conn.commit()
    print(f"  Updated {len(updates)} card name groups with PokeAPI translations.")

    # --- Summary ---
    n_sets  = conn.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    n_cards = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    n_de    = conn.execute("SELECT COUNT(*) FROM cards WHERE name_de IS NOT NULL").fetchone()[0]
    n_fr    = conn.execute("SELECT COUNT(*) FROM cards WHERE name_fr IS NOT NULL").fetchone()[0]
    n_it    = conn.execute("SELECT COUNT(*) FROM cards WHERE name_it IS NOT NULL").fetchone()[0]
    n_ja    = conn.execute("SELECT COUNT(*) FROM cards WHERE name_ja IS NOT NULL").fetchone()[0]
    print(f"\nDone: {n_sets} sets, {n_cards} cards")
    print(f"  DE names: {n_de} | FR: {n_fr} | IT: {n_it} | JA: {n_ja}")
    print(f"  DB: {DB_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
