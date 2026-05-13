"""
Resolve spouse_mp_id on the declaration table.

After declarations are loaded, this module scans every row where
spouse_surname / spouse_given_name is non-empty and tries to find a
matching mp_index entry.  When found, declaration.spouse_mp_id is set to
that mp_id — indicating that the declaring MP's spouse is themselves an MP.

Matching uses the same Greek→Latin normalisation as src/link/match.py so that
accented, lower-case, or slightly variant spellings still resolve correctly.

Two passes:
  1. Exact match on (surname_norm, given_norm).
  2. Exact match on surname_norm alone, when the normalised given name is
     empty or the declaration omits it.

Usage:
    python -m src.link.spouse_mp [--db PATH] [--fiscal-year YEAR]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from src.link.match import normalize_name

DEFAULT_DB = Path(__file__).parent.parent.parent / "data" / "db" / "zegrift.sqlite"


# ─── core resolution ─────────────────────────────────────────────────────────

def _build_mp_index(con: sqlite3.Connection) -> tuple[
    dict[tuple[str, str], int],   # (sur_norm, giv_norm) -> mp_id  (exact full)
    dict[str, list[int]],         # sur_norm -> [mp_id, ...]        (surname bucket)
]:
    """Load mp_index into two lookup structures for fast matching."""
    rows = con.execute(
        "SELECT mp_id, surname_gr, given_name_gr, surname_lat, given_name_lat FROM mp_index"
    ).fetchall()

    exact: dict[tuple[str, str], int] = {}
    by_surname: dict[str, list[int]] = {}

    for mp_id, sur_gr, giv_gr, sur_lat, giv_lat in rows:
        # Prefer Greek-script names when available
        sur_raw = sur_gr if (sur_gr and _has_greek(sur_gr)) else sur_lat
        giv_raw = giv_gr if (giv_gr and _has_greek(giv_gr)) else giv_lat
        sur_norm = normalize_name(sur_raw or "")
        giv_norm = normalize_name(giv_raw or "")

        exact[(sur_norm, giv_norm)] = mp_id
        by_surname.setdefault(sur_norm, []).append(mp_id)

    return exact, by_surname


def _has_greek(s: str) -> bool:
    return any("Ͱ" <= c <= "Ͽ" or "ἀ" <= c <= "῿" for c in s)


def _find_spouse_mp_id(
    spouse_surname: str,
    spouse_given: str,
    exact: dict[tuple[str, str], int],
    by_surname: dict[str, list[int]],
) -> int | None:
    """Return the mp_id of the spouse if they appear in mp_index, else None."""
    sur_norm = normalize_name(spouse_surname)
    giv_norm = normalize_name(spouse_given)

    if not sur_norm:
        return None

    # Pass 1: exact (surname + given)
    if giv_norm:
        mp_id = exact.get((sur_norm, giv_norm))
        if mp_id is not None:
            return mp_id

    # Pass 2: surname-only (only when unique)
    candidates = by_surname.get(sur_norm, [])
    if len(candidates) == 1:
        return candidates[0]

    return None


def resolve_spouse_mp_ids(
    con: sqlite3.Connection,
    fiscal_year: int | None = None,
) -> int:
    """
    Scan declarations and populate spouse_mp_id where the spouse is also an MP.

    Returns the number of rows updated.
    """
    exact, by_surname = _build_mp_index(con)

    where = "WHERE spouse_surname IS NOT NULL"
    params: list = []
    if fiscal_year is not None:
        where += " AND fiscal_year = ?"
        params.append(fiscal_year)

    rows = con.execute(
        f"SELECT decl_id, mp_id, spouse_surname, spouse_given_name FROM declaration {where}",
        params,
    ).fetchall()

    updated = 0
    with con:
        for decl_id, mp_id, spouse_surname, spouse_given in rows:
            spouse_mp_id = _find_spouse_mp_id(
                spouse_surname or "",
                spouse_given or "",
                exact,
                by_surname,
            )
            # Never link a declaration to itself (guard against data corruption)
            if spouse_mp_id == mp_id:
                spouse_mp_id = None

            con.execute(
                "UPDATE declaration SET spouse_mp_id = ? WHERE decl_id = ?",
                (spouse_mp_id, decl_id),
            )
            if spouse_mp_id is not None:
                updated += 1

    return updated


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--fiscal-year", type=int, default=None,
                   help="Restrict resolution to a single fiscal year")
    args = p.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA foreign_keys = ON")

    n = resolve_spouse_mp_ids(con, fiscal_year=args.fiscal_year)
    con.close()
    print(f"spouse_mp_id resolved: {n} declaration(s) updated")


if __name__ == "__main__":
    main()
