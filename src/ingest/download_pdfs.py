"""
Download all MP asset-declaration PDFs listed in the mp_index table.

Supports multiple years (2022–2025+).  Uses Playwright's in-page fetch()
to bypass Akamai WAF.  Falls back to plain ``requests`` if Playwright is
not installed or --no-playwright is given.

Features:
  - Skips files already on disk whose size matches content-length (resume).
  - Throttles to ~2 req/s; exponential backoff on 429/5xx.
  - Records sha256, content-length, fetched_at in the pdf_file table.
  - Prints a summary report at the end.

Usage:
    python -m src.ingest.download_pdfs --year 2025                    # single year
    python -m src.ingest.download_pdfs --all-years                    # all years
    python -m src.ingest.download_pdfs --year 2022 --limit 10        # smoke test
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
from tqdm import tqdm

from src.ingest.scrape_index import (
    AVAILABLE_YEARS, _build_year_config, YearConfig,
    DEFAULT_DB,
)

DEFAULT_OUT = Path(__file__).parent.parent.parent / "data" / "pdfs"
THROTTLE_S = 0.5   # seconds between requests (~2 req/s)
CHUNK_SIZE = 1 << 15  # 32 KB read chunks


# ─── HTTP (requests-based, may be blocked by Akamai) ─────────────────────────

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
def _download_one_requests(session: requests.Session, url: str, dest: Path) -> tuple[int, str]:
    """Download url to dest using requests. Returns (content_length, sha256hex)."""
    resp = session.get(url, timeout=30, stream=True)
    if resp.status_code == 429:
        resp.raise_for_status()  # triggers retry
    resp.raise_for_status()

    h = hashlib.sha256()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    try:
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(CHUNK_SIZE):
                f.write(chunk)
                h.update(chunk)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    return dest.stat().st_size, h.hexdigest()


# ─── Playwright-based batch downloader ──────────────────────────────────────

def _download_batch_playwright(
    rows: list[tuple[int, str, str, str, int]],
    out_dir: Path,
    con: sqlite3.Connection,
    throttle: float = THROTTLE_S,
) -> tuple[int, int, list]:
    """Download PDFs using Playwright's in-page fetch() to bypass Akamai.

    Parameters
    ----------
    rows : list of (mp_id, surname_lat, given_name_lat, pdf_url, fiscal_year)
    out_dir : base directory, PDFs saved under out_dir/{year_label}/
    con : SQLite connection for recording downloads
    throttle : seconds to wait between downloads

    Returns (ok_count, failed_count, failures_list)
    """
    from playwright.sync_api import sync_playwright

    ok = 0
    failed = 0
    failures: list[tuple[int, str, str]] = []

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

        # Group by year_label from URL to warm up sessions per index page
        # Determine year_label from the URL directory pattern
        current_year = None

        for mp_id, surname_lat, given_name_lat, pdf_url, fiscal_year in tqdm(rows, unit="pdf"):
            year_label = fiscal_year + 1
            slug = f"{surname_lat}_{given_name_lat}".replace(" ", "_")
            year_dir = out_dir / str(year_label)
            year_dir.mkdir(parents=True, exist_ok=True)
            dest = year_dir / f"{mp_id}_{slug}.pdf"

            if dest.exists() and dest.stat().st_size > 1000:
                continue

            # Visit index page if we changed years (warm up session cookies)
            if current_year != year_label:
                cfg = _build_year_config(year_label)
                tqdm.write(f"  Warming up session for year {year_label}...")
                try:
                    page.goto(cfg.index_url, wait_until="networkidle", timeout=60_000)
                    page.wait_for_timeout(3000)
                    if "Access Denied" in page.title():
                        tqdm.write(f"  WARNING: Index page blocked for {year_label}",
                                   file=sys.stderr)
                except Exception as e:
                    tqdm.write(f"  WARNING: Could not load index: {e}", file=sys.stderr)
                current_year = year_label

            # Download via in-page fetch()
            try:
                result = page.evaluate("""async (url) => {
                    try {
                        const resp = await fetch(url, { credentials: 'include' });
                        if (!resp.ok) return { error: 'HTTP ' + resp.status };
                        const blob = await resp.blob();
                        const buffer = await blob.arrayBuffer();
                        const bytes = new Uint8Array(buffer);
                        let binary = '';
                        // Process in chunks to avoid call stack overflow
                        const chunkSize = 8192;
                        for (let i = 0; i < bytes.length; i += chunkSize) {
                            const slice = bytes.subarray(i, Math.min(i + chunkSize, bytes.length));
                            binary += String.fromCharCode.apply(null, slice);
                        }
                        return { size: blob.size, b64: btoa(binary) };
                    } catch (e) {
                        return { error: e.message };
                    }
                }""", pdf_url)

                if "error" in result:
                    raise RuntimeError(result["error"])

                data = base64.b64decode(result["b64"])
                if data[:5] != b"%PDF-":
                    raise RuntimeError("Response is not a PDF")

                dest.write_bytes(data)
                sha = hashlib.sha256(data).hexdigest()
                _record(con, mp_id, dest, len(data), sha, fiscal_year)
                ok += 1

            except Exception as exc:
                failed += 1
                failures.append((mp_id, pdf_url, str(exc)))
                tqdm.write(f"FAIL mp_id={mp_id}: {exc}", file=sys.stderr)

            time.sleep(throttle)

        browser.close()

    return ok, failed, failures


# ─── DB helpers ──────────────────────────────────────────────────────────────

def _already_downloaded(con: sqlite3.Connection, mp_id: int, fiscal_year: int) -> bool:
    row = con.execute(
        "SELECT content_length, path FROM pdf_file WHERE mp_id=? AND fiscal_year=?",
        (mp_id, fiscal_year),
    ).fetchone()
    if not row:
        return False
    content_length, path = row
    p = Path(path)
    return p.exists() and p.stat().st_size == content_length


def _record(con: sqlite3.Connection, mp_id: int, path: Path, size: int,
            sha: str, fiscal_year: int) -> None:
    with con:
        con.execute(
            """
            INSERT INTO pdf_file (mp_id, fiscal_year, path, sha256, content_length, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(mp_id, fiscal_year) DO UPDATE SET
                path=excluded.path, sha256=excluded.sha256,
                content_length=excluded.content_length, fetched_at=excluded.fetched_at
            """,
            (mp_id, fiscal_year, str(path), sha, size,
             datetime.now(timezone.utc).isoformat()),
        )


# ─── Main ────────────────────────────────────────────────────────────────────

def download_all(
    db_path: Path,
    out_dir: Path,
    years: list[int] | None = None,
    limit: int | None = None,
    use_playwright: bool = True,
) -> None:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")

    # Build the list of (mp_id, surname, given, url, fiscal_year) to download
    query = "SELECT mp_id, surname_lat, given_name_lat, pdf_url FROM mp_index ORDER BY mp_id"
    all_rows = con.execute(query).fetchall()

    # Map mp_id → fiscal_year from pdf_url pattern
    def _fiscal_year_from_url(url: str) -> int:
        """Extract fiscal year from URL directory pattern like xrhsh2024_etos2025."""
        m = re.search(r"xrhsh(\d{4})_etos", url)
        if m:
            return int(m.group(1))
        # Fallback: extract from suffix
        m = re.search(r"_(\d{4})e?\.pdf$", url, re.IGNORECASE)
        if m:
            return int(m.group(1)) - 1
        return 2024  # default

    rows_with_fy = []
    for mp_id, surname, given, url in all_rows:
        fy = _fiscal_year_from_url(url)
        if years and (fy + 1) not in years:
            continue
        if _already_downloaded(con, mp_id, fy):
            continue
        rows_with_fy.append((mp_id, surname, given, url, fy))

    if limit:
        rows_with_fy = rows_with_fy[:limit]

    total_to_download = len(rows_with_fy)
    print(f"PDFs to download: {total_to_download}")

    if total_to_download == 0:
        print("Nothing to download.")
        con.close()
        return

    if use_playwright:
        try:
            ok, failed, failures = _download_batch_playwright(
                rows_with_fy, out_dir, con,
            )
            con.close()
            print(f"\nDone: {ok} downloaded | {failed} failed")
            if failures:
                print(f"\nFailed ({len(failures)}):")
                for mp_id, url, err in failures[:20]:
                    print(f"  mp_id={mp_id}  {url}\n    {err}")
            return
        except ImportError:
            print("Playwright not available, falling back to requests",
                  file=sys.stderr)

    # requests-based fallback
    session = requests.Session()
    session.headers["User-Agent"] = "zegrift/0.1"

    ok = skipped = failed = 0
    failures: list[tuple[int, str, str]] = []

    for mp_id, surname_lat, given_name_lat, pdf_url, fiscal_year in tqdm(rows_with_fy, unit="pdf"):
        year_label = fiscal_year + 1
        slug = f"{surname_lat}_{given_name_lat}".replace(" ", "_")
        year_dir = out_dir / str(year_label)
        year_dir.mkdir(parents=True, exist_ok=True)
        dest = year_dir / f"{mp_id}_{slug}.pdf"

        try:
            size, sha = _download_one_requests(session, pdf_url, dest)
            _record(con, mp_id, dest, size, sha, fiscal_year)
            ok += 1
        except Exception as exc:
            failed += 1
            failures.append((mp_id, pdf_url, str(exc)))
            tqdm.write(f"FAIL mp_id={mp_id}: {exc}", file=sys.stderr)

        time.sleep(THROTTLE_S)

    con.close()

    print(f"\nDone: {ok} downloaded | {failed} failed")
    if failures:
        print(f"\nFailed downloads ({len(failures)}):")
        for mp_id, url, err in failures[:20]:
            print(f"  mp_id={mp_id}  {url}\n    {err}")
        if len(failures) > 20:
            print(f"  ... and {len(failures)-20} more")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Download MP asset-declaration PDFs.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Download only the first N PDFs (for testing)",
    )

    grp = parser.add_mutually_exclusive_group(required=False)
    grp.add_argument(
        "--year", type=int, default=None,
        help=f"Download PDFs for a single year (available: {AVAILABLE_YEARS})",
    )
    grp.add_argument(
        "--all-years", action="store_true",
        help="Download PDFs for all available years",
    )

    parser.add_argument("--no-playwright", action="store_true",
                        help="Use requests instead of Playwright (may be blocked by Akamai)")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path} -- run scrape_index first", file=sys.stderr)
        sys.exit(1)

    # Determine which years
    if args.all_years:
        years = AVAILABLE_YEARS
    elif args.year:
        years = [args.year]
    else:
        years = None  # all years in the index

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading PDFs to {out_dir} ...")
    download_all(
        db_path, out_dir,
        years=years,
        limit=args.limit,
        use_playwright=not args.no_playwright,
    )


if __name__ == "__main__":
    main()
