"""
Migration 003: declarant role + spouse identity on the declaration table.

Adds six columns to `declaration` so we can distinguish PDFs filed by the
MP themselves vs PDFs filed by their spouse / cohabitation partner, and
preserve the household-linking metadata printed on page 1 of the form.

Idempotent: each ALTER is guarded by a PRAGMA table_info() existence check.

Usage:
    python -m src.db.migrations.schema_003_declarant_role [--db PATH]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).parent.parent.parent.parent / "data" / "db" / "zegrift.sqlite"

# table -> list of (column_name, column_definition)
COLUMNS_TO_ADD: dict[str, list[tuple[str, str]]] = {
    "declaration": [
        ("declarant_role",         "TEXT"),
        ("declarant_role_raw",     "TEXT"),
        ("spouse_surname",         "TEXT"),
        ("spouse_given_name",      "TEXT"),
        ("obligation_period_from", "TEXT"),
        ("obligation_period_to",   "TEXT"),
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
    print(f"\nMigration 003 complete: {added} columns added")


if __name__ == "__main__":
    main()
