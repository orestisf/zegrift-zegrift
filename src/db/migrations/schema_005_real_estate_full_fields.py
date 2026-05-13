"""
Migration 005: Full real estate field coverage.

Adds all columns parsed from the PDF that were previously missing:

real_estate table:
  + owner_type          TEXT  -- ΚΑΤΟΧΟΣ (ΥΠΟΧΡΕΟΣ / ΣΥΖΥΓΟΣ)
  + status              TEXT  -- ΚΑΤΑΣΤΑΣΗ Ή ΜΕΤΑΒΟΛΗ
  + region              TEXT  -- ΠΕΡΙΦΕΡΕΙΑ
  + prefecture          TEXT  -- ΝΟΜΟΣ
  + floor               TEXT  -- ΟΡΟΦΟΣ ΚΤΙΣΜΑΤΟΣ
  + property_condition  TEXT  -- ΚΑΤΑΣΤΑΣΗ ΑΚΙΝΗΤΟΥ
  + build_year          INTEGER  -- ΕΤΟΣ ΚΑΤΑΣΚΕΥΗΣ
  + other_area_m2       REAL  -- ΕΠΙΦΑΝΕΙΑ ΒΟΗΘΗΤΙΚΩΝ ΧΩΡΩΝ
  + transfer_year       INTEGER  -- ΕΤΟΣ ΜΕΤΑΒΙΒΑΣΗΣ
  + energy_production_kw REAL -- ΙΣΧΥΣ ΜΟΝΑΔΑΣ ΠΑΡΑΓΩΓΗΣ ΕΝΕΡΓΕΙΑΣ
  Also fixes: landsize_m2 now stores ΕΠΙΦΑΝΕΙΑ ΕΔΑΦΟΥΣ (plot area),
              area_m2 now stores ΕΠΙΦΑΝΕΙΑ ΚΥΡΙΩΝ ΧΩΡΩΝ (main floor area).

real_estate_acquisition table:
  + heir_name                   TEXT  -- ΕΠΩΝΥΜΟ/ΟΝΟΜΑ ΚΛΗΡΟΝΟΜΟΥΜΕΝΟΥ
  + heir_capacity               TEXT  -- ΙΔΙΟΤΗΤΑ ΚΛΗΡΟΝΟΜΟΥ
  + heir_acquisition_method     TEXT  -- ΤΡΟΠΟΣ ΑΠΟΚΤΗΣΗΣ ΙΔΙΟΤΗΤΑΣ ΚΛΗΡΟΝΟΜΟΥ
  + money_sources               TEXT  -- ΠΗΓΕΣ ΠΡΟΕΛΕΥΣΗΣ ΧΡΗΜΑΤΩΝ
  + acquisition_contract_number TEXT  -- ΑΡΙΘΜΟΣ ΣΥΜΒΟΛΑΙΟΥ ΑΠΟΚΤΗΣΗΣ
  + currency                    TEXT  -- ΝΟΜΙΣΜΑ
  + disposal_contract_number    TEXT  -- ΑΡΙΘΜΟΣ ΣΥΜΒΟΛΑΙΟΥ ΕΚΠΟΙΗΣΗΣ/ΜΕΤΑΒΟΛΗΣ
  + disposal_objective_value_eur REAL -- ΑΝΤΙΚΕΙΜΕΝΙΚΗ ΑΞΙΑ ΕΚΠΟΙΗΣΗΣ/ΜΕΤΑΒΟΛΗΣ
  + kaek                        TEXT  -- ΚΩΔ. ΑΡ. ΕΘΝΙΚΟΥ ΚΤΗΜΑΤΟΛΟΓΙΟΥ (Κ.Α.Ε.Κ.)
  + notes                       TEXT  -- ΠΑΡΑΤΗΡΗΣΕΙΣ

Usage:
    python -m src.db.migrations.schema_005_real_estate_full_fields [--db PATH]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).parent.parent.parent.parent / "data" / "db" / "zegrift.sqlite"


def existing_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def migrate(db_path: Path) -> int:
    con = sqlite3.connect(str(db_path))
    changed = 0

    with con:
        # ── real_estate ───────────────────────────────────────────────────────
        re_cols = existing_columns(con, "real_estate")

        re_additions = [
            ("owner_type",           "TEXT"),
            ("status",               "TEXT"),
            ("region",               "TEXT"),
            ("prefecture",           "TEXT"),
            ("floor",                "TEXT"),
            ("property_condition",   "TEXT"),
            ("build_year",           "INTEGER"),
            ("other_area_m2",        "REAL"),
            ("transfer_year",        "INTEGER"),
            ("energy_production_kw", "REAL"),
        ]
        for col, typ in re_additions:
            if col not in re_cols:
                con.execute(f"ALTER TABLE real_estate ADD COLUMN {col} {typ}")
                print(f"  + real_estate.{col} ({typ})")
                changed += 1

        # ── real_estate_acquisition ───────────────────────────────────────────
        rea_cols = existing_columns(con, "real_estate_acquisition")

        rea_additions = [
            ("heir_name",                    "TEXT"),
            ("heir_capacity",                "TEXT"),
            ("heir_acquisition_method",      "TEXT"),
            ("money_sources",                "TEXT"),
            ("acquisition_contract_number",  "TEXT"),
            ("currency",                     "TEXT"),
            ("disposal_contract_number",     "TEXT"),
            ("disposal_objective_value_eur", "REAL"),
            ("kaek",                         "TEXT"),
            ("notes",                        "TEXT"),
        ]
        for col, typ in rea_additions:
            if col not in rea_cols:
                con.execute(f"ALTER TABLE real_estate_acquisition ADD COLUMN {col} {typ}")
                print(f"  + real_estate_acquisition.{col} ({typ})")
                changed += 1

    con.close()
    return changed


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(DEFAULT_DB))
    args = p.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    n = migrate(db_path)
    print(f"\nMigration 005 complete: {n} change(s) applied")


if __name__ == "__main__":
    main()
