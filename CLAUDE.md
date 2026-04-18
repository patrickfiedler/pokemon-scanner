# Pokemon Card Scanner — CLAUDE.md

A self-hosted web app for scanning and identifying Pokémon trading cards.  
Cards are multilingual: primarily German, also English, Italian, French, Japanese.

## How the App Works

1. **Scan**: Phone camera shows a live viewfinder. User aligns the card's bottom edge to the guide line.
2. **OCR**: On capture, the frontend sends only the viewfinder area (object-fit:cover crop) to the backend.
3. **Extract**: Backend crops the bottom strip (y=70–87%, full width) and runs Tesseract OCR.
   - Whitelist: `0123456789/ABCDEFGHIJKLMNOPQRSTUVWXYZ` (digits + slash + uppercase for set codes)
   - PSM 11 (sparse text) — finds text anywhere in the strip
   - Extracts `NNN/TTT` (collector number / set total) and optionally a set code (e.g. `M23H`)
4. **Lookup**: Backend queries SQLite for matching cards, filtering by:
   1. Set code (e.g. `M23H` → `UPPER(set_id) LIKE %M23H%`) — most specific
   2. Set total (`s.total = TTT`) — narrows to the right set when no code
   3. Number only — fallback
5. **Display**: Returns card name (German preferred), set name, image URL.

**Manual lookup**: User can also type `NNN/TTT` directly in the UI.

## Architecture

```
pokemon/
├── src/
│   ├── backend/
│   │   └── main.py          # FastAPI: /scan (OCR), /lookup, /sets
│   └── frontend/
│       └── static/
│           ├── index.html   # Viewfinder UI, guide line
│           └── app.js       # Camera crop, fetch to backend, result display
├── import_cards.py          # Data import: TCGdex + PokeAPI (run once + incremental)
├── data/
│   ├── cards.db             # SQLite: sets + cards (all languages)
│   └── pokeapi_cache/       # JSON cache: one file per Pokémon species slug
└── deploy/
    ├── setup.sh             # First-time VPS setup
    └── update.sh            # git pull + restart service
```

**Stack**: FastAPI · SQLite · Vanilla JS · Tesseract OCR · nginx · systemd · Debian VPS  
**Server**: `root@pokemon.mrfiedler.de`  
**Auth**: Simple token auth on backend (token in `.env`)

## Database Schema

```sql
sets  (id, name_en, name_de, name_fr, name_it, name_ja, series, total)
cards (id, set_id, number, name_en, name_de, name_fr, name_it, name_ja, image)
```

Name priority for display: `de > en > it > fr > ja`

## Data Sources

### ✅ TCGdex API (in use — primary card data)
- **URL**: `https://api.tcgdex.net/v2/{lang}/sets/{id}`
- **Coverage**: 200+ EN sets, partial DE/FR/IT/JA (not all sets translated)
- **What we use**: Set list + card list per set, localised names, card counts
- **Limits**: None stated; we process sequentially with no artificial delay
- **Import**: `import_cards.py` Step 1+2; incremental (skips sets where total is unchanged)

### ✅ PokeAPI (in use — species name translations)
- **URL**: `https://pokeapi.co/api/v2/pokemon-species/{slug}`
- **What we use**: `names[]` array → DE/FR/IT/JA species names for cards missing TCGdex translations
- **Fair use policy**: No rate limit, but must locally cache all responses
- **Our caching**: `data/pokeapi_cache/api_v2_pokemon-species_{slug}.json` (null for 404s)
- **Connection**: Single persistent HTTPS connection reused across all lookups
- **Delay**: 100ms between live requests (skipped for cached hits)
- **Import**: `import_cards.py` Step 3; incremental (only updated sets, only missing names)
- **Limitation**: Only covers actual Pokémon species — Trainer/Energy/Item cards get 404 (cached)
- **Slug derivation**: Strip TCG suffixes (ex, EX, GX, V, VMAX, VSTAR, LV.X, ◆) from EN name, lowercase + hyphenate

### ❌ pokemontcg.io (tried, decided against)
- Free API with key; has `foreignData` field for DE/IT/JA names
- **Rejected**: Requires API key, rate-limited on free tier, coverage incomplete for non-EN sets
- **Better alternative**: TCGdex is fully open, no key, better multilingual coverage

### ❌ PokemonTCG/pokemon-tcg-data (GitHub repo, tried, decided against)
- Static JSON files with all EN card data; `git clone` to server
- **Rejected**: English only; no DE/FR/IT/JA names; replaced by TCGdex
- **Better alternative**: TCGdex API covers multilingual needs

### ❌ pokemondb.net (researched, not used)
- Human-readable Pokédex with German names visible on species pages
- **Rejected**: Scraping would be fragile and against ToS; PokeAPI covers the same data cleanly

### ⚠️ pokebase (Python library, considered, not used)
- Official Python wrapper for PokeAPI with auto disk-caching (`~/.cache/pokebase/`)
- **Not adopted**: We implemented equivalent caching ourselves; avoids extra dependency
- **Would use if**: Our manual cache breaks or we need more PokeAPI endpoints

## Import Script

```bash
python import_cards.py           # incremental (skip unchanged sets)
python import_cards.py --force   # full re-import
python import_cards.py -h        # help
```

Steps:
1. Fetch set lists for all 5 languages
2. For each EN set: if total changed (or --force), fetch all language variants → update DB
3. For cards in updated sets missing DE/FR/IT names: call PokeAPI (with cache)

No app restart needed after import — backend opens DB fresh per request.

## Commands

```bash
# Local dev (not typically needed — runs on VPS)
pip install -r requirements.txt
uvicorn src.backend.main:app --reload

# Deploy
ssh root@pokemon.mrfiedler.de "bash /opt/pokemon-scanner/deploy/update.sh"

# Import (on VPS)
ssh root@pokemon.mrfiedler.de "cd /opt/pokemon-scanner && venv/bin/python import_cards.py"

# Pull debug images
./fetch-debug.sh    # rsyncs data/debug/ from VPS
```

## Known Limitations

- **Set code mismatch**: Printed code `M23H` ≠ TCGdex ID `2023sv` — set code OCR falls back to set_total gracefully
- **Japanese coverage**: Only ~14 JA card names from TCGdex (set ID mismatch between JA/EN endpoints)
- **Holographic cards**: OCR can struggle with foil glare on the number area
- **Trainer/Item/Energy cards**: No PokeAPI translation available (not species); show EN name only

