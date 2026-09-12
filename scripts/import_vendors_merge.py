"""
Merge the master vendor list (Downloads\\Vendors.xlsx — Non-Authorised + Authorised sheets) into the
agent DB, WITHOUT creating duplicates:

- Extracts ALL valid email addresses from each (often messy) cell → stored semicolon-joined in
  Vendor.email, so an RFQ is sent to EVERY contact (send_rfqs turns ';' into ',').
- De-dupes against vendors already in the DB (incl. the ~32 from the intl.sales study) by
  normalised name / shared email domain / close name — matches MERGE (union of emails + brands)
  instead of adding a second row.
- Enriches the source list: where the study found a real contact email for a vendor that's in the
  Excel (matched by domain) but that email isn't in the Excel yet, it's added — and a NON-DESTRUCTIVE
  copy `Vendors_updated.xlsx` is written next to the original (your file is left untouched).

Safe to re-run. Run with the agent STOPPED (SQLite single-writer):
    python scripts/import_vendors_merge.py                 # uses Downloads\\Vendors.xlsx
    python scripts/import_vendors_merge.py "C:\\path\\Vendors.xlsx"
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import get_settings              # noqa: E402
from core.database import init_database, get_session, Vendor  # noqa: E402

DEFAULT_XLSX = os.path.join(os.path.expanduser("~"), "Downloads", "Vendors.xlsx")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Free/personal email domains must NOT be used to MATCH two vendors — many different vendors use them,
# so matching on them would wrongly merge unrelated companies. (Name-match still applies.)
FREE_DOMAINS = {"gmail.com", "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "yahoo.com",
                "yahoo.co.in", "yahoo.com.cn", "aol.com", "icloud.com", "gmx.com", "gmx.de",
                "qq.com", "163.com", "126.com", "sina.com", "foxmail.com", "rediffmail.com"}
_SUFFIX = {"PVT", "PRIVATE", "LIMITED", "LTD", "LLC", "INC", "CORP", "CORPORATION", "CO", "COMPANY",
           "PTE", "GMBH", "KG", "KFT", "ELECTRONICS", "ELECTRONIC", "TECHNOLOGY", "TECHNOLOGIES",
           "TECH", "INTERNATIONAL", "INTL", "GROUP", "GLOBAL", "HK", "SEMICONDUCTOR", "SEMI",
           "TRADING", "THE", "AND"}


def emails_from(cell) -> list[str]:
    out = []
    for e in EMAIL_RE.findall(str(cell or "")):
        e = e.lower().strip(".,;:'\"<>() ")
        if e and e not in out:
            out.append(e)
    return out


def norm_name(n) -> str:
    n = re.sub(r"[^A-Z0-9 ]", " ", (n or "").upper())
    n = " ".join(w for w in n.split() if w not in _SUFFIX)
    return re.sub(r"\s+", " ", n).strip()


def domain(e: str) -> str:
    return e.split("@")[-1].lower() if "@" in e else ""


def biz_domain(e: str) -> str:
    """Company domain used for matching — blank for free/personal domains (never match on those)."""
    d = domain(e)
    return d if d and d not in FREE_DOMAINS else ""


def _tokens_upper(val) -> list[str]:
    out = []
    for t in re.split(r"[,;/\n\r\t ]+", str(val or "")):
        t = t.strip().strip(".,;:").upper()
        if t and t not in out:
            out.append(t)
    return out


def _hdr_map(ws):
    hdr = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    return {str(h).strip().lower(): i for i, h in enumerate(hdr) if h}


def _cell(row, hmap, *keys):
    for k in keys:
        for h, i in hmap.items():
            if k in h and i < len(row):
                return row[i]
    return None


def read_excel(path):
    """Return (records, workbook, sheet_info) where records = [{name, type, brands, emails, sheet, row}]."""
    import openpyxl
    wb = openpyxl.load_workbook(path)
    recs = []
    if "Non-Authorised" in wb.sheetnames:
        ws = wb["Non-Authorised"]; hm = _hdr_map(ws)
        for ridx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            name = _cell(row, hm, "vendors name", "name")
            if not name or not str(name).strip():
                continue
            recs.append({"name": str(name).strip(), "type": "non_authorized",
                         "brands": _tokens_upper(_cell(row, hm, "brands", "mfr", "manufacturer")),
                         "emails": emails_from(_cell(row, hm, "email")),
                         "sheet": "Non-Authorised", "row": ridx,
                         "email_col": _cell(row, hm, "email") is not None and next((i for h, i in hm.items() if "email" in h), None)})
    if "Authorised" in wb.sheetnames:
        ws = wb["Authorised"]; hm = _hdr_map(ws)
        for ridx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            line = _cell(row, hm, "product line")
            if not line or not str(line).strip():
                continue
            recs.append({"name": str(line).strip(), "type": "authorized",
                         "brands": _tokens_upper(line),
                         "emails": emails_from(_cell(row, hm, "mail")),
                         "sheet": "Authorised", "row": ridx,
                         "email_col": next((i for h, i in hm.items() if "mail" in h), None)})
    return recs, wb


def find_match(rec, by_norm, by_domain):
    """Return an existing Vendor to merge into, or None. Match: normalised name, then shared domain."""
    v = by_norm.get(norm_name(rec["name"]))
    if v:
        return v
    for e in rec["emails"]:
        d = biz_domain(e)          # never match on a free/personal domain
        if d and d in by_domain:
            return by_domain[d]
    return None


def main(path=DEFAULT_XLSX):
    if not os.path.exists(path):
        print(f"[import] file not found: {path}")
        return 1
    settings = get_settings()
    init_database(settings.database_url)
    session = get_session(settings.database_url)
    try:
        recs, wb = read_excel(path)
        existing = session.query(Vendor).all()
        by_norm, by_domain = {}, {}
        # study vendors already in DB (seeded by seed_intl_vendors) -> domain -> their email(s)
        study_domain_emails = {}
        for v in existing:
            by_norm.setdefault(norm_name(v.name), v)
            for e in emails_from(v.email):
                d = biz_domain(e)
                if d:
                    by_domain.setdefault(d, v)
                    if v.notes and "intl.sales study" in v.notes:
                        study_domain_emails.setdefault(d, set()).add(e)

        created = merged = 0
        for rec in recs:
            match = find_match(rec, by_norm, by_domain)
            if match:
                cur = emails_from(match.email)
                new = [e for e in rec["emails"] if e not in cur]
                if new:
                    match.email = "; ".join(cur + new)
                match.brands = sorted(set((match.brands or []) + rec["brands"])) or match.brands
                for e in rec["emails"]:
                    d = biz_domain(e)
                    if d:
                        by_domain.setdefault(d, match)
                merged += 1
            else:
                v = Vendor(name=rec["name"], email="; ".join(rec["emails"]),  # "" (not None) — column is NOT NULL; send skips blank
                           vendor_type=rec["type"], brands=rec["brands"] or ["ANY"],
                           currency="USD", active=True, notes="from Vendors.xlsx")
                session.add(v); session.flush()
                by_norm[norm_name(rec["name"])] = v
                for e in rec["emails"]:
                    d = biz_domain(e)
                    if d:
                        by_domain.setdefault(d, v)
                created += 1
        session.commit()

        # Enrich the Excel: append study-found emails to matching rows (by domain), non-destructively.
        enriched = 0
        for rec in recs:
            col = rec.get("email_col")
            if col is None:
                continue
            row_domains = {biz_domain(e) for e in rec["emails"] if biz_domain(e)}
            add = set()
            for d in row_domains:
                for se in study_domain_emails.get(d, set()):
                    if se not in rec["emails"]:
                        add.add(se)
            if add:
                ws = wb[rec["sheet"]]
                cell = ws.cell(row=rec["row"], column=col + 1)
                cell.value = ((str(cell.value).strip() + "; ") if cell.value else "") + "; ".join(sorted(add))
                enriched += 1
        out_path = os.path.join(os.path.dirname(path), "Vendors_updated.xlsx")
        if enriched:
            wb.save(out_path)

        total = session.query(Vendor).count()
        print(f"[import] Vendors.xlsx: {len(recs)} rows read.")
        print(f"[import] DB: {created} new vendors created, {merged} merged into existing (no duplicates). Total vendor rows: {total}.")
        print(f"[import] Emails stored semicolon-joined → RFQ sends to ALL contacts.")
        if enriched:
            print(f"[import] Enriched {enriched} Excel row(s) with study-found emails → wrote {out_path} (original untouched).")
        else:
            print("[import] No study emails needed adding to the Excel (all already present or no domain match).")
        return 0
    except Exception as e:
        session.rollback()
        print(f"[import] ERROR: {e}")
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_XLSX))
