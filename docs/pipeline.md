# Running the pipeline

The pipeline has two parallel data sources (Parliament PDFs, Vouliwatch API)
that merge in the unified schema, plus a linking step in between.

```
┌──────────────────────────┐   ┌──────────────────────────┐
│  Parliament PDFs (2024)  │   │  Vouliwatch API (15–23)  │
└────────────┬─────────────┘   └────────────┬─────────────┘
             │                              │
   ┌─────────▼─────────┐         ┌──────────▼──────────┐
   │ 1. scrape_index   │         │ 5. vouliwatch       │
   │ 2. download_pdfs  │         │    --phase identity │
   │ 3. parse_pdf      │         └──────────┬──────────┘
   │ 4. db.load        │                    │
   └─────────┬─────────┘                    │
             │                              │
             └──────────────┬───────────────┘
                            │
                  ┌─────────▼─────────┐
                  │ 6. link.match     │
                  └─────────┬─────────┘
                            │
                  ┌─────────▼─────────────┐
                  │ 7. vouliwatch         │
                  │    --phase itemized   │
                  └───────────────────────┘
```

All steps are **idempotent**: re-running any step won't duplicate data.

---

## 0. Prerequisites

```bash
pip install -e .
```

Optional but recommended (for clean Greek text in parliament-PDF data):
- **Tesseract OCR** with the Greek language pack (`tesseract-ocr-ell` on Linux,
  `brew install tesseract tesseract-lang` on macOS, or the official Windows
  installer + `tessdata/grc.traineddata` and `tessdata/ell.traineddata`).

Initialise the SQLite database from `schema.sql`:

```bash
python -c "import sqlite3; from pathlib import Path; \
  Path('data/db').mkdir(parents=True, exist_ok=True); \
  con = sqlite3.connect('data/db/zegrift.sqlite'); \
  con.executescript(Path('src/db/schema.sql').read_text(encoding='utf-8')); \
  con.close(); print('DB ready')"
```

If you have an existing DB from an older schema, run the migration:

```bash
python -m src.db.migrations.schema_002_vouliwatch_parity
```

---

## 1. Scrape the parliament index

Pulls the index page and writes one row per MP into `mp_index`.

```bash
python -m src.ingest.scrape_index
```

Result: `mp_index` populated with ~1837 rows (mp_id, Greek + Latin names,
PDF URL). Idempotent — re-runs upsert by mp_id.

---

## 2. Download all PDFs

```bash
python -m src.ingest.download_pdfs --out-dir data/pdfs/2025
```

Useful flags:
- `--limit N` — fetch only the first N (smoke test)
- `--out-dir PATH` — override the default `data/pdfs/2025`

Behaviour:
- Skips files already on disk with matching content-length (safe to resume).
- Throttles to ~2 req/s, exponential backoff on 429 / 5xx.
- Records sha256 + content-length in `pdf_file`.

Expected wall-clock: roughly 15–30 minutes for the full ~1837-file corpus,
depending on parliament-site latency.

---

## 3. Parse PDFs to JSON

The parser runs per-PDF and writes one JSON file per MP into `data/parsed/`.

Single PDF:

```bash
python -m src.parse.parse_pdf data/pdfs/2025/4431678_GKOYNTARAS_ANTONIOS.pdf \
  --mp-id 4431678 \
  --out data/parsed/4431678.json
```

Whole corpus (simple bash loop — there's no built-in batch runner yet):

```bash
mkdir -p data/parsed
for f in data/pdfs/2025/*.pdf; do
  mp_id=$(basename "$f" | cut -d_ -f1)
  python -m src.parse.parse_pdf "$f" --mp-id "$mp_id" \
    --out "data/parsed/${mp_id}.json"
done
```

What the parser does:
- For each page table, identifies the section by column count + Greek title
  keyword (rows 2–4 of every table contain the section title).
- Extracts every column as either a number, date, year, or text.
- If Tesseract is available, attempts CMap reconstruction so Greek text is
  decoded to Unicode; otherwise text stays as garbled bytes but numeric
  data is always clean.
- Writes `ParsedDeclaration` JSON (income, vehicles, deposits, real estate,
  business shares, loans, securities, safe deposit boxes, real estate rights).

Parser version is stamped into each JSON (`PARSER_VERSION = "0.1.2"`). Bumping
the version on a re-parse produces a new declaration row in the DB without
overwriting the old one.

---

## 4. Load parsed JSONs into the DB

```bash
python -m src.db.load --all-parsed data/parsed/
```

Or a single file:

```bash
python -m src.db.load --json data/parsed/4431678.json
```

Behaviour:
- Upserts `declaration(mp_id, fiscal_year, parser_version)` — one row per
  parse pass.
- Deletes prior child rows for that `decl_id` before re-inserting, so re-runs
  are safe.

---

## 5. Vouliwatch — Phase 1 (identity)

```bash
python -m src.ingest.vouliwatch --phase identity
```

Fetches `/api/home` (full member roster) and `/api/member/{slug}` for each
member. Populates:
- `vouli_member` — slug, surname, given name, party
- `vouli_fiscal_year` — year-level aggregated totals (mainly a cross-check
  cache; the itemized data lives in our main tables after Phase 2)

All raw responses are cached on disk under `data/api_cache/`. Re-runs are
fast (cache hit, no throttle). Rate-limited to ~90 req/min on cold runs.

Expected wall-clock: ~10 minutes cold (~437 members × 1 detail call each),
seconds when fully cached.

Flags:
- `--limit N` — process only first N members (smoke test)

---

## 6. Link MPs across sources

```bash
python -m src.link.match
```

Populates `mp_link(mp_id → vouli_slug)` via three passes:
1. Exact match on (surname, given name) after Greek→Latin transliteration
2. Exact surname only (if unique)
3. Fuzzy (rapidfuzz token_sort_ratio ≥ 85%)

Outputs match counts per method and an "unmatched" list (use
`--report unmatched.csv` to write it). Unmatched MPs are usually:
- Vouliwatch-only MPs whose parliament term ended before 2024
- Parliament-only MPs who never declared via Vouliwatch
- Name discrepancies needing manual override

---

## 7. Vouliwatch — Phase 2 (itemized)

```bash
python -m src.ingest.vouliwatch --phase itemized
```

Reads cached `/member/{slug}` responses (no network) and, for every member in
`mp_link`, creates a synthetic `declaration` row with
`parser_version='vouliwatch'` plus itemized rows in:

| Vouliwatch array     | Our table        |
|----------------------|------------------|
| `revenues[]`         | `income_line`    |
| `properties[]`       | `real_estate`    |
| `deposits[]`         | `deposit`        |
| `loans[]`            | `loan`           |
| `companies[]`        | `business_share` |
| `mobile_properties[]`| `vehicle`        |
| `pmproducts[]`       | `security_holding` |
| `lockers[]`          | `safe_deposit_box` |

Idempotent: deletes prior child rows for each Vouliwatch `decl_id` before
re-inserting. Sub-minute even on the full corpus.

---

## Convenience: run-all

If you want to do steps 5 + 7 in one call (with linking already done):

```bash
python -m src.ingest.vouliwatch --phase all
```

Note: this is *not* a full-pipeline runner — it just chains Vouliwatch's two
phases. The parliament side (1–4) and the linker (6) are still separate
commands.

---

## Verifying the result

After step 7, every linked MP should have up to 6 declarations:

```sql
SELECT mp_id, fiscal_year, parser_version, declaration_serial
FROM declaration
WHERE mp_id = 4568095   -- Avramakis Eleftherios
ORDER BY fiscal_year, parser_version;
```

| decl_id | fiscal_year | parser_version | declaration_serial      |
|---------|-------------|----------------|--------------------------|
| 44      | 2019        | vouliwatch     | vouliwatch:35969         |
| 43      | 2020        | vouliwatch     | vouliwatch:35449         |
| 42      | 2021        | vouliwatch     | vouliwatch:34931         |
| 41      | 2022        | vouliwatch     | vouliwatch:34486         |
| 40      | 2023        | vouliwatch     | vouliwatch:34035         |
| (new)   | 2024        | 0.1.2          | Δ…………                    |

A cross-source query (all real estate for one MP over all years):

```sql
SELECT d.fiscal_year, d.parser_version,
       re.kind, re.country, re.value_eur, re.share_pct
FROM real_estate re
JOIN declaration d ON re.decl_id = d.decl_id
WHERE d.mp_id = 4568095
ORDER BY d.fiscal_year;
```

---

## Common issues

**`tesseract_unavailable: Greek text not decoded`** in the parse step's
`errors` field — Tesseract isn't installed. Numbers and dates still parse
correctly; only Greek strings are affected. Fix by installing Tesseract +
Greek language pack and re-parsing.

**`Phase 2: mp_link is empty`** — you ran `vouliwatch --phase itemized`
before `link.match`. Run the linker first.

**`ERROR: DB not found`** — run the schema initialisation from the
Prerequisites section.

**429 errors on Vouliwatch ingest** — the throttler should keep you under
the limit; if you hit one, the tenacity retry decorator will back off and
retry up to 5 times. If it persists, the API may be rate-limiting on a
shorter window — wait a minute and re-run; cached responses will skip
network entirely.
