"""
Link mp_index entries (from the parliament PDF index) to vouli_member
slugs (from the Vouliwatch API) by normalizing and matching names.

Normalization strategy:
  - Both sides use the same Greek→Latin transliteration that the Hellenic
    Parliament applies to filenames (Α→A, Β→V, Γ→G, etc.).
  - Greek text from the API is in Unicode; filenames from the parliament
    are already in Latin (ASCII uppercase).
  - After transliteration, we compare (surname_norm, given_name_norm) pairs.

Three passes:
  1. Exact match on (surname_norm, given_name_norm).
  2. Exact match on surname_norm alone (for MPs whose given name differs
     slightly between the two sources).
  3. Fuzzy match (rapidfuzz token_sort_ratio) for remaining unmatched.
     Threshold: ≥ 85 → flagged 'fuzzy', requires human review.

Usage:
    python -m src.link.match [--db PATH] [--report PATH]
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz

DEFAULT_DB = Path(__file__).parent.parent.parent / "data" / "db" / "zegrift.sqlite"

# ─── Greek → Latin transliteration table ─────────────────────────────────────
# Mirrors the scheme Hellenic Parliament uses in PDF filenames.
# Greek capital → Latin capital.
_GR_TO_LAT: dict[str, str] = {
    "Α": "A",  "Β": "V",  "Γ": "G",  "Δ": "D",  "Ε": "E",
    "Ζ": "Z",  "Η": "I",  "Θ": "TH", "Ι": "I",  "Κ": "K",
    "Λ": "L",  "Μ": "M",  "Ν": "N",  "Ξ": "X",  "Ο": "O",
    "Π": "P",  "Ρ": "R",  "Σ": "S",  "Τ": "T",  "Υ": "Y",
    "Φ": "F",  "Χ": "CH", "Ψ": "PS", "Ω": "O",
    # Accented vowels → unaccented Latin
    "Ά": "A",  "Έ": "E",  "Ή": "I",  "Ί": "I",
    "Ό": "O",  "Ύ": "Y",  "Ώ": "O",
    # Lowercase
    "α": "A",  "β": "V",  "γ": "G",  "δ": "D",  "ε": "E",
    "ζ": "Z",  "η": "I",  "θ": "TH", "ι": "I",  "κ": "K",
    "λ": "L",  "μ": "M",  "ν": "N",  "ξ": "X",  "ο": "O",
    "π": "P",  "ρ": "R",  "σ": "S",  "ς": "S",  "τ": "T",
    "υ": "Y",  "φ": "F",  "χ": "CH", "ψ": "PS", "ω": "O",
    "ά": "A",  "έ": "E",  "ή": "I",  "ί": "I",  "ϊ": "I",
    "ΐ": "I",  "ό": "O",  "ύ": "Y",  "ϋ": "Y",  "ΰ": "Y",
    "ώ": "O",
}


def _transliterate(text: str) -> str:
    """Transliterate Greek characters to Latin uppercase."""
    result = []
    for ch in text:
        if ch in _GR_TO_LAT:
            result.append(_GR_TO_LAT[ch])
        else:
            # For Latin or other chars: strip diacritics, uppercase
            normalized = unicodedata.normalize("NFD", ch)
            ascii_ch = normalized.encode("ascii", "ignore").decode("ascii")
            result.append(ascii_ch.upper())
    return "".join(result)


def normalize_name(name: str) -> str:
    """
    Normalize a name for matching:
      - strip whitespace / punctuation
      - transliterate Greek to Latin
      - uppercase
      - collapse runs of spaces
    """
    name = name.strip()
    name = _transliterate(name)
    # Replace separators (hyphens, dots, commas) with space
    name = re.sub(r"[-.,/]", " ", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


# ─── loading ─────────────────────────────────────────────────────────────────

def load_mp_index(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        "SELECT mp_id, surname_lat, given_name_lat, surname_gr, given_name_gr FROM mp_index"
    ).fetchall()
    result = []
    for mp_id, sur_lat, giv_lat, sur_gr, giv_gr in rows:
        # Prefer the Greek-script name if it is actual Greek; else use the Latin
        if sur_gr and any("Α" <= c <= "Ω" or "α" <= c <= "ω" for c in sur_gr):
            sur_norm = normalize_name(sur_gr)
            giv_norm = normalize_name(giv_gr or "")
        else:
            sur_norm = normalize_name(sur_lat)
            giv_norm = normalize_name(giv_lat or "")
        result.append({
            "mp_id": mp_id,
            "sur_norm": sur_norm,
            "giv_norm": giv_norm,
            "display": f"{sur_lat} {giv_lat}",
        })
    return result


def load_vouli_members(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute("SELECT slug, surname, given_name FROM vouli_member").fetchall()
    result = []
    for slug, surname, given_name in rows:
        sur_norm = normalize_name(surname or "")
        giv_norm = normalize_name(given_name or "")
        result.append({
            "slug": slug,
            "sur_norm": sur_norm,
            "giv_norm": giv_norm,
            "full_norm": f"{sur_norm} {giv_norm}",
            "display": f"{surname} {given_name}",
        })
    return result


# ─── matching ─────────────────────────────────────────────────────────────────

def match_members(
    mp_list: list[dict],
    vouli_list: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Returns (links, unmatched_mps).

    Each link dict: {mp_id, vouli_slug, confidence, method}
    """
    # Build lookup indices for vouli
    exact_full: dict[tuple[str, str], str] = {}   # (sur, giv) -> slug
    by_surname: dict[str, list[str]] = {}          # sur -> [slug, ...]

    for v in vouli_list:
        key = (v["sur_norm"], v["giv_norm"])
        exact_full[key] = v["slug"]
        by_surname.setdefault(v["sur_norm"], []).append(v["slug"])

    links: list[dict] = []
    unmatched: list[dict] = []

    for mp in mp_list:
        sur, giv = mp["sur_norm"], mp["giv_norm"]

        # Pass 1: exact (surname, given)
        slug = exact_full.get((sur, giv))
        if slug:
            links.append({"mp_id": mp["mp_id"], "vouli_slug": slug,
                          "confidence": 1.0, "method": "exact_normalized"})
            continue

        # Pass 2: exact surname only (if unique match)
        candidates = by_surname.get(sur, [])
        if len(candidates) == 1:
            links.append({"mp_id": mp["mp_id"], "vouli_slug": candidates[0],
                          "confidence": 0.9, "method": "exact_surname"})
            continue

        # Pass 3: fuzzy — score full name against all vouli entries
        mp_full = f"{sur} {giv}"
        best_score = 0.0
        best_slug = None
        for v in vouli_list:
            score = fuzz.token_sort_ratio(mp_full, v["full_norm"]) / 100.0
            if score > best_score:
                best_score = score
                best_slug = v["slug"]

        if best_score >= 0.85 and best_slug:
            links.append({"mp_id": mp["mp_id"], "vouli_slug": best_slug,
                          "confidence": best_score, "method": "fuzzy"})
        else:
            unmatched.append(mp)

    return links, unmatched


# ─── persistence ─────────────────────────────────────────────────────────────

def save_links(con: sqlite3.Connection, links: list[dict]) -> int:
    n = 0
    with con:
        for lnk in links:
            con.execute(
                """
                INSERT INTO mp_link (mp_id, vouli_slug, confidence, method)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(mp_id) DO UPDATE SET
                    vouli_slug=excluded.vouli_slug,
                    confidence=excluded.confidence,
                    method=excluded.method
                """,
                (lnk["mp_id"], lnk["vouli_slug"], lnk["confidence"], lnk["method"]),
            )
            n += 1
    return n


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Link mp_index to vouli_member by name.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--report", default=None, help="Write unmatched CSV to this path")
    args = parser.parse_args(argv)

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA foreign_keys = ON")

    mp_list = load_mp_index(con)
    vouli_list = load_vouli_members(con)
    print(f"mp_index: {len(mp_list)} entries")
    print(f"vouli_member: {len(vouli_list)} entries")

    if not vouli_list:
        print("WARNING: vouli_member is empty — run vouliwatch.py first", file=sys.stderr)

    links, unmatched = match_members(mp_list, vouli_list)

    by_method: dict[str, int] = {}
    for lnk in links:
        by_method[lnk["method"]] = by_method.get(lnk["method"], 0) + 1

    print(f"\nMatched: {len(links)}/{len(mp_list)}")
    for method, count in sorted(by_method.items()):
        print(f"  {method}: {count}")
    print(f"Unmatched: {len(unmatched)}")

    n = save_links(con, links)
    con.close()
    print(f"Saved {n} links -> mp_link table")

    if args.report:
        import csv
        with open(args.report, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["mp_id", "sur_norm", "giv_norm", "display"])
            w.writeheader()
            w.writerows(unmatched)
        print(f"Unmatched report -> {args.report}")
    elif unmatched:
        print("\nFirst 20 unmatched:")
        for mp in unmatched[:20]:
            print(f"  mp_id={mp['mp_id']}  {mp['display']}")


if __name__ == "__main__":
    main()
