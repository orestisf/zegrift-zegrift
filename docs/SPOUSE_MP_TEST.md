# Spouse MP Declaration Handling - Test & Validation Report

## Changes Summary

Parser v0.1.3 → 0.1.4 brings three related changes for handling declarations where both spouses are MPs:

### 1. **Renamed Role Date Columns** (`schema.sql`, `parse_pdf.py`, `load.py`)
- `obligation_period_from` → `role_acquisition_date` (ΗΜΕΡΟΜΗΝΙΑ ΑΠΟΚΤΗΣΗΣ ΙΔΙΟΤΗΤΑΣ)
- `obligation_period_to` → `role_loss_date` (ΗΜ. ΑΠΩΛΕΙΑΣ ΙΔΙΟΤΗΤΑΣ)
- Old names were misleading; new names match the Greek form labels printed on page 1

### 2. **Added Spouse MP Foreign Key** (`schema.sql`)
```sql
spouse_mp_id INTEGER REFERENCES mp_index(mp_id)  -- set when spouse is also an MP
```

### 3. **New Spouse Resolution Module** (`src/link/spouse_mp.py`)
```python
resolve_spouse_mp_ids(con, fiscal_year=None) -> int
```
- Scans declarations with non-null `spouse_surname`/`spouse_given_name`
- Normalises names using Greek→Latin transliteration
- Looks up `mp_index` for matching MPs
- Writes `spouse_mp_id` when found
- Guards against self-links and ambiguous surname-only matches

### 4. **Schema Migration** (`src/db/migrations/schema_004_spouse_mp.py`)
- Idempotent column rename (requires SQLite 3.25+)
- Adds `spouse_mp_id` FK column
- Runnable: `python -m src.db.migrations.schema_004_spouse_mp --db data/db/zegrift.sqlite`

---

## Validation Results

### ✅ Unit Tests (12/12 passing)
Run with: `python -m pytest tests/test_spouse_mp.py -v`

- ✓ Exact (surname, given) name matching
- ✓ Latin-name normalisation (TZANAKOPOYLOS → ΤΖΑΝΑΚΟΠΟΥΛΟΣ)
- ✓ Surname-only match when unique
- ✓ Ambiguous surname returns None
- ✓ Unknown name returns None
- ✓ **Bidirectional linking** (both spouses are MPs)
  - Achtsioglou (4557979) ↔ Tzanakopoulos (4557310)
  - Each declaration's `spouse_mp_id` points to the other
- ✓ Non-MP spouse leaves `spouse_mp_id` NULL
- ✓ Missing spouse info skips resolution
- ✓ Self-link guard (corrupted data protection)
- ✓ Fiscal-year filter (restrict to specific year)
- ✓ Schema shape (new columns present, old absent)

### ✅ Integration Test (End-to-End)
Run with: `cd /tmp && python test_pipeline.py`

**Setup:**
```
Test DB with 2 MPs (real-world spouse pair):
  - 4557979: ACHTSIOGLOY EYTYCHIA (Ευτυχία Αχτσιόγλου)
  - 4557310: TZANAKOPOYLOS DIMITRIOS (Δημήτριος Τζανακόπουλος)
```

**Flow:**
1. Insert 2 mock declarations with spouse names
2. Run `resolve_spouse_mp_ids(con)`
3. Verify bidirectional `spouse_mp_id` linkage
4. Verify `role_acquisition_date` / `role_loss_date` persisted

**Result:**
```
✓ Inserted 2 mock declarations (Achtsioglou & Tzanakopoulos)
✓ Ran resolve_spouse_mp_ids: 2 declarations updated
✓ Bidirectional spouse linking verified:
    Achtsioglou (4557979) → spouse_mp_id = 4557310
    Tzanakopoulos (4557310) → spouse_mp_id = 4557979
✓ Role dates persisted:
    mp_id=4557310: acquired=20/02/2015, lost=None
    mp_id=4557979: acquired=15/01/2020, lost=None
✅ Full spouse-MP pipeline test PASSED
```

---

## Testing with Real PDFs (10 Declarations)

### Prerequisites
```bash
# Install dependencies
pip install -e ".[dev]"

# Create test database
python -m src.db.migrations.schema_004_spouse_mp --db /tmp/test.sqlite
```

### Pipeline Commands

**1. Scrape Parliament Index**
```bash
python -m src.ingest.scrape_index --db /tmp/test.sqlite --out-csv /tmp/index.csv
# Output: ~300 MPs from parliament website
```

**2. Download 10 PDFs**
```bash
python -m src.ingest.download_pdfs --db /tmp/test.sqlite --limit 10 --out-dir /tmp/pdfs/
# Output: ~50-100 MB (depends on PDF sizes)
```

**3. Parse PDFs to JSON**
```bash
for pdf in /tmp/pdfs/*.pdf; do
  python -m src.parse.parse_pdf "$pdf" > "${pdf%.pdf}.json"
done
```

**4. Load Parsed Data into DB**
```bash
python -m src.db.load --db /tmp/test.sqlite --all-parsed /tmp/parsed/
# Output: decl_id values for each loaded declaration
```

**5. Resolve Spouse MP IDs**
```bash
python -m src.link.spouse_mp --db /tmp/test.sqlite
# Output: number of declarations updated
```

**6. Verify Results**
```bash
sqlite3 /tmp/test.sqlite <<EOF
-- Show all declarations with spouse info
SELECT 
  d.decl_id, d.mp_id, 
  m.surname_lat, m.given_name_lat,
  d.declarant_role, 
  d.spouse_surname, d.spouse_given_name, 
  d.spouse_mp_id,
  d.role_acquisition_date, d.role_loss_date
FROM declaration d
LEFT JOIN mp_index m ON d.mp_id = m.mp_id
ORDER BY d.mp_id;

-- Count spouse-MP pairs
SELECT COUNT(*) as spouse_mp_count 
FROM declaration WHERE spouse_mp_id IS NOT NULL;
EOF
```

---

## Known Limitations & Future Work

1. **PDF Download Dependency**: The parliament website (hellenicparliament.gr) serves PDFs with access restrictions that may require authentication or rate-limiting workarounds.

2. **Partial Declarations**: Some MPs filed only spouse declarations (declarant_role='spouse'), not their own. These still have a `spouse_surname` field pointing to the MP. The code handles this.

3. **Role Date Accuracy**: `role_acquisition_date` and `role_loss_date` come directly from the PDF form's page-1 ΙΔΙΟΤΗΤΑ table. If the form wasn't filled in, these will be empty/NULL.

4. **Name Normalisation**: The transliterator handles accented Greek, but rare patronymic formats or multi-part surnames may cause false negatives. See `src/link/match.py:normalize_name()`.

5. **Spouse Self-Link Guard**: Code explicitly prevents `spouse_mp_id == mp_id` (data corruption guard), but the parliament data should never produce this.

---

## File Changes Checklist

- [x] `src/db/schema.sql` — column renames, new FK
- [x] `src/parse/parse_pdf.py` — v0.1.4, ParsedDeclaration fields, _parse_header
- [x] `src/db/load.py` — SQL INSERT/UPDATE, _dict_to_declaration
- [x] `src/db/migrations/schema_004_spouse_mp.py` — new migration
- [x] `src/link/spouse_mp.py` — new module with resolver function
- [x] `tests/test_spouse_mp.py` — 12 unit + integration tests
- [x] Branch pushed to `origin/claude/handle-spouse-mp-declarations-CuGJH`

---

## Next Steps

1. **Real-World Test**: Run the 10-PDF pipeline above (when network access is available)
2. **Vouliwatch Linking**: After loading, run `python -m src.link.match --db ...` to cross-link with Vouliwatch API data (2015–2023)
3. **Data Export**: Query household wealth when both spouses are MPs without double-counting
4. **Documentation**: Update `/docs/data-model.md` with spouse_mp_id usage examples
