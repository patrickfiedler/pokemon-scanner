# Pokemon Card Scanner — CLAUDE.md

A self-hosted web app for scanning and identifying Pokémon trading cards.
Cards are multilingual: primarily German, also English, Italian, French, Japanese.

**Server**: `root@pokemon.mrfiedler.de` · App lives at `/opt/pokemon-scanner/`
**Stack**: FastAPI · SQLite · Vanilla JS · Tesseract OCR · OVHcloud Mistral vision LLM · OpenCV · nginx · systemd · Debian VPS

---

## How a Scan Works (end-to-end)

1. **Camera**: Phone shows a live viewfinder with a card-frame bracket overlay (46% wide, 86% tall, centred).
2. **Capture**: JS crops the video frame to the card bracket bounds (with 3% margin) and sends it as JPEG to `POST /scan`.
3. **OCR**: Backend runs three overlapping bottom-strip crops (y=82–91%, 84–93%, 86–95%) through Tesseract (PSM 11, digits+slash only). `extract_number()` votes across all three strips, corrects leading-digit OCR errors, returns `(number, set_total, set_code)`.
4. **LLM first** (if enabled): `extract_number_llm()` sends two crops to Mistral Small 3.2:
   - Bottom strip (y=75–100%, 1200px wide) → collector number
   - Top strip (y=0–30%, 1200px wide) → Pokémon name
   LLM result overrides Tesseract. Name is used for auto-disambiguation when multiple sets match.
5. **Energy path**: If LLM name contains "energie/energy/energia" → `_is_energy_name()` triggers energy branch:
   - Type embedded in name (e.g. "Feuer-Energie") → `_canonical_energy_name()` maps directly
   - Generic name ("Basis-Energie") → run LLM energy detector + color detector in parallel → color wins for Darkness/Metal/Psychic
6. **DB lookup**: `cards_by_number()` queries SQLite with priority: set_code → set_total → number only. `_filter_by_name()` further narrows multi-match results using the LLM-read Pokémon name.
7. **Response**: Returns matches (German name preferred), `scan_id`, OCR debug image.
8. **Add to collection**: User taps ➕ → `POST /collection/{user_id}/{card_id}/add?scan_id=xxx` → lazy-fetches TCGdex detail (hp, types, rarity, etc.), writes `{scan_id}_added.json` debug marker.

**Manual lookup**: User types `NNN/TTT` in the UI → `GET /lookup?number=NNN/TTT`.

---

## File Tree

```
pokemon/
├── src/
│   ├── backend/
│   │   └── main.py              # All backend logic (see section below)
│   └── frontend/
│       └── static/
│           ├── index.html       # Single-page app shell
│           └── app.js           # All frontend logic (see section below)
├── import_cards.py              # Data import: TCGdex + PokeAPI
├── data/
│   ├── cards.db                 # SQLite: sets + cards (all languages) + collections
│   ├── card_images/             # Local webp cache of TCGdex card images
│   ├── debug/                   # Per-scan debug files (see Debug section)
│   ├── energy_refs/             # Reference energy card images for LLM few-shot
│   └── pokeapi_cache/           # JSON cache: one file per Pokémon species slug
├── deploy/
│   ├── setup.sh                 # First-time VPS setup
│   ├── update.sh                # git pull + migrations + restart
│   ├── migrate.py               # Migration runner helper
│   └── nginx.conf               # nginx reverse proxy config
├── fetch-debug.sh               # Pull data/debug/ from VPS + journal
└── requirements.txt
```

---

## Backend: `src/backend/main.py`

Key sections and approximate line numbers (will drift):

| Section | ~Lines | Description |
|---|---|---|
| Constants / globals | 34–46 | `DB_PATH`, `DEBUG_DIR`, `OVH_API_KEY`, `_llm_enabled` |
| `init_db()` | 49–80 | Creates `collection` table; lazy-adds detail columns to `cards` |
| `_tcgdex_enrich()` | 83–129 | Fetches hp/types/rarity/stage etc. from TCGdex on first card add |
| `_parse_json_fields()` | 132–140 | Parses JSON text columns into Python objects |
| Auth middleware | 142–186 | `TokenAuthMiddleware` — HMAC token, no server state |
| `save_debug()` | 199–216 | Writes `_original.jpg`, `_roi.jpg`, `_result.json` per scan; returns `scan_id` |
| OCR pipeline | 219–292 | `_ocr_strip()`, `preprocess_for_ocr()`, `ocr_image()`, `extract_number()` |
| `extract_number_llm()` | 321–384 | Mistral vision call; strips markdown fences before JSON parse |
| Energy LLM | 436–496 | `_detect_energy_type_llm()` — few-shot with 9 reference images |
| Energy constants | 528–556 | `_ENERGY_TYPE_MAP`, `_HUE_ENERGY_MAP` |
| Energy helpers | 558–652 | `_is_energy_name()`, `_canonical_energy_name()`, `_detect_energy_type_by_color()`, `_best_energy_card()` |
| DB helpers | 655–750 | `cards_by_name()`, `cards_by_number()`, `enrich_with_set_name()`, `_filter_by_name()` |
| `/scan` endpoint | 755–837 | Main scan flow |
| `/lookup` | 840–857 | Manual number entry |
| `/card/{id}` | 860–871 | Single card detail |
| `/card-image/{id}` | 874–913 | Image proxy/cache (tries DE art first, falls back to EN) |
| `/collection/*` | 932–1000 | Collection CRUD endpoints |

### Auth

- Password set via `SCANNER_PASSWORD` env var (in `.env` on VPS).
- Token = `HMAC-SHA256(password, "pokescan-v1")` — stable, survives restarts.
- Sent as `X-Token` header on every request.
- Static assets (`.js`, `.css`, `.webp`) and `/card-image/*` are public (no token needed).
- `_llm_enabled` global: set `False` permanently if OVH API returns 401.

### Energy Detection Chain

For a card whose LLM name contains "energie/energy/energia":

```
1. _canonical_energy_name(llm_name)
   → if name contains "feuer/fire/🔥" etc. → canonical type directly (method="name")
   → if still generic ("Basis-Energie") → proceed to step 2

2. Run in parallel (sequential in code, both always run):
   a. _detect_energy_type_llm(img)   → sends card + 9 reference images to Mistral
   b. _detect_energy_type_by_color(img) → HSV heuristics

3. Merge results:
   _color_wins = {"Darkness Energy", "Metal Energy", "Psychic Energy"}
   - color ∈ _color_wins AND llm ∉ _color_wins → use color  (method="color")
   - else llm is set → use llm                              (method="llm")
   - else color is set → use color                          (method="color")
   - else → use raw llm_name as fallback                    (method="fallback")

4. cards_by_name(canonical) → _best_energy_card() → prefer base1 set
```

**Color detection heuristics** (`_detect_energy_type_by_color`):
- Crops central 20–80% vertically, 10–90% horizontally
- Metal: `mean_s < 65` (grey/silver, low saturation)
- Darkness: `dark_ratio > 17%` (pixels with V < 80)
- Psychic: `pink_ratio > 15%` (hue 130–179) AND `orange_ratio < 8%` (hue 5–35)
- Others: dominant hue histogram bin → `_HUE_ENERGY_MAP`
- All metrics logged as `[Color] ...` for debugging

**Energy reference images** stored in `data/energy_refs/{type}.jpg` — downloaded from TCGdex base set on first run, resized to 200px wide for the LLM payload.

### Error Codes (returned in `error` field)

| Code | Meaning | Frontend message |
|---|---|---|
| `no_ocr_output` | Tesseract got blank image | "Kein Text erkannt – bessere Beleuchtung…" |
| `no_number_found` | OCR text exists but no NNN/TTT found | "Nummer nicht gefunden – Karte so halten…" |
| `no_match` | Number found, no DB match | "Karte (NNN/TTT) nicht in der Datenbank gefunden." |
| HTTP 500 | Unhandled server exception | "Serverfehler beim Scannen. Bitte nochmal versuchen." |

### Structured Logging

Grep these prefixes in `journalctl`:

| Prefix | When |
|---|---|
| `[LLM] response:` | Raw LLM text (may show markdown fences if model misbehaves) |
| `[LLM] error:` | JSONDecodeError or HTTP error from LLM call |
| `[LLM energy] detected:` | Energy type word returned by LLM |
| `[LLM energy] error:` | Error from energy LLM call |
| `[Color] ...` | Color detector result with all metrics |
| `[Energy] name=... canonical=... method=...` | Final energy decision |
| `[Scan] result=ok/no_match/no_ocr_output/no_number_found` | Per-scan summary |

---

## Frontend: `src/frontend/static/app.js`

Key globals and sections:

| Variable/Section | Description |
|---|---|
| `TYPE_DE` (~L62) | EN→DE type name map (Elektro, Finsternis, etc.) |
| `TYPE_ORDER` (~L69) | Canonical order for type filter chips |
| `RARITY_DE`, `STAGE_DE` (~L516) | German rarity/stage translations |
| `lastScanId` (~L57) | Stored from `/scan` response; passed to `/add` for debug correlation |
| `typeFilter` (~L60) | Active type filter (null = all) |
| `apiFetch()` (~L9) | Injects X-Token, handles 401 → password overlay |
| `captureBtn` listener (~L205) | Two-step crop: viewfinder cover-crop → card bracket crop (46% × 86%, 3% margin) |
| `handleScanResult()` (~L252) | Parses scan response, maps error codes to German messages |
| `buildTypeChips()` (~L437) | Builds dynamic type filter row from collection |
| `applyChips()` (~L403) | Sorts (newest/name/quantity/KP↑/KP↓) + filters (category + type) |
| Card detail overlay | Shows name, set, number, KP, type emoji, rarity (DE), stage (DE), ➕/➖ |

**Card bracket CSS** (must stay in sync with JS crop math):
- `#card-frame`: `width: 46%`, centred (`left: 50%`), `top: 7%`, `bottom: 7%`
- JS: `cfLeft = (0.27 - 0.03) * sw`, `cfTop = (0.07 - 0.03) * sh`, `cfW = (0.46 + 0.06) * sw`

**Collection features**:
- Two profiles (names/colors from `.env`)
- Sort: Neueste · Name · Häufigste · KP ↓ · KP ↑
- Filter by category chip (Alle / Pokémon / Trainer / Energie)
- Filter by type chip row (only shows types present in collection)
- Card detail: full image, DE name, set, number, HP, type, rarity, stage, quantity ±

---

## Database Schema

```sql
sets  (id, name_en, name_de, name_fr, name_it, name_ja, series, total)

cards (id, set_id, number, name_en, name_de, name_fr, name_it, name_ja, image,
       category, hp, types, rarity, stage, description, dex_id,
       attacks, variants, detail_fetched)
-- types/attacks/variants/dex_id: JSON strings, parsed by _parse_json_fields()
-- detail_fetched: 0 until first TCGdex enrichment

collection (user_id, card_id, quantity, added_at)
-- user_id: "0" or "1" (profile index)
-- PRIMARY KEY (user_id, card_id)
```

Name display priority: `de > en > it > fr > ja`

---

## Data Sources

### ✅ TCGdex API (in use — primary card data)
- **URL**: `https://api.tcgdex.net/v2/{lang}/sets/{id}` and `/en/cards/{id}`
- **Coverage**: 200+ EN sets, partial DE/FR/IT/JA
- **Import**: `import_cards.py` Step 1+2 — incremental (skips sets where total unchanged)
- **Lazy enrichment**: `_tcgdex_enrich()` fetches hp/types/rarity etc. on first card add/lookup
- **Image cache**: `GET /card-image/{id}` → tries DE art first, falls back to EN, caches as `.webp`

### ✅ PokeAPI (in use — species name translations)
- **URL**: `https://pokeapi.co/api/v2/pokemon-species/{slug}`
- **Cache**: `data/pokeapi_cache/api_v2_pokemon-species_{slug}.json` (null JSON for 404s)
- **Delay**: 100ms between live requests; skipped for cached hits
- **Limitation**: Only Pokémon species — Trainer/Energy/Item get 404 (cached as null)
- **Slug derivation**: Strip TCG suffixes (ex, EX, GX, V, VMAX, VSTAR, LV.X, ◆), lowercase + hyphenate

### ✅ OVHcloud AI Endpoints (in use — vision LLM)
- **Model**: `Mistral-Small-3.2-24B-Instruct-2506`
- **URL**: `https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions`
- **Key**: `OVH_API_KEY` in `.env`
- **Used for**: Number extraction + Pokémon name reading; energy type identification (few-shot)
- **Known quirk**: Model sometimes wraps JSON in markdown fences (` ```json `) — stripped before parse
- **Fallback**: If 401, `_llm_enabled` set False for rest of session; Tesseract-only mode

### ❌ pokemontcg.io — Rejected (key required, rate-limited, incomplete non-EN coverage)
### ❌ PokemonTCG/pokemon-tcg-data GitHub repo — Rejected (English only)
### ❌ pokemondb.net — Rejected (scraping fragile + ToS)
### ⚠️ pokebase Python lib — Considered but not adopted (we implemented caching ourselves)

---

## Import Script

```bash
# On VPS:
venv/bin/python import_cards.py           # incremental (skip unchanged sets)
venv/bin/python import_cards.py --force   # full re-import all sets
venv/bin/python import_cards.py -h        # help

# Shortcut:
ssh root@pokemon.mrfiedler.de "cd /opt/pokemon-scanner && venv/bin/python import_cards.py"
```

Steps:
1. Fetch set list for all 5 languages from TCGdex
2. For each EN set: if total changed (or `--force`), fetch cards in all languages → upsert DB
3. For cards in updated sets missing DE/FR/IT names: call PokeAPI (cached)

No restart needed — backend opens DB fresh per request.

---

## Deploy

```bash
# Deploy latest code to VPS:
ssh root@pokemon.mrfiedler.de "bash /opt/pokemon-scanner/deploy/update.sh"

# What update.sh does:
# 1. git pull --ff-only
# 2. Self-re-execs if update.sh itself changed
# 3. pip install -r requirements.txt
# 4. Runs pending DB migrations (deploy/migrate.py tracks applied ones)
# 5. systemctl restart pokemon-scanner
```

**Migrations** are defined as bash functions in `update.sh` and tracked in a `schema_migrations` SQLite table. Add new migrations at the bottom — never rename or reorder existing ones.

**nginx** (`deploy/nginx.conf`): plain HTTP reverse proxy to uvicorn on port 8000. `client_max_body_size 10M` for image uploads. Currently has `no-cache` headers (dev mode — see todo `nginx-cache`).

**Python environment**:
- Always use `venv/` in project root (created by `setup.sh`)
- `venv/bin/pip install -r requirements.txt` — never bare `pip install`
- `venv/` lives only on VPS; if working locally, create one or use system packages

---

## Debug System

Every scan writes 3–4 files to `data/debug/` on the VPS:

```
{scan_id}_original.jpg   # Full card image as received from frontend
{scan_id}_roi.jpg        # Preprocessed bottom strip (greyscale + threshold)
{scan_id}_result.json    # OCR text, extracted number, matches, error codes, LLM info
{scan_id}_added.json     # Written ONLY when card is added → marks scan as "working"
```

`scan_id` format: `%Y%m%dT%H%M%S%f` UTC (e.g. `20260419T073012123456`).

### Pulling debug data locally

```bash
./fetch-debug.sh
# Rsyncs data/debug/ from VPS + pulls full service journal
# Output: data/debug/*.{jpg,json} + data/debug/journal.log
```

### Classifying scans

```bash
cd data/debug

# Working scans (card added to collection)
ls *_added.json

# Problematic scans (scanned but not added)
python3 -c "
import json, glob, os
for f in sorted(glob.glob('*_result.json')):
    ts = f[:-12]
    if os.path.exists(ts + '_added.json'): continue
    d = json.load(open(f))
    mc = d.get('match_count', len(d.get('matches', [])))
    print(ts, 'error=' + str(d.get('error')), 'matches=' + str(mc), 'llm=' + str(d.get('llm_name')))
"
```

### Grepping the journal

```bash
# All scan outcomes (ok / no_match / no_number_found / no_ocr_output)
grep '\[Scan\]' data/debug/journal.log

# LLM errors (markdown fences, timeouts, auth)
grep '\[LLM\] error' data/debug/journal.log

# Energy detection details
grep '\[Energy\]\|\[Color\]\|\[LLM energy\]' data/debug/journal.log

# 500 errors with root cause
grep -A 5 'Exception in ASGI' data/debug/journal.log

# Live journal on server:
ssh root@pokemon.mrfiedler.de "journalctl -u pokemon-scanner -f"
```

### Common failure patterns

| Symptom | Likely cause | Fix |
|---|---|---|
| `[LLM] error: Expecting value` | Mistral wrapped JSON in ` ```json ``` ` | Already fixed (strip fences before parse) |
| `[Scan] result=no_number_found` + garbled OCR | Card not aligned in bracket / glare | User positioning |
| `match_count=2`, `llm_used=False` | LLM returned None → kids see choice screen | Fix LLM issues (fences, auth) |
| `sqlite3.OperationalError: no such column: c.number` | Missing alias in fallback query | Fixed in b036538 |
| Energy: `energy_canonical=None`, `number='?'` | Both LLM energy + color returned None → fallback used raw name which isn't in DB | Known gap — manual energy add planned |
| `_llm_enabled=False` in logs | OVH API key expired/invalid (401) | Rotate key in `.env`, restart service |

---

## Commands Reference

```bash
# Deploy
ssh root@pokemon.mrfiedler.de "bash /opt/pokemon-scanner/deploy/update.sh"

# Import new cards
ssh root@pokemon.mrfiedler.de "cd /opt/pokemon-scanner && venv/bin/python import_cards.py"

# Pull debug data + journal
./fetch-debug.sh

# Live log tail
ssh root@pokemon.mrfiedler.de "journalctl -u pokemon-scanner -f"

# Local dev (app runs on VPS, rarely needed locally)
uvicorn src.backend.main:app --reload
```

---

## Open Todos

| ID | Title | Notes |
|---|---|---|
| `energy-manual-add` | Manual energy card entry UI | Type picker with example images; replaces unreliable scanning |
| `energy-debug` | Continue tuning energy color thresholds | After deploying, check `[Color]` log lines for failing cards |
| `qwen-energy` | Try Qwen2.5-VL-72B for energy LLM | If Mistral energy detection stays unreliable |
| `japanese-ocr` | Japanese card support | Tesseract digit OCR, investigate TCGdex JA set ID mismatch |
| `nginx-cache` | Re-enable nginx caching | Remove dev `no-cache` headers from `deploy/nginx.conf` |

---

## Known Limitations

- **Set code mismatch**: Printed code (e.g. `M23H`) often differs from TCGdex set ID (`sv07`) — OCR falls back to set_total gracefully
- **Japanese coverage**: Only ~14 JA card names from TCGdex (set ID mismatch between JA/EN API endpoints)
- **Holographic cards**: Foil glare on the number area can confuse Tesseract; LLM handles these better
- **Energy cards**: Generic "Basis-Energie" detection relies on LLM + color — unreliable for metal/psychic. Manual entry planned as replacement.
- **Trainer/Item/Energy cards**: No PokeAPI translation (not species); fall back to EN name
- **Multi-match disambiguation**: Requires LLM to read the Pokémon name; if LLM is down/erroring, kids see a pick-one screen

