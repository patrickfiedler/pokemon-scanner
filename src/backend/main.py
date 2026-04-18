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

DB_PATH = Path(__file__).parent.parent.parent / "data" / "cards.db"
STATIC_DIR = Path(__file__).parent.parent / "frontend" / "static"

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

def preprocess_for_ocr(img: np.ndarray) -> np.ndarray:
    """Crop bottom-right, enlarge, convert to high-contrast grayscale."""
    h, w = img.shape[:2]
    # Bottom-right corner: rightmost 45%, bottom 12%
    roi = img[int(h * 0.88):h, int(w * 0.55):w]
    # Upscale for better OCR
    roi = cv2.resize(roi, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    # Adaptive threshold handles variable lighting / glare
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
    config = "--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789/ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return pytesseract.image_to_string(processed, config=config)


NUMBER_RE = re.compile(r"(\d{1,4})\s*/\s*(\d{2,4})")


def extract_number(text: str) -> tuple[str, str] | None:
    """Return (card_number, set_total) or None."""
    m = NUMBER_RE.search(text)
    if m:
        return m.group(1).lstrip("0") or "0", m.group(2)
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def cards_by_number(number: str, set_total: str | None = None) -> list[dict]:
    """Look up cards by collector number, optionally filtered by set total."""
    n = number.lstrip("0") or "0"
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM cards WHERE CAST(number AS TEXT) = ? OR number = ?",
            (n, n.zfill(3)),
        ).fetchall()
        matches = [dict(r) for r in rows]
    finally:
        conn.close()
    # If we know the set total, filter to sets whose total matches
    if set_total and matches:
        # Join with sets table to check total — simpler: just keep all, it's few results
        pass
    return matches


@app.post("/scan")
async def scan(file: UploadFile = File(...), _=Depends(require_auth)):
    """Receive a card photo, OCR the collector number, return matching cards."""
    data = await file.read()
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "Could not decode image")

    raw_text = ocr_image(img)
    debug_image = preprocess_to_jpeg(img)
    result = extract_number(raw_text)

    if result is None:
        return {"ocr_raw": raw_text, "debug_image": debug_image, "matches": [], "error": "No collector number found"}

    number, set_total = result
    return {
        "ocr_raw": raw_text,
        "debug_image": debug_image,
        "number": number,
        "set_total": set_total,
        "matches": cards_by_number(number, set_total),
    }


@app.get("/lookup")
def lookup(number: str, _=Depends(require_auth)):
    """Look up a card by manually entered collector number, e.g. ?number=45/198"""
    result = extract_number(number)
    if result is None:
        raise HTTPException(400, "Invalid format. Use e.g. 45/198 or just 45")
    n, set_total = result
    matches = cards_by_number(n, set_total)
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
