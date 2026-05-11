"""
Scrape the Hellenic Parliament 2024 asset-declaration index and persist
each MP's entry into the mp_index table.

Usage:
    python -m src.ingest.scrape_index [--db PATH] [--out-csv PATH]
"""

import argparse
import csv
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

INDEX_URL = (
    "https://www.hellenicparliament.gr/Organosi-kai-Leitourgia/"
    "epitropi-elegxou-ton-oikonomikon-ton-komaton-kai-ton-vouleftwn/"
    "Diloseis-Periousiakis-Katastasis2025/"
    "Ethsies-Diloseis-Periousiakis-Katastasis2025"
)
# PDF filenames follow: SURNAME_GIVENNAME_MPID_2025e.pdf
# The mp_id is the last numeric segment before _2025e.
_PDF_ID_RE = re.compile(r"/([^/]+)_(\d+)_2025e\.pdf$", re.IGNORECASE)
_FISCAL_YEAR = 2024

DEFAULT_DB = Path(__file__).parent.parent.parent / "data" / "db" / "zegrift.sqlite"
SCHEMA_SQL = Path(__file__).parent.parent / "db" / "schema.sql"


# ─── helpers ────────────────────────────────────────────────────────────────

def _parse_mp_id_and_lat_names(pdf_url: str) -> tuple[int, str, str]:
    """Return (mp_id, surname_lat, given_name_lat) from a PDF URL.

    The filename pattern is SURNAME_GIVENNAME_MPID_2025e.pdf where GIVENNAME
    may itself contain underscores for multi-part given names.
    """
    m = _PDF_ID_RE.search(pdf_url)
    if not m:
        raise ValueError(f"Cannot parse mp_id from URL: {pdf_url}")
    name_part, mp_id_str = m.group(1), m.group(2)
    # name_part = "SURNAME_GIVEN" or "SURNAME_GIVEN_MIDDLE"
    parts = name_part.split("_")
    surname_lat = parts[0]
    given_name_lat = " ".join(parts[1:]) if len(parts) > 1 else ""
    return int(mp_id_str), surname_lat, given_name_lat


def fetch_index(url: str = INDEX_URL) -> list[dict]:
    """Fetch the index page and return a list of MP dicts."""
    resp = requests.get(url, timeout=30, headers={"User-Agent": "zegrift/0.1"})
    resp.raise_for_status()
    # Page is Windows-1253 encoded; requests may mis-detect as ISO-8859
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")

    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        a = tds[2].find("a", href=True)
        if not a:
            continue
        href = a["href"]
        if "2025e.pdf" not in href.lower():
            continue

        # Ensure absolute URL
        if href.startswith("/"):
            href = "https://www.hellenicparliament.gr" + href

        try:
            mp_id, surname_lat, given_name_lat = _parse_mp_id_and_lat_names(href)
        except ValueError as exc:
            print(f"WARNING: {exc}", file=sys.stderr)
            continue

        # Table cell text — may be Greek script or Latin depending on section
        surname_gr = tds[0].get_text(strip=True)
        given_name_gr = tds[1].get_text(strip=True)

        rows.append(
            {
                "mp_id": mp_id,
                "surname_gr": surname_gr,
                "given_name_gr": given_name_gr,
                "surname_lat": surname_lat,
                "given_name_lat": given_name_lat,
                "pdf_url": href,
            }
        )

    return rows


# ─── persistence ────────────────────────────────────────────────────────────

def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    con.executescript(schema)
    return con


def upsert_rows(con: sqlite3.Connection, rows: list[dict], scraped_at: str) -> int:
    inserted = 0
    with con:
        for r in rows:
            cur = con.execute(
                """
                INSERT INTO mp_index
                    (mp_id, surname_gr, given_name_gr, surname_lat, given_name_lat,
                     pdf_url, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mp_id) DO UPDATE SET
                    surname_gr    = excluded.surname_gr,
                    given_name_gr = excluded.given_name_gr,
                    surname_lat   = excluded.surname_lat,
                    given_name_lat= excluded.given_name_lat,
                    pdf_url       = excluded.pdf_url,
                    scraped_at    = excluded.scraped_at
                """,
                (
                    r["mp_id"],
                    r["surname_gr"],
                    r["given_name_gr"],
                    r["surname_lat"],
                    r["given_name_lat"],
                    r["pdf_url"],
                    scraped_at,
                ),
            )
            inserted += cur.rowcount
    return inserted


# ─── CLI ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Scrape MP index into SQLite.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    parser.add_argument("--out-csv", default=None, help="Also write a CSV for inspection")
    args = parser.parse_args(argv)

    print(f"Fetching {INDEX_URL} ...")
    rows = fetch_index()
    print(f"Found {len(rows)} entries")

    if not rows:
        print("ERROR: no rows parsed — check page structure", file=sys.stderr)
        sys.exit(1)

    scraped_at = datetime.now(timezone.utc).isoformat()
    db_path = Path(args.db)
    con = open_db(db_path)
    n = upsert_rows(con, rows, scraped_at)
    con.close()
    print(f"Upserted {n} rows -> {db_path}")

    if args.out_csv:
        with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"CSV written -> {args.out_csv}")


if __name__ == "__main__":
    main()
