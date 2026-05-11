-- zegrift schema
-- Apply in order; later migrations are schema_002.sql, schema_003.sql, ...

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ─── Acquisition ────────────────────────────────────────────────────────────

-- Roster scraped from the parliament index page
CREATE TABLE IF NOT EXISTS mp_index (
    mp_id           INTEGER PRIMARY KEY,   -- numeric ID from the PDF filename
    surname_gr      TEXT NOT NULL,
    given_name_gr   TEXT NOT NULL,
    surname_lat     TEXT NOT NULL,         -- as in PDF filename (e.g. AKTYPIS)
    given_name_lat  TEXT NOT NULL,
    pdf_url         TEXT NOT NULL,
    scraped_at      TEXT NOT NULL          -- ISO-8601
);

-- One row per downloaded file; re-running ingest is idempotent
CREATE TABLE IF NOT EXISTS pdf_file (
    mp_id           INTEGER NOT NULL REFERENCES mp_index(mp_id),
    fiscal_year     INTEGER NOT NULL,
    path            TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    content_length  INTEGER NOT NULL,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (mp_id, fiscal_year)
);

-- ─── Parsed declarations ────────────────────────────────────────────────────

-- One row per parse pass (keyed by parser_version so re-parses don't overwrite)
CREATE TABLE IF NOT EXISTS declaration (
    decl_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mp_id              INTEGER NOT NULL REFERENCES mp_index(mp_id),
    fiscal_year        INTEGER NOT NULL,
    declaration_serial TEXT,              -- e.g. Σ2298-6181-3803-3101-9260-7
    submitted_at       TEXT,              -- date printed on form
    parser_version     TEXT NOT NULL,
    parsed_at          TEXT NOT NULL,
    UNIQUE (mp_id, fiscal_year, parser_version)
);

-- Real estate / immovable property
CREATE TABLE IF NOT EXISTS real_estate (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decl_id             INTEGER NOT NULL REFERENCES declaration(decl_id),
    row_index           INTEGER NOT NULL,
    kind                TEXT,             -- e.g. "διαμέρισμα", "αγροτεμάχιο"
    share_pct           REAL,
    area_m2             REAL,             -- covered/main area
    landsize_m2         REAL,             -- plot/land area
    location_raw        TEXT,             -- raw bytes from pdfplumber (may be garbled)
    location_decoded    TEXT,             -- after CMap/OCR decode
    country             TEXT,
    acquisition_year    INTEGER,
    value_eur           REAL,             -- declared/objective value
    rights              TEXT,             -- ΠΛΗΡΗΣ ΚΥΡΙΟΤΗΤΑ, ΣΥΓΚΥΡΙΟΤΗΤΑ, etc.
    acquisition_method  TEXT,             -- ΓΟΝΙΚΗ ΠΑΡΟΧΗ, ΑΓΟΡΑ, etc.
    swimming_pool       REAL,
    currency            TEXT,
    partner             INTEGER DEFAULT 0,  -- 0=obligor, 1=spouse
    extraction_method   TEXT NOT NULL,    -- positional|cmap_decoded|region_ocr|failed|vouliwatch
    confidence          REAL
);

-- Vehicles
CREATE TABLE IF NOT EXISTS vehicle (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decl_id             INTEGER NOT NULL REFERENCES declaration(decl_id),
    row_index           INTEGER NOT NULL,
    make                TEXT,             -- vehicle type (ΕΠΙΒΑΤΙΚΟ Ι.Χ., etc.)
    model               TEXT,             -- cc / length
    year                INTEGER,
    value_eur           REAL,
    ownership_pct       REAL,
    acquisition_method  TEXT,             -- howgetit
    state               TEXT,             -- ΑΠΟΚΤΗΣΗ ΣΤΗΝ ΤΡΕΧΟΥΣΑ/ΠΡΟΗΓΟΥΜΕΝΗ ΧΡΗΣΗ
    partner             INTEGER DEFAULT 0,
    extraction_method   TEXT NOT NULL,
    confidence          REAL
);

-- Bank deposits
CREATE TABLE IF NOT EXISTS deposit (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decl_id             INTEGER NOT NULL REFERENCES declaration(decl_id),
    row_index           INTEGER NOT NULL,
    bank                TEXT,
    account_type        TEXT,             -- e.g. savings, current, time
    balance_eur         REAL,
    beneficiaries       INTEGER,          -- number of co-holders
    country             TEXT,
    currency            TEXT,
    partner             INTEGER DEFAULT 0,
    extraction_method   TEXT NOT NULL,
    confidence          REAL
);

-- Securities (shares, bonds, funds, insurance contracts)
-- Maps to Vouliwatch's pmproducts (despite the name, it's their investment-products table)
CREATE TABLE IF NOT EXISTS security_holding (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decl_id             INTEGER NOT NULL REFERENCES declaration(decl_id),
    row_index           INTEGER NOT NULL,
    instrument          TEXT,             -- ΕΙΔΟΣ ΧΡΕΟΓΡΑΦΟΥ (instrument type)
    title               TEXT,             -- ΤΙΤΛΟΣ ΧΡΕΟΓΡΑΦΟΥ
    quantity            REAL,
    acquisition_value_eur REAL,           -- cost_buy
    value_eur           REAL,             -- current valuation (cost)
    sale_value_eur      REAL,             -- cost_sell
    state               TEXT,             -- ΑΠΟΚΤΗΣΗ ΣΕ ΠΡΟΗΓΟΥΜΕΝΗ/ΤΡΕΧΟΥΣΑ ΧΡΗΣΗ
    currency            TEXT,
    partner             INTEGER DEFAULT 0,
    extraction_method   TEXT NOT NULL,
    confidence          REAL
);

-- Safe deposit boxes
CREATE TABLE IF NOT EXISTS safe_deposit_box (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decl_id             INTEGER NOT NULL REFERENCES declaration(decl_id),
    row_index           INTEGER NOT NULL,
    institution         TEXT,
    country             TEXT,
    rental_year         INTEGER,
    beneficiaries       TEXT,             -- co-holders (free text)
    notes               TEXT,
    partner             INTEGER DEFAULT 0,
    extraction_method   TEXT NOT NULL,
    confidence          REAL
);

-- Real estate rights / acquisitions (19-col table — transactions, not descriptions)
CREATE TABLE IF NOT EXISTS real_estate_acquisition (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decl_id             INTEGER NOT NULL REFERENCES declaration(decl_id),
    row_index           INTEGER NOT NULL,
    rights_type         TEXT,             -- e.g. "ΠΛΗΡΗΣ ΚΥΡΙΟΤΗΤΑ 100 %", "ΣΥΓΚΥΡΙΟΤΗΤΑ 33.33 %"
    rights_pct          REAL,             -- extracted numeric % from rights_type
    acquisition_method  TEXT,             -- ΓΟΝΙΚΗ ΠΑΡΟΧΗ, ΑΓΟΡΑ, etc.
    price_paid_eur      REAL,
    objective_value_eur REAL,
    received_price_eur  REAL,
    partner             INTEGER DEFAULT 0,
    extraction_method   TEXT NOT NULL,
    confidence          REAL
);

-- Business participations / company shares
CREATE TABLE IF NOT EXISTS business_share (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decl_id             INTEGER NOT NULL REFERENCES declaration(decl_id),
    row_index           INTEGER NOT NULL,
    company             TEXT,
    share_pct           REAL,
    value_eur           REAL,             -- book_value / cost
    participation_type  TEXT,             -- ΕΙΔΟΣ ΣΥΜΜΕΤΟΧΗΣ
    business_type       TEXT,             -- company_type (industry/sector)
    state               TEXT,             -- ΑΠΟΚΤΗΣΗ ΣΕ ΠΡΟΗΓΟΥΜΕΝΗ ΧΡΗΣΗ, etc.
    initial_capital_eur REAL,
    purchase_value_eur  REAL,             -- buy_value
    sale_value_eur      REAL,             -- sell_value
    start_year          INTEGER,
    currency            TEXT,
    partner             INTEGER DEFAULT 0,
    extraction_method   TEXT NOT NULL,
    confidence          REAL
);

-- Loans (liabilities)
CREATE TABLE IF NOT EXISTS loan (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decl_id             INTEGER NOT NULL REFERENCES declaration(decl_id),
    row_index           INTEGER NOT NULL,
    lender              TEXT,
    kind                TEXT,             -- mortgage, consumer, etc.
    original_amount_eur REAL,
    outstanding_eur     REAL,
    start_date          TEXT,             -- ISO date or year
    end_date            TEXT,
    currency            TEXT,
    partner             INTEGER DEFAULT 0,
    extraction_method   TEXT NOT NULL,
    confidence          REAL
);

-- Income lines
CREATE TABLE IF NOT EXISTS income_line (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decl_id             INTEGER NOT NULL REFERENCES declaration(decl_id),
    row_index           INTEGER NOT NULL,
    source              TEXT,
    kind                TEXT,             -- salary, pension, rental, business, etc.
    amount_eur          REAL,
    currency            TEXT,
    partner             INTEGER DEFAULT 0,
    extraction_method   TEXT NOT NULL,
    confidence          REAL
);

-- Per-declaration parse quality audit
CREATE TABLE IF NOT EXISTS extraction_audit (
    decl_id             INTEGER PRIMARY KEY REFERENCES declaration(decl_id),
    fields_extracted    INTEGER NOT NULL,
    fields_failed       INTEGER NOT NULL,
    cmap_glyphs_mapped  INTEGER,
    ocr_regions_used    INTEGER,
    errors_json         TEXT,             -- JSON array of error strings
    needs_review        INTEGER NOT NULL DEFAULT 0
);

-- ─── Vouliwatch API data (2015-2023) ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS vouli_member (
    slug            TEXT PRIMARY KEY,
    surname         TEXT NOT NULL,
    given_name      TEXT NOT NULL,
    party           TEXT,
    raw_json        TEXT NOT NULL         -- full API response payload
);

CREATE TABLE IF NOT EXISTS vouli_fiscal_year (
    slug            TEXT NOT NULL REFERENCES vouli_member(slug),
    fiscal_year     INTEGER NOT NULL,
    total_wealth    REAL,
    raw_json        TEXT NOT NULL,
    PRIMARY KEY (slug, fiscal_year)
);

-- ─── Cross-source identity link ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS mp_link (
    mp_id           INTEGER PRIMARY KEY REFERENCES mp_index(mp_id),
    vouli_slug      TEXT REFERENCES vouli_member(slug),
    confidence      REAL NOT NULL,
    method          TEXT NOT NULL         -- exact_normalized|fuzzy|manual
);
