"""
Vouliwatch fiscal-data API client + itemized ingester.

The ingest runs in two phases:

  Phase 1 — identity:
    Fetches /api/home (member roster) and /api/member/{slug} for each MP.
    Upserts vouli_member (identity) and vouli_fiscal_year (year-level totals).
    Caches every raw HTTP response on disk for re-use.

  Phase 2 — itemized:
    Re-reads the cached /member/{slug} responses (no network) and, for every
    member already linked to an mp_id via mp_link, creates a synthetic
    `declaration` row with parser_version='vouliwatch' plus itemized child
    rows in: income_line, real_estate, deposit, loan, business_share,
    vehicle, security_holding, safe_deposit_box.

Order of operations end-to-end:
    1.  python -m src.ingest.vouliwatch --phase identity
    2.  python -m src.link.match
    3.  python -m src.ingest.vouliwatch --phase itemized

(or run --phase all to do 1 + 3 in a single invocation, but linking still
needs to happen between them — `all` re-reads from cache after fetching.)

Rate limit: 100 req/min — we stay at ~90 req/min.

Usage:
    python -m src.ingest.vouliwatch [--db PATH] [--cache-dir PATH]
                                    [--limit N] [--phase identity|itemized|all]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
from tqdm import tqdm

BASE_URL = "https://pothenesxes.vouliwatch.gr/api"
DEFAULT_DB = Path(__file__).parent.parent.parent / "data" / "db" / "zegrift.sqlite"
DEFAULT_CACHE = Path(__file__).parent.parent.parent / "data" / "api_cache"
THROTTLE_S = 60 / 90   # ~0.67 s between requests → 90 req/min

VOULIWATCH_PARSER_VERSION = "vouliwatch"


# ─── HTTP with cache ──────────────────────────────────────────────────────────

def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in {429, 500, 502, 503, 504}
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _get(session: requests.Session, url: str, cache_dir: Path) -> Any:
    """GET url, cache response on disk, return parsed JSON."""
    key = hashlib.md5(url.encode()).hexdigest()
    cache_path = cache_dir / f"{key}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _cache_path(cache_dir: Path, url: str) -> Path:
    return cache_dir / f"{hashlib.md5(url.encode()).hexdigest()}.json"


# ─── identity-table helpers ───────────────────────────────────────────────────

def _upsert_member(con: sqlite3.Connection, slug: str, first: str, last: str,
                   party: str | None, raw: str) -> None:
    with con:
        con.execute(
            """
            INSERT INTO vouli_member (slug, surname, given_name, party, raw_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                surname=excluded.surname, given_name=excluded.given_name,
                party=excluded.party, raw_json=excluded.raw_json
            """,
            (slug, last.strip(), first.strip(), party, raw),
        )


def _upsert_fiscal_year(con: sqlite3.Connection, slug: str, year: int,
                        total_wealth: float | None, raw: str) -> None:
    with con:
        con.execute(
            """
            INSERT INTO vouli_fiscal_year (slug, fiscal_year, total_wealth, raw_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(slug, fiscal_year) DO UPDATE SET
                total_wealth=excluded.total_wealth, raw_json=excluded.raw_json
            """,
            (slug, year, total_wealth, raw),
        )


def _total_wealth(fy: dict) -> float | None:
    """Sum the components we have; return None if all missing."""
    parts = [
        fy.get("total_revenue"), fy.get("total_deposits"),
        fy.get("total_stocks"), fy.get("total_companies"),
    ]
    nums = []
    for p in parts:
        if p is not None and p != "None":
            try:
                nums.append(float(p))
            except (ValueError, TypeError):
                pass
    return sum(nums) if nums else None


def _party_name(fy: dict) -> str | None:
    pp = fy.get("politicalparty")
    if isinstance(pp, dict):
        return pp.get("name")
    return None


# ─── Phase 1: identity ingest ─────────────────────────────────────────────────

def ingest_identity(
    db_path: Path,
    cache_dir: Path,
    limit: int | None = None,
) -> None:
    """Fetch roster + per-member detail; upsert vouli_member + vouli_fiscal_year."""
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")

    session = requests.Session()
    session.headers["User-Agent"] = "zegrift/0.1"
    session.headers["Accept"] = "application/json"

    print("Phase 1 (identity): fetching member roster from /api/home ...")
    home = _get(session, f"{BASE_URL}/home", cache_dir)
    members = home.get("members", [])
    if limit:
        members = members[:limit]
    print(f"  {len(members)} members to process")

    ok = failed = 0
    for m in tqdm(members, unit="member"):
        slug = m.get("slug", "")
        if not slug:
            continue

        first = m.get("first_name", "")
        last = m.get("last_name", "")
        _upsert_member(con, slug, first, last, None, json.dumps(m, ensure_ascii=False))

        url = f"{BASE_URL}/member/{slug}"
        was_cached = _cache_path(cache_dir, url).exists()
        try:
            detail = _get(session, url, cache_dir)
            member_full = detail.get("member", {})
            fiscal_years: list[dict] = member_full.get("fiscal", []) or []

            for fy in fiscal_years:
                year = fy.get("year_of_use")
                try:
                    year = int(year) if year is not None else None
                except (ValueError, TypeError):
                    year = None
                if year is None:
                    continue

                total = _total_wealth(fy)
                party = _party_name(fy)
                if party:
                    _upsert_member(con, slug, first, last, party,
                                   json.dumps(m, ensure_ascii=False))

                _upsert_fiscal_year(
                    con, slug, year, total,
                    json.dumps(fy, ensure_ascii=False),
                )

            ok += 1
        except Exception as exc:
            tqdm.write(f"FAIL {slug}: {exc}", file=sys.stderr)
            failed += 1

        # Only throttle on actual network hits
        if not was_cached:
            time.sleep(THROTTLE_S)

    con.close()
    print(f"  Phase 1 done: {ok} members, {failed} failed")


# ─── Phase 2: itemized ingest ─────────────────────────────────────────────────

def _to_float(v: Any) -> float | None:
    if v is None or v == "" or v == "None":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_int(v: Any) -> int | None:
    if v is None or v == "" or v == "None":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return None


def _to_partner(v: Any) -> int:
    """Vouliwatch's `partner`: numeric 0/1 (0 = obligor, 1 = spouse)."""
    if v is None:
        return 0
    try:
        return 1 if int(v) else 0
    except (ValueError, TypeError):
        return 0


def _to_year(v: Any) -> int | None:
    """Date strings like '2002', '2016-07-17'."""
    if v is None or v == "":
        return None
    s = str(v)[:4]
    try:
        y = int(s)
        if 1900 <= y <= 2100:
            return y
    except ValueError:
        pass
    return None


def _upsert_vouli_declaration(con: sqlite3.Connection, mp_id: int,
                              year: int, fy: dict) -> int:
    """Create or update a synthetic declaration row. Returns decl_id."""
    fiscal_data_id = fy.get("id")
    serial = f"vouliwatch:{fiscal_data_id}" if fiscal_data_id else None
    cur = con.execute(
        """
        INSERT INTO declaration
            (mp_id, fiscal_year, declaration_serial, submitted_at,
             parser_version, parsed_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(mp_id, fiscal_year, parser_version) DO UPDATE SET
            declaration_serial = excluded.declaration_serial,
            parsed_at          = excluded.parsed_at
        RETURNING decl_id
        """,
        (mp_id, year, serial, None, VOULIWATCH_PARSER_VERSION),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    return con.execute(
        "SELECT decl_id FROM declaration WHERE mp_id=? AND fiscal_year=? AND parser_version=?",
        (mp_id, year, VOULIWATCH_PARSER_VERSION),
    ).fetchone()[0]


_METHOD = "vouliwatch"
_CONF = 1.0


def _itemize_fiscal_year(con: sqlite3.Connection, decl_id: int, fy: dict) -> int:
    """Insert child rows for one Vouliwatch fiscal-year record. Returns rows inserted."""
    n = 0

    # revenues[] → income_line
    for i, r in enumerate(fy.get("revenues") or []):
        con.execute(
            """INSERT INTO income_line
               (decl_id, row_index, source, kind, amount_eur, currency, partner,
                extraction_method, confidence)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (decl_id, i, r.get("source"), None, _to_float(r.get("amount")),
             r.get("currency"), _to_partner(r.get("partner")), _METHOD, _CONF),
        )
        n += 1

    # properties[] → real_estate
    for i, r in enumerate(fy.get("properties") or []):
        con.execute(
            """INSERT INTO real_estate
               (decl_id, row_index, kind, share_pct, area_m2, landsize_m2,
                location_raw, location_decoded, country, acquisition_year,
                value_eur, rights, acquisition_method, swimming_pool,
                currency, partner, extraction_method, confidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (decl_id, i,
             r.get("type"),
             _to_float(r.get("percent")),
             _to_float(r.get("size")),
             _to_float(r.get("landsize")),
             None, r.get("country"),
             r.get("country"),
             _to_year(r.get("date")),
             _to_float(r.get("cost")),
             r.get("rights"),
             r.get("howgetit"),
             _to_float(r.get("swimming_pool")),
             r.get("currency"),
             _to_partner(r.get("partner")),
             _METHOD, _CONF),
        )
        n += 1

    # deposits[] → deposit
    for i, r in enumerate(fy.get("deposits") or []):
        con.execute(
            """INSERT INTO deposit
               (decl_id, row_index, bank, account_type, balance_eur,
                beneficiaries, country, currency, partner,
                extraction_method, confidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (decl_id, i,
             r.get("idrima"),
             r.get("row"),
             _to_float(r.get("cost")),
             _to_int(r.get("beneficiaries")),
             r.get("country"),
             r.get("currency"),
             _to_partner(r.get("partner")),
             _METHOD, _CONF),
        )
        n += 1

    # loans[] → loan
    for i, r in enumerate(fy.get("loans") or []):
        con.execute(
            """INSERT INTO loan
               (decl_id, row_index, lender, kind, original_amount_eur,
                outstanding_eur, start_date, end_date, currency, partner,
                extraction_method, confidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (decl_id, i,
             r.get("from"),
             None,
             _to_float(r.get("initial")),
             _to_float(r.get("actual")),
             r.get("from_date"),
             r.get("to_date"),
             r.get("currency"),
             _to_partner(r.get("partner")),
             _METHOD, _CONF),
        )
        n += 1

    # companies[] → business_share
    for i, r in enumerate(fy.get("companies") or []):
        con.execute(
            """INSERT INTO business_share
               (decl_id, row_index, company, share_pct, value_eur,
                participation_type, business_type, state,
                initial_capital_eur, purchase_value_eur, sale_value_eur,
                start_year, currency, partner,
                extraction_method, confidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (decl_id, i,
             r.get("name"),
             None,                                    # share_pct (not in API)
             _to_float(r.get("cost")),                # book value
             r.get("type"),                           # participation_type
             r.get("company_type"),
             r.get("state"),
             _to_float(r.get("initial_capital")),
             _to_float(r.get("buy_value")),
             _to_float(r.get("sell_value")),
             _to_int(r.get("year")),
             r.get("currency"),
             _to_partner(r.get("partner")),
             _METHOD, _CONF),
        )
        n += 1

    # mobile_properties[] → vehicle
    for i, r in enumerate(fy.get("mobile_properties") or []):
        cc = r.get("cc")
        con.execute(
            """INSERT INTO vehicle
               (decl_id, row_index, make, model, year, value_eur,
                ownership_pct, acquisition_method, state, partner,
                extraction_method, confidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (decl_id, i,
             r.get("type"),
             str(cc) if cc is not None else None,
             _to_year(r.get("date")),
             None,                                    # value_eur not in API
             _to_float(r.get("owner")),
             r.get("howgetit"),
             r.get("state"),
             _to_partner(r.get("partner")),
             _METHOD, _CONF),
        )
        n += 1

    # pmproducts[] → security_holding
    for i, r in enumerate(fy.get("pmproducts") or []):
        con.execute(
            """INSERT INTO security_holding
               (decl_id, row_index, instrument, title, quantity,
                acquisition_value_eur, value_eur, sale_value_eur,
                state, currency, partner,
                extraction_method, confidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (decl_id, i,
             r.get("row"),
             r.get("title"),
             _to_float(r.get("quantity")),
             _to_float(r.get("cost_buy")),
             _to_float(r.get("cost")),
             _to_float(r.get("cost_sell")),
             r.get("state"),
             r.get("currency"),
             _to_partner(r.get("partner")),
             _METHOD, _CONF),
        )
        n += 1

    # lockers[] → safe_deposit_box
    # (no populated sample seen — defensive field access)
    for i, r in enumerate(fy.get("lockers") or []):
        con.execute(
            """INSERT INTO safe_deposit_box
               (decl_id, row_index, institution, country, rental_year,
                beneficiaries, partner, extraction_method, confidence)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (decl_id, i,
             r.get("idrima") or r.get("institution"),
             r.get("country"),
             _to_year(r.get("rental_year") or r.get("year") or r.get("date")),
             r.get("beneficiaries"),
             _to_partner(r.get("partner")),
             _METHOD, _CONF),
        )
        n += 1

    return n


def ingest_itemized(db_path: Path, cache_dir: Path) -> None:
    """Phase 2: read cached member responses; insert declaration + child rows."""
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")

    slug_to_mp = dict(con.execute("SELECT vouli_slug, mp_id FROM mp_link").fetchall())
    if not slug_to_mp:
        print("Phase 2: mp_link is empty — run `python -m src.link.match` first.",
              file=sys.stderr)
        con.close()
        return

    print(f"Phase 2 (itemized): {len(slug_to_mp)} linked members")

    decls = rows = skipped = 0
    for slug, mp_id in tqdm(sorted(slug_to_mp.items()), unit="member"):
        cache_path = _cache_path(cache_dir, f"{BASE_URL}/member/{slug}")
        if not cache_path.exists():
            skipped += 1
            continue
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        member_full = data.get("member", {})
        fiscal: list[dict] = member_full.get("fiscal", []) or []

        for fy in fiscal:
            year = fy.get("year_of_use")
            try:
                year = int(year) if year is not None else None
            except (ValueError, TypeError):
                year = None
            if year is None:
                continue
            with con:
                decl_id = _upsert_vouli_declaration(con, mp_id, year, fy)
                # Wipe prior child rows for idempotent re-runs
                for tbl in ("income_line", "vehicle", "deposit", "real_estate",
                            "business_share", "loan", "security_holding",
                            "safe_deposit_box"):
                    con.execute(f"DELETE FROM {tbl} WHERE decl_id=?", (decl_id,))
                rows += _itemize_fiscal_year(con, decl_id, fy)
                decls += 1

    con.close()
    print(f"  Phase 2 done: {decls} declarations, {rows} child rows, "
          f"{skipped} cache misses")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N members (Phase 1 only)")
    parser.add_argument("--phase", choices=("identity", "itemized", "all"),
                        default="all",
                        help="Which phase to run (default: all)")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    cache_dir = Path(args.cache_dir)

    if args.phase in ("identity", "all"):
        ingest_identity(db_path, cache_dir, limit=args.limit)
    if args.phase in ("itemized", "all"):
        ingest_itemized(db_path, cache_dir)


if __name__ == "__main__":
    main()
