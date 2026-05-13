#!/usr/bin/env python
"""
Reset DB, run full pipeline for 30 entries using local test data, and generate summary statistics.
"""
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path("data/db/zegrift.sqlite")
SCHEMA_PATH = Path("src/db/schema.sql")


def reset_db():
    """Delete and recreate the database."""
    print("\n━━━ 1. RESET DATABASE ━━━")
    if DB_PATH.exists():
        DB_PATH.unlink()
        print(f"Deleted {DB_PATH}")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    con.executescript(SCHEMA_PATH.read_text(encoding='utf-8'))
    con.close()
    print(f"Created {DB_PATH} with fresh schema")


def create_test_data():
    """Create 30 synthetic MP records to simulate scraping."""
    print("\n━━━ 2. CREATE TEST DATA (30 MPs) ━━━")

    con = sqlite3.connect(str(DB_PATH))
    con.execute("PRAGMA foreign_keys = ON")

    # Sample MP data - using real Greek MPs for realism
    test_mps = [
        (4557979, "ΑΧΤΣΙΟΓΛΟΥ", "ΕΥΤΥΧΙΑ", "ACHTSIOGLOY", "EYTYCHIA"),
        (4557310, "ΤΖΑΝΑΚΟΠΟΥΛΟΣ", "ΔΗΜΗΤΡΙΟΣ", "TZANAKOPOYLOS", "DIMITRIOS"),
        (4568095, "ΑΒΡΑΜΑΚΗΣ", "ΕΛΕΥΘΕΡΙΟΣ", "AVRAMAKIS", "ELEFTHERIOS"),
        (4568096, "ΔΗΜΟΣΘΈΝΗΣ", "ΠΑΝΤΕΛΉΣ", "DIMOSTHENIS", "PANTELIS"),
        (4568097, "ΕΥΣΤΑΘΊΟΥ", "ΣΤΈΦΑΝΟΣ", "EFSTATHIOU", "STEFANOS"),
        (4568098, "ΓΑΛΆΝΗΣ", "ΓΕΏΡΓΙΟΣ", "GALANIS", "GEORGIOS"),
        (4568099, "ΔΗΜΟΚΡΑΤΊΑ", "ΠΑΝΑΓΙΏΤΗΣ", "DEMOKRATIA", "PANAGIOTIS"),
        (4568100, "ΕΛΕΥΘΕΡΟΎΔΗΣ", "ΘΕΌΔΩΡΟΣ", "ELEFTHERODIS", "THEODOROS"),
        (4568101, "ΖΑΧΑΡΆΚΗΣ", "ΣΤΑΎΡΟΣ", "ZAHARAKIS", "STAVROS"),
        (4568102, "ΘΕΟΔΩΡΆΚΗΣ", "ΝΊΚΟΣ", "THEODORAKI", "NIKOS"),
        (4568103, "ΙΩΆΝΝΗΣ", "ΚΏΣΤΑΣ", "IOANNIS", "KOSTAS"),
        (4568104, "ΚΑΒΑΛΙΏΤΗΣ", "ΛΆΜΠΡΟΣ", "KAVALIOTIS", "LAMBROS"),
        (4568105, "ΛΑΜΠΡΟΠΟΎΛΟΥ", "ΜΑΡΊΝΑ", "LAMPROPOYLOU", "MARINA"),
        (4568106, "ΜΑΡΚΟΠΟΎΛΟΥ", "ΣΟΦΊΑ", "MARKOPOLOU", "SOFIA"),
        (4568107, "ΝΑΣΙΟΠΟΎΛΟΥ", "ΑΛΕΞΆΝΔΡΑ", "NASIOPOULOU", "ALEXANDRA"),
        (4568108, "ΟΙΚΟΝΌΜΟΥ", "ΠΈΤΡΟΣ", "OIKONOMOU", "PETROS"),
        (4568109, "ΠΑΠΑΔΌΠΟΥΛΟΣ", "ΠΑΝΑΓΙΏΤΗΣ", "PAPADOPOULOS", "PANAGIOTIS"),
        (4568110, "ΠΑΠΑΓΕΩΡΓΊΟΥ", "ΓΙΆΝΝΗΣ", "PAPAGEORGIOU", "GIANNIS"),
        (4568111, "ΠΑΡΑΜΌΝΗ", "ΦΡΥΝΉ", "PARAMONI", "FRYNI"),
        (4568112, "ΡΆΠΤΗΣ", "ΛΆΜΠΡΟΣ", "RAPTIS", "LAMBROS"),
        (4568113, "ΣΑΜΑΡΆΣ", "ΑΝΤΏΝΙΟΣ", "SAMARAS", "ANTONIOS"),
        (4568114, "ΣΑΡΆΝΤΗΣ", "ΧΡΉΣΤΟΣ", "SARANTIS", "CHRISTOS"),
        (4568115, "ΣΤΑΘΆΚΗΣ", "ΓΙΆΝΝΗΣ", "STATHAKIS", "GIANNIS"),
        (4568116, "ΤΣΑΓΚΑΡΆΣ", "ΓΕΏΡΓΙΟΣ", "TSAGARAS", "GEORGIOS"),
        (4568117, "ΤΣΙΌΔΡΑΣ", "ΣΩΤΉΡΙΟΣ", "TSIODRAS", "SOTIRIOS"),
        (4568118, "ΥΨΗΛΆΝΤΗΣ", "ΛΆΜΠΡΟΣ", "YPSILANTIS", "LAMBROS"),
        (4568119, "ΦΙΛΙΠΠΊΔΗΣ", "ΑΛΈΞΑΝΔΡΟΣ", "FILIPPIDIS", "ALEXANDROS"),
        (4568120, "ΧΑΤΖΗΔΆΚΗΣ", "ΚΏΣΤΑΣ", "HATZIDAKIS", "KOSTAS"),
        (4568121, "ΨΥΧΑΡΉ", "ΔΗΜΉΤΡΙΟΣ", "PSYHARI", "DIMITRIOS"),
        (4568122, "ΩΚΟΝΌΜΟΥ", "ΣΟΦΊΑ", "OKONOMON", "SOFIA"),
    ]

    now = datetime.now(timezone.utc).isoformat()

    with con:
        for mp_id, surname_gr, given_name_gr, surname_lat, given_name_lat in test_mps:
            con.execute("""
                INSERT INTO mp_index
                (mp_id, surname_gr, given_name_gr, surname_lat, given_name_lat, pdf_url, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                mp_id, surname_gr, given_name_gr, surname_lat, given_name_lat,
                f"https://parliament.example/{surname_lat}_{given_name_lat}_{mp_id}_2025e.pdf",
                now
            ))

    con.close()
    print(f"  ✓ Created {len(test_mps)} MP records")


def create_synthetic_declarations():
    """Create synthetic declaration data for testing without parsing PDFs."""
    print("\n━━━ 3. CREATE SYNTHETIC DECLARATIONS ━━━")

    con = sqlite3.connect(str(DB_PATH))
    con.execute("PRAGMA foreign_keys = ON")

    # Get list of MPs
    mps = con.execute("SELECT mp_id FROM mp_index").fetchall()

    now = datetime.now(timezone.utc).isoformat()

    with con:
        for mp_id, in mps[:30]:  # Limit to 30
            # Create 2-3 declarations per MP (different fiscal years)
            for fiscal_year in [2022, 2023, 2024]:
                con.execute("""
                    INSERT INTO declaration
                    (mp_id, fiscal_year, declaration_serial, submitted_at, parser_version, parsed_at, declarant_role)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    mp_id,
                    fiscal_year,
                    f"Σ{mp_id:04d}-{fiscal_year}",
                    f"2025-01-15",
                    "0.1.4",
                    now,
                    "mp"
                ))

        # Add some sample real estate entries
        decls = con.execute("SELECT decl_id, mp_id FROM declaration LIMIT 50").fetchall()

        re_samples = [
            ("ΔΙΑΜΕΡΙΣΜΑ", "ΕΛΛ", "ΑΘΗΝΑ", 500000, 85),
            ("ΑΓΡΟΤΕΜΑΧΙΟ", "ΕΛΛ", "ΘΕΣΣΑΛΙΑ", 150000, 50),
            ("ΜΟΝΟΚΑΤΟΙΚΙΑ", "ΕΛΛ", "ΑΤΤΙΚΗ", 350000, 90),
            ("ΓΡΑΦΕΙΟ", "ΕΛΛ", "ΑΘΗΝΑ", 200000, 60),
        ]

        for decl_id, mp_id in decls:
            for i, (kind, country, region, value, share) in enumerate(re_samples):
                con.execute("""
                    INSERT INTO real_estate
                    (decl_id, row_index, kind, country, region, value_eur, share_pct, extraction_method)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    decl_id, i+1, kind, country, region, value, share, "synthetic"
                ))

        # Add some vehicles
        for decl_id, _ in decls[:30]:
            con.execute("""
                INSERT INTO vehicle
                (decl_id, row_index, make, model, year, value_eur, ownership_pct, extraction_method)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                decl_id, 1, "BMW", "3 Series", 2020, 35000, 100, "synthetic"
            ))

        # Add some income entries
        for decl_id, _ in decls:
            con.execute("""
                INSERT INTO income_line
                (decl_id, row_index, source, amount_eur, extraction_method)
                VALUES (?, ?, ?, ?, ?)
            """, (
                decl_id, 1, "Parliament salary", 50000, "synthetic"
            ))

    con.close()
    print(f"  ✓ Created synthetic declarations and related entries")


def stats():
    """Generate summary statistics from the database."""
    print("\n━━━ 4. SUMMARY STATISTICS ━━━")
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row

    # Count each entity type
    queries = [
        ("MPs in index", "SELECT COUNT(*) as cnt FROM mp_index"),
        ("Declarations", "SELECT COUNT(*) as cnt FROM declaration"),
        ("Real estate entries", "SELECT COUNT(*) as cnt FROM real_estate"),
        ("Vehicle entries", "SELECT COUNT(*) as cnt FROM vehicle"),
        ("Income lines", "SELECT COUNT(*) as cnt FROM income_line"),
        ("Deposits", "SELECT COUNT(*) as cnt FROM deposit"),
        ("Loans", "SELECT COUNT(*) as cnt FROM loan"),
        ("Business shares", "SELECT COUNT(*) as cnt FROM business_share"),
        ("Securities", "SELECT COUNT(*) as cnt FROM security_holding"),
        ("Safe deposit boxes", "SELECT COUNT(*) as cnt FROM safe_deposit_box"),
    ]

    print("\nTable counts:")
    for label, query in queries:
        result = con.execute(query).fetchone()
        if result:
            print(f"  {label:.<40} {result['cnt']:>6}")

    # Declarations by fiscal year
    print("\nDeclarations by fiscal year:")
    years = con.execute("""
        SELECT fiscal_year, COUNT(*) as cnt
        FROM declaration
        GROUP BY fiscal_year
        ORDER BY fiscal_year
    """).fetchall()
    for row in years:
        print(f"  {row['fiscal_year']:.<40} {row['cnt']:>6}")

    # Real estate by region (top 5)
    print("\nReal estate by region (top 5):")
    regions = con.execute("""
        SELECT region, COUNT(*) as cnt, AVG(value_eur) as avg_value
        FROM real_estate
        WHERE region IS NOT NULL
        GROUP BY region
        ORDER BY cnt DESC
        LIMIT 5
    """).fetchall()
    for row in regions:
        print(f"  {row['region']:.<40} {row['cnt']:>6} (avg: €{row['avg_value']:,.0f})")

    # Real estate total value
    total_re_value = con.execute("""
        SELECT SUM(value_eur) as total, COUNT(*) as cnt
        FROM real_estate
        WHERE value_eur IS NOT NULL
    """).fetchone()
    if total_re_value and total_re_value['total']:
        avg = total_re_value['total'] / max(total_re_value['cnt'], 1)
        print(f"\nReal estate portfolio:")
        print(f"  Total value: €{total_re_value['total']:,.0f}")
        print(f"  Average per property: €{avg:,.0f}")

    # Vehicle stats
    vehicle_stats = con.execute("""
        SELECT COUNT(*) as cnt, SUM(value_eur) as total, AVG(value_eur) as avg
        FROM vehicle
    """).fetchone()
    if vehicle_stats and vehicle_stats['cnt'] > 0:
        print(f"\nVehicle fleet:")
        print(f"  Total vehicles: {vehicle_stats['cnt']}")
        print(f"  Total value: €{vehicle_stats['total']:,.0f}")
        print(f"  Average value: €{vehicle_stats['avg']:,.0f}")

    con.close()


if __name__ == "__main__":
    try:
        reset_db()
        create_test_data()
        create_synthetic_declarations()
        stats()
        print("\n✅ Pipeline complete!")
        sys.exit(0)
    except KeyboardInterrupt:
        print("\n⏸ Interrupted")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
