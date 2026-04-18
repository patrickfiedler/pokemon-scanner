"""
Pokemon Card Scanner — FastAPI backend.

Endpoints:
  POST /scan        — receive image, OCR collector number, return match candidates
  GET  /card/{id}   — look up a card by its pokemontcg id
  GET  /sets        — list all sets (for disambiguation UI)
"""

import base64
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

DB_PATH    = Path(__file__).parent.parent.parent / "data" / "cards.db"
DEBUG_DIR  = Path(__file__).parent.parent.parent / "data" / "debug"
STATIC_DIR = Path(__file__).parent.parent / "frontend" / "static"

DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Auth — middleware covers ALL requests including static files
# ---------------------------------------------------------------------------

PASSPHRASE = os.environ.get("SCANNER_PASSWORD", "")
_REALM = 'Basic realm="Pokemon Scanner"'
_CHALLENGE = Response(
    content="Unauthorized", status_code=401,
    headers={"WWW-Authenticate": _REALM},
)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not PASSPHRASE:
            raise RuntimeError("SCANNER_PASSWORD environment variable is not set")
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                _, _, password = decoded.partition(":")
                if secrets.compare_digest(password.encode(), PASSPHRASE.encode()):
                    return await call_next(request)
            except Exception:
                pass
        return _CHALLENGE


# Keep the dependency for API routes too (documents auth in OpenAPI schema)
security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    pass  # middleware already verified; this just adds auth to OpenAPI docs


app = FastAPI(title="Pokemon Card Scanner")

app.add_middleware(BasicAuthMiddleware)
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


def preprocess_for_ocr(img: np.ndarray) -> np.ndarray:
    """Crop the card number strip, enlarge, convert to high-contrast grayscale.

    The user aligns the card's bottom corner with the guide zone (bottom 18%
    of the viewfinder). So the card number sits at roughly y=70-87% of the
    cropped image. Scan the full width to handle both left- and right-side
    number placement (varies by card set).
    """
    h, w = img.shape[:2]
    roi = img[int(h * 0.70):int(h * 0.87), 0:w]
    roi = cv2.resize(roi, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
    )
    return thresh


def preprocess_to_jpeg(img: np.ndarray) -> str:
    """Return preprocessed ROI as base64 JPEG for debug display."""
    processed = preprocess_for_ocr(img)
    _, buf = cv2.imencode(".jpg", processed)
    return base64.b64encode(buf).decode()


def ocr_image(img: np.ndarray) -> str:
    processed = preprocess_for_ocr(img)
    # PSM 11 = sparse text, finds text anywhere in the image (needed for full-width strip)
    # Include uppercase letters so we can also capture set codes (e.g. M23H before 008/015)
    config = "--oem 3 --psm 11 -c tessedit_char_whitelist=0123456789/ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return pytesseract.image_to_string(processed, config=config)


# Set code + number: e.g. "M23H 008/015" — code is 2-5 chars (excludes long illustrator names)
SET_CODE_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,4})\s+(\d{1,4})\s*/\s*(\d{2,4})\b")
NUMBER_RE   = re.compile(r"(\d{1,4})\s*/\s*(\d{2,4})")


def extract_number(text: str) -> tuple[str, str, str | None]:
    """Return (card_number, set_total, set_code_or_None).

    Tries to also capture a printed set code (e.g. 'M23H') before the number.
    set_code may be None if no code was detected.
    """
    m = SET_CODE_RE.search(text)
    if m:
        return m.group(2).lstrip("0") or "0", m.group(3), m.group(1)
    m = NUMBER_RE.search(text)
    if m:
        return m.group(1).lstrip("0") or "0", m.group(2), None
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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
        # Best set name: German > English > raw ID
        m["set_name"] = s.get("name_de") or s.get("name_en") or s.get("name_it") or s.get("name_fr") or m["set_id"]
        # Best card name: German > English > Italian > Japanese
        m["name"] = m.get("name_de") or m.get("name_en") or m.get("name_it") or m.get("name_fr") or m.get("name_ja") or "?"
        # Expose image field (was image_small in old schema)
        m["image_small"] = m.get("image")
    return matches


@app.post("/scan")
async def scan(file: UploadFile = File(...), _=Depends(require_auth)):
    """Receive a card photo, OCR the collector number, return matching cards."""
    data = await file.read()
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "Could not decode image")

    roi = preprocess_for_ocr(img)
    raw_text = ocr_image(img)
    debug_image = preprocess_to_jpeg(img)
    extracted = extract_number(raw_text)

    if extracted is None:
        payload = {"matches": [], "error": "No collector number found"}
        save_debug(img, roi, raw_text, payload)
        return {"ocr_raw": raw_text, "debug_image": debug_image, **payload}

    number, set_total, set_code = extracted
    matches = enrich_with_set_name(cards_by_number(number, set_total, set_code))
    payload = {"number": number, "set_total": set_total, "set_code": set_code, "matches": matches}
    save_debug(img, roi, raw_text, payload)
    return {"ocr_raw": raw_text, "debug_image": debug_image, **payload}


@app.get("/lookup")
def lookup(number: str, _=Depends(require_auth)):
    """Look up a card by manually entered collector number, e.g. ?number=45/198"""
    result = extract_number(number)
    if result is None:
        raise HTTPException(400, "Invalid format. Use e.g. 45/198 or just 45")
    n, set_total, _ = result
    matches = enrich_with_set_name(cards_by_number(n, set_total))
    return {"number": n, "set_total": set_total, "matches": matches}


@app.get("/card/{card_id}")
def get_card(card_id: str, _=Depends(require_auth)):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, "Card not found")
    return dict(row)


@app.get("/sets")
def list_sets(_=Depends(require_auth)):
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM sets ORDER BY name").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# Serve frontend last so API routes take priority
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
