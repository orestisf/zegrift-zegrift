"""
Main PDF parser for the 2024 asset-declaration form.

Usage:
    from src.parse.parse_pdf import parse_pdf
    result = parse_pdf("data/pdfs/2025/4431678_GKOYNTARAS_ANTONIOS.pdf")

Or as a CLI for inspection:
    python -m src.parse.parse_pdf data/pdfs/2025/4431678_GKOYNTARAS_ANTONIOS.pdf
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import pdfplumber

from src.parse.templates.decl_2024 import (
    HEADER_SKIP_ROWS,
    NCOLS_TO_SECTION,
    SECTION_TITLE_KEYWORDS,
    HEADER, INCOME, VEHICLES, SAFE_DEPOSIT_BOXES, LOANS_DETAIL, DEPOSITS,
    LOANS_ASSETS, SECURITIES, BUSINESS_SHARES, REAL_ESTATE,
    REAL_ESTATE_RIGHTS, UNKNOWN,
    INCOME_COLS, VEHICLE_COLS, DEPOSIT_COLS, REAL_ESTATE_COLS,
    BUSINESS_SHARE_COLS, LOANS_ASSETS_COLS, LOANS_DETAIL_COLS,
    SECURITIES_COLS, SAFE_DEPOSIT_COLS, REAL_ESTATE_RIGHTS_COLS,
)
from src.parse.font_cmap import build_cmap, decode_text, TesseractUnavailable

PARSER_VERSION = "0.1.2"

# Greek decimal notation uses comma as decimal separator
_NUM_RE = re.compile(r"[-\d.,]+")


# ─── helpers ─────────────────────────────────────────────────────────────────

def _cell(row: list, idx: int) -> str:
    """Safely get a cell value from a table row, stripping whitespace."""
    try:
        v = row[idx]
        return (v or "").strip() if isinstance(v, str) else ""
    except IndexError:
        return ""


def _parse_greek_number(s: str) -> float | None:
    """Parse a Greek-locale number string: '29.674,25' -> 29674.25"""
    if not s:
        return None
    s = s.strip().replace("\n", "").replace(" ", "")
    # Strip leading/trailing junk
    m = _NUM_RE.search(s)
    if not m:
        return None
    s = m.group()
    # Greek format: dots as thousands separator, comma as decimal
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_year(s: str) -> int | None:
    m = re.search(r"\b(19|20)\d{2}\b", s)
    return int(m.group()) if m else None


def _extract_trailing_number(s: str) -> int | None:
    """Extract trailing digit sequence from a row index like 'Ακίνητο 3' (garbled+'3')."""
    m = re.search(r"(\d+)\s*$", s)
    return int(m.group(1)) if m else None


def _decode(raw: str, cmap: dict[int, str]) -> tuple[str, str, float]:
    """Return (raw, decoded, confidence). Decoded == raw when cmap empty."""
    if not cmap:
        return raw, raw, 0.0
    decoded, conf = decode_text(raw, cmap)
    method = "cmap_decoded" if conf > 0.5 else "positional"
    return raw, decoded, conf


def _method(raw: str, decoded: str, confidence: float, tesseract_ok: bool) -> str:
    if not tesseract_ok and confidence < 0.5:
        return "positional"
    if confidence >= 0.8:
        return "cmap_decoded"
    if tesseract_ok:
        return "region_ocr"
    return "positional"


# ─── result dataclasses ───────────────────────────────────────────────────────

@dataclass
class IncomeRow:
    row_index: int
    category_raw: str = ""
    category_decoded: str = ""
    amount: float | None = None
    notes_raw: str = ""
    extraction_method: str = "positional"
    confidence: float = 0.0


@dataclass
class VehicleRow:
    row_index: int
    vehicle_info_raw: str = ""      # ΕΙΔΟΣ ΟΧΗΜΑΤΟΣ (vehicle type raw)
    vehicle_info_decoded: str = ""
    cc_str: str = ""                # ΚΥΒΙΣΜΟΣ (engine cc or boat length)
    ownership_pct: float | None = None
    first_reg_year: int | None = None
    acquisition_year: int | None = None
    purchase_price: float | None = None
    extraction_method: str = "positional"
    confidence: float = 0.0


@dataclass
class DepositRow:
    row_index: int
    bank_raw: str = ""
    bank_decoded: str = ""
    account_type_raw: str = ""
    account_type_decoded: str = ""
    count: int | None = None
    amount: float | None = None
    notes_raw: str = ""
    extraction_method: str = "positional"
    confidence: float = 0.0


@dataclass
class RealEstateRow:
    row_index: int
    property_type_raw: str = ""
    property_type_decoded: str = ""
    municipality_raw: str = ""
    municipality_decoded: str = ""
    total_area_m2: float | None = None
    covered_area_m2: float | None = None
    other_area_m2: float | None = None
    acquisition_year: int | None = None
    build_year: int | None = None
    purchase_price: float | None = None
    objective_value: float | None = None
    extraction_method: str = "positional"
    confidence: float = 0.0


@dataclass
class BusinessShareRow:
    row_index: int
    company_raw: str = ""        # ΕΠΩΝΥΜΙΑ ΕΠΙΧΕΙΡΗΣΗΣ (company name, col 5)
    company_decoded: str = ""
    participation_type_raw: str = ""  # ΕΙΔΟΣ ΣΥΜΜΕΤΟΧΗΣ (col 3)
    share_pct: float | None = None   # % ΚΑΤΑ ΤΟ ΕΤΟΣ ΔΗΛΩΣΗΣ (col 9)
    book_value: float | None = None  # ΚΕΦΑΛΑΙΟ ΕΙΣΦΟΡΑΣ 31/12 (col 12)
    start_year: int | None = None
    extraction_method: str = "positional"
    confidence: float = 0.0


@dataclass
class LoanAssetsRow:
    row_index: int
    loan_type_raw: str = ""
    loan_type_decoded: str = ""
    periodic_amount: float | None = None
    ownership_pct: float | None = None
    start_year: int | None = None
    end_year: int | None = None
    original_amount: float | None = None
    remaining_amount: float | None = None
    extraction_method: str = "positional"
    confidence: float = 0.0


@dataclass
class LoanDetailRow:
    row_index: int
    loan_type_raw: str = ""
    loan_type_decoded: str = ""
    bank_raw: str = ""
    bank_decoded: str = ""
    original_amount: float | None = None
    outstanding: float | None = None
    start_date: str = ""
    end_date: str = ""
    extraction_method: str = "positional"
    confidence: float = 0.0


@dataclass
class SecurityHoldingRow:
    row_index: int
    owner_type_raw: str = ""       # ΕΠΕΝΔΥΤΗΣ (ΥΠΟΧΡΕΟΣ / ΣΥΖΥΓΟΣ)
    instrument_type_raw: str = ""  # ΕΙΔΟΣ ΧΡΕΟΓΡΑΦΟΥ
    instrument_type_decoded: str = ""
    title_raw: str = ""            # ΤΙΤΛΟΣ ΧΡΕΟΓΡΑΦΟΥ
    title_decoded: str = ""
    quantity: float | None = None
    acquisition_value: float | None = None
    valuation: float | None = None   # current book/market value
    sale_value: float | None = None
    extraction_method: str = "positional"
    confidence: float = 0.0


@dataclass
class SafeDepositBoxRow:
    row_index: int
    owner_raw: str = ""
    institution_raw: str = ""
    institution_decoded: str = ""
    country_raw: str = ""
    rental_year: int | None = None
    notes_raw: str = ""
    extraction_method: str = "positional"
    confidence: float = 0.0


@dataclass
class RealEstateRightsRow:
    row_index: int
    rights_type_raw: str = ""      # e.g. "ΠΛΗΡΗΣ ΚΥΡΙΟΤΗΤΑ 100 %", "ΣΥΓΚΥΡΙΟΤΗΤΑ 33.33 %"
    rights_pct: float | None = None  # numeric % extracted from rights_type_raw
    acquisition_method_raw: str = ""  # ΓΟΝΙΚΗ ΠΑΡΟΧΗ, ΑΓΟΡΑ, etc.
    acquisition_method_decoded: str = ""
    price_paid: float | None = None
    objective_value: float | None = None
    received_price: float | None = None
    extraction_method: str = "positional"
    confidence: float = 0.0


@dataclass
class ParsedDeclaration:
    mp_id: int
    fiscal_year: int
    declaration_serial: str = ""
    submitted_at: str = ""
    parser_version: str = PARSER_VERSION
    declarant_surname_raw: str = ""
    declarant_given_raw: str = ""
    income: list[IncomeRow] = field(default_factory=list)
    vehicles: list[VehicleRow] = field(default_factory=list)
    deposits: list[DepositRow] = field(default_factory=list)
    real_estate: list[RealEstateRow] = field(default_factory=list)
    business_shares: list[BusinessShareRow] = field(default_factory=list)
    loans_assets: list[LoanAssetsRow] = field(default_factory=list)
    loans_detail: list[LoanDetailRow] = field(default_factory=list)
    securities: list[SecurityHoldingRow] = field(default_factory=list)
    safe_deposit_boxes: list[SafeDepositBoxRow] = field(default_factory=list)
    real_estate_rights: list[RealEstateRightsRow] = field(default_factory=list)
    # Audit
    fields_extracted: int = 0
    fields_failed: int = 0
    cmap_glyphs_mapped: int = 0
    ocr_regions_used: int = 0
    errors: list[str] = field(default_factory=list)
    needs_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── section parsers ──────────────────────────────────────────────────────────

def _parse_header(table: list[list], page, result: ParsedDeclaration) -> None:
    """Extract declaration serial, MP name, submission date from page 1."""
    # Row 0 always has the serial in column 3: e.g. "Σ2298-..."
    if table and table[0]:
        serial_raw = _cell(table[0], 3)
        m = re.search(r"[A-Z0-9][\d\-A-Z]+", serial_raw)
        if m:
            result.declaration_serial = m.group()

    # Data rows: rows 2-5 contain field-label:value pairs in cols 0 and 2
    for row in table[2:]:
        value = _cell(row, 2)
        if not value:
            continue
        if re.search(r"\d{2}/\d{2}/\d{4}", value):
            result.submitted_at = re.search(r"\d{2}/\d{2}/\d{4}", value).group()
        elif not result.declarant_surname_raw:
            result.declarant_surname_raw = value
        elif not result.declarant_given_raw:
            result.declarant_given_raw = value

    # Submission date may be a free-floating text element (not inside any table)
    if not result.submitted_at:
        raw_text = page.extract_text() or ""
        m = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", raw_text)
        if m:
            result.submitted_at = m.group(1)

    result.fields_extracted += 2


def _parse_income(table: list[list], cmap: dict, result: ParsedDeclaration) -> None:
    c = INCOME_COLS
    for i, row in enumerate(table[HEADER_SKIP_ROWS:]):
        if not any(row):
            continue
        amount_str = _cell(row, c["amount"])
        # Skip rows where the amount cell contains no digits (e.g. column header row)
        if not amount_str or not any(ch.isdigit() for ch in amount_str):
            continue
        cat_raw = _cell(row, c["category_raw"])
        cat_raw2, cat_dec, conf = _decode(cat_raw, cmap)
        r = IncomeRow(
            row_index=i,
            category_raw=cat_raw,
            category_decoded=cat_dec,
            amount=_parse_greek_number(amount_str),
            notes_raw=_cell(row, c["notes_raw"]),
            extraction_method="cmap_decoded" if conf > 0.5 else "positional",
            confidence=conf,
        )
        result.income.append(r)
        result.fields_extracted += 1


def _parse_vehicles(table: list[list], cmap: dict, result: ParsedDeclaration) -> None:
    c = VEHICLE_COLS
    for i, row in enumerate(table[HEADER_SKIP_ROWS:]):
        if not any(row):
            continue
        acq_yr = _parse_year(_cell(row, c["acquisition_year"]))
        first_yr = _parse_year(_cell(row, c["first_reg_year"]))
        # Skip header/title rows that have no year
        if acq_yr is None and first_yr is None:
            continue
        info_raw = _cell(row, c["vehicle_info_raw"])
        _, info_dec, conf = _decode(info_raw, cmap)
        pct_str = _cell(row, c["ownership_pct_str"])
        pct = _parse_greek_number(re.sub(r"[^\d.,]", "", pct_str)) if pct_str else None
        r = VehicleRow(
            row_index=i,
            vehicle_info_raw=info_raw,
            vehicle_info_decoded=info_dec,
            cc_str=_cell(row, c["cc_str"]),
            ownership_pct=pct,
            first_reg_year=first_yr,
            acquisition_year=acq_yr,
            purchase_price=_parse_greek_number(_cell(row, c["purchase_price"])),
            extraction_method="cmap_decoded" if conf > 0.5 else "positional",
            confidence=conf,
        )
        result.vehicles.append(r)
        result.fields_extracted += 1


def _parse_deposits(table: list[list], cmap: dict, result: ParsedDeclaration) -> None:
    c = DEPOSIT_COLS
    for i, row in enumerate(table[HEADER_SKIP_ROWS:]):
        if not any(row):
            continue
        amt_str = _cell(row, c["amount"])
        if not amt_str or not any(ch.isdigit() for ch in amt_str):
            continue
        bank_raw = _cell(row, c["bank_raw"])
        _, bank_dec, b_conf = _decode(bank_raw, cmap)
        acct_raw = _cell(row, c["account_type_raw"])
        _, acct_dec, a_conf = _decode(acct_raw, cmap)
        count_str = _cell(row, c["count"])
        conf = max(b_conf, a_conf)
        r = DepositRow(
            row_index=i,
            bank_raw=bank_raw,
            bank_decoded=bank_dec,
            account_type_raw=acct_raw,
            account_type_decoded=acct_dec,
            count=int(count_str) if count_str.isdigit() else None,
            amount=_parse_greek_number(amt_str),
            notes_raw=_cell(row, c["notes_raw"]),
            extraction_method="cmap_decoded" if conf > 0.5 else "positional",
            confidence=conf,
        )
        result.deposits.append(r)
        result.fields_extracted += 1


def _parse_real_estate(table: list[list], cmap: dict, result: ParsedDeclaration) -> None:
    c = REAL_ESTATE_COLS
    for i, row in enumerate(table[HEADER_SKIP_ROWS:]):
        if not any(row):
            continue
        idx_raw = _cell(row, c["index_raw"])
        # Skip title row ("Ακίνητα...") and column-header row ("AA") — both have no digit in col 0
        if not re.search(r"\d", idx_raw):
            continue
        row_num = _extract_trailing_number(idx_raw) or (i + 1)
        mun_raw = _cell(row, c["municipality_raw"])
        _, mun_dec, m_conf = _decode(mun_raw, cmap)
        prop_raw = _cell(row, c["property_type_raw"])
        _, prop_dec, p_conf = _decode(prop_raw, cmap)
        conf = max(m_conf, p_conf)
        total_area = _parse_greek_number(_cell(row, c["total_area_m2"]))
        covered_area = _parse_greek_number(_cell(row, c["covered_area_m2"]))
        r = RealEstateRow(
            row_index=row_num,
            property_type_raw=prop_raw,
            property_type_decoded=prop_dec,
            municipality_raw=mun_raw,
            municipality_decoded=mun_dec,
            total_area_m2=total_area,
            covered_area_m2=covered_area,
            other_area_m2=_parse_greek_number(_cell(row, c["other_area_m2"])),
            acquisition_year=_parse_year(_cell(row, c["acquisition_year"])),
            build_year=_parse_year(_cell(row, c["build_year"])),
            purchase_price=None,    # not available in 20-col description table
            objective_value=None,
            extraction_method="cmap_decoded" if conf > 0.5 else "positional",
            confidence=conf,
        )
        result.real_estate.append(r)
        result.fields_extracted += 1


def _parse_business_shares(table: list[list], cmap: dict, result: ParsedDeclaration) -> None:
    c = BUSINESS_SHARE_COLS
    for i, row in enumerate(table[HEADER_SKIP_ROWS:]):
        if not any(row):
            continue
        # Skip title/header rows: require a valid 4-digit start year in col 10
        start_year = _parse_year(_cell(row, c["start_year"]))
        if start_year is None:
            continue
        co_raw = _cell(row, c["company_raw"])
        _, co_dec, conf = _decode(co_raw, cmap)
        part_raw = _cell(row, c["participation_type_raw"])
        pct_str = _cell(row, c["share_pct"])
        pct = _parse_greek_number(pct_str) if pct_str else None
        r = BusinessShareRow(
            row_index=i,
            company_raw=co_raw,
            company_decoded=co_dec,
            participation_type_raw=part_raw,
            share_pct=pct,
            book_value=_parse_greek_number(_cell(row, c["book_value"])),
            start_year=start_year,
            extraction_method="cmap_decoded" if conf > 0.5 else "positional",
            confidence=conf,
        )
        result.business_shares.append(r)
        result.fields_extracted += 1


def _parse_loans_assets(table: list[list], cmap: dict, result: ParsedDeclaration) -> None:
    c = LOANS_ASSETS_COLS
    for i, row in enumerate(table[HEADER_SKIP_ROWS:]):
        if not any(row):
            continue
        orig_str = _cell(row, c["original_amount"])
        if not orig_str:
            continue
        lt_raw = _cell(row, c["loan_type_raw"])
        _, lt_dec, conf = _decode(lt_raw, cmap)
        r = LoanAssetsRow(
            row_index=i,
            loan_type_raw=lt_raw,
            loan_type_decoded=lt_dec,
            periodic_amount=_parse_greek_number(_cell(row, c["periodic_amount"])),
            ownership_pct=_parse_greek_number(_cell(row, c["ownership_pct"])),
            start_year=_parse_year(_cell(row, c["start_year"])),
            end_year=_parse_year(_cell(row, c["end_year"])),
            original_amount=_parse_greek_number(orig_str),
            remaining_amount=_parse_greek_number(_cell(row, c["remaining_amount"])),
            extraction_method="cmap_decoded" if conf > 0.5 else "positional",
            confidence=conf,
        )
        result.loans_assets.append(r)
        result.fields_extracted += 1


def _parse_loans_detail(table: list[list], cmap: dict, result: ParsedDeclaration) -> None:
    c = LOANS_DETAIL_COLS
    for i, row in enumerate(table[HEADER_SKIP_ROWS:]):
        if not any(row):
            continue
        # start_date must be a real dd/mm/yyyy date; outstanding must contain digits
        start = _cell(row, c["start_date"])
        outstanding_str = _cell(row, c["outstanding"])
        if start and not re.search(r"\d{2}/\d{2}/\d{4}", start):
            start = ""
        if outstanding_str and not any(ch.isdigit() for ch in outstanding_str):
            outstanding_str = ""
        if not start and not outstanding_str:
            continue
        lt_raw = _cell(row, c["loan_type_raw"])
        _, lt_dec, lt_conf = _decode(lt_raw, cmap)
        bank_raw = _cell(row, c["bank_raw"])
        _, bank_dec, bk_conf = _decode(bank_raw, cmap)
        conf = max(lt_conf, bk_conf)
        r = LoanDetailRow(
            row_index=i,
            loan_type_raw=lt_raw,
            loan_type_decoded=lt_dec,
            bank_raw=bank_raw,
            bank_decoded=bank_dec,
            original_amount=_parse_greek_number(_cell(row, c["original_amount"])),
            outstanding=_parse_greek_number(outstanding_str),
            start_date=start,
            end_date=_cell(row, c["end_date"]),
            extraction_method="cmap_decoded" if conf > 0.5 else "positional",
            confidence=conf,
        )
        result.loans_detail.append(r)
        result.fields_extracted += 1


def _parse_securities(table: list[list], cmap: dict, result: ParsedDeclaration) -> None:
    c = SECURITIES_COLS
    for i, row in enumerate(table[HEADER_SKIP_ROWS:]):
        if not any(row):
            continue
        qty_str = _cell(row, c["quantity_str"])
        # Skip title/header rows that have no digit in the quantity cell
        if not qty_str or not any(ch.isdigit() for ch in qty_str):
            continue
        inst_raw = _cell(row, c["instrument_type_raw"])
        _, inst_dec, i_conf = _decode(inst_raw, cmap)
        title_raw = _cell(row, c["title_raw"])
        _, title_dec, t_conf = _decode(title_raw, cmap)
        conf = max(i_conf, t_conf)
        r = SecurityHoldingRow(
            row_index=i,
            owner_type_raw=_cell(row, c["owner_type_raw"]),
            instrument_type_raw=inst_raw,
            instrument_type_decoded=inst_dec,
            title_raw=title_raw,
            title_decoded=title_dec,
            quantity=_parse_greek_number(qty_str),
            acquisition_value=_parse_greek_number(_cell(row, c["acquisition_value"])),
            valuation=_parse_greek_number(_cell(row, c["valuation"])),
            sale_value=_parse_greek_number(_cell(row, c["sale_value"])),
            extraction_method="cmap_decoded" if conf > 0.5 else "positional",
            confidence=conf,
        )
        result.securities.append(r)
        result.fields_extracted += 1


def _parse_safe_deposit_boxes(table: list[list], cmap: dict, result: ParsedDeclaration) -> None:
    c = SAFE_DEPOSIT_COLS
    for i, row in enumerate(table[HEADER_SKIP_ROWS:]):
        if not any(row):
            continue
        # Filter by rental_year — header/title rows have no valid 4-digit year
        rental_year = _parse_year(_cell(row, c["rental_year"]))
        if rental_year is None:
            continue
        inst_raw = _cell(row, c["institution_raw"])
        _, inst_dec, conf = _decode(inst_raw, cmap)
        r = SafeDepositBoxRow(
            row_index=i,
            owner_raw=_cell(row, c["owner_raw"]),
            institution_raw=inst_raw,
            institution_decoded=inst_dec,
            country_raw=_cell(row, c["country_raw"]),
            rental_year=rental_year,
            notes_raw=_cell(row, c["notes_raw"]),
            extraction_method="cmap_decoded" if conf > 0.5 else "positional",
            confidence=conf,
        )
        result.safe_deposit_boxes.append(r)
        result.fields_extracted += 1


_RIGHTS_PCT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


def _parse_real_estate_rights(table: list[list], cmap: dict, result: ParsedDeclaration) -> None:
    c = REAL_ESTATE_RIGHTS_COLS
    for i, row in enumerate(table[HEADER_SKIP_ROWS:]):
        if not any(row):
            continue
        idx_raw = _cell(row, c["index_raw"])
        # Skip title/header rows: "ΑΚΙΝΗΤΟ N" always contains a digit in col 0
        if not re.search(r"\d", idx_raw):
            continue
        row_num = _extract_trailing_number(idx_raw) or (i + 1)
        rights_raw = _cell(row, c["rights_type_raw"])
        acq_raw = _cell(row, c["acquisition_method_raw"])
        _, acq_dec, conf = _decode(acq_raw, cmap)
        # Extract ownership percentage from rights_type text.
        # The form uses Western decimal notation in % values ("33.33 %"),
        # so we use float() directly rather than _parse_greek_number.
        pct_match = _RIGHTS_PCT_RE.search(rights_raw)
        rights_pct: float | None = None
        if pct_match:
            try:
                rights_pct = float(pct_match.group(1).replace(",", "."))
            except ValueError:
                pass
        r = RealEstateRightsRow(
            row_index=row_num,
            rights_type_raw=rights_raw,
            rights_pct=rights_pct,
            acquisition_method_raw=acq_raw,
            acquisition_method_decoded=acq_dec,
            price_paid=_parse_greek_number(_cell(row, c["price_paid"])),
            objective_value=_parse_greek_number(_cell(row, c["objective_value"])),
            received_price=_parse_greek_number(_cell(row, c["received_price"])),
            extraction_method="cmap_decoded" if conf > 0.5 else "positional",
            confidence=conf,
        )
        result.real_estate_rights.append(r)
        result.fields_extracted += 1


# ─── section identification ───────────────────────────────────────────────────

def _strip_accents(s: str) -> str:
    """Remove diacritical marks so 'ΟΧΉΜΑΤΑ' matches keyword 'ΟΧΗΜΑΤ'."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def _identify_section(table: list[list], cmap: dict) -> str:
    if not table or not table[0]:
        return UNKNOWN
    ncols = len(table[0])
    section = NCOLS_TO_SECTION.get(ncols, UNKNOWN)
    if not isinstance(section, list):
        return section

    # Ambiguous column count — scan rows 2-4 col 0 for the section title.
    # Row position varies: normally row 2, but can be row 3 when row 1 is blank.
    # Skip the declaration serial row ("ΑΡΙΘΜΟΣ ΔΗΛΩΣΗΣ") and form header
    # ("ΔΗΛΩΣΗ ΠΕΡΙΟΥΣΙΑΚΗΣ ΚΑΤΑΣΤΑΣΗΣ") — those are never section titles.
    _SKIP_PHRASES = ("ΑΡΙΘΜΟΣ ΔΗΛΩΣΗΣ", "ΔΗΛΩΣΗ ΠΕΡΙΟΥΣΙΑΚΗΣ")

    title_raw = ""
    for row_idx in range(2, min(5, len(table))):
        candidate = _cell(table[row_idx], 0)
        if not candidate:
            continue
        candidate_up = _strip_accents(candidate).upper()
        if any(ph in candidate_up for ph in _SKIP_PHRASES):
            continue
        title_raw = candidate
        break

    if cmap and title_raw:
        title_decoded, _ = decode_text(title_raw, cmap)
    else:
        title_decoded = title_raw

    title_norm = _strip_accents(title_decoded).upper()
    for keyword, sec in SECTION_TITLE_KEYWORDS.items():
        if keyword in title_norm:
            return sec

    # Fallback: raw text (useful when CMap empty but title is readable)
    title_raw_norm = _strip_accents(title_raw).upper()
    for keyword, sec in SECTION_TITLE_KEYWORDS.items():
        if keyword in title_raw_norm:
            return sec

    return UNKNOWN


# ─── main entry point ─────────────────────────────────────────────────────────

def parse_pdf(
    pdf_path: str | Path,
    mp_id: int = 0,
    fiscal_year: int = 2024,
    use_cmap: bool = True,
) -> ParsedDeclaration:
    """
    Parse one asset-declaration PDF and return a ParsedDeclaration.

    Parameters
    ----------
    pdf_path : path to the downloaded PDF
    mp_id    : the numeric ID from the filename / mp_index table
    fiscal_year : fiscal year the declaration covers (default 2024)
    use_cmap : attempt per-PDF font CMap reconstruction for Greek text
    """
    result = ParsedDeclaration(mp_id=mp_id, fiscal_year=fiscal_year)
    cmap: dict[int, str] = {}

    # Attempt CMap reconstruction
    if use_cmap:
        try:
            cmap = build_cmap(pdf_path)
            result.cmap_glyphs_mapped = len(cmap)
        except TesseractUnavailable:
            result.errors.append("tesseract_unavailable: Greek text not decoded")
        except Exception as exc:
            result.errors.append(f"cmap_error: {exc}")

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue
            table = tables[0]   # Table 0 is always the main content table
            section = _identify_section(table, cmap)

            try:
                if section == HEADER:
                    _parse_header(table, page, result)
                elif section == INCOME:
                    _parse_income(table, cmap, result)
                elif section == VEHICLES:
                    _parse_vehicles(table, cmap, result)
                elif section == DEPOSITS:
                    _parse_deposits(table, cmap, result)
                elif section == REAL_ESTATE:
                    _parse_real_estate(table, cmap, result)
                elif section == BUSINESS_SHARES:
                    _parse_business_shares(table, cmap, result)
                elif section == LOANS_ASSETS:
                    _parse_loans_assets(table, cmap, result)
                elif section == LOANS_DETAIL:
                    _parse_loans_detail(table, cmap, result)
                elif section == SECURITIES:
                    _parse_securities(table, cmap, result)
                elif section == SAFE_DEPOSIT_BOXES:
                    _parse_safe_deposit_boxes(table, cmap, result)
                elif section == REAL_ESTATE_RIGHTS:
                    _parse_real_estate_rights(table, cmap, result)
                # UNKNOWN: intentionally skipped
            except Exception as exc:
                result.errors.append(f"page_error(section={section}): {exc}")
                result.fields_failed += 1

    result.needs_review = bool(result.errors) or result.fields_failed > 0
    return result


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Parse a single asset-declaration PDF.")
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument("--mp-id", type=int, default=0)
    parser.add_argument("--no-cmap", action="store_true", help="Skip CMap OCR")
    parser.add_argument("--out", default=None, help="Write JSON to this path")
    args = parser.parse_args(argv)

    result = parse_pdf(args.pdf, mp_id=args.mp_id, use_cmap=not args.no_cmap)
    data = result.to_dict()
    out_str = json.dumps(data, ensure_ascii=False, indent=2)

    if args.out:
        Path(args.out).write_text(out_str, encoding="utf-8")
        print(f"Written -> {args.out}")
    else:
        # Use sys.stdout.buffer to avoid cp1253 issues in the terminal
        sys.stdout.buffer.write(out_str.encode("utf-8"))
        sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
