"""
Remove automated / bounce addresses (mailer-daemon, postmaster, no-reply, ...) that were accidentally
learned onto vendor rows before the contact-learning filter was added. Reuses the SAME
`_is_system_addr` definition as the live code, so it stays in sync. Safe to re-run.
"""
import os
import sys

os.chdir(r"D:\IT Dept\Developements\AI Agent\Autonomous Agent\autonomous-agent v1.1")
sys.path.insert(0, os.getcwd())

import sqlite3                                              # noqa: E402
from core.vendor_sourcing import _addrs, _is_system_addr   # noqa: E402

con = sqlite3.connect("data/autonomous.db")
cur = con.cursor()
cur.execute("SELECT id, name, email FROM vendors WHERE email LIKE '%@%'")
rows = cur.fetchall()

fixed = 0
for vid, name, email in rows:
    addrs = _addrs(email)
    clean = [a for a in addrs if not _is_system_addr(a)]
    if len(clean) != len(addrs):
        removed = [a for a in addrs if _is_system_addr(a)]
        cur.execute("UPDATE vendors SET email = ? WHERE id = ?", ("; ".join(clean), vid))
        print(f"cleaned '{name}' (id {vid}): removed {removed}")
        fixed += 1

con.commit()
con.close()
print(f"done — {fixed} vendor row(s) cleaned of automated/bounce addresses")
