# zegrift

A queryable dataset of every Greek MP's annual asset declaration, built from
two independent sources unified into one schema:

| Source | Coverage | Strengths | Weaknesses |
|---|---|---|---|
| Hellenic Parliament PDFs | 2024 only, ~1837 MPs | Most recent year; authoritative source | Greek font without ToUnicode CMap — needs Tesseract for clean text |
| Vouliwatch API | 2015–2023, ~437 MPs | Clean Unicode; itemized; multi-year history | One year behind; smaller member set |

Both sources are mapped onto the same schema. A query like *"all properties for
MP X across all years and sources"* is a single join.

## Documentation index

- **[pipeline.md](pipeline.md)** — step-by-step instructions for running the
  end-to-end pipeline (scrape → download → parse → load → Vouliwatch → link).
- **[data-model.md](data-model.md)** — the SQLite schema, the source-merge
  convention, and example queries.
- **[implementation-plan.md](implementation-plan.md)** — original design
  document with rationale for the architectural choices (Greek font handling,
  source-merge strategy, etc.).

## Quick start

```bash
# 1. Install
pip install -e .

# 2. Initialise the DB
python -c "import sqlite3; from pathlib import Path; \
  Path('data/db').mkdir(parents=True, exist_ok=True); \
  con = sqlite3.connect('data/db/zegrift.sqlite'); \
  con.executescript(Path('src/db/schema.sql').read_text(encoding='utf-8')); \
  con.close()"

# 3. Run the pipeline (see docs/pipeline.md for details)
python -m src.ingest.scrape_index
python -m src.ingest.download_pdfs
python -m src.parse.parse_pdf data/pdfs/2025/{file}.pdf --out data/parsed/{mp_id}.json
python -m src.db.load --all-parsed data/parsed/
python -m src.ingest.vouliwatch --phase identity
python -m src.link.match
python -m src.ingest.vouliwatch --phase itemized
```

## Repository layout

```
src/
  ingest/         scrape_index.py, download_pdfs.py, vouliwatch.py
  parse/          parse_pdf.py, font_cmap.py, region_ocr.py,
                  templates/decl_2024.py
  db/             schema.sql, load.py, migrations/
  link/           match.py
data/
  pdfs/2025/      downloaded declaration PDFs
  parsed/         parsed JSON per MP
  api_cache/      cached Vouliwatch API responses
  db/zegrift.sqlite
tests/
docs/             you are here
```

## Requirements

- Python 3.11+
- Optional: **Tesseract OCR** (with the Greek language pack) — required to
  decode Greek text in the parliament PDFs. Without it, the parser still
  extracts all numeric data correctly but Greek strings remain garbled. The
  Vouliwatch ingest doesn't need Tesseract (the API returns clean Unicode).

## Status

- Phases 0–4 (acquisition, extraction, persistence, cross-source link)
  implemented and tested on 5 sample MPs end-to-end.
- Phase 5 (metrics) and Phase 6 (dashboard UI) are deferred until full-corpus
  validation completes.
