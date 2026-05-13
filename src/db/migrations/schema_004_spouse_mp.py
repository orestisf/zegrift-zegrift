"""
Migration 004: spouse_mp_id FK + rename obligation_period columns.

Changes to `declaration`:
  - Renames obligation_period_from → role_acquisition_date
    (= ΗΜΕΡΟΜΗΝΙΑ ΑΠΟΚΤΗΣΗΣ ΙΔΙΟΤΗΤΑΣ printed on page 1)
  - Renames obligation_period_to → role_loss_date
    (= ΗΜ. ΑΠΩΛΕΙΑΣ ΙΔΙΟΤΗΤΑΣ printed on page 1; NULL when still serving)
  - Adds spouse_mp_id INTEGER REFERENCES mp_index(mp_id)
    (populated by src/link/spouse_mp.py when the spouse is also an MP)

SQLite supports RENAME COLUMN since 3.25.0 (2018-09-15).

Usage:
    python -m src.db.migrations.schema_004_spouse_mp [--db PATH]
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
    con.execute("PRAGMA foreign_keys = OFF")  # required while renaming columns
    changed = 0
    with con:
        cols = existing_columns(con, "declaration")
        if not cols:
            print("  SKIP declaration: table does not exist", file=sys.stderr)
            con.close()
            return 0

        # Rename obligation_period_from → role_acquisition_date
        if "obligation_period_from" in cols and "role_acquisition_date" not in cols:
            con.execute(
                "ALTER TABLE declaration RENAME COLUMN obligation_period_from TO role_acquisition_date"
            )
            print("  renamed declaration.obligation_period_from → role_acquisition_date")
            changed += 1

        # Rename obligation_period_to → role_loss_date
        if "obligation_period_to" in cols and "role_loss_date" not in cols:
            con.execute(
                "ALTER TABLE declaration RENAME COLUMN obligation_period_to TO role_loss_date"
            )
            print("  renamed declaration.obligation_period_to → role_loss_date")
            changed += 1

        # Add spouse_mp_id (nullable FK)
        cols = existing_columns(con, "declaration")  # re-fetch after renames
        if "spouse_mp_id" not in cols:
            con.execute(
                "ALTER TABLE declaration ADD COLUMN spouse_mp_id INTEGER REFERENCES mp_index(mp_id)"
            )
            print("  + declaration.spouse_mp_id (INTEGER REFERENCES mp_index(mp_id))")
            changed += 1

    con.execute("PRAGMA foreign_keys = ON")
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
    print(f"\nMigration 004 complete: {n} change(s) applied")


if __name__ == "__main__":
    main()
