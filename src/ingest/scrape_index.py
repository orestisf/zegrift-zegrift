"""
Scrape the Hellenic Parliament asset-declaration index pages and persist
each MP's entry into the mp_index table.

Supports multiple years (2022–2025+).  Uses Playwright for fetching because
the parliament site employs Akamai WAF that blocks plain ``requests``.
Falls back to ``requests`` if Playwright is not installed.

Usage:
    python -m src.ingest.scrape_index --year 2025              # single year
    python -m src.ingest.scrape_index --all-years              # all known years
    python -m src.ingest.scrape_index --all-years --out-csv .  # + per-year CSVs
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

DEFAULT_DB = Path(__file__).parent.parent.parent / "data" / "db" / "zegrift.sqlite"
SCHEMA_SQL = Path(__file__).parent.parent / "db" / "schema.sql"

_BASE = (
    "https://www.hellenicparliament.gr/Organosi-kai-Leitourgia/"
    "epitropi-elegxou-ton-oikonomikon-ton-komaton-kai-ton-vouleftwn"
)


# ─── per-year configuration ─────────────────────────────────────────────────

@dataclass(frozen=True)
class YearConfig:
    """All year-specific constants needed to scrape one index page."""
    year_label: int        # the "etos" number that appears in the URL (2022, 2023, …)
    fiscal_year: int       # the fiscal year the declaration covers (year_label − 1)
    index_url: str         # full URL of the index page
    pdf_suffix_re: re.Pattern  # regex to extract (name_part, mp_id) from a PDF filename
    pdf_dir: str           # subdirectory under data/pdfs/ (e.g. "2025")


def _build_year_config(year_label: int) -> YearConfig:
    """Build a YearConfig for a given year label.

    URL patterns observed:
      2022: …/Diloseis-Periousiakis-Katastasis2022/Ethsies-Diloseis-…2022
            PDF suffix: _2022.pdf  (no 'e')
      2023+: …/Diloseis-Periousiakis-Katastasis{Y}/Ethsies-Diloseis-…{Y}
             PDF suffix: _{Y}e.pdf
    """
    index_url = (
        f"{_BASE}/Diloseis-Periousiakis-Katastasis{year_label}/"
        f"Ethsies-Diloseis-Periousiakis-Katastasis{year_label}"
    )
    # 2022 PDFs use _2022.pdf (no trailing 'e'); 2023+ use _YYYYe.pdf
    if year_label <= 2022:
        suffix_re = re.compile(
            rf"/([^/]+)_(\d+)_{year_label}\.pdf$", re.IGNORECASE
        )
    else:
        suffix_re = re.compile(
            rf"/([^/]+)_(\d+)_{year_label}e\.pdf$", re.IGNORECASE
        )
    return YearConfig(
        year_label=year_label,
        fiscal_year=year_label - 1,
        index_url=index_url,
        pdf_suffix_re=suffix_re,
        pdf_dir=str(year_label),
    )


# All known years with declarations on the parliament site.
AVAILABLE_YEARS: list[int] = [2022, 2023, 2024, 2025]


# ─── helpers ────────────────────────────────────────────────────────────────

def _parse_mp_id_and_lat_names(
    pdf_url: str,
    suffix_re: re.Pattern,
) -> tuple[int, str, str]:
    """Return (mp_id, surname_lat, given_name_lat) from a PDF URL.

    The filename pattern is SURNAME_GIVENNAME_MPID_{suffix}.pdf where
    GIVENNAME may itself contain underscores for multi-part given names.
    """
    m = suffix_re.search(pdf_url)
    if not m:
        raise ValueError(f"Cannot parse mp_id from URL: {pdf_url}")
    name_part, mp_id_str = m.group(1), m.group(2)
    parts = name_part.split("_")
    surname_lat = parts[0]
    given_name_lat = " ".join(parts[1:]) if len(parts) > 1 else ""
    return int(mp_id_str), surname_lat, given_name_lat


# ─── page fetchers ──────────────────────────────────────────────────────────

def _fetch_html_playwright(url: str) -> str:
    """Fetch page HTML using Playwright (Edge) to bypass Akamai WAF."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            channel="msedge",
            args=["--start-minimized"],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="el-GR",
            timezone_id="Europe/Athens",
        )
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(3000)
        html = page.content()
        title = page.title()
        browser.close()

    if "Access Denied" in title:
        raise RuntimeError(f"Akamai WAF blocked access to {url}")
    return html


def _fetch_html_requests(url: str) -> str:
    """Fetch page HTML using requests (may be blocked by Akamai)."""
    import requests

    resp = requests.get(url, timeout=30, headers={"User-Agent": "zegrift/0.1"})
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def _fetch_html(url: str, use_playwright: bool = True) -> str:
    """Fetch page HTML, preferring Playwright, falling back to requests."""
    if use_playwright:
        try:
            return _fetch_html_playwright(url)
        except ImportError:
            print("  Playwright not available, falling back to requests",
                  file=sys.stderr)
        except RuntimeError as e:
            print(f"  Playwright blocked: {e}", file=sys.stderr)
            print("  Falling back to requests", file=sys.stderr)
    return _fetch_html_requests(url)


# ─── index parser ───────────────────────────────────────────────────────────

def fetch_index(
    cfg: YearConfig,
    *,
    use_playwright: bool = True,
) -> list[dict]:
    """Fetch one year's index page and return a list of MP dicts."""
    html = _fetch_html(cfg.index_url, use_playwright=use_playwright)
    soup = BeautifulSoup(html, "lxml")

    rows: list[dict] = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        a = tds[2].find("a", href=True)
        if not a:
            continue
        href = a["href"]
        if ".pdf" not in href.lower():
            continue

        # Ensure absolute URL and normalise to https
        if href.startswith("/"):
            href = "https://www.hellenicparliament.gr" + href
        href = href.replace("http://www.", "https://www.")

        try:
            mp_id, surname_lat, given_name_lat = _parse_mp_id_and_lat_names(
                href, cfg.pdf_suffix_re,
            )
        except ValueError as exc:
            print(f"  WARNING: {exc}", file=sys.stderr)
            continue

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
                "fiscal_year": cfg.fiscal_year,
                "year_label": cfg.year_label,
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


def save_index_json(
    rows: list[dict],
    out_dir: Path,
    year_label: int,
) -> Path:
    """Save the scraped index as a JSON file for archival."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{year_label}_index.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    return path


# ─── CLI ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Scrape MP declaration index pages into SQLite.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")

    grp = parser.add_mutually_exclusive_group(required=False)
    grp.add_argument(
        "--year", type=int, default=None,
        help=f"Scrape a single year (available: {AVAILABLE_YEARS})",
    )
    grp.add_argument(
        "--all-years", action="store_true",
        help="Scrape all available years",
    )

    parser.add_argument("--out-csv", default=None,
                        help="Write per-year CSVs to this directory")
    parser.add_argument("--out-json", default=None,
                        help="Write per-year index JSONs to this directory (archival)")
    parser.add_argument("--no-playwright", action="store_true",
                        help="Use requests instead of Playwright (may be blocked)")
    args = parser.parse_args(argv)

    # Determine which years to scrape
    if args.all_years:
        years = AVAILABLE_YEARS
    elif args.year:
        if args.year not in AVAILABLE_YEARS:
            print(f"ERROR: year {args.year} not in {AVAILABLE_YEARS}", file=sys.stderr)
            sys.exit(1)
        years = [args.year]
    else:
        # Default: latest year (backward compatible)
        years = [AVAILABLE_YEARS[-1]]

    db_path = Path(args.db)
    con = open_db(db_path)
    scraped_at = datetime.now(timezone.utc).isoformat()
    use_pw = not args.no_playwright

    grand_total = 0
    for year_label in years:
        cfg = _build_year_config(year_label)
        print(f"\nScraping year {year_label} (fiscal {cfg.fiscal_year}) ...")
        print(f"  URL: {cfg.index_url}")

        try:
            rows = fetch_index(cfg, use_playwright=use_pw)
        except Exception as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)
            continue

        print(f"  Found {len(rows)} entries")
        if not rows:
            print("  WARNING: no rows parsed — check page structure", file=sys.stderr)
            continue

        n = upsert_rows(con, rows, scraped_at)
        print(f"  Upserted {n} rows -> {db_path}")
        grand_total += len(rows)

        # Optional archival JSON
        if args.out_json:
            p = save_index_json(rows, Path(args.out_json), year_label)
            print(f"  Index JSON -> {p}")

        # Optional CSV
        if args.out_csv:
            csv_dir = Path(args.out_csv)
            csv_dir.mkdir(parents=True, exist_ok=True)
            csv_path = csv_dir / f"{year_label}_index.csv"
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            print(f"  CSV -> {csv_path}")

    con.close()
    print(f"\nTotal: {grand_total} entries across {len(years)} year(s)")


if __name__ == "__main__":
    main()
