"""
Download all MP asset-declaration PDFs listed in the mp_index table.

Features:
  - Skips files already on disk whose size matches content-length (resume).
  - Throttles to ~2 req/s; exponential backoff on 429/5xx.
  - Records sha256, content-length, fetched_at in the pdf_file table.
  - Prints a summary report at the end.

Usage:
    python -m src.ingest.download_pdfs [--db PATH] [--out-dir PATH] [--limit N]
"""

import argparse
import hashlib
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
from tqdm import tqdm

FISCAL_YEAR = 2024
DEFAULT_DB = Path(__file__).parent.parent.parent / "data" / "db" / "zegrift.sqlite"
DEFAULT_OUT = Path(__file__).parent.parent.parent / "data" / "pdfs" / "2025"
THROTTLE_S = 0.5   # seconds between requests (~2 req/s)
CHUNK_SIZE = 1 << 15  # 32 KB read chunks


# ─── HTTP ────────────────────────────────────────────────────────────────────

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
def _download_one(session: requests.Session, url: str, dest: Path) -> tuple[int, str]:
    """Download url to dest. Returns (content_length, sha256hex)."""
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


# ─── DB helpers ──────────────────────────────────────────────────────────────

def _already_downloaded(con: sqlite3.Connection, mp_id: int) -> bool:
    row = con.execute(
        "SELECT content_length, path FROM pdf_file WHERE mp_id=? AND fiscal_year=?",
        (mp_id, FISCAL_YEAR),
    ).fetchone()
    if not row:
        return False
    content_length, path = row
    p = Path(path)
    return p.exists() and p.stat().st_size == content_length


def _record(con: sqlite3.Connection, mp_id: int, path: Path, size: int, sha: str) -> None:
    with con:
        con.execute(
            """
            INSERT INTO pdf_file (mp_id, fiscal_year, path, sha256, content_length, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(mp_id, fiscal_year) DO UPDATE SET
                path=excluded.path, sha256=excluded.sha256,
                content_length=excluded.content_length, fetched_at=excluded.fetched_at
            """,
            (mp_id, FISCAL_YEAR, str(path), sha, size, datetime.now(timezone.utc).isoformat()),
        )


# ─── Main ────────────────────────────────────────────────────────────────────

def download_all(
    db_path: Path,
    out_dir: Path,
    limit: int | None = None,
) -> None:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")

    query = "SELECT mp_id, surname_lat, given_name_lat, pdf_url FROM mp_index ORDER BY mp_id"
    rows = con.execute(query).fetchall()
    if limit:
        rows = rows[:limit]

    session = requests.Session()
    session.headers["User-Agent"] = "zegrift/0.1"

    ok = skipped = failed = 0
    failures: list[tuple[int, str, str]] = []

    for mp_id, surname_lat, given_name_lat, pdf_url in tqdm(rows, unit="pdf"):
        slug = f"{surname_lat}_{given_name_lat}".replace(" ", "_")
        dest = out_dir / f"{mp_id}_{slug}.pdf"

        if _already_downloaded(con, mp_id):
            skipped += 1
            continue

        try:
            size, sha = _download_one(session, pdf_url, dest)
            _record(con, mp_id, dest, size, sha)
            ok += 1
        except Exception as exc:
            failed += 1
            failures.append((mp_id, pdf_url, str(exc)))
            tqdm.write(f"FAIL mp_id={mp_id}: {exc}", file=sys.stderr)

        time.sleep(THROTTLE_S)

    con.close()

    total = ok + skipped + failed
    print(f"\nDone: {total} total | {ok} downloaded | {skipped} skipped | {failed} failed")
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
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path} -- run scrape_index first", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading PDFs to {out_dir} ...")
    download_all(db_path, out_dir, limit=args.limit)


if __name__ == "__main__":
    main()
