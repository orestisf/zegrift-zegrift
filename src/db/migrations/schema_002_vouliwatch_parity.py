"""
Migration 002: schema parity with Vouliwatch.

Adds columns to existing child tables so the Vouliwatch ingest can write
itemized rows into the same schema the PDF parser uses.

Idempotent: each ALTER is guarded by a PRAGMA table_info() existence check.

Usage:
    python -m src.db.migrations.schema_002_vouliwatch_parity [--db PATH]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).parent.parent.parent.parent / "data" / "db" / "zegrift.sqlite"

# table → list of (column_name, column_definition)
COLUMNS_TO_ADD: dict[str, list[tuple[str, str]]] = {
    "income_line": [
        ("currency", "TEXT"),
        ("partner",  "INTEGER DEFAULT 0"),
    ],
    "vehicle": [
        ("ownership_pct",      "REAL"),
        ("acquisition_method", "TEXT"),
        ("state",              "TEXT"),
        ("partner",            "INTEGER DEFAULT 0"),
    ],
    "deposit": [
        ("beneficiaries", "INTEGER"),
        ("country",       "TEXT"),
        ("currency",      "TEXT"),
        ("partner",       "INTEGER DEFAULT 0"),
    ],
    "real_estate": [
        ("landsize_m2",        "REAL"),
        ("country",            "TEXT"),
        ("rights",             "TEXT"),
        ("acquisition_method", "TEXT"),
        ("swimming_pool",      "REAL"),
        ("currency",           "TEXT"),
        ("partner",            "INTEGER DEFAULT 0"),
    ],
    "security_holding": [
        ("state",    "TEXT"),
        ("currency", "TEXT"),
        ("partner",  "INTEGER DEFAULT 0"),
    ],
    "business_share": [
        ("participation_type",  "TEXT"),
        ("business_type",       "TEXT"),
        ("state",               "TEXT"),
        ("initial_capital_eur", "REAL"),
        ("purchase_value_eur",  "REAL"),
        ("sale_value_eur",      "REAL"),
        ("start_year",          "INTEGER"),
        ("currency",            "TEXT"),
        ("partner",             "INTEGER DEFAULT 0"),
    ],
    "loan": [
        ("start_date", "TEXT"),
        ("end_date",   "TEXT"),
        ("currency",   "TEXT"),
        ("partner",    "INTEGER DEFAULT 0"),
    ],
    "safe_deposit_box": [
        ("beneficiaries", "TEXT"),
        ("partner",       "INTEGER DEFAULT 0"),
    ],
    "real_estate_acquisition": [
        ("partner", "INTEGER DEFAULT 0"),
    ],
}


def existing_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def migrate(db_path: Path) -> int:
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA foreign_keys = ON")
    added = 0
    with con:
        for table, cols in COLUMNS_TO_ADD.items():
            existing = existing_columns(con, table)
            if not existing:
                print(f"  SKIP {table}: table does not exist", file=sys.stderr)
                continue
            for col_name, col_def in cols:
                if col_name in existing:
                    continue
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
                print(f"  + {table}.{col_name} ({col_def})")
                added += 1
    con.close()
    return added


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(DEFAULT_DB))
    args = p.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    added = migrate(db_path)
    print(f"\nMigration 002 complete: {added} columns added")


if __name__ == "__main__":
    main()
