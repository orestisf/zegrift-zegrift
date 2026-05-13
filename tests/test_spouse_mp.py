"""
Tests for spouse-MP cross-linking (spouse_mp_id resolution).

These tests use in-memory SQLite databases so no real PDF download is needed.
The two real-world MPs used as fixtures are Achtsioglou (mp_id=4557979) and
Tzanakopoulos (mp_id=4557310), who are married to each other.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.link.spouse_mp import resolve_spouse_mp_ids, _find_spouse_mp_id, _build_mp_index

# ─── canonical IDs from the 2025 declarations ─────────────────────────────────
MP_ACHTSIOGLOU   = 4557979   # ACHTSIOGLOY_EYTYCHIA  — Ευτυχία Αχτσιόγλου
MP_TZANAKOPOULOS = 4557310   # TZANAKOPOYLOS_DIMITRIOS — Δημήτριος Τζανακόπουλος


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_db() -> sqlite3.Connection:
    """Create a minimal in-memory schema matching schema.sql."""
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript("""
        CREATE TABLE mp_index (
            mp_id          INTEGER PRIMARY KEY,
            surname_gr     TEXT NOT NULL,
            given_name_gr  TEXT NOT NULL,
            surname_lat    TEXT NOT NULL,
            given_name_lat TEXT NOT NULL,
            pdf_url        TEXT NOT NULL DEFAULT '',
            scraped_at     TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE declaration (
            decl_id               INTEGER PRIMARY KEY AUTOINCREMENT,
            mp_id                 INTEGER NOT NULL REFERENCES mp_index(mp_id),
            fiscal_year           INTEGER NOT NULL,
            declaration_serial    TEXT,
            submitted_at          TEXT,
            parser_version        TEXT NOT NULL DEFAULT '0.1.4',
            parsed_at             TEXT NOT NULL DEFAULT '',
            declarant_role        TEXT,
            declarant_role_raw    TEXT,
            spouse_surname        TEXT,
            spouse_given_name     TEXT,
            spouse_mp_id          INTEGER REFERENCES mp_index(mp_id),
            role_acquisition_date TEXT,
            role_loss_date        TEXT,
            UNIQUE (mp_id, fiscal_year, parser_version)
        );
    """)
    return con


def _insert_mp(con: sqlite3.Connection, mp_id: int, sur_gr: str, giv_gr: str,
               sur_lat: str, giv_lat: str) -> None:
    con.execute(
        "INSERT INTO mp_index (mp_id, surname_gr, given_name_gr, surname_lat, given_name_lat)"
        " VALUES (?, ?, ?, ?, ?)",
        (mp_id, sur_gr, giv_gr, sur_lat, giv_lat),
    )


def _insert_decl(con: sqlite3.Connection, decl_id: int, mp_id: int,
                 spouse_surname: str | None, spouse_given: str | None,
                 role: str = "mp") -> None:
    con.execute(
        """INSERT INTO declaration
           (decl_id, mp_id, fiscal_year, declarant_role, spouse_surname, spouse_given_name)
           VALUES (?, ?, 2024, ?, ?, ?)""",
        (decl_id, mp_id, role, spouse_surname, spouse_given),
    )


# ─── unit: _find_spouse_mp_id ─────────────────────────────────────────────────

def test_find_spouse_exact_match():
    """Exact (surname, given) match resolves correctly."""
    con = _make_db()
    _insert_mp(con, MP_ACHTSIOGLOU, "ΑΧΤΣΙΟΓΛΟΥ", "ΕΥΤΥΧΙΑ", "ACHTSIOGLOY", "EYTYCHIA")
    exact, by_surname = _build_mp_index(con)

    result = _find_spouse_mp_id("ΑΧΤΣΙΟΓΛΟΥ", "ΕΥΤΥΧΙΑ", exact, by_surname)
    assert result == MP_ACHTSIOGLOU


def test_find_spouse_latin_name():
    """Latinised names from the filename also resolve via normalisation."""
    con = _make_db()
    _insert_mp(con, MP_TZANAKOPOULOS, "ΤΖΑΝΑΚΟΠΟΥΛΟΣ", "ΔΗΜΗΤΡΙΟΣ",
               "TZANAKOPOYLOS", "DIMITRIOS")
    exact, by_surname = _build_mp_index(con)

    result = _find_spouse_mp_id("TZANAKOPOYLOS", "DIMITRIOS", exact, by_surname)
    assert result == MP_TZANAKOPOULOS


def test_find_spouse_surname_only_when_unique():
    """Surname-only pass fires when the given name is absent and match is unique."""
    con = _make_db()
    _insert_mp(con, MP_ACHTSIOGLOU, "ΑΧΤΣΙΟΓΛΟΥ", "ΕΥΤΥΧΙΑ", "ACHTSIOGLOY", "EYTYCHIA")
    exact, by_surname = _build_mp_index(con)

    result = _find_spouse_mp_id("ΑΧΤΣΙΟΓΛΟΥ", "", exact, by_surname)
    assert result == MP_ACHTSIOGLOU


def test_find_spouse_surname_only_ambiguous_returns_none():
    """Surname-only pass returns None when multiple MPs share the surname."""
    con = _make_db()
    _insert_mp(con, 1, "ΠΑΠΑΔΟΠΟΥΛΟΣ", "ΓΕΩΡΓΙΟΣ", "PAPADOPOULOS", "GEORGIOS")
    _insert_mp(con, 2, "ΠΑΠΑΔΟΠΟΥΛΟΣ", "ΝΙΚΟΣ",    "PAPADOPOULOS", "NIKOS")
    exact, by_surname = _build_mp_index(con)

    result = _find_spouse_mp_id("ΠΑΠΑΔΟΠΟΥΛΟΣ", "", exact, by_surname)
    assert result is None


def test_find_spouse_unknown_name_returns_none():
    con = _make_db()
    _insert_mp(con, MP_ACHTSIOGLOU, "ΑΧΤΣΙΟΓΛΟΥ", "ΕΥΤΥΧΙΑ", "ACHTSIOGLOY", "EYTYCHIA")
    exact, by_surname = _build_mp_index(con)

    result = _find_spouse_mp_id("ΑΝΩΝΥΜΟΣ", "ΤΥΧΑΙΟΣ", exact, by_surname)
    assert result is None


# ─── integration: resolve_spouse_mp_ids ───────────────────────────────────────

def test_resolve_both_spouses_are_mps():
    """
    Both Achtsioglou and Tzanakopoulos appear in mp_index.
    After resolution each declaration should reference the other's mp_id.
    """
    con = _make_db()
    _insert_mp(con, MP_ACHTSIOGLOU,   "ΑΧΤΣΙΟΓΛΟΥ",   "ΕΥΤΥΧΙΑ",   "ACHTSIOGLOY",   "EYTYCHIA")
    _insert_mp(con, MP_TZANAKOPOULOS, "ΤΖΑΝΑΚΟΠΟΥΛΟΣ", "ΔΗΜΗΤΡΙΟΣ", "TZANAKOPOYLOS", "DIMITRIOS")

    with con:
        # Achtsioglou's declaration lists Tzanakopoulos as spouse
        _insert_decl(con, 1, MP_ACHTSIOGLOU,
                     spouse_surname="ΤΖΑΝΑΚΟΠΟΥΛΟΣ", spouse_given="ΔΗΜΗΤΡΙΟΣ")
        # Tzanakopoulos's declaration lists Achtsioglou as spouse
        _insert_decl(con, 2, MP_TZANAKOPOULOS,
                     spouse_surname="ΑΧΤΣΙΟΓΛΟΥ", spouse_given="ΕΥΤΥΧΙΑ")

    updated = resolve_spouse_mp_ids(con)
    assert updated == 2

    rows = {r[0]: r[1] for r in con.execute(
        "SELECT decl_id, spouse_mp_id FROM declaration ORDER BY decl_id"
    )}
    assert rows[1] == MP_TZANAKOPOULOS, "Achtsioglou's decl should link to Tzanakopoulos"
    assert rows[2] == MP_ACHTSIOGLOU,   "Tzanakopoulos's decl should link to Achtsioglou"


def test_resolve_spouse_not_mp_leaves_null():
    """When the spouse is not in mp_index, spouse_mp_id stays NULL."""
    con = _make_db()
    _insert_mp(con, MP_ACHTSIOGLOU, "ΑΧΤΣΙΟΓΛΟΥ", "ΕΥΤΥΧΙΑ", "ACHTSIOGLOY", "EYTYCHIA")

    with con:
        _insert_decl(con, 1, MP_ACHTSIOGLOU,
                     spouse_surname="ΙΔΙΩΤΗΣ", spouse_given="ΑΝΩΝΥΜΟΣ")

    updated = resolve_spouse_mp_ids(con)
    assert updated == 0

    row = con.execute("SELECT spouse_mp_id FROM declaration WHERE decl_id=1").fetchone()
    assert row[0] is None


def test_resolve_no_spouse_info_leaves_null():
    """Declarations with NULL spouse_surname are skipped entirely."""
    con = _make_db()
    _insert_mp(con, MP_ACHTSIOGLOU, "ΑΧΤΣΙΟΓΛΟΥ", "ΕΥΤΥΧΙΑ", "ACHTSIOGLOY", "EYTYCHIA")

    with con:
        _insert_decl(con, 1, MP_ACHTSIOGLOU, spouse_surname=None, spouse_given=None)

    updated = resolve_spouse_mp_ids(con)
    assert updated == 0


def test_resolve_does_not_self_link():
    """A declaration should never reference its own mp_id via spouse_mp_id."""
    con = _make_db()
    _insert_mp(con, MP_ACHTSIOGLOU, "ΑΧΤΣΙΟΓΛΟΥ", "ΕΥΤΥΧΙΑ", "ACHTSIOGLOY", "EYTYCHIA")

    with con:
        # Deliberately corrupt data: spouse name matches the declarant themselves
        _insert_decl(con, 1, MP_ACHTSIOGLOU,
                     spouse_surname="ΑΧΤΣΙΟΓΛΟΥ", spouse_given="ΕΥΤΥΧΙΑ")

    updated = resolve_spouse_mp_ids(con)
    assert updated == 0

    row = con.execute("SELECT spouse_mp_id FROM declaration WHERE decl_id=1").fetchone()
    assert row[0] is None


def test_resolve_fiscal_year_filter():
    """fiscal_year parameter restricts which declarations are processed."""
    con = _make_db()
    _insert_mp(con, MP_ACHTSIOGLOU,   "ΑΧΤΣΙΟΓΛΟΥ",   "ΕΥΤΥΧΙΑ",   "ACHTSIOGLOY",   "EYTYCHIA")
    _insert_mp(con, MP_TZANAKOPOULOS, "ΤΖΑΝΑΚΟΠΟΥΛΟΣ", "ΔΗΜΗΤΡΙΟΣ", "TZANAKOPOYLOS", "DIMITRIOS")

    with con:
        con.execute(
            """INSERT INTO declaration
               (decl_id, mp_id, fiscal_year, declarant_role, spouse_surname, spouse_given_name)
               VALUES (1, ?, 2023, 'mp', 'ΤΖΑΝΑΚΟΠΟΥΛΟΣ', 'ΔΗΜΗΤΡΙΟΣ'),
                      (2, ?, 2024, 'mp', 'ΤΖΑΝΑΚΟΠΟΥΛΟΣ', 'ΔΗΜΗΤΡΙΟΣ')""",
            (MP_ACHTSIOGLOU, MP_ACHTSIOGLOU),
        )

    updated = resolve_spouse_mp_ids(con, fiscal_year=2024)
    assert updated == 1

    rows = {r[0]: r[1] for r in con.execute(
        "SELECT decl_id, spouse_mp_id FROM declaration ORDER BY decl_id"
    )}
    assert rows[1] is None              # fiscal_year=2023, was skipped
    assert rows[2] == MP_TZANAKOPOULOS  # fiscal_year=2024, was processed


# ─── schema: new columns present ──────────────────────────────────────────────

def test_schema_has_new_columns():
    """Verify role_acquisition_date, role_loss_date, spouse_mp_id exist."""
    con = _make_db()
    cols = {row[1] for row in con.execute("PRAGMA table_info(declaration)")}
    assert "role_acquisition_date" in cols
    assert "role_loss_date" in cols
    assert "spouse_mp_id" in cols
    # Old names must not exist
    assert "obligation_period_from" not in cols
    assert "obligation_period_to" not in cols


def test_schema_obligation_period_columns_gone():
    """Confirm the legacy column names are absent from a fresh schema."""
    con = _make_db()
    cols = {row[1] for row in con.execute("PRAGMA table_info(declaration)")}
    assert "obligation_period_from" not in cols
    assert "obligation_period_to" not in cols
