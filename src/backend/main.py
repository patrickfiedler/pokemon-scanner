"""
Pokemon Card Scanner — FastAPI backend.

Endpoints:
  POST /scan        — receive image, OCR collector number, return match candidates
  GET  /card/{id}   — look up a card by its pokemontcg id
  GET  /sets        — list all sets (for disambiguation UI)
"""

import io
import json
import re
import sqlite3
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from PIL import Image

DB_PATH = Path(__file__).parent.parent / "data" / "cards.db"
STATIC_DIR = Path(__file__).parent.parent / "src" / "frontend" / "static"

app = FastAPI(title="Pokemon Card Scanner")

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
    # Bottom-right 25% x 12% — where collector number lives
    roi = img[int(h * 0.88):h, int(w * 0.55):w]
    # Upscale for better OCR
    roi = cv2.resize(roi, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    # Adaptive threshold handles variable lighting / glare
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
    )
    return thresh


def ocr_image(img: np.ndarray) -> str:
    processed = preprocess_for_ocr(img)
    config = "--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789/ABCDEFGHIJKLMNOPQRSTUVWXYZ "
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

@app.post("/scan")
async def scan(file: UploadFile = File(...)):
    """
    Receive a card photo, OCR the collector number, return matching cards.
    """
    data = await file.read()
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "Could not decode image")

    raw_text = ocr_image(img)
    result = extract_number(raw_text)

    if result is None:
        return {"ocr_raw": raw_text, "matches": [], "error": "No collector number found"}

    number, set_total = result

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM cards WHERE CAST(number AS TEXT) = ? OR number = ?",
            (number, number.zfill(3)),
        ).fetchall()
        matches = [dict(r) for r in rows]
    finally:
        conn.close()

    return {
        "ocr_raw": raw_text,
        "number": number,
        "set_total": set_total,
        "matches": matches,
    }


@app.get("/card/{card_id}")
def get_card(card_id: str):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, "Card not found")
    return dict(row)


@app.get("/sets")
def list_sets():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM sets ORDER BY name").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# Serve frontend last so API routes take priority
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
