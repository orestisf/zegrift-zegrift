# Data model

SQLite, file `data/db/zegrift.sqlite`. Schema source: [`src/db/schema.sql`](../src/db/schema.sql).

## Source-merge convention

Both the parliament-PDF parser and the Vouliwatch API ingester write into the
same tables. The `declaration` row's `parser_version` column tells you where
the data came from:

| `parser_version` | Source | Fiscal years |
|---|---|---|
| `0.1.3` (current) | Parliament PDF parser | 2024 |
| `vouliwatch`   | Vouliwatch API ingester | 2015–2023 |

The `(mp_id, fiscal_year, parser_version)` triple is unique, so the same MP
can have:
- Multiple Vouliwatch declarations (one per year 2019–2023)
- One PDF-parsed declaration (2024)
- Multiple PDF-parsed declarations if the parser version is bumped (older
  ones survive as `0.1.0`, `0.1.1`, etc. for comparison)

Child tables (`real_estate`, `deposit`, `loan`, etc.) reference `decl_id`,
so cross-source queries are plain JOINs.

---

## Tables

### Acquisition layer

#### `mp_index`
Parliament roster, scraped from the index page.

| Column | Type | Notes |
|---|---|---|
| `mp_id`            | INTEGER PK | Numeric ID from PDF filename |
| `surname_gr`       | TEXT       | Greek |
| `given_name_gr`    | TEXT       | Greek |
| `surname_lat`      | TEXT       | Latin (as in PDF filename) |
| `given_name_lat`   | TEXT       | Latin |
| `pdf_url`          | TEXT       | Direct download URL |
| `scraped_at`       | TEXT       | ISO-8601 |

#### `pdf_file`
One row per downloaded file (idempotent on (mp_id, fiscal_year)).

| Column | Type | Notes |
|---|---|---|
| `mp_id`           | INTEGER FK → mp_index |
| `fiscal_year`     | INTEGER |
| `path`            | TEXT | Local file path |
| `sha256`          | TEXT |
| `content_length`  | INTEGER |
| `fetched_at`      | TEXT | ISO-8601 |

---

### Declaration core

#### `declaration`
One row per parse pass / data-source-year.

| Column | Type | Notes |
|---|---|---|
| `decl_id`                | INTEGER PK AUTOINCREMENT |
| `mp_id`                  | INTEGER FK → mp_index |
| `fiscal_year`            | INTEGER |
| `declaration_serial`     | TEXT, nullable | PDF: `Σ2298-…`; Vouliwatch: `vouliwatch:<id>` |
| `submitted_at`           | TEXT, nullable | Only available from PDFs |
| `parser_version`         | TEXT | See source-merge table above |
| `parsed_at`              | TEXT | ISO-8601 |
| `declarant_role`         | TEXT, nullable | `mp` \| `minister` \| `spouse` \| `other` \| NULL — see "Declarant role" below |
| `declarant_role_raw`     | TEXT, nullable | Decoded Greek role text, kept for audit |
| `spouse_surname`         | TEXT, nullable | The other half of the household (from the page-1 "spouse box") |
| `spouse_given_name`      | TEXT, nullable | |
| `obligation_period_from` | TEXT, nullable | Start of the obligation window (`dd/mm/yyyy` as printed) |
| `obligation_period_to`   | TEXT, nullable | End — may be NULL if the obligation is still open |

Unique on `(mp_id, fiscal_year, parser_version)`.

##### Declarant role

A single household (MP + spouse) usually produces **two** declaration PDFs:
one filed by the MP themselves and one filed by their spouse / cohabitation
partner — both listed under the parliament's "pothen-esches" index. The
`mp_id` column always points at *the person whose name is in the PDF
filename* (i.e. the obligor of that particular declaration). `declarant_role`
tells you which of the two roles that person plays:

| `declarant_role` | Page-1 ΙΔΙΟΤΗΤΑ text contains | Means |
|---|---|---|
| `mp`       | `ΒΟΥΛΕΥΤ…` | This declaration was filed by the MP themselves |
| `minister` | `ΥΠΟΥΡΓ…`  | Filed by a minister (may or may not also be an MP) |
| `spouse`   | `ΣΥΖΥΓ…` or `ΣΣΣ…` | Filed by the MP's spouse / registered cohabitation partner; the MP's name is in `spouse_surname` / `spouse_given_name` |
| `other`    | anything else | Mayor, public-organisation president, etc. — kept for completeness |
| `NULL`     | (could not decode page 1) | Rare; declaration is flagged `needs_review = 1` |

Vouliwatch declarations are always `declarant_role = 'mp'` (the API only
covers MPs, never spouses).

Spouse linkage is symmetric: an MP's PDF carries their spouse's name in
`spouse_surname`/`spouse_given_name`, and the spouse's PDF carries the
MP's name in those same columns. Joining on `(surname, given_name)` after
normalization gives you household pairs.

---

### Itemized child tables

All child tables share a common shape:
- `id` PK, `decl_id` FK → declaration
- `row_index` — position within the source list (for traceability)
- `extraction_method` — `positional` | `cmap_decoded` | `region_ocr` | `vouliwatch` | `failed`
- `confidence` — 0..1 (parser confidence; always `1.0` for Vouliwatch)
- `partner` — 0 = obligor / MP themselves, 1 = spouse

Section-specific columns below.

#### `income_line`
| Column | Type | Source | Notes |
|---|---|---|---|
| `source`     | TEXT | both | Income category / employer |
| `kind`       | TEXT | (reserved) | Future: salary / pension / rental / business |
| `amount_eur` | REAL | both | |
| `currency`   | TEXT | Vouliwatch | |

#### `real_estate`
| Column | Type | Source | Notes |
|---|---|---|---|
| `kind`               | TEXT | both | e.g. ΔΙΑΜΕΡΙΣΜΑ, ΟΙΚΟΠΕΔΟ |
| `share_pct`          | REAL | Vouliwatch | |
| `area_m2`            | REAL | both | Covered / main area |
| `landsize_m2`        | REAL | Vouliwatch | Plot / land area |
| `location_raw`       | TEXT | PDF | Pre-decode |
| `location_decoded`   | TEXT | PDF | Post-decode |
| `country`            | TEXT | Vouliwatch | Full address chain |
| `acquisition_year`   | INTEGER | both | |
| `value_eur`          | REAL | both | Objective/declared value |
| `rights`             | TEXT | Vouliwatch | ΠΛΗΡΗΣ ΚΥΡΙΟΤΗΤΑ, ΣΥΓΚΥΡΙΟΤΗΤΑ, … |
| `acquisition_method` | TEXT | Vouliwatch | ΑΓΟΡΑ, ΓΟΝΙΚΗ ΠΑΡΟΧΗ, … |
| `swimming_pool`      | REAL | Vouliwatch | |
| `currency`           | TEXT | Vouliwatch | |

#### `real_estate_acquisition`
The 19-col "real estate rights / acquisitions" table from the 2024 PDFs —
captures transaction details (price paid, objective value at acquisition,
received price). Vouliwatch doesn't expose this granularity separately.

| Column | Type | Notes |
|---|---|---|
| `rights_type`         | TEXT | Raw, e.g. "ΠΛΗΡΗΣ ΚΥΡΙΟΤΗΤΑ 100 %" |
| `rights_pct`          | REAL | Extracted % |
| `acquisition_method`  | TEXT | |
| `price_paid_eur`      | REAL | |
| `objective_value_eur` | REAL | |
| `received_price_eur`  | REAL | |

#### `vehicle`
| Column | Type | Source | Notes |
|---|---|---|---|
| `make`               | TEXT | both | Vehicle type (e.g. ΕΠΙΒΑΤΙΚΟ Ι.Χ.) |
| `model`              | TEXT | both | cc / boat length |
| `year`               | INTEGER | both | Acquisition year |
| `value_eur`          | REAL | PDF | Purchase price |
| `ownership_pct`      | REAL | both | |
| `acquisition_method` | TEXT | Vouliwatch | |
| `state`              | TEXT | Vouliwatch | New this year vs prior |

#### `deposit`
| Column | Type | Source | Notes |
|---|---|---|---|
| `bank`           | TEXT | both | |
| `account_type`   | TEXT | both | savings, current, time, etc. |
| `balance_eur`    | REAL | both | |
| `beneficiaries`  | INTEGER | Vouliwatch | Number of co-holders |
| `country`        | TEXT | Vouliwatch | |
| `currency`       | TEXT | Vouliwatch | |

#### `security_holding`
Maps to Vouliwatch's `pmproducts` (their securities/investment-products table
despite the misleading name).

| Column | Type | Source | Notes |
|---|---|---|---|
| `instrument`            | TEXT | both | ΕΙΔΟΣ ΧΡΕΟΓΡΑΦΟΥ |
| `title`                 | TEXT | both | ΤΙΤΛΟΣ |
| `quantity`              | REAL | both | |
| `acquisition_value_eur` | REAL | both | cost_buy |
| `value_eur`             | REAL | both | Current valuation |
| `sale_value_eur`        | REAL | both | cost_sell |
| `state`                 | TEXT | Vouliwatch | |
| `currency`              | TEXT | Vouliwatch | |

#### `business_share`
| Column | Type | Source | Notes |
|---|---|---|---|
| `company`              | TEXT | both | |
| `share_pct`            | REAL | PDF | |
| `value_eur`            | REAL | both | Book value at year-end |
| `participation_type`   | TEXT | Vouliwatch | e.g. ΕΤΑΙΡΟΣ ΣΕ Ι.Κ.Ε. |
| `business_type`        | TEXT | Vouliwatch | Industry / sector |
| `state`                | TEXT | Vouliwatch | |
| `initial_capital_eur`  | REAL | Vouliwatch | |
| `purchase_value_eur`   | REAL | Vouliwatch | buy_value |
| `sale_value_eur`       | REAL | Vouliwatch | sell_value |
| `start_year`           | INTEGER | both | |
| `currency`             | TEXT | Vouliwatch | |

#### `loan`
| Column | Type | Source | Notes |
|---|---|---|---|
| `lender`              | TEXT | both | Bank / counterparty |
| `kind`                | TEXT | PDF | mortgage, consumer, etc. |
| `original_amount_eur` | REAL | both | |
| `outstanding_eur`     | REAL | both | |
| `start_date`          | TEXT | both | ISO date or year |
| `end_date`            | TEXT | both | |
| `currency`            | TEXT | Vouliwatch | |

#### `safe_deposit_box`
| Column | Type | Source | Notes |
|---|---|---|---|
| `institution`    | TEXT    | both | Bank |
| `country`        | TEXT    | both | |
| `rental_year`    | INTEGER | both | |
| `beneficiaries`  | TEXT    | Vouliwatch | Co-holders (free text) |
| `notes`          | TEXT    | PDF | |

---

### Quality & audit

#### `extraction_audit`
One row per PDF declaration; null for Vouliwatch decls.

| Column | Notes |
|---|---|
| `decl_id`            | PK FK → declaration |
| `fields_extracted`   | Count |
| `fields_failed`      | Count |
| `cmap_glyphs_mapped` | If CMap reconstruction ran |
| `ocr_regions_used`   | If region-OCR fallback ran |
| `errors_json`        | JSON array of error strings |
| `needs_review`       | 0 / 1 |

---

### Vouliwatch raw layer

These hold the source-of-truth API data; the itemized rows are derived from
the cached responses during Phase 2.

#### `vouli_member`
| Column | Notes |
|---|---|
| `slug`         | PK |
| `surname`      | Greek |
| `given_name`   | Greek |
| `party`        | Most recent observed |
| `raw_json`     | Full `/home` member entry |

#### `vouli_fiscal_year`
Aggregated totals per (slug, year). Useful as a cross-check against summing
our child-table rows.

| Column | Notes |
|---|---|
| `slug`         | FK → vouli_member |
| `fiscal_year`  | |
| `total_wealth` | Sum of revenue + deposits + stocks + companies |
| `raw_json`     | Full fiscal-year API record |

Unique on `(slug, fiscal_year)`.

---

### Cross-source link

#### `mp_link`
| Column | Notes |
|---|---|
| `mp_id`        | PK FK → mp_index |
| `vouli_slug`   | FK → vouli_member |
| `confidence`   | 0..1 |
| `method`       | `exact_normalized` \| `exact_surname` \| `fuzzy` \| `manual` |

One mp_id maps to at most one slug. A slug can serve multiple mp_ids (e.g.
when the parliament index has both a current and an alternate entry for the
same MP).

---

## Example queries

**All real estate for one MP, every year, every source:**

```sql
SELECT d.fiscal_year, d.parser_version,
       re.kind, re.country, re.area_m2, re.value_eur, re.share_pct
FROM real_estate re
JOIN declaration d ON re.decl_id = d.decl_id
WHERE d.mp_id = 4568095
ORDER BY d.fiscal_year;
```

**Total declared wealth per MP per year (from Vouliwatch + 2024 PDFs):**

```sql
SELECT d.mp_id, d.fiscal_year,
       COALESCE(SUM(re.value_eur), 0) AS real_estate,
       COALESCE(SUM(dep.balance_eur), 0) AS deposits,
       COALESCE(SUM(sh.value_eur), 0) AS securities,
       COALESCE(SUM(bs.value_eur), 0) AS business_shares
FROM declaration d
LEFT JOIN real_estate    re  ON re.decl_id  = d.decl_id
LEFT JOIN deposit        dep ON dep.decl_id = d.decl_id
LEFT JOIN security_holding sh ON sh.decl_id = d.decl_id
LEFT JOIN business_share bs  ON bs.decl_id = d.decl_id
GROUP BY d.mp_id, d.fiscal_year, d.parser_version;
```

**Cross-source consistency check** (does our parsed 2024 deposit total roughly
match a linear extrapolation from the 2022→2023 Vouliwatch trend?):

```sql
WITH per_year AS (
  SELECT d.mp_id, d.fiscal_year, SUM(dep.balance_eur) AS deposits
  FROM declaration d
  JOIN deposit dep ON dep.decl_id = d.decl_id
  GROUP BY d.mp_id, d.fiscal_year
)
SELECT * FROM per_year
WHERE mp_id = 4568095
ORDER BY fiscal_year;
```

**MP + spouse declarations for one household (2024 PDFs):**

```sql
-- The MP's own declaration plus their spouse's separately-filed declaration
SELECT d.decl_id, d.mp_id, mi.surname_lat, mi.given_name_lat,
       d.declarant_role, d.spouse_surname, d.spouse_given_name
FROM declaration d
JOIN mp_index mi ON mi.mp_id = d.mp_id
WHERE d.fiscal_year = 2024
  AND d.parser_version LIKE '0.1.%'
  AND (
       (mi.surname_gr = 'ΚΑΣΣΕΛΑΚΗΣ')                              -- the MP himself
    OR (d.spouse_surname = 'ΚΑΣΣΕΛΑΚΗΣ' AND d.declarant_role = 'spouse')  -- his spouse
  );
```

**Pair every MP with their separately-filing spouse, if any:**

```sql
SELECT mp.mp_id   AS mp_decl_id,
       mp.mp_id   AS mp_mp_id,
       sp.mp_id   AS spouse_mp_id,
       mp.spouse_surname AS spouse_name
FROM declaration mp
LEFT JOIN declaration sp
  ON sp.fiscal_year = mp.fiscal_year
 AND sp.declarant_role = 'spouse'
 AND sp.spouse_surname = (
       SELECT surname_gr FROM mp_index WHERE mp_id = mp.mp_id
     )
WHERE mp.declarant_role = 'mp'
  AND mp.fiscal_year = 2024
  AND mp.parser_version LIKE '0.1.%';
```

**Find MPs whose wealth more than doubled over the Vouliwatch window:**

```sql
SELECT v_old.slug,
       v_old.total_wealth AS wealth_2019,
       v_new.total_wealth AS wealth_2023,
       v_new.total_wealth / v_old.total_wealth AS multiple
FROM vouli_fiscal_year v_old
JOIN vouli_fiscal_year v_new USING (slug)
WHERE v_old.fiscal_year = 2019 AND v_new.fiscal_year = 2023
  AND v_old.total_wealth > 0
  AND v_new.total_wealth > 2 * v_old.total_wealth
ORDER BY multiple DESC
LIMIT 20;
```

---

## Migrations

Schema-modifying changes go under [`src/db/migrations/`](../src/db/migrations/)
as numbered Python scripts. Each is idempotent (guards each ALTER with a
`PRAGMA table_info` existence check).

Applied so far:
- `schema_002_vouliwatch_parity.py` — added 36 columns to existing child
  tables so Vouliwatch's richer itemized records map cleanly onto the same
  schema.
- `schema_003_declarant_role.py` — added 6 columns to `declaration` so we
  can tell apart PDFs filed by the MP themselves vs PDFs filed by the MP's
  spouse, and preserve the household-linking metadata (spouse name +
  obligation period) printed on page 1.
