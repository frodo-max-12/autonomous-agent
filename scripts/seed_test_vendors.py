"""Fresh start for end-to-end testing.

Wipes the transactional data (emails, quotations, leads, negotiations, vendor RFQs/quotes) AND
replaces the vendor list with 3 sample vendors — all 'ANY' open-market brokers, so EVERY part routes
to all 3 and you can watch consolidation pick the lowest cost across them. Keeps HSN duties + FX
rates (reference data). Nothing in Gmail is touched — only the agent's local SQLite DB.

STOP the agent first (Ctrl+C in its window). SQLite allows only ONE writer, so this fails with
'database is locked' if the agent is still running.

Usage:  python scripts/seed_test_vendors.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text
from config.settings import get_settings
from core.database import init_database, get_session, Vendor

CLEAR = ["audit_log", "vendor_quotes", "vendor_rfqs", "vendor_pos", "negotiation_rounds",
         "bom_items", "quotations", "emails", "leads", "customer_history",
         "classification_feedback", "vendors"]  # keeps hsn_duties + fx_rates

VENDORS = [
    ("Vendor 1 (FAE Test)", "fae1@company-b.example"),
    ("Vendor 2",            "vendor2@example.com"),
    ("Vendor 3",            "vendor3@example.com"),
]


def main() -> int:
    settings = get_settings()
    init_database(settings.database_url)
    engine = create_engine(settings.database_url)
    try:
        with engine.begin() as conn:  # atomic: all deletes commit together, or none on lock/error
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            for t in CLEAR:
                try:
                    conn.execute(text(f"DELETE FROM {t}"))
                    conn.execute(text("DELETE FROM sqlite_sequence WHERE name=:t"), {"t": t})
                except Exception:
                    pass
    except Exception as e:
        print("ERROR: could not clear the database — is the agent still running?")
        print("  Stop it (Ctrl+C in its window), then run this again.")
        print(f"  detail: {e}")
        return 1

    session = get_session(settings.database_url)
    try:
        for name, email in VENDORS:
            session.add(Vendor(name=name, email=email, brands=["ANY"],
                               vendor_type="non_authorized", currency="USD", active=True))
        session.commit()
        print("Fresh start complete — cleared emails / quotations / leads / RFQs (kept HSN + FX).")
        print(f"Seeded {len(VENDORS)} test vendors (brands=ANY → every part routes to all 3):")
        for v in session.query(Vendor).order_by(Vendor.id).all():
            print(f"  - {v.name:<22} {v.email:<32} brands={v.brands}")
        print("\nNow start the agent:  python main.py")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
