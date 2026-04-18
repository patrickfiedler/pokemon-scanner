"""
Pokemon Card Scanner — FastAPI backend.

Endpoints:
  POST /scan        — receive image, OCR collector number, return match candidates
  GET  /card/{id}   — look up a card by its pokemontcg id
  GET  /sets        — list all sets (for disambiguation UI)
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytesseract
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

DB_PATH    = Path(__file__).parent.parent.parent / "data" / "cards.db"
DEBUG_DIR  = Path(__file__).parent.parent.parent / "data" / "debug"
IMAGE_DIR  = Path(__file__).parent.parent.parent / "data" / "card_images"
STATIC_DIR = Path(__file__).parent.parent / "frontend" / "static"
TCGDEX_BASE = "https://api.tcgdex.net/v2"

# Vision LLM (Mistral Small 3.2 on OVHcloud AI Endpoints) — optional fallback
OVH_API_KEY = os.getenv("OVH_API_KEY", "")
_OVH_LLM_URL = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions"
_llm_enabled = bool(OVH_API_KEY)  # disabled at runtime on auth errors

DEBUG_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collection (
            user_id   TEXT NOT NULL,
            card_id   TEXT NOT NULL,
            quantity  INTEGER NOT NULL DEFAULT 1,
            added_at  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, card_id)
        )
    """)
    # Card detail columns populated lazily on first scan/add
    existing = {row[1] for row in conn.execute("PRAGMA table_info(cards)")}
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


init_db()


def _tcgdex_enrich(conn: sqlite3.Connection, card_id: str) -> None:
    """Fetch full card detail from TCGdex and cache it in the DB.

    Runs at most once per card (detail_fetched flag). Silent fallback:
    if TCGdex is unavailable the card still works with basic data.
    """
    row = conn.execute(
        "SELECT detail_fetched FROM cards WHERE id = ?", (card_id,)
    ).fetchone()
    if row is None or row["detail_fetched"]:
        return
    try:
        url = f"{TCGDEX_BASE}/en/cards/{card_id}"
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
        if "status" in data:   # TCGdex error response (e.g. 404 JSON)
            return
    except Exception:
        return  # TCGdex unavailable — card still added with basic data
    conn.execute("""
        UPDATE cards SET
            image          = COALESCE(?, image),
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
        data.get("image"),
        data.get("category"),
        data.get("hp"),
        json.dumps(data.get("types"))    if data.get("types")    else None,
        data.get("rarity"),
        data.get("stage"),
        data.get("description"),
        json.dumps(data.get("dexId"))    if data.get("dexId")    else None,
        json.dumps(data.get("attacks"))  if data.get("attacks")  else None,
        json.dumps(data.get("variants")) if data.get("variants") else None,
        card_id,
    ))
    conn.commit()


def _parse_json_fields(card: dict) -> dict:
    """Parse JSON text fields (types, attacks, variants, dex_id) into Python objects."""
    for field in ("types", "attacks", "variants", "dex_id"):
        if card.get(field) and isinstance(card[field], str):
            try:
                card[field] = json.loads(card[field])
            except Exception:
                card[field] = None
    return card

# ---------------------------------------------------------------------------
# Auth — token middleware; token is derived from password via HMAC
# ---------------------------------------------------------------------------

PASSPHRASE = os.environ.get("SCANNER_PASSWORD", "")
PROFILE_NAMES  = [
    os.environ.get("PROFILE_1_NAME", "Ash"),
    os.environ.get("PROFILE_2_NAME", "Misty"),
]
PROFILE_COLORS = ["#e63946", "#4895ef"]  # red, blue — fixed

# Stable token: changes only when SCANNER_PASSWORD changes.
# No server-side state needed; survives restarts.
_TOKEN = hmac.new(PASSPHRASE.encode(), b"pokescan-v1", hashlib.sha256).hexdigest()

# Paths that never require a token (static assets have a "." in last segment)
_PUBLIC = {"/api/login", "/"}


def _is_public(path: str) -> bool:
    """Static assets (have an extension) and card images are always public."""
    return path in _PUBLIC or path.startswith("/card-image/") or "." in path.rsplit("/", 1)[-1]


class TokenAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not PASSPHRASE:
            raise RuntimeError("SCANNER_PASSWORD environment variable is not set")
        if _is_public(request.url.path):
            return await call_next(request)
        token = request.headers.get("X-Token", "")
        if secrets.compare_digest(token, _TOKEN):
            return await call_next(request)
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)


app = FastAPI(title="Pokemon Card Scanner")

app.add_middleware(TokenAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Image preprocessing + OCR
# ---------------------------------------------------------------------------

def save_debug(img: np.ndarray, roi: np.ndarray, ocr_raw: str, result: dict) -> None:
    """Save original image, preprocessed ROI, and OCR result to data/debug/."""
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
    base = DEBUG_DIR / ts
    cv2.imwrite(str(base) + "_original.jpg", img)
    cv2.imwrite(str(base) + "_roi.jpg", roi)
    # Omit image URLs from JSON to keep it readable
    slim = {k: v for k, v in result.items() if k != "matches"}
    slim["match_count"] = len(result.get("matches", []))
    slim["match_names"] = [m.get("name") for m in result.get("matches", [])]
    (base.parent / (base.name + "_result.json")).write_text(
        json.dumps({"ocr_raw": ocr_raw, **slim}, ensure_ascii=False, indent=2)
    )


def _ocr_strip(roi_bgr: np.ndarray) -> str:
    """Preprocess a BGR image strip and return Tesseract output."""
    roi = cv2.resize(roi_bgr, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
    )
    config = (
        "--oem 3 --psm 11 "
        "-c tessedit_char_whitelist=0123456789/"
    )
    return pytesseract.image_to_string(thresh, config=config)


def preprocess_for_ocr(img: np.ndarray) -> np.ndarray:
    """Return the primary ROI as a preprocessed image (for debug display)."""
    h, w = img.shape[:2]
    roi = img[int(h * 0.84):int(h * 0.93), 0:w]
    roi = cv2.resize(roi, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
    )


def ocr_image(img: np.ndarray) -> str:
    """OCR the card number from three overlapping bottom strips and combine."""
    h, w = img.shape[:2]
    # With the bracket guide, the card fills the viewfinder and its number
    # lands at y≈0.82-0.93 of the captured frame. Three overlapping strips
    # give tolerance for slight variation in card position.
    strips = [(0.82, 0.91), (0.84, 0.93), (0.86, 0.95)]
    parts = []
    for y0, y1 in strips:
        roi = img[int(h * y0):int(h * y1), 0:w]
        if roi.size == 0:
            continue
        parts.append(_ocr_strip(roi))
    return "\n".join(parts)


# Set code + number: e.g. "M23H 008/015" — code is 2-5 chars (excludes long illustrator names)
SET_CODE_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,4})\s+(\d{1,4})\s*/\s*(\d{2,4})\b")
NUMBER_RE   = re.compile(r"(\d{1,4})\s*/\s*(\d{2,4})")


def extract_number(text: str) -> tuple[str, str, str | None]:
    """Return (card_number, set_total, set_code_or_None).

    Tries to also capture a printed set code (e.g. 'M23H') before the number.
    Collects ALL number/total matches from the combined strip text, applies
    a leading-digit correction when num > total (e.g. '97/15' → '7/15'),
    then votes — the most frequent valid candidate wins. This handles both
    OCR noise (stray leading digits) and false positives from other text.
    """
    from collections import Counter
    m = SET_CODE_RE.search(text)
    if m:
        return m.group(2).lstrip("0") or "0", m.group(3), m.group(1)

    candidates = []
    for m in NUMBER_RE.finditer(text):
        num_str, total_str = m.group(1), m.group(2)
        num, total = int(num_str), int(total_str)
        while num > total and len(num_str) > 1:
            num_str = num_str[1:]
            num = int(num_str)
        if num <= total:
            candidates.append((num_str.lstrip("0") or "0", total_str))

    if not candidates:
        return None
    (num_str, total_str), _ = Counter(candidates).most_common(1)[0]
    return num_str, total_str, None


# ---------------------------------------------------------------------------
# Vision LLM fallback
# ---------------------------------------------------------------------------

def _prepare_llm_crops(jpg_bytes: bytes) -> tuple[bytes, bytes]:
    """Return (bottom_strip_jpg, top_strip_jpg) for LLM analysis.

    Bottom strip (y=75-100%): collector number area.
    Top strip (y=0-30%): Pokémon name + HP area.
    Both are upscaled to 1200px wide for legibility.
    """
    arr = np.frombuffer(jpg_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    target_w = 1200
    scale = target_w / w

    def _encode(crop: np.ndarray) -> bytes:
        crop = cv2.resize(crop, (target_w, max(1, int(crop.shape[0] * scale))),
                          interpolation=cv2.INTER_CUBIC)
        _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
        return buf.tobytes()

    return _encode(img[int(h * 0.75):, :]), _encode(img[:int(h * 0.30), :])


def extract_number_llm(jpg_bytes: bytes) -> dict | None:
    """Ask Mistral Small 3.2 (vision) for the collector number and Pokémon name.

    Returns {"number": "7", "total": "15", "name": "Pikachu"} or None.
    Sends two crops: bottom strip for the number, top strip for the name.
    Disables itself for the rest of the session on 401 auth errors.
    """
    global _llm_enabled
    if not _llm_enabled:
        return None
    bottom_bytes, top_bytes = _prepare_llm_crops(jpg_bytes)
    b64_bottom = base64.b64encode(bottom_bytes).decode()
    b64_top = base64.b64encode(top_bytes).decode()
    try:
        resp = httpx.post(
            _OVH_LLM_URL,
            headers={"Authorization": f"Bearer {OVH_API_KEY}"},
            json={
                "model": "Mistral-Small-3.2-24B-Instruct-2506",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64_bottom}"}},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64_top}"}},
                        {"type": "text",
                         "text": (
                             "Image 1 is the bottom strip of a Pokémon trading card. "
                             "Image 2 is the top of the same card showing the Pokémon name and HP. "
                             "From Image 1: find the collector number at the bottom-left corner "
                             "in format NUMBER/TOTAL (e.g. '7/15', '45/198'). "
                             "It is NOT the HP, attack damage, weakness, or Pokédex number. "
                             "From Image 2: read the Pokémon name exactly as printed on the card. "
                             "Reply with ONLY valid JSON, no markdown, no explanation: "
                             "{\"number\": \"7/15\", \"name\": \"Pikachu\"} "
                             "Use null for any field you cannot read clearly."
                         )},
                    ],
                }],
                "max_tokens": 60,
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        print(f"[LLM] response: {text!r}")
        parsed = json.loads(text)
        number_str = parsed.get("number") or ""
        m = NUMBER_RE.search(number_str)
        name = parsed.get("name") or None
        if m:
            return {"number": m.group(1).lstrip("0") or "0",
                    "total": m.group(2), "name": name}
        if name:
            # Name found but no valid collector number (e.g. energy cards)
            return {"number": None, "total": None, "name": name}
    except Exception as exc:
        err = str(exc)
        print(f"[LLM] error: {err}")
        if "401" in err:
            _llm_enabled = False
            print("[LLM] API key rejected (401) — disabling LLM for this session")
    return None


def _detect_energy_type_llm(img: np.ndarray) -> str | None:
    """Ask the LLM what energy symbol it sees in the card center.

    Used when the card name ('Basis-Energie') doesn't reveal the type.
    Sends a center crop and asks for fire/fist/lightning etc.
    Returns a canonical energy name or None.
    """
    if not _llm_enabled:
        return None
    h, w = img.shape[:2]
    center = img[int(h * 0.20):int(h * 0.80), int(w * 0.10):int(w * 0.90)]
    target_w = 800
    scale = target_w / center.shape[1]
    center = cv2.resize(center, (target_w, max(1, int(center.shape[0] * scale))),
                        interpolation=cv2.INTER_CUBIC)
    _, buf = cv2.imencode(".jpg", center, [cv2.IMWRITE_JPEG_QUALITY, 90])
    b64 = base64.b64encode(buf.tobytes()).decode()
    try:
        resp = httpx.post(
            _OVH_LLM_URL,
            headers={"Authorization": f"Bearer {OVH_API_KEY}"},
            json={
                "model": "Mistral-Small-3.2-24B-Instruct-2506",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text",
                         "text": (
                             "This is the center of a Pokémon Basic Energy card. "
                             "Use BOTH the dominant background color AND the energy symbol to identify the type. "
                             "Color hints: red/orange=fire, blue=water, green=grass, yellow=lightning, "
                             "orange-brown=fighting, pink/purple=psychic, black=darkness, "
                             "silver/grey=metal, multicolor=dragon, pink-pastel=fairy, white=colorless. "
                             "Reply with ONLY one word from: fire, water, grass, lightning, fighting, "
                             "psychic, darkness, metal, dragon, fairy, colorless. No other words."
                         )},
                    ],
                }],
                "max_tokens": 10,
            },
            timeout=20,
        )
        resp.raise_for_status()
        word = resp.json()["choices"][0]["message"]["content"].strip().lower()
        print(f"[LLM energy] symbol detected: {word!r}")
        for keywords, canonical in _ENERGY_TYPE_MAP:
            if any(kw in word for kw in keywords):
                return canonical
    except Exception as exc:
        print(f"[LLM energy] error: {exc}")
    return None



    """Return matches whose name (any language) matches the given name.

    Tries exact match first, then prefix, then substring — always returning
    the tightest set that has at least one result.
    """
    name_lower = name.lower().strip()
    langs = ("de", "en", "it", "fr", "ja")

    def _names(m):
        return [(m.get(f"name_{l}") or "").lower() for l in langs]

    for strategy in (
        lambda ns: any(n == name_lower for n in ns),          # exact
        lambda ns: any(n.startswith(name_lower) for n in ns), # prefix
        lambda ns: any(name_lower in n for n in ns),          # substring
    ):
        filtered = [m for m in matches if strategy(_names(m))]
        if filtered:
            return filtered
    return matches  # no name match at all — return all


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

_ENERGY_KEYWORDS = ("energie", "energy", "energia")

# Map any emoji/word the LLM might use → canonical English DB name
_ENERGY_TYPE_MAP: list[tuple[tuple[str, ...], str]] = [
    (("fire",    "feuer",  "feu",    "fuoco",     "🔥"),                         "Fire Energy"),
    (("water",   "wasser", "eau",    "acqua",     "💧"),                         "Water Energy"),
    (("grass",   "pflanz", "plante", "erba",      "🌿", "🍃"),                   "Grass Energy"),
    (("lightning","elektro","blitz", "foudre",    "fulmine", "⚡", "⚡️"),        "Lightning Energy"),
    (("fighting","fight",  "kampf",  "combat",    "lotta", "fist", "👊"),        "Fighting Energy"),
    (("psychic", "psycho", "psy",    "psico",     "mental", "🔮", "💜"),         "Psychic Energy"),
    (("darkness","dark",   "finster","dunkel",    "shadow", "ténèbres", "🌑"),   "Darkness Energy"),
    (("metal",   "metall", "steel",  "stahl",     "métal",  "metallo", "⚙", "🔩"), "Metal Energy"),
    (("dragon",  "drache", "dragon", "drago",     "🐉"),                         "Dragon Energy"),
    (("fairy",   "fee",    "feen",   "fée",       "fata",   "🌸"),               "Fairy Energy"),
    (("colorless","farblos","incolore"),                                           "Colorless Energy"),
]

# Hue ranges (OpenCV: 0-179) → canonical energy name
# Each entry: (hue_min, hue_max, canonical)
# Red wraps around 0/179, handled specially below.
_HUE_ENERGY_MAP = [
    (18,  35,  "Lightning Energy"),   # yellow
    (36,  85,  "Grass Energy"),       # green
    (86, 130,  "Water Energy"),       # blue
    (131, 160, "Psychic Energy"),     # purple/violet
    (161, 179, "Fire Energy"),        # red (upper wrap)
    (0,   17,  "Fire Energy"),        # red (lower wrap)
]


def _is_energy_name(name: str) -> bool:
    n = name.lower()
    return any(kw in n for kw in _ENERGY_KEYWORDS)


def _canonical_energy_name(name: str) -> str:
    """Map LLM-returned energy name (may include emoji/German) to canonical DB name."""
    n = name.lower()
    for keywords, canonical in _ENERGY_TYPE_MAP:
        if any(kw in n for kw in keywords):
            return canonical
    return name  # unknown type — fall through to color detection


def _detect_energy_type_by_color(img: np.ndarray) -> str | None:
    """Determine energy type from dominant card color.

    Crops the central 40% of the card (where the energy symbol is large),
    converts to HSV, filters out near-white and near-black pixels,
    then finds the most common hue bucket.

    Returns a canonical energy name like 'Fire Energy', or None if unclear.
    """
    h, w = img.shape[:2]
    # Central region: vertically 25-75%, horizontally 15-85%
    crop = img[int(h * 0.25):int(h * 0.75), int(w * 0.15):int(w * 0.85)]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

    # Mask out near-white (card border) and near-black (darkness energy needs special handling)
    s = hsv[:, :, 1]  # saturation
    v = hsv[:, :, 2]  # value/brightness
    colorful_mask = (s > 60) & (v > 60)  # keep only vivid, non-dark pixels

    # Special case: if almost no colorful pixels → Darkness or Metal (dark/grey)
    colorful_ratio = colorful_mask.sum() / colorful_mask.size
    if colorful_ratio < 0.05:
        # Check if mostly dark → Darkness, or grey/metallic → Metal
        mean_v = v.mean()
        mean_s = s.mean()
        return "Darkness Energy" if mean_v < 80 else "Metal Energy"

    hues = hsv[:, :, 0][colorful_mask]
    if len(hues) == 0:
        return None

    # Find dominant hue using histogram (bins of width ~10 degrees)
    hist, bin_edges = np.histogram(hues, bins=18, range=(0, 180))
    dominant_bin = int(np.argmax(hist))
    dominant_hue = int(bin_edges[dominant_bin])

    for hue_min, hue_max, canonical in _HUE_ENERGY_MAP:
        if hue_min <= dominant_hue <= hue_max:
            print(f"[Color] dominant hue={dominant_hue} → {canonical}")
            return canonical

    print(f"[Color] dominant hue={dominant_hue} → unmapped")
    return None


def _best_energy_card(matches: list[dict]) -> list[dict]:
    """Return a single best-match energy card, preferring the base set."""
    if not matches:
        return matches
    # Priority: base1 > other base* sets > first result
    for preferred in ("base1", "base"):
        for m in matches:
            if m.get("set_id", "").startswith(preferred):
                return [m]
    return [matches[0]]


def cards_by_name(name: str) -> list[dict]:
    """Look up cards by name (any language), exact then substring."""
    name_lower = name.lower().strip()
    conn = get_db()
    try:
        for operator in ("=", "LIKE"):
            val = name_lower if operator == "=" else f"%{name_lower}%"
            rows = conn.execute(
                """SELECT * FROM cards WHERE
                   LOWER(name_de) {} ? OR LOWER(name_en) {} ? OR
                   LOWER(name_it) {} ? OR LOWER(name_fr) {} ? OR
                   LOWER(name_ja) {} ?""".format(*([operator] * 5)),
                [val] * 5,
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]
    finally:
        conn.close()
    return []



def cards_by_number(number: str, set_total: str | None = None, set_code: str | None = None) -> list[dict]:
    """Look up cards by collector number.

    Filtering priority (most → least specific):
      1. set_code match against set ID (case-insensitive substring)
      2. set_total match
      3. number only (no filter)
    Each level falls back to the next if it returns no results.
    """
    n = number.lstrip("0") or "0"
    num_clause = "(CAST(c.number AS TEXT) = ? OR c.number = ?)"
    num_args   = (n, n.zfill(3))

    conn = get_db()
    try:
        # Try set_code filter first (most specific)
        if set_code:
            rows = conn.execute(
                f"""SELECT c.* FROM cards c JOIN sets s ON c.set_id = s.id
                    WHERE {num_clause} AND UPPER(s.id) LIKE ?""",
                (*num_args, f"%{set_code.upper()}%"),
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]

        # Try set_total filter
        if set_total:
            rows = conn.execute(
                f"""SELECT c.* FROM cards c JOIN sets s ON c.set_id = s.id
                    WHERE {num_clause} AND s.total = ?""",
                (*num_args, int(set_total)),
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]

        # Fall back to number only
        rows = conn.execute(
            f"SELECT * FROM cards WHERE {num_clause}",
            num_args,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def enrich_with_set_name(matches: list[dict]) -> list[dict]:
    """Add set_name_* fields and compute best display name for each card."""
    if not matches:
        return matches
    set_ids = list({m["set_id"] for m in matches})
    conn = get_db()
    try:
        placeholders = ",".join("?" * len(set_ids))
        rows = conn.execute(
            f"SELECT id, name_en, name_de, name_fr, name_it, name_ja FROM sets WHERE id IN ({placeholders})",
            set_ids,
        ).fetchall()
        sets = {r["id"]: dict(r) for r in rows}
    finally:
        conn.close()
    for m in matches:
        s = sets.get(m["set_id"], {})
        m["set_name"] = s.get("name_de") or s.get("name_en") or s.get("name_it") or s.get("name_fr") or m["set_id"]
        m["name"] = m.get("name_de") or m.get("name_en") or m.get("name_it") or m.get("name_fr") or m.get("name_ja") or "?"
        m["image_small"] = f"/card-image/{m['id']}" if m.get("image") else None
        _parse_json_fields(m)
    return matches


@app.post("/api/login")
async def login(body: dict):
    """Validate password and return a long-lived auth token."""
    if not secrets.compare_digest(body.get("password", ""), PASSPHRASE):
        raise HTTPException(401, "Wrong password")
    return {"token": _TOKEN}


@app.post("/scan")
async def scan(file: UploadFile = File(...)):
    """Receive a card photo, OCR the collector number, return matching cards."""
    data = await file.read()
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "Could not decode image")

    roi = preprocess_for_ocr(img)
    raw_text = ocr_image(img)
    roi_debug = preprocess_for_ocr(img)
    _, buf = cv2.imencode(".jpg", roi_debug)
    debug_image = base64.b64encode(buf).decode()

    llm_used = False
    llm_name = None
    extracted = None
    if _llm_enabled:
        # LLM first: more reliable and returns the Pokémon name for auto-disambiguation
        llm_result = extract_number_llm(data)
        if llm_result:
            llm_used = True
            llm_name = llm_result.get("name")
            num = llm_result.get("number")
            if num:
                extracted = (num, llm_result["total"], None)

    # Tesseract fallback if LLM unavailable or returned nothing
    if extracted is None:
        extracted = extract_number(raw_text)

    # Energy cards identified by name — skip number lookup entirely
    if llm_name and _is_energy_name(llm_name):
        canonical = _canonical_energy_name(llm_name)
        # If name alone doesn't resolve type (e.g. "Basis-Energie"), ask LLM then color
        if canonical == llm_name:
            canonical = _detect_energy_type_llm(img) or _detect_energy_type_by_color(img) or llm_name
        matches = _best_energy_card(enrich_with_set_name(cards_by_name(canonical)))
        number, set_total, set_code = "?", None, None
    elif extracted is None:
        payload = {"matches": [], "error": "No collector number found"}
        save_debug(img, roi, raw_text, payload)
        return {"ocr_raw": raw_text, "debug_image": debug_image, **payload}
    else:
        number, set_total, set_code = extracted
        matches = enrich_with_set_name(cards_by_number(number, set_total, set_code))
        # Auto-disambiguate using LLM-read name when multiple sets match
        if llm_name and len(matches) > 1:
            matches = _filter_by_name(matches, llm_name)

    payload = {"number": number, "set_total": set_total, "set_code": set_code,
               "matches": matches, "llm_used": llm_used, "llm_name": llm_name}
    save_debug(img, roi, raw_text, payload)
    return {"ocr_raw": raw_text, "debug_image": debug_image, **payload}


@app.get("/lookup")
def lookup(number: str):
    """Look up a card by manually entered collector number, e.g. ?number=45/198"""
    result = extract_number(number)
    if result is None:
        raise HTTPException(400, "Invalid format. Use e.g. 45/198 or just 45")
    n, set_total, _ = result
    matches = enrich_with_set_name(cards_by_number(n, set_total))
    # Enrich each match with TCGdex detail (fetches image if missing; no-op if already done)
    if matches:
        conn = get_db()
        try:
            for m in matches:
                _tcgdex_enrich(conn, m["id"])
        finally:
            conn.close()
        matches = enrich_with_set_name(cards_by_number(n, set_total))
    return {"number": n, "set_total": set_total, "matches": matches}


@app.get("/card/{card_id}")
def get_card(card_id: str):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Card not found")
        _tcgdex_enrich(conn, card_id)
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    finally:
        conn.close()
    return _parse_json_fields(dict(row))


@app.get("/card-image/{card_id}")
async def card_image(card_id: str):
    """Serve card image from local cache; fetch from TCGdex on first request."""
    # Sanitise: only allow characters that appear in TCGdex card IDs
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", card_id):
        raise HTTPException(400, "Invalid card ID")

    cache_file = IMAGE_DIR / f"{card_id}.webp"
    if cache_file.exists():
        return FileResponse(cache_file, media_type="image/webp")

    # Look up the base image URL from DB
    conn = get_db()
    try:
        row = conn.execute("SELECT image FROM cards WHERE id = ?", (card_id,)).fetchone()
    finally:
        conn.close()

    if not row or not row["image"]:
        raise HTTPException(404, "No image available for this card")

    # Try German image first (TCGdex serves /de/ for localised card art),
    # fall back to English if not available (e.g. Japanese-only sets).
    base_url = row["image"]
    urls_to_try = [base_url.replace("/en/", "/de/", 1) + "/low.webp",
                   base_url + "/low.webp"]
    img_data = None
    for url in urls_to_try:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                if r.status == 200:
                    img_data = r.read()
                    break
        except Exception:
            continue
    if not img_data:
        raise HTTPException(502, "Could not fetch image from TCGdex")

    cache_file.write_bytes(img_data)
    return Response(content=img_data, media_type="image/webp")


@app.get("/sets")
def list_sets():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM sets ORDER BY name").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@app.get("/profiles")
def get_profiles():
    return [{"id": str(i), "name": n, "color": c}
            for i, (n, c) in enumerate(zip(PROFILE_NAMES, PROFILE_COLORS))]


@app.get("/collection/{user_id}")
def get_collection(user_id: str):
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT c.*, col.quantity FROM collection col
               JOIN cards c ON col.card_id = c.id
               WHERE col.user_id = ?
               ORDER BY col.added_at DESC""",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return enrich_with_set_name([dict(r) for r in rows])


@app.get("/collection/{user_id}/{card_id}")
def get_collection_item(user_id: str, card_id: str):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT quantity FROM collection WHERE user_id=? AND card_id=?",
            (user_id, card_id),
        ).fetchone()
    finally:
        conn.close()
    return {"quantity": row["quantity"] if row else 0}


@app.post("/collection/{user_id}/{card_id}/add")
def add_to_collection(user_id: str, card_id: str):
    conn = get_db()
    try:
        # Lazy enrichment on first add — silent fallback if TCGdex is unavailable
        _tcgdex_enrich(conn, card_id)
        conn.execute(
            """INSERT INTO collection (user_id, card_id, quantity)
               VALUES (?, ?, 1)
               ON CONFLICT(user_id, card_id) DO UPDATE SET quantity = quantity + 1""",
            (user_id, card_id),
        )
        conn.commit()
        qty = conn.execute(
            "SELECT quantity FROM collection WHERE user_id=? AND card_id=?",
            (user_id, card_id),
        ).fetchone()["quantity"]
    finally:
        conn.close()
    return {"quantity": qty}


@app.post("/collection/{user_id}/{card_id}/remove")
def remove_from_collection(user_id: str, card_id: str):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT quantity FROM collection WHERE user_id=? AND card_id=?",
            (user_id, card_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Not in collection")
        if row["quantity"] <= 1:
            conn.execute(
                "DELETE FROM collection WHERE user_id=? AND card_id=?",
                (user_id, card_id),
            )
            qty = 0
        else:
            conn.execute(
                "UPDATE collection SET quantity = quantity - 1 WHERE user_id=? AND card_id=?",
                (user_id, card_id),
            )
            qty = row["quantity"] - 1
        conn.commit()
    finally:
        conn.close()
    return {"quantity": qty}


# Serve frontend last so API routes take priority
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
