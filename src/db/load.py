"""
Load a ParsedDeclaration into SQLite.

All inserts are idempotent by (mp_id, fiscal_year, parser_version).
A new parser run produces a new declaration row and new child rows;
old rows from previous parser versions are left intact.

Usage:
    python -m src.db.load --json data/parsed/4431678.json
    python -m src.db.load --all-parsed data/parsed/
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from src.parse.parse_pdf import (
    ParsedDeclaration, PARSER_VERSION,
    IncomeRow, VehicleRow, DepositRow, RealEstateRow,
    BusinessShareRow, LoanAssetsRow, LoanDetailRow,
    SecurityHoldingRow, SafeDepositBoxRow, RealEstateRightsRow,
)

DEFAULT_DB = Path(__file__).parent.parent.parent / "data" / "db" / "zegrift.sqlite"
SCHEMA_SQL = Path(__file__).parent / "schema.sql"


def open_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def load_declaration(con: sqlite3.Connection, d: ParsedDeclaration) -> int:
    """
    Upsert one ParsedDeclaration.  Returns the decl_id (existing or new).
    """
    with con:
        cur = con.execute(
            """
            INSERT INTO declaration
                (mp_id, fiscal_year, declaration_serial, submitted_at,
                 parser_version, parsed_at,
                 declarant_role, declarant_role_raw,
                 spouse_surname, spouse_given_name,
                 role_acquisition_date, role_loss_date)
            VALUES (?, ?, ?, ?, ?, datetime('now'),
                    ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mp_id, fiscal_year, parser_version) DO UPDATE SET
                declaration_serial    = excluded.declaration_serial,
                submitted_at          = excluded.submitted_at,
                parsed_at             = excluded.parsed_at,
                declarant_role        = excluded.declarant_role,
                declarant_role_raw    = excluded.declarant_role_raw,
                spouse_surname        = excluded.spouse_surname,
                spouse_given_name     = excluded.spouse_given_name,
                role_acquisition_date = excluded.role_acquisition_date,
                role_loss_date        = excluded.role_loss_date
            RETURNING decl_id
            """,
            (
                d.mp_id, d.fiscal_year, d.declaration_serial, d.submitted_at,
                d.parser_version,
                d.declarant_role or None,
                d.declarant_role_raw or None,
                d.spouse_surname_raw or None,
                d.spouse_given_raw or None,
                d.role_acquisition_date or None,
                d.role_loss_date or None,
            ),
        )
        row = cur.fetchone()
        if row:
            decl_id = row[0]
        else:
            decl_id = con.execute(
                "SELECT decl_id FROM declaration WHERE mp_id=? AND fiscal_year=? AND parser_version=?",
                (d.mp_id, d.fiscal_year, d.parser_version),
            ).fetchone()[0]

        # Delete old child rows for this decl_id before re-inserting
        for tbl in (
            "income_line", "vehicle", "deposit", "real_estate",
            "business_share", "loan", "security_holding",
            "safe_deposit_box", "real_estate_acquisition", "extraction_audit",
        ):
            con.execute(f"DELETE FROM {tbl} WHERE decl_id=?", (decl_id,))

        # Income
        for r in d.income:
            con.execute(
                """INSERT INTO income_line
                   (decl_id, row_index, source, kind, amount_eur, extraction_method, confidence)
                   VALUES (?,?,?,?,?,?,?)""",
                (decl_id, r.row_index, r.category_decoded or r.category_raw,
                 None, r.amount, r.extraction_method, r.confidence),
            )

        # Vehicles
        for r in d.vehicles:
            con.execute(
                """INSERT INTO vehicle
                   (decl_id, row_index, make, model, year, value_eur, extraction_method, confidence)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (decl_id, r.row_index,
                 r.vehicle_info_decoded or r.vehicle_info_raw,
                 r.cc_str or None,
                 r.acquisition_year,
                 r.purchase_price,
                 r.extraction_method, r.confidence),
            )

        # Deposits
        for r in d.deposits:
            con.execute(
                """INSERT INTO deposit
                   (decl_id, row_index, bank, account_type, balance_eur, extraction_method, confidence)
                   VALUES (?,?,?,?,?,?,?)""",
                (decl_id, r.row_index,
                 r.bank_decoded or r.bank_raw,
                 r.account_type_decoded or r.account_type_raw,
                 r.amount, r.extraction_method, r.confidence),
            )

        # Real estate
        for r in d.real_estate:
            con.execute(
                """INSERT INTO real_estate
                   (decl_id, row_index, kind, share_pct, area_m2,
                    location_raw, location_decoded, acquisition_year, value_eur,
                    extraction_method, confidence)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (decl_id, r.row_index,
                 r.property_type_decoded or r.property_type_raw,
                 None,
                 r.covered_area_m2 if not r.total_area_m2 else r.total_area_m2,
                 r.municipality_raw, r.municipality_decoded,
                 r.acquisition_year,
                 r.objective_value if r.objective_value else r.purchase_price,
                 r.extraction_method, r.confidence),
            )

        # Business shares — stored in the loan table with kind='business_share'
        for r in d.business_shares:
            con.execute(
                """INSERT INTO business_share
                   (decl_id, row_index, company, share_pct, value_eur,
                    extraction_method, confidence)
                   VALUES (?,?,?,?,?,?,?)""",
                (decl_id, r.row_index,
                 r.company_decoded or r.company_raw,
                 r.share_pct, r.book_value,
                 r.extraction_method, r.confidence),
            )

        # Loans (loans_assets + loans_detail merged into the loan table)
        for r in d.loans_assets:
            con.execute(
                """INSERT INTO loan
                   (decl_id, row_index, lender, kind, original_amount_eur,
                    outstanding_eur, extraction_method, confidence)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (decl_id, r.row_index,
                 None,
                 r.loan_type_decoded or r.loan_type_raw,
                 r.original_amount, r.remaining_amount,
                 r.extraction_method, r.confidence),
            )
        for r in d.loans_detail:
            con.execute(
                """INSERT INTO loan
                   (decl_id, row_index, lender, kind, original_amount_eur,
                    outstanding_eur, extraction_method, confidence)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (decl_id, r.row_index + 1000,   # offset to avoid PK clash with loans_assets rows
                 r.bank_decoded or r.bank_raw,
                 r.loan_type_decoded or r.loan_type_raw,
                 r.original_amount, r.outstanding,
                 r.extraction_method, r.confidence),
            )

        # Securities
        for r in d.securities:
            con.execute(
                """INSERT INTO security_holding
                   (decl_id, row_index, instrument, title, quantity,
                    acquisition_value_eur, value_eur, sale_value_eur,
                    extraction_method, confidence)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (decl_id, r.row_index,
                 r.instrument_type_decoded or r.instrument_type_raw,
                 r.title_decoded or r.title_raw,
                 r.quantity,
                 r.acquisition_value,
                 r.valuation,
                 r.sale_value,
                 r.extraction_method, r.confidence),
            )

        # Safe deposit boxes
        for r in d.safe_deposit_boxes:
            con.execute(
                """INSERT INTO safe_deposit_box
                   (decl_id, row_index, institution, country, rental_year,
                    notes, extraction_method, confidence)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (decl_id, r.row_index,
                 r.institution_decoded or r.institution_raw,
                 r.country_raw,
                 r.rental_year,
                 r.notes_raw,
                 r.extraction_method, r.confidence),
            )

        # Real estate rights / acquisitions
        for r in d.real_estate_rights:
            con.execute(
                """INSERT INTO real_estate_acquisition
                   (decl_id, row_index, rights_type, rights_pct, acquisition_method,
                    price_paid_eur, objective_value_eur, received_price_eur,
                    extraction_method, confidence)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (decl_id, r.row_index,
                 r.rights_type_raw,
                 r.rights_pct,
                 r.acquisition_method_decoded or r.acquisition_method_raw,
                 r.price_paid,
                 r.objective_value,
                 r.received_price,
                 r.extraction_method, r.confidence),
            )

        # Audit
        con.execute(
            """INSERT INTO extraction_audit
               (decl_id, fields_extracted, fields_failed, cmap_glyphs_mapped,
                ocr_regions_used, errors_json, needs_review)
               VALUES (?,?,?,?,?,?,?)""",
            (decl_id, d.fields_extracted, d.fields_failed, d.cmap_glyphs_mapped,
             d.ocr_regions_used, json.dumps(d.errors, ensure_ascii=False),
             int(d.needs_review)),
        )

    return decl_id


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Load parsed declarations into SQLite.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--json", dest="json_path", help="Load a single parsed JSON file")
    grp.add_argument("--all-parsed", dest="parsed_dir",
                     help="Load all *.json files from this directory")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    con = open_db(db_path)
    paths: list[Path] = []
    if args.json_path:
        paths = [Path(args.json_path)]
    else:
        paths = sorted(Path(args.parsed_dir).glob("*.json"))

    ok = failed = 0
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            # Reconstruct ParsedDeclaration from dict
            d = _dict_to_declaration(data)
            decl_id = load_declaration(con, d)
            print(f"  loaded mp_id={d.mp_id} -> decl_id={decl_id}")
            ok += 1
        except Exception as exc:
            print(f"FAIL {p.name}: {exc}", file=sys.stderr)
            failed += 1

    con.close()
    print(f"\nDone: {ok} loaded, {failed} failed")


def _dict_to_declaration(data: dict) -> ParsedDeclaration:
    d = ParsedDeclaration(
        mp_id=data["mp_id"],
        fiscal_year=data["fiscal_year"],
        declaration_serial=data.get("declaration_serial", ""),
        submitted_at=data.get("submitted_at", ""),
        parser_version=data.get("parser_version", PARSER_VERSION),
        declarant_surname_raw=data.get("declarant_surname_raw", ""),
        declarant_given_raw=data.get("declarant_given_raw", ""),
        declarant_patronymic_raw=data.get("declarant_patronymic_raw", ""),
        declarant_role=data.get("declarant_role", ""),
        declarant_role_raw=data.get("declarant_role_raw", ""),
        spouse_surname_raw=data.get("spouse_surname_raw", ""),
        spouse_given_raw=data.get("spouse_given_raw", ""),
        spouse_patronymic_raw=data.get("spouse_patronymic_raw", ""),
        role_acquisition_date=data.get("role_acquisition_date", ""),
        role_loss_date=data.get("role_loss_date", ""),
        fields_extracted=data.get("fields_extracted", 0),
        fields_failed=data.get("fields_failed", 0),
        cmap_glyphs_mapped=data.get("cmap_glyphs_mapped", 0),
        ocr_regions_used=data.get("ocr_regions_used", 0),
        errors=data.get("errors", []),
        needs_review=data.get("needs_review", False),
    )
    for r in data.get("income", []):
        d.income.append(IncomeRow(**r))
    for r in data.get("vehicles", []):
        d.vehicles.append(VehicleRow(**r))
    for r in data.get("deposits", []):
        d.deposits.append(DepositRow(**r))
    for r in data.get("real_estate", []):
        d.real_estate.append(RealEstateRow(**r))
    for r in data.get("business_shares", []):
        d.business_shares.append(BusinessShareRow(**r))
    for r in data.get("loans_assets", []):
        d.loans_assets.append(LoanAssetsRow(**r))
    for r in data.get("loans_detail", []):
        d.loans_detail.append(LoanDetailRow(**r))
    for r in data.get("securities", []):
        d.securities.append(SecurityHoldingRow(**r))
    for r in data.get("safe_deposit_boxes", []):
        d.safe_deposit_boxes.append(SafeDepositBoxRow(**r))
    for r in data.get("real_estate_rights", []):
        d.real_estate_rights.append(RealEstateRightsRow(**r))
    return d


if __name__ == "__main__":
    main()
