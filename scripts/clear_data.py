"""
Fresh start: clear all transactional / test data from data/autonomous.db, KEEPING the live vendor
list and the HSN duties (your setup). Take a timestamped backup of data/autonomous.db first.

Cleared: emails, bom_items, quotations, leads, customer_history, negotiation_rounds, vendor_rfqs,
         vendor_quotes, vendor_pos, audit_log, market_offers, part_info, classification_feedback,
         fx_rates.
Kept:    vendors, hsn_duties, and the schema itself.
IDs restart at 1 for the cleared tables (empty table → next rowid is 1).
"""
import sqlite3

DB = "data/autonomous.db"
CLEAR = ["vendor_pos", "vendor_quotes", "vendor_rfqs", "negotiation_rounds", "bom_items",
         "quotations", "customer_history", "leads", "emails", "audit_log", "market_offers",
         "part_info", "classification_feedback", "fx_rates"]
KEEP = ["vendors", "hsn_duties"]


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()

    print("BEFORE:")
    for t in CLEAR + KEEP:
        cur.execute(f'SELECT COUNT(*) FROM "{t}"')
        print(f"  {t:26} {cur.fetchone()[0]}")

    for t in CLEAR:
        cur.execute(f'DELETE FROM "{t}"')

    # reset any autoincrement high-water marks so IDs restart at 1
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'")
    if cur.fetchone():
        cur.executemany("DELETE FROM sqlite_sequence WHERE name=?", [(t,) for t in CLEAR])

    con.commit()
    con.execute("VACUUM")

    print("AFTER:")
    for t in CLEAR + KEEP:
        cur.execute(f'SELECT COUNT(*) FROM "{t}"')
        print(f"  {t:26} {cur.fetchone()[0]}")
    con.close()
    print("DONE — transactional/test data cleared; vendors + HSN kept.")


if __name__ == "__main__":
    main()
