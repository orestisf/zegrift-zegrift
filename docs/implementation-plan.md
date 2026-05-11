# Greek MP Wealth Dashboard — Implementation Plan

Status: draft, written 2026-05-11. Covers Phase 0 through Phase 4 (data acquisition,
extraction, persistence, cross-source linking). Phase 5 (metrics) and Phase 6 (dashboard)
are deferred until the data layer is solid.

## 1. Goal

Build a queryable dataset of every Greek MP's annual asset declaration ("Δήλωση
Περιουσιακής Κατάστασης") for fiscal year 2024, combine it with the historical
2015–2023 series available from the Vouliwatch fiscal-data API, and use that as
the foundation for later analyses of wealth, wealth change, and outliers.

In-scope for this plan:
- Scrape the Hellenic Parliament index of 2024 declarations.
- Download all ~800–900 PDF declarations.
- Parse them into structured rows.
- Persist into SQLite.
- Pull and persist Vouliwatch API data.
- Link the two sources by MP identity.

Out of scope here (separate plan later):
- Statistical analyses, ranking metrics, outlier detection.
- Visualization / dashboard UI.

## 2. Data sources

### 2.1 Vouliwatch fiscal-data API
- Base: `https://pothenesxes.vouliwatch.gr/api`
- No auth. Rate limit: 100 req/min/IP. 429 on exceed.
- Year coverage: **2015–2023** (no 2024 yet).
- Endpoints we will use:
  - `GET /home` — dashboard summary; gives us years available and top-line rankings.
  - `POST /home/members` — filtered member list. Use as canonical roster.
  - `GET /member/{slug}` and `POST /member` — per-member fiscal breakdown across years.
  - `GET /analytics/parties` — party metadata (branding, names).
  - Analytics endpoints (`/analytics/party-wealth*`, `/analytics/party-inequality`, etc.) —
    not strictly needed for ingestion, but useful for cross-check totals during validation.

### 2.2 Hellenic Parliament 2024 declarations
- Index page:
  `https://www.hellenicparliament.gr/Organosi-kai-Leitourgia/epitropi-elegxou-ton-oikonomikon-ton-komaton-kai-ton-vouleftwn/Diloseis-Periousiakis-Katastasis2025/Ethsies-Diloseis-Periousiakis-Katastasis2025`
- PDF URL pattern:
  `https://www.hellenicparliament.gr/userfiles/pothen/xrhsh2024_etos2025/{SURNAME}_{GIVEN_NAME}_{MP_ID}_2025e.pdf`
- Direct download. No auth, no CSRF, no captcha. Confirmed during scoping.
- Each PDF: 10–11 pages, ~200 KB, produced by Oracle BI Publisher,
  identical template across MPs.

## 3. Key technical risk: Greek-font encoding in the PDFs

This is the single hardest piece of the project. Documented here so the chosen
strategy is unambiguous.

### 3.1 The problem
The PDFs embed a subset of `Albany WT J` (a Monotype world-fonts variant) as the
font for all Greek text. The subset has **no `/ToUnicode` CMap**, meaning the
PDF carries glyph indices but no mapping from those glyphs back to Unicode code
points. As a result every standard text extractor (pdfplumber, pypdf,
poppler's pdftotext) returns Greek characters as the replacement character
`�` / `?`.

Latin-script content, numbers, dates, IBANs, and amounts are rendered in
plain Helvetica and **extract perfectly**. So the structural skeleton of every
declaration is recoverable; only the Greek glyphs need a separate strategy.

The font subset is regenerated per file, so glyph IDs are not stable across
PDFs — i.e. there is no one-time CMap fix that transfers.

### 3.2 Strategy — two complementary techniques

**A. Positional extraction (primary, no OCR needed)**
The template is fixed across all MPs. We annotate bounding boxes for each
section of the form **once** (using one or two reference PDFs), then for every
other PDF we pull whatever falls inside each box using
`pdfplumber.page.within_bbox(...).extract_table()` or `.extract_text()`. We
already know what each box semantically *is* (real estate row, vehicle make,
deposit balance, etc.) by position, so the broken Greek labels do not matter.

This gives us perfect extraction for: amounts, dates, percentages, IBANs,
share codes, vehicle plates (Latin), and any free-text fields the declarant
filled in using Latin script (often the case for bank names like "Eurobank",
"Piraeus Bank").

**B. Glyph-shape OCR for Greek free-text values (secondary)**
For free-text cells that contain Greek script (e.g. Greek-spelled street
addresses, Greek-spelled bank names, Greek-spelled vehicle makes), we run a
narrow OCR pass on just those cell regions. Two options, in preferred order:

- **B1. Per-PDF font CMap reconstruction.** Extract the embedded `Albany WT J`
  subset with `fonttools`, render each glyph to a small bitmap, OCR each
  glyph individually with Tesseract (`ell` language). This produces a
  glyph_id → Unicode map *for that single PDF*, which we then apply to
  re-decode the broken text already extracted by pdfplumber. Advantages:
  positions stay intact; only ~50–80 glyphs per PDF to OCR (Greek caps +
  lowercase + accents + a handful of punctuation), so it is fast and per-glyph
  OCR is much more accurate than full-page OCR.
- **B2. Region-level OCR fallback.** If B1 fails (e.g. a glyph that Tesseract
  cannot disambiguate at small size), render the specific cell region of the
  page to a 300-DPI image and OCR that region with Greek Tesseract.

Both B1 and B2 require Tesseract installed on the machine — see §6.

**Decision rule at runtime:** every parsed cell carries an extraction-method
tag (`positional`, `cmap_decoded`, `region_ocr`, `failed`) and a confidence
score. Cells tagged `failed` are written to a manual-review queue.

## 4. Phased plan

### Phase 0 — Environment & index scrape

- Initialize the repo (Python project, `uv` or `venv` + `requirements.txt`).
- Install runtime deps (see §6).
- Install Tesseract OCR + Greek language pack as a one-time machine setup
  (Windows installer from UB Mannheim build). Document the install path in
  `docs/setup.md` (separate file, written when we get to setup).
- Scrape the 2024 declarations index page:
  - Parse the HTML table.
  - Normalize names (Greek script + accented variants).
  - Extract `(surname, given_name, mp_id, pdf_url)` per row.
  - Persist into `mp_index` table (see §7).
- Validate count vs. expected ~300 MPs (note: the index page also lists
  ex-MPs from earlier terms who still have a filing obligation; total may
  reach 800–900). Decide whether to include ex-MPs based on what we find.

Deliverable: `src/ingest/scrape_index.py`, run produces a populated `mp_index`
table with every PDF URL.

### Phase 1 — PDF acquisition

- Downloader: per-MP, with resume support and content-hash recording.
  - Skip if file already on disk **and** `content-length` matches.
  - Throttle to ~2 req/s; backoff on 429/5xx.
  - Store under `data/pdfs/2025/{mp_id}_{slug}.pdf` (committed nowhere; see §5).
- Record per-file metadata: HTTP status, content-length, sha256, fetched_at.
- Surface a summary report at end of run.

Deliverable: `src/ingest/download_pdfs.py`; populated `pdf_file` table; full PDF
set on disk.

### Phase 2 — PDF parser (positional + OCR)

This is the biggest piece of work. Build it in vertical slices: one section
end-to-end before starting the next.

Suggested slice order (easiest financial-value yield first):

1. **Header section** — declaration serial, MP name fields, spouse name fields,
   submission date. Mostly Latin/numeric, low risk.
2. **Real estate table** — property type, share %, area, location, year of
   acquisition, value. Mix of numeric (high yield) and Greek free text (needs OCR).
3. **Vehicles table** — make, model, year, value.
4. **Deposits table** — bank name, account type, balance.
5. **Securities holdings** — instrument, quantity, value.
6. **Business participations** — company, share %, value.
7. **Loans** — lender, original amount, outstanding, kind.
8. **Income lines** — source, amount, kind.

For each slice:
- Annotate bounding boxes in `src/parse/templates/decl_2024.py` as
  module-level constants. Keep them in one file so the schema is reviewable.
- Implement `parse_<section>(page) -> list[Row]`.
- Run against the 20-PDF dev sample; eyeball a CSV diff; iterate on boxes
  until field-extraction rate ≥ ~95% for numeric fields, ≥ ~85% for Greek
  free-text fields (after the OCR pass).
- Tag every output row with parser version and extraction method.

Two sub-tasks ride alongside parsing:

- **Font CMap reconstructor** (`src/parse/font_cmap.py`): given a PDF, extract
  the Albany WT J subset with `fonttools`, render each glyph, OCR it, return
  `dict[glyph_id, str]`. Cache the result keyed on the font subset's content
  hash so we don't re-OCR identical subsets.
- **Region OCR fallback** (`src/parse/region_ocr.py`): given a page and a
  bbox, render that region at 300 DPI and OCR with Tesseract `--lang ell`.

Deliverable: `src/parse/` package; `parse_pdf(path) -> Declaration`
function; per-PDF JSON dump in `data/parsed/{mp_id}.json` for inspection
before DB load.

### Phase 3 — SQLite persistence

- Create the schema (see §7) via `src/db/schema.sql`. No ORM for now —
  raw SQL via `sqlite3` stdlib is sufficient at this size and keeps the
  schema reviewable.
- Loader: `src/db/load.py` takes a parsed declaration and upserts all rows
  in a single transaction. Idempotent by `(mp_id, fiscal_year, parser_version)`.
- Audit table records per-PDF: parser version, total fields extracted, OCR
  glyph count, list of failed regions, manual-review flag.
- One-shot integrity checks after load:
  - Every `declaration` has an `mp` row.
  - Every line-item row has a `declaration` parent.
  - No null mp_id, no null fiscal_year.

Deliverable: `data/db/zegrift.sqlite` with the 2024 declarations loaded.

### Phase 4 — Vouliwatch API ingestion + cross-source linking

- API client: `src/ingest/vouliwatch.py` with rate-limit awareness (token
  bucket at 90 req/min to stay under the 100 cap) and on-disk response cache
  keyed on URL+params hash.
- Pull the canonical member list (`POST /home/members`) → load into
  `vouli_member`. Treat slug as the primary key on the Vouliwatch side.
- For each Vouliwatch member, pull per-year fiscal data (`POST /member`)
  for 2015–2023 → load into `vouli_fiscal_year` and child tables that mirror
  the structure we use for the PDFs (real estate, deposits, etc.). This makes
  cross-year queries trivial.
- Linking (`src/link/match.py`):
  - Normalize both sides: strip accents, uppercase, ASCII-transliterate Greek
    → Latin using a fixed table (Α→A, Β→V, Η→I, etc. — the same scheme
    parliament uses in PDF filenames, e.g. ΑΚΤΥΠΗΣ → AKTYPIS).
  - Exact match on `(surname_normalized, given_name_normalized)`.
  - Fuzzy fallback (`rapidfuzz`) above 90% ratio → flagged for human review.
  - Persist into `mp_link(mp_id, vouli_slug, confidence, method)`.
- Sanity check: number of unmatched MPs on each side; manual list to resolve.

Deliverable: full Vouliwatch dataset in the DB; `mp_link` populated;
unmatched-entries report.

### Phase 5 (deferred) — Metrics and analyses

Planned but not part of this implementation plan. Topics to address in the
next plan:
- Δ wealth 2023 → 2024 per MP, absolute and %.
- Δ wealth vs. declared income (the politically interesting metric).
- Per-asset-class breakdowns.
- Aggregates by party, by government position held, by tenure.
- Outliers (≥ 3σ) flagged for inspection.

### Phase 6 (deferred) — Dashboard

Also for a later plan. Streamlit is the fastest path for v1 given the
Python stack; revisit if/when public-facing distribution becomes a goal.

## 5. Repo layout

```
zegrift-zegrift/
├── docs/
│   ├── implementation-plan.md   (this file)
│   └── setup.md                 (env + Tesseract install — written in Phase 0)
├── src/
│   ├── ingest/
│   │   ├── scrape_index.py
│   │   ├── download_pdfs.py
│   │   └── vouliwatch.py
│   ├── parse/
│   │   ├── templates/
│   │   │   └── decl_2024.py     (bounding boxes per section)
│   │   ├── font_cmap.py
│   │   ├── region_ocr.py
│   │   └── parse_pdf.py
│   ├── db/
│   │   ├── schema.sql
│   │   └── load.py
│   └── link/
│       └── match.py
├── data/                        (gitignored — see §5.1)
│   ├── pdfs/2025/
│   ├── parsed/
│   ├── api_cache/
│   └── db/zegrift.sqlite
├── tests/
│   └── parse/                   (golden-file tests on 5–10 fixture PDFs)
├── pyproject.toml
└── .gitignore
```

### 5.1 What is and is not committed

Committed: all of `src/`, `docs/`, `tests/`, the schema SQL, the bounding-box
template constants, a small set of fixture PDFs under `tests/fixtures/` for
regression testing (3–5 representative declarations, redacted only if
necessary — these are public documents so probably no redaction needed).

Not committed: `data/` in full (PDFs ~200 MB, SQLite a few MB, API cache).
The DB is reproducible from `src/` + the source URLs, so it does not need
git history. We will ship a `make ingest` or equivalent entry point that
rebuilds everything from scratch.

## 6. Dependencies

Python ≥ 3.11. Pin in `pyproject.toml`. Use `uv` for speed if available, else
plain `venv` + `pip`.

Runtime:
- `pdfplumber` — primary PDF text/table extraction.
- `pypdf` — fallback extractor; also used to inspect font streams.
- `fonttools` — extract embedded fonts for the CMap reconstructor.
- `Pillow` — render glyphs and page regions.
- `pytesseract` — OCR wrapper.
- `requests` — HTTP client.
- `beautifulsoup4` + `lxml` — index-page scraping.
- `rapidfuzz` — fuzzy name matching.
- `tenacity` — retry/backoff.
- `tqdm` — progress bars on long ingest runs.

Tooling-only / dev:
- `pytest`, `pytest-snapshot` for golden-file tests on parsed output.
- `ruff` for lint/format.

System dependency (one-time install):
- **Tesseract OCR** + Greek language data (`tess-lang-ell`). Recommended:
  UB Mannheim Windows build. Set `pytesseract.pytesseract.tesseract_cmd`
  explicitly from an env var so the parser doesn't rely on PATH.

Note: `pdftoppm` and LibreOffice are not installed on this machine per the
global notes, and we don't need them — `pdfplumber`/`Pillow` give us page
rendering, and we never need PDF → DOCX conversion.

## 7. SQLite schema (draft)

Final DDL will live in `src/db/schema.sql`. Sketch:

```sql
-- Roster scraped from the parliament index page
CREATE TABLE mp_index (
    mp_id           INTEGER PRIMARY KEY,        -- the numeric ID embedded in the PDF filename
    surname_gr      TEXT NOT NULL,
    given_name_gr   TEXT NOT NULL,
    surname_lat     TEXT NOT NULL,              -- as it appears in the PDF filename
    given_name_lat  TEXT NOT NULL,
    pdf_url         TEXT NOT NULL,
    scraped_at      TEXT NOT NULL                -- ISO timestamp
);

-- One row per downloaded PDF (lets us re-run idempotently)
CREATE TABLE pdf_file (
    mp_id           INTEGER NOT NULL REFERENCES mp_index(mp_id),
    fiscal_year     INTEGER NOT NULL,            -- 2024 for this batch
    path            TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    content_length  INTEGER NOT NULL,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (mp_id, fiscal_year)
);

-- One row per parse pass (lets us re-parse with newer parser versions
-- without dropping the old rows)
CREATE TABLE declaration (
    decl_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mp_id              INTEGER NOT NULL REFERENCES mp_index(mp_id),
    fiscal_year        INTEGER NOT NULL,
    declaration_serial TEXT,                     -- e.g. Σ2298-6181-3803-3101-9260-7
    submitted_at       TEXT,                     -- as printed on the PDF
    parser_version     TEXT NOT NULL,
    parsed_at          TEXT NOT NULL,
    UNIQUE (mp_id, fiscal_year, parser_version)
);

-- Child tables for line items. All share the same shape: FK to declaration,
-- raw + decoded text columns, extraction_method tag, confidence.
-- (Single example shown; the rest follow the same pattern.)
CREATE TABLE real_estate (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decl_id             INTEGER NOT NULL REFERENCES declaration(decl_id),
    row_index           INTEGER NOT NULL,        -- order on the page
    kind                TEXT,                    -- decoded
    share_pct           REAL,
    area_m2             REAL,
    location_raw        TEXT,                    -- raw bytes from pdfplumber (may be garbage)
    location_decoded    TEXT,                    -- after CMap or region OCR
    acquisition_date    TEXT,
    value_eur           REAL,
    extraction_method   TEXT NOT NULL,           -- positional|cmap_decoded|region_ocr|failed
    confidence          REAL                     -- 0..1
);

-- Other child tables: vehicle, deposit, security_holding, business_share,
-- loan, income_line. Same pattern.

CREATE TABLE extraction_audit (
    decl_id             INTEGER PRIMARY KEY REFERENCES declaration(decl_id),
    fields_extracted    INTEGER NOT NULL,
    fields_failed       INTEGER NOT NULL,
    cmap_glyphs_mapped  INTEGER,
    ocr_regions_used    INTEGER,
    errors_json         TEXT,
    needs_review        INTEGER NOT NULL DEFAULT 0
);

-- Vouliwatch side — mirrors the per-MP, per-year fiscal data from the API.
CREATE TABLE vouli_member (
    slug            TEXT PRIMARY KEY,
    surname         TEXT NOT NULL,
    given_name      TEXT NOT NULL,
    party           TEXT,
    raw_json        TEXT NOT NULL                -- keep the full payload
);

CREATE TABLE vouli_fiscal_year (
    slug            TEXT NOT NULL REFERENCES vouli_member(slug),
    fiscal_year     INTEGER NOT NULL,
    total_wealth    REAL,
    raw_json        TEXT NOT NULL,
    PRIMARY KEY (slug, fiscal_year)
);
-- (Per-asset-class child tables on the Vouliwatch side can wait until
-- we know exactly which fields the metrics layer needs.)

-- Cross-source link
CREATE TABLE mp_link (
    mp_id           INTEGER PRIMARY KEY REFERENCES mp_index(mp_id),
    vouli_slug      TEXT REFERENCES vouli_member(slug),
    confidence      REAL NOT NULL,
    method          TEXT NOT NULL                -- exact_normalized|fuzzy|manual
);
```

Indexing decisions to revisit when query patterns are known — at this size
the defaults are fine for now.

## 8. Validation strategy

- **Fixture tests**: 3–5 PDFs checked into `tests/fixtures/`, with a
  hand-curated expected-output JSON. `pytest` asserts parsed output matches.
  Catches regressions when bounding boxes shift.
- **Cross-source numeric sanity**: where the same MP has 2023 data from the
  API and 2024 data from the PDF, compare structural plausibility
  (#vehicles, #properties, ballpark wealth change). Massive deltas flagged.
- **Coverage report**: per parser run, log
  `extracted / expected` field counts and `needs_review` counts. Stable
  trendline run-over-run = healthy.
- **Manual spot check**: pick 10 random MPs after each parser change, open
  the original PDF in a viewer, compare side-by-side with the DB rows.

## 9. Open risks and decisions to revisit

- **Tesseract Greek accuracy on tiny glyphs**: if per-glyph OCR (B1) is
  unreliable for accented characters (ά vs. α), we fall back harder on
  region-level OCR (B2) which has more context.
- **Index page completeness**: parliament site sometimes paginates or
  splits by parliamentary term. Confirm we have *all* 2024 filers in Phase 0
  before downstream work.
- **MP identity stability**: the `mp_id` in the PDF filename should be stable
  across years. If not, cross-year joins in Phase 4 need a different key
  (likely the Vouliwatch slug as the canonical identity).
- **Vouliwatch coverage gaps**: some MPs from the parliament list may not
  appear in Vouliwatch (ex-MPs from older terms, late additions). Expected
  and tolerated; tracked via the `mp_link` unmatched report.
- **Schema churn**: line-item tables likely gain columns once we see the
  variety of fields filled in across PDFs. Plan for one or two schema
  migrations during Phase 2 — keep them in numbered SQL files
  (`schema_001.sql`, `schema_002.sql`).
- **Backward compatibility with 2023-and-earlier PDFs**: per user decision,
  we are *not* parsing those — the API covers them. If that decision
  reverses, Phase 2's parser will need a second template module since the
  pre-2024 form layout may differ.

## 10. First concrete next steps

1. Create `pyproject.toml`, `.gitignore`, and `docs/setup.md` (env + Tesseract).
2. Initialize git if the user wants one.
3. Implement `src/ingest/scrape_index.py`.
4. Implement `src/ingest/download_pdfs.py`.
5. Pick the two reference PDFs and start annotating bounding boxes for the
   header section in `src/parse/templates/decl_2024.py`.

Stop after each of these and verify before continuing — especially #3 and #4,
where surprises in the index page or the URL pattern are most likely.
