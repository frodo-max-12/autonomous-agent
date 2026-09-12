"""Reset the autonomous agent's database for a fresh start.

Usage (STOP the agent first — Ctrl+C — so the DB file isn't locked):
    python scripts/reset_db.py          # clear TRANSACTIONAL data only (emails, quotations, leads,
                                        # vendor RFQs/quotes, negotiations, audit) — KEEPS your
                                        # loaded vendors, verified HSN duties and FX rates so you
                                        # don't have to re-import 388 vendors.
    python scripts/reset_db.py --all    # wipe EVERYTHING (also vendors, HSN, FX) — total fresh start.

Nothing is deleted from Gmail — only the agent's own local SQLite DB.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text
from config.settings import get_settings
from core.database import init_database

# Child → parent-ish order (FKs are disabled during the wipe anyway).
TRANSACTIONAL = [
    "audit_log", "vendor_quotes", "vendor_rfqs", "vendor_pos", "negotiation_rounds",
    "bom_items", "quotations", "emails", "leads", "customer_history", "classification_feedback",
]
REFERENCE = ["vendors", "hsn_duties", "fx_rates"]  # your loaded/verified data — kept unless --all


def main(wipe_all: bool) -> int:
    settings = get_settings()
    init_database(settings.database_url)  # make sure the schema exists before we clear it
    db_path = settings.database_url.replace("sqlite:///", "")
    engine = create_engine(settings.database_url)

    tables = TRANSACTIONAL + (REFERENCE if wipe_all else [])
    print(f"Resetting {db_path}")
    print("Mode:", "FULL WIPE (also vendors / HSN / FX)" if wipe_all else "transactional only (keeps vendors / HSN / FX)")
    total = 0
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for t in tables:
            try:
                n = conn.execute(text(f"DELETE FROM {t}")).rowcount
                total += max(n, 0)
                try:
                    conn.execute(text("DELETE FROM sqlite_sequence WHERE name=:t"), {"t": t})
                except Exception:
                    pass  # sqlite_sequence only exists if there are AUTOINCREMENT tables
                print(f"  cleared {t:<24} {n if n and n > 0 else 0} row(s)")
            except Exception as e:
                print(f"  skip    {t:<24} ({e})")
    print(f"\nDone — {total} row(s) removed.")
    if not wipe_all:
        kept = engine.connect().execute(text("SELECT COUNT(*) FROM vendors")).scalar()
        print(f"Kept {kept} vendors + your HSN duties + FX rates. Ready for fresh inquiries.")
    else:
        print("Everything wiped. Re-import vendors:  python scripts/import_company_b_vendors.py \"C:\\Users\\HP\\Downloads\\Vendors.xlsx\"")
    return 0


if __name__ == "__main__":
    sys.exit(main("--all" in sys.argv))
