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
    python import_cards.py           # incremental: skip unchanged sets
    python import_cards.py --force   # full re-import of all sets
"""

import http.client
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "https://api.tcgdex.net/v2"
LANGUAGES = ["en", "de", "fr", "it", "ja"]
DB_PATH = Path(__file__).parent / "data" / "cards.db"
POKEAPI_CACHE = Path(__file__).parent / "data" / "pokeapi_cache"


# ---------------------------------------------------------------------------
# HTTP helpers
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


# Persistent HTTPS connection to PokeAPI — reuses TCP+TLS across all species lookups.
_pokeapi_conn: http.client.HTTPSConnection | None = None

def _pokeapi_get(path: str) -> dict | None:
    """GET from pokeapi.co with connection reuse and local file cache."""
    global _pokeapi_conn
    POKEAPI_CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = POKEAPI_CACHE / f"{path.lstrip('/').replace('/', '_')}.json"

    if cache_file.exists():
        raw = cache_file.read_text()
        return json.loads(raw)  # may be null (cached 404)

    for attempt in range(3):
        try:
            if _pokeapi_conn is None:
                _pokeapi_conn = http.client.HTTPSConnection("pokeapi.co", timeout=15)
            _pokeapi_conn.request("GET", path, headers={"User-Agent": "pokemon-scanner/1.0 (local import)"})
            resp = _pokeapi_conn.getresponse()
            body = resp.read()
            if resp.status == 404:
                cache_file.write_text("null")
                return None
            if resp.status == 200:
                data = json.loads(body)
                cache_file.write_text(json.dumps(data))
                return data
            # Unexpected status — reset connection and retry
            _pokeapi_conn.close()
            _pokeapi_conn = None
            time.sleep(1.0)
        except Exception:
            _pokeapi_conn = None
            if attempt < 2:
                time.sleep(1.5 ** attempt)
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
    """Return {lang_code: name} from PokeAPI, with local file cache."""
    data = _pokeapi_get(f"/api/v2/pokemon-species/{slug}")
    if not data:
        return {}
    want = {"de", "fr", "it", "ja"}
    return {
        e["language"]["name"]: e["name"]
        for e in data.get("names", [])
        if e["language"]["name"] in want
    }


def store_card_detail(conn: sqlite3.Connection, card_id: str, data: dict) -> None:
    """Write enriched card fields from a TCGdex /en/cards/{id} response."""
    conn.execute("""
        UPDATE cards SET
            category       = ?,
            hp             = ?,
            types          = ?,
            rarity         = ?,
            stage          = ?,
            description    = ?,
            dex_id         = ?,
            attacks        = ?,
            variants       = ?,
            detail_fetched = 1
        WHERE id = ?
    """, (
        data.get("category"),
        data.get("hp"),
        json.dumps(data.get("types"))   if data.get("types")   else None,
        data.get("rarity"),
        data.get("stage"),
        data.get("description"),
        json.dumps(data.get("dexId"))   if data.get("dexId")   else None,
        json.dumps(data.get("attacks")) if data.get("attacks") else None,
        json.dumps(data.get("variants"))if data.get("variants")else None,
        card_id,
    ))
    conn.commit()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist; add any missing columns (migrations)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sets (
            id       TEXT PRIMARY KEY,
            name_en  TEXT,
            name_de  TEXT,
            name_fr  TEXT,
            name_it  TEXT,
            name_ja  TEXT,
            series   TEXT,
            total    INTEGER
        );

        CREATE TABLE IF NOT EXISTS cards (
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

        CREATE INDEX IF NOT EXISTS cards_set_number ON cards(set_id, number);
        CREATE INDEX IF NOT EXISTS cards_number     ON cards(number);
    """)
    # Schema migrations: add columns introduced after initial deploy
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sets)")}
    for col in ("name_fr",):
        if col not in existing:
            conn.execute(f"ALTER TABLE sets ADD COLUMN {col} TEXT")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(cards)")}
    for col, typedef in [
        ("name_fr",        "TEXT"),
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


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def main() -> None:
    import sys
    force  = "--force"  in sys.argv
    enrich = "--enrich" in sys.argv
    if "-h" in sys.argv or "--help" in sys.argv:
        print("""Usage: python import_cards.py [--force] [--enrich] [-h]

Imports Pokémon TCG card data into the local SQLite database.

Steps:
  1. TCGdex  — fetches multilingual card/set names (de, en, fr, it, ja)
  2. PokeAPI — fills in missing DE/FR/IT names via species lookup

Options:
  (none)    Incremental: skip sets whose card count hasn't changed.
            PokeAPI only runs for newly added/updated sets.
  --force   Full re-import of all sets (slow, ~5-10 min).
  --enrich  Fetch full card details (hp, types, attacks…) for all collection
            cards that are missing detail data. Run this after TCGdex was
            temporarily unavailable during scanning.
  -h        Show this help message.

DB: """ + str(DB_PATH))
        sys.exit(0)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    # --- --enrich mode: backfill detail data for collection cards ---
    if enrich:
        rows = conn.execute("""
            SELECT DISTINCT c.id FROM cards c
            JOIN collection col ON c.id = col.card_id
            WHERE c.detail_fetched = 0 OR c.detail_fetched IS NULL
        """).fetchall()
        total = len(rows)
        if total == 0:
            print("All collection cards already have detail data.")
        else:
            print(f"Enriching {total} collection cards with TCGdex detail data…")
            ok = 0
            for i, row in enumerate(rows, 1):
                card_id = row[0]
                print(f"  [{i}/{total}] {card_id}", end="", flush=True)
                data = fetch_json(f"{BASE_URL}/en/cards/{card_id}")
                if data and "status" not in data:
                    store_card_detail(conn, card_id, data)
                    print(" ✓")
                    ok += 1
                else:
                    print(" ✗ (not found or unavailable)")
                time.sleep(0.1)
            print(f"\nDone: {ok}/{total} cards enriched.")
        conn.close()
        return

    # Load existing set totals to enable skip logic
    known_sets: dict[str, int] = {
        row["id"]: row["total"]
        for row in conn.execute("SELECT id, total FROM sets")
    }

    # --- Step 1: collect set IDs available per language ---
    print("Fetching set lists per language…")
    lang_set_ids: dict[str, set] = {}
    for lang in LANGUAGES:
        sets = fetch_json(f"{BASE_URL}/{lang}/sets") or []
        lang_set_ids[lang] = {s["id"] for s in sets}
        print(f"  {lang}: {len(lang_set_ids[lang])} sets")

    en_set_ids = sorted(lang_set_ids["en"])
    total_sets = len(en_set_ids)
    skipped = 0
    updated_set_ids: set[str] = set()

    # --- Step 2: for each EN set, collect multilingual data ---
    for idx, set_id in enumerate(en_set_ids, 1):

        # Fetch EN set list entry to get card count without a full detail call
        # We'll check total against DB; skip if unchanged (unless --force)
        en_set = fetch_json(f"{BASE_URL}/en/sets/{set_id}")
        if not en_set or "cards" not in en_set:
            print(f"[{idx}/{total_sets}] {set_id} — skipped (no EN data)")
            continue

        total_cards = (
            en_set.get("cardCount", {}).get("official")
            or en_set.get("cardCount", {}).get("total")
            or 0
        )

        if not force and known_sets.get(set_id) == total_cards:
            skipped += 1
            continue  # Set unchanged — skip all API calls for it

        print(f"[{idx}/{total_sets}] {set_id}", end="", flush=True)
        series = extract_series(en_set)
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
        updated_set_ids.add(set_id)

    print(f"\nTCGdex import: {total_sets - skipped} sets updated, {skipped} unchanged (skipped).")

    # --- Step 3: PokeAPI species name enrichment ---
    # Only process cards from sets touched in step 2 that still lack a DE name.
    if not updated_set_ids:
        print("\nPokeAPI step: nothing to do (no sets were updated).")
    else:
        placeholders = ",".join("?" * len(updated_set_ids))
        missing_names = [
            row[0] for row in conn.execute(
                f"SELECT DISTINCT name_en FROM cards "
                f"WHERE set_id IN ({placeholders}) AND name_en IS NOT NULL "
                f"AND (name_de IS NULL OR name_fr IS NULL OR name_it IS NULL)",
                list(updated_set_ids),
            )
        ]

        # Deduplicate slugs — one API call per unique Pokémon species
        slug_order: list[str] = []
        slug_cache: dict[str, dict] = {}
        for name_en in missing_names:
            slug, _ = parse_card_name(name_en)
            if slug not in slug_cache:
                slug_cache[slug] = {}
                slug_order.append(slug)

        print(f"\nPokeAPI: {len(missing_names)} card names → {len(slug_order)} unique species to look up…")
        found = 0
        for idx, slug in enumerate(slug_order, 1):
            cache_file = POKEAPI_CACHE / f"api_v2_pokemon-species_{slug}.json"
            from_cache = cache_file.exists()
            names = fetch_pokeapi_names(slug)
            slug_cache[slug] = names
            if names:
                found += 1
            print("." if names else "x", end="" if idx % 50 else f" {idx}\n", flush=True)
            if not from_cache:
                time.sleep(0.1)  # courtesy delay for live requests; connection reuse reduces server load
        print(f"\n  {found}/{len(slug_order)} species found.")

        # Update cards — COALESCE keeps existing TCGdex translations intact
        updates = []
        for name_en in missing_names:
            slug, suffix = parse_card_name(name_en)
            names = slug_cache.get(slug)
            if not names:
                continue
            def localized(lang: str, _names: dict = names, _suffix: str = suffix) -> str | None:
                n = _names.get(lang)
                return (n + _suffix) if n else None
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
        print(f"  {found}/{len(slug_order)} species found, {len(updates)} card name groups updated.")

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
