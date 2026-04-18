#!/usr/bin/env bash
# Run the Pokemon Card Scanner locally (HTTP, localhost only)
set -e
cd "$(dirname "$0")"

if [ ! -f data/cards.db ]; then
  echo "Card database not found. Run setup first:"
  echo "  git clone https://github.com/PokemonTCG/pokemon-tcg-data data/pokemon-tcg-data"
  echo "  pip install -r requirements.txt"
  echo "  python import_cards.py"
  exit 1
fi

if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

if [ -z "$SCANNER_PASSWORD" ]; then
  echo "ERROR: SCANNER_PASSWORD is not set. Copy .env.example to .env and set a passphrase."
  exit 1
fi

uvicorn src.backend.main:app --host 0.0.0.0 --port 8000 --reload
