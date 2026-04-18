# Pokemon Card Scanner

A simple app to help scan and identify Pokemon trading cards for kids.
Cards are primarily German, with some English, Italian, and Japanese.

## Project Goal

Build a simple app that:
1. Scans a Pokemon card (via phone camera or webcam)
2. Identifies the card (set + collector number via OCR)
3. Looks up card details from an online database (pokemontcg.io)
4. Displays card info (name, set, image, stats)

## Architecture Principles

- KISS: Start simple, add complexity only when needed
- DRY: Shared utilities for OCR, API calls
- Unix philosophy: small focused modules

## Key Technical Decisions

- **Card identification**: OCR the bottom-right collector number (e.g. `014/198`)
  - Unique per card within a set; language-independent (always numeric)
  - No need for image ML model — card number is printed on every card
- **Set identification**: OCR the set code or use the set symbol (visual)
  - Strategy: read card number + cross-reference set via pokemontcg.io API
- **API**: `PokemonTCG/pokemon-tcg-data` (GitHub) — clone to server, load into SQLite
  - All English card data, all sets, free, no key, no rate limits
  - `git pull` periodically for new sets
  - Lookup by set code + collector number
- **Language handling**: Each card is mono-lingual (a German card is all German, etc.)
  - The collector number `NNN/TTT` is the same across all language editions of a set
  - No multilingual OCR needed — just read the number, look it up, done

## Stack

**Pure web app — no backend, no app store, works in Safari on iPhone/iPad.**

- **Frontend**: Vanilla JS or Vue (lightweight, no build step needed to start)
- **OCR**: Tesseract.js (runs in-browser via WebAssembly — no server needed)
- **Camera**: browser `getUserMedia` API
- **API**: pokemontcg.io (CORS-friendly, direct from browser)
- **Hosting**: GitHub Pages / Netlify / Vercel (free)
- **PWA**: Add to home screen on iPhone for app-like feel

### iOS Safari notes
- Camera via `getUserMedia` works in Safari on iOS 11+ ✓
- On iOS, only Safari can access the camera in web apps (Chrome/Firefox on iOS cannot)
- App must be used in Safari (or added to home screen as PWA)

## File Structure

```
pokemon/
├── CLAUDE.md              # This file
├── todo.md                # Structured task list
├── session_log.md         # Running log of changes
├── go_on_from_here.md     # Session handoff notes
├── docs/
│   └── YYYY-MM-DD - *.md  # Research & project notes
└── src/                   # Source code (TBD)
```

## Commands

(To be filled in once stack is decided)

## Notes

- Card number format: `NNN/TTT` bottom-right (language-independent!)
- Set symbol: small icon bottom-left (can be matched via template or API)
- Holographic/special cards may cause OCR issues (foil glare)
- pokemontcg.io API key: free at https://dev.pokemontcg.io
