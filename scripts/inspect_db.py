"""Quick look at the agent's local database — row counts per table + the latest emails, quotations
and vendor RFQs. Read-only; safe to run any time (even while the agent is running).

Usage:
    python scripts/inspect_db.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text
from config.settings import get_settings

TABLES = ["emails", "quotations", "bom_items", "negotiation_rounds", "leads",
          "vendors", "vendor_rfqs", "vendor_quotes", "vendor_pos", "hsn_duties", "fx_rates", "audit_log"]


def main() -> int:
    settings = get_settings()
    db_path = settings.database_url.replace("sqlite:///", "")
    print(f"Database: {db_path}\n")
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        print("ROW COUNTS")
        for t in TABLES:
            try:
                n = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                print(f"  {t:<22} {n}")
            except Exception as e:
                print(f"  {t:<22} (n/a: {e})")

        def dump(title, sql):
            print(f"\n{title}")
            try:
                rows = conn.execute(text(sql)).fetchall()
                if not rows:
                    print("  (none)")
                for r in rows:
                    print("  " + " | ".join("" if v is None else str(v) for v in r))
            except Exception as e:
                print(f"  (n/a: {e})")

        dump("LATEST 10 EMAILS (id | type | from | status | subject)",
             "SELECT id, email_type, from_email, status, substr(subject,1,45) "
             "FROM emails ORDER BY id DESC LIMIT 10")
        dump("LATEST 10 QUOTATIONS (quote# | status | ccy | total | customer)",
             "SELECT quote_number, status, currency, round(total_amount,2), substr(customer_email,1,30) "
             "FROM quotations ORDER BY id DESC LIMIT 10")
        dump("LATEST 10 VENDOR RFQs (quote_id | vendor | status)",
             "SELECT quotation_id, vendor_name, status FROM vendor_rfqs ORDER BY id DESC LIMIT 10")
    return 0


if __name__ == "__main__":
    sys.exit(main())
