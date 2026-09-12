"""Regression tests for the 2026-07-06 consolidation-flow alignment (from the real worksheet
a real customer worksheet). Covers: split-sourcing + blended cost, NO BID, shortfall hold, single-vendor
(unchanged), mixed-currency FX-normalised pick, per-quote margin override, missing-currency
inference, and the acknowledgement authorized-parts block.

Run:  python tests/test_consolidation_scenarios.py     (from anywhere; no pytest needed)
"""
import sys, os
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.database import init_database, Quotation, VendorQuote, QuoteStatus
from core import consolidation

DB_FILE = os.path.join(PROJECT_ROOT, "data", "test_consolidation_scenarios.db")
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)
engine, Session = init_database("sqlite:///" + DB_FILE)

# Fake settings — v1.1 USD math: expenses=0 here so resale = cost * (1+margin) stays exact.
settings = SimpleNamespace(
    default_margin_percent=15.0,
    get_margin_overrides=lambda: {},
    default_expenses_percent=0.0,
    usd_inr_fx_rate=83.0,
)

passed, failed = [], []

def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("  OK  " if cond else " FAIL ") + name + (f"  -- {detail}" if detail else ""))

def add_quote(session, mpn, mfr, qty, **kw):
    q = Quotation(email_id=1, quote_number=f"T-{mpn}", account="main", currency="USD",
                  status=QuoteStatus.SOURCING_VENDORS,
                  line_items=[{"mpn": mpn, "manufacturer": mfr, "quantity": qty, "description": mpn}], **kw)
    session.add(q); session.flush()
    return q

def vq(session, q, vendor, cost, offered=None, mfr=None, currency="USD", notes=None, lead_time=None):
    o = VendorQuote(quotation_id=q.id, vendor_name=vendor, mpn=q.line_items[0]["mpn"],
                    manufacturer=mfr, cost_price=cost, currency=currency,
                    offered_qty=offered, notes=notes, lead_time=lead_time)
    session.add(o)
    return o

s = Session()

# 1) SINGLE-VENDOR PREFERENCE: the cheapest vendor (VendA) can only supply 10k of the 12k. The buyer
# does NOT want a cross-vendor split, so the cheapest vendor that covers the FULL 12k (VendB, no cap)
# is chosen ALONE — even though it's dearer per unit. No split, no blended cost, one source.
q1 = add_quote(s, "SPLIT1", "TE", 12000)
vq(s, q1, "VendA", 1.00, offered=10000); vq(s, q1, "VendB", 1.20)
s.commit(); consolidation.consolidate_and_price(q1, s, settings)
line1 = q1.line_items[0]
check("single-full-qty priced", line1["pricing_status"] == "priced", line1["pricing_status"])
check("single-full-qty picks the full-cover vendor", line1["selected_vendor"] == "VendB", line1["selected_vendor"])
check("single-full-qty resale = 1.20*1.15", abs(line1["unit_price"] - round(1.20*1.15, 4)) < 1e-6, str(line1["unit_price"]))
check("single-full-qty is one leg", len(line1.get("fulfillment") or []) == 1)
check("no vendor names in remark", "VendA" not in str(line1["remark"]) and "VendB" not in str(line1["remark"]), line1["remark"])
check("single-full-qty one vendor selected",
      s.query(VendorQuote).filter_by(quotation_id=q1.id, is_selected=True).count() == 1)

# 1b) LAST-RESORT SPLIT: NO single vendor can cover the full 12k (both capped), so the qty is split
# across vendors — priced on the covered amount but HELD for review, and the customer remark NEVER
# names our vendors (the per-supplier legs live in `fulfillment`, dashboard-only).
q1b = add_quote(s, "SPLIT2", "TE", 12000)
vq(s, q1b, "VendA", 1.00, offered=10000); vq(s, q1b, "VendB", 1.20, offered=5000)
s.commit(); consolidation.consolidate_and_price(q1b, s, settings)
l1b = q1b.line_items[0]
check("last-resort split held for review", l1b["pricing_status"] == "needs_review", l1b["pricing_status"])
check("last-resort split resale still set (covered)", l1b["unit_price"] is not None)
check("last-resort split has 2 legs", len(l1b.get("fulfillment") or []) == 2)
check("last-resort split both vendors selected",
      s.query(VendorQuote).filter_by(quotation_id=q1b.id, is_selected=True).count() == 2)
check("last-resort split hides vendor names",
      "VendA" not in str(l1b["remark"]) and "VendB" not in str(l1b["remark"]), l1b["remark"])

# 1c) SINGLE-VENDOR DATE-CODE SPLIT: ONE vendor offers the same part in two date codes at different
# prices, together covering the 3000 ask (1000 @ $15 DC22+, 2000 @ $17 DC24+) and it's the cheapest
# source. Consolidation takes it, and the CUSTOMER quote shows TWO separate lines — one per date code
# with its own qty + price — NOT one blended line, priced (same vendor = not held for review), no name.
q1c = add_quote(s, "DCSPLIT1", "Altera", 3000)
o1 = vq(s, q1c, "VendDC", 15.0, offered=1000); o1.date_code = "22+"
o2 = vq(s, q1c, "VendDC", 17.0, offered=2000); o2.date_code = "24+"
s.commit(); consolidation.consolidate_and_price(q1c, s, settings)
lots = [it for it in q1c.line_items if it.get("mpn") == "DCSPLIT1"]
by_dc = {it.get("date_code"): it for it in lots}
check("date-code split -> 2 customer lines", len(lots) == 2, str(len(lots)))
check("date-code split has both date codes", set(by_dc) == {"22+", "24+"}, str(set(by_dc)))
check("DC22+ line = 1000 @ 15*1.15",
      by_dc.get("22+", {}).get("quantity") == 1000 and abs(by_dc["22+"]["unit_price"] - round(15.0*1.15, 4)) < 1e-6,
      str(by_dc.get("22+")))
check("DC24+ line = 2000 @ 17*1.15",
      by_dc.get("24+", {}).get("quantity") == 2000 and abs(by_dc["24+"]["unit_price"] - round(17.0*1.15, 4)) < 1e-6,
      str(by_dc.get("24+")))
check("date-code split both priced (same vendor, not held)",
      all(it["pricing_status"] == "priced" for it in lots), str([it["pricing_status"] for it in lots]))
check("date-code split hides vendor name",
      all("VendDC" not in str(it.get("remark")) for it in lots), str([it.get("remark") for it in lots]))
check("date-code split mentions the date code",
      all("date code" in str(it.get("remark")).lower() for it in lots))

# ===== AI SOURCING PLAN (non-hardcoded): LLM decides the plan, code validates + prices =====
class FakePlanner:
    """Stand-in for ClaudeClient.plan_sourcing — returns a canned plan so we can test the code path
    (validation + execution + fallback) without a live model."""
    def __init__(self, plan): self._plan = plan
    def plan_sourcing(self, mpn, description, manufacturer, req_qty, lots, market=None):
        return self._plan

# 1d) AI chooses the cheaper multi-lot buy over the blunt single-vendor rule. A single vendor (B) can
# cover the full 3000 but is +77%; the AI buyer combines the two cheaper DC22+ lots (A 2000 + C 1000).
# Code VALIDATES the plan and prices it (blended); being cross-vendor it's held for review, names hidden.
q1d = add_quote(s, "AIPLAN1", "TE", 3000)
a = vq(s, q1d, "VendA", 10.00, offered=2000); a.date_code = "22+"
c_ = vq(s, q1d, "VendC", 10.50, offered=1000); c_.date_code = "22+"
b = vq(s, q1d, "VendB", 18.00, offered=3000); b.date_code = "24+"
s.commit()
planner = FakePlanner({"allocation": [{"lot": 0, "qty": 2000}, {"lot": 1, "qty": 1000}],
                       "hold_for_review": True, "shortage": False, "reason": "single vendor +77%"})
consolidation.consolidate_and_price(q1d, s, settings, claude=planner)
l1d = q1d.line_items[0]
vends = {f["vendor"] for f in (l1d.get("fulfillment") or [])}
check("AI plan used A+C, not B", vends == {"VendA", "VendC"}, str(vends))
check("AI plan blended = (2000*10+1000*10.5)/3000*1.15",
      abs(l1d["unit_price"] - round(((2000*10.0 + 1000*10.5)/3000)*1.15, 4)) < 1e-6, str(l1d["unit_price"]))
check("AI cross-vendor plan held for review", l1d["pricing_status"] == "needs_review", l1d["pricing_status"])
check("AI plan hides vendor names",
      "VendA" not in str(l1d["remark"]) and "VendC" not in str(l1d["remark"]), l1d["remark"])

# 1e) UNSAFE AI plan -> DETERMINISTIC FALLBACK: the model over-allocates (asks 5000 from a lot with only
# 2000) -> validation rejects it -> the deterministic rule runs and picks the single full-cover vendor
# (VendB), priced normally. Proves a bad plan can never reach the customer.
q1e = add_quote(s, "AIPLAN2", "TE", 3000)
a2 = vq(s, q1e, "VendA", 10.00, offered=2000); a2.date_code = "22+"
b2 = vq(s, q1e, "VendB", 18.00, offered=3000); b2.date_code = "24+"
s.commit()
bad = FakePlanner({"allocation": [{"lot": 0, "qty": 5000}], "hold_for_review": False, "shortage": False})
consolidation.consolidate_and_price(q1e, s, settings, claude=bad)
l1e = q1e.line_items[0]
check("unsafe AI plan -> fallback picks full-cover vendor", l1e["selected_vendor"] == "VendB", l1e["selected_vendor"])
check("fallback priced (single vendor)", l1e["pricing_status"] == "priced", l1e["pricing_status"])
check("fallback resale = 18*1.15", abs(l1e["unit_price"] - round(18.0*1.15, 4)) < 1e-6, str(l1e["unit_price"]))

# 2) NO BID: no vendor quoted
q2 = add_quote(s, "NOBID1", "RS", 50); s.commit()
consolidation.consolidate_and_price(q2, s, settings)
line2 = q2.line_items[0]
check("no_bid status", line2["pricing_status"] == "no_bid")
check("no_bid price None", line2["unit_price"] is None)
check("no_bid remark", line2["remark"] == "NO BID")

# 3) SINGLE vendor (degenerate case unchanged)
q3 = add_quote(s, "SOLO1", "Molex", 100); vq(s, q3, "VendC", 2.00); s.commit()
consolidation.consolidate_and_price(q3, s, settings)
line3 = q3.line_items[0]
check("single priced", line3["pricing_status"] == "priced")
check("single resale = cost*1.15", abs(line3["unit_price"] - 2.30) < 1e-6, str(line3["unit_price"]))
check("single not split", not str(line3["remark"]).startswith("Split"))

# 4) SHORTFALL: only 3k of 5k available across all vendors -> needs_review
q4 = add_quote(s, "SHORT1", "AVX", 5000); vq(s, q4, "VendD", 3.00, offered=3000); s.commit()
consolidation.consolidate_and_price(q4, s, settings)
line4 = q4.line_items[0]
check("shortfall needs_review", line4["pricing_status"] == "needs_review")
check("shortfall price still set (covered)", line4["unit_price"] is not None)
check("shortfall remark mentions availability", "3,000" in line4["remark"] and "5,000" in line4["remark"], line4["remark"])

# 5) MIXED-currency vendors on a USD quote (v1.1): a USD cost vs an INR cost (converted to USD at FX);
# the cheaper landed cost wins. USD 0.40 < INR 50/83 = 0.602 USD -> the USD vendor is selected.
q5 = Quotation(email_id=1, quote_number="T-USD1", account="main", currency="USD",
               status=QuoteStatus.SOURCING_VENDORS,
               line_items=[{"mpn": "USD1", "manufacturer": "KEMET", "quantity": 100, "description": "cap"}])
s.add(q5); s.flush()
vq(s, q5, "UsdVendor", 0.40, currency="USD"); vq(s, q5, "InrVendor", 50.0, currency="INR")
s.commit(); consolidation.consolidate_and_price(q5, s, settings)
line5 = q5.line_items[0]
check("mixed: cheaper USD vendor wins", line5["selected_vendor"] == "UsdVendor", line5["selected_vendor"])
check("mixed: priced in USD", line5["pricing_status"] == "priced" and line5["cost_currency"] == "USD")
check("mixed: resale = 0.40*1.15", abs(line5["unit_price"] - round(0.40 * 1.15, 4)) < 1e-6, line5["unit_price"])

# 5b) PER-QUOTE MARGIN OVERRIDE: flat 5% instead of the 15% default
q6 = add_quote(s, "MARG1", "TE", 100, margin_percent_override=5.0)
vq(s, q6, "VendE", 100.0); s.commit()
consolidation.consolidate_and_price(q6, s, settings)
line6 = q6.line_items[0]
check("margin override applied (5%)", abs(line6["margin_percent"] - 5.0) < 1e-9, str(line6["margin_percent"]))
check("margin override resale = cost*1.05", abs(line6["unit_price"] - 105.0) < 1e-6, str(line6["unit_price"]))

# ===== SAFETY & GATING BUNDLE =====
from datetime import datetime, timezone, timedelta

# 8) PACKAGING HOMOGENEITY: neither vendor can cover 12k alone (both capped) so a split is forced, and
# it blends Tape&Reel + Bulk -> needs_review. (Caps are required now: with an uncapped vendor the
# single-vendor preference would pick one supplier and never split.)
q7 = add_quote(s, "PKG1", "TE", 12000)
vq(s, q7, "VendReel", 1.00, offered=10000, mfr="TE"); s.query(VendorQuote).filter_by(quotation_id=q7.id).first()
for v in s.query(VendorQuote).filter_by(quotation_id=q7.id): v.packaging = "Tape & Reel"
vq(s, q7, "VendBulk", 1.10, mfr="TE", offered=5000)
s.commit()
for v in s.query(VendorQuote).filter_by(quotation_id=q7.id, vendor_name="VendBulk"): v.packaging = "Bulk"
s.commit()
consolidation.consolidate_and_price(q7, s, settings)
line7 = q7.line_items[0]
check("pkg mismatch -> needs_review", line7["pricing_status"] == "needs_review", line7["pricing_status"])
check("pkg mismatch remark", "packaging mismatch" in line7["remark"].lower(), line7["remark"])

# 8b) HOMOGENEOUS split (both reel) -> priced
q8 = add_quote(s, "PKG2", "TE", 12000)
vq(s, q8, "VA", 1.00, offered=10000); vq(s, q8, "VB", 1.10)
s.commit()
for v in s.query(VendorQuote).filter_by(quotation_id=q8.id): v.packaging = "Reel"
s.commit()
consolidation.consolidate_and_price(q8, s, settings)
check("homogeneous split priced", q8.line_items[0]["pricing_status"] == "priced")

# 9) EXPIRED vendor quote at consolidation -> needs_review
q9 = add_quote(s, "EXP1", "Molex", 100)
vq(s, q9, "VendExp", 2.00)
s.commit()
for v in s.query(VendorQuote).filter_by(quotation_id=q9.id):
    v.valid_until = datetime(2020, 1, 1)  # long past
s.commit()
consolidation.consolidate_and_price(q9, s, settings)
line9 = q9.line_items[0]
check("expired quote -> needs_review", line9["pricing_status"] == "needs_review", line9["pricing_status"])
check("expired remark", "expired" in line9["remark"].lower(), line9["remark"])

# 10) EUD flag set on a dual-use part (FPGA), still priced (gate decides to hold)
q10 = add_quote(s, "EUD1", "Xilinx", 10)
q10.line_items = [{"mpn": "XC7A100T", "manufacturer": "Xilinx", "quantity": 10, "description": "FPGA Artix-7"}]
s.add(VendorQuote(quotation_id=q10.id, vendor_name="VendF", mpn="XC7A100T", cost_price=50.0, currency="USD"))
s.commit()
consolidation.consolidate_and_price(q10, s, settings)
line10 = q10.line_items[0]
check("EUD flagged on FPGA", line10.get("eud_required") is True, str(line10.get("eud_reason")))
check("EUD line still priced", line10["pricing_status"] == "priced")

# 11) CONFIDENCE GATE hardening
from core.email_processor import EmailProcessor
gp = EmailProcessor.__new__(EmailProcessor)
gp.account = "main"; gp.automatic_mode = False
gp.auto_send_confidence = True; gp.min_margin_percent = 8.0; gp.auto_send_max_value = 0.0
gp._send_customer_reply = lambda **kw: False  # suppress real send; confident path -> "suppressed"

def gate_quote(qn, lines, currency_assumed=False, currency="USD"):
    q = Quotation(email_id=1, quote_number=qn, customer_name="C", customer_email="c@x.com",
                  account="main", currency=currency, currency_assumed=currency_assumed,
                  status=QuoteStatus.VENDOR_PRICED, line_items=lines)
    s.add(q); s.flush(); return q

PRICED = {"mpn": "A", "description": "a", "manufacturer": "m", "quantity": 10,
          "unit_price": 10.0, "pricing_status": "priced", "margin_percent": 15.0}
def acts(q): return " ".join(gp.finalize_autonomous_quote(s, q))

a = acts(gate_quote("GATE-OK", [dict(PRICED)]))
check("gate: clean USD quote is confident", "suppressed" in a, a)
# v1.1: an assumed-USD quote is NOT held (USD is the standing International default)
a = acts(gate_quote("GATE-USD-ASSUMED", [dict(PRICED)], currency_assumed=True))
check("gate: assumed-USD does NOT hold", "suppressed" in a, a)
# v1.1: a non-USD sale currency (SGD/EUR) IS held for a human
a = acts(gate_quote("GATE-SGD", [dict(PRICED)], currency="SGD"))
check("gate: non-USD currency -> hold", "escalated_to_dashboard" in a and "non-USD" in a, a)
a = acts(gate_quote("GATE-THIN", [dict(PRICED, margin_percent=5.0)]))
check("gate: thin margin -> hold", "margin floor" in a, a)
a = acts(gate_quote("GATE-EUD", [dict(PRICED, eud_required=True)]))
check("gate: EUD -> hold", "End-User Declaration" in a, a)
gp.auto_send_max_value = 1000.0
a = acts(gate_quote("GATE-VAL", [dict(PRICED, unit_price=500.0)]))  # 500*10 = 5000 > 1000
check("gate: value ceiling -> hold", "ceiling" in a, a)
gp.auto_send_max_value = 0.0

# ===== VENDOR-PO REPLY (a supplier answering OUR PO must NOT be read as a customer PO) =====
from core.database import VendorPO
gp.gmail = SimpleNamespace(mark_as_read=lambda g: None, add_label=lambda g, l: None)
def _vpo_reply(qn, po_no, vstatus, reason="no stock"):
    q = Quotation(email_id=1, quote_number=qn, account="main", currency="USD",
                  status=QuoteStatus.PO_FORWARDED, line_items=[{"mpn": "P", "quantity": 1}])
    s.add(q); s.flush()
    vpo = VendorPO(quotation_id=q.id, vendor_name="V1", vendor_email="v@x.com", po_number=po_no,
                   thread_id="TH-" + po_no, status="placed", line_items=[{"mpn": "P"}])
    s.add(vpo); s.flush()
    gp.claude = SimpleNamespace(classify_po_response=lambda **kw: {"status": vstatus, "reason": reason})
    r = gp._handle_vendor_po_reply(vpo, {"gmail_id": "g", "thread_id": vpo.thread_id,
                                         "subject": f"Re: Purchase Order {po_no}", "body_text": reason}, s)
    return q, vpo, r
# Vendor DECLINES our PO -> cancelled + order flagged for a human; NOT a customer 'thank you for PO'
q_d, vpo_d, r_d = _vpo_reply("VPO-D", "AE-PO-20260710-9001-1", "declined")
check("vendor-po: reply typed vendor_po_reply", r_d["type"] == "vendor_po_reply", r_d)
check("vendor-po: decline -> cancelled", vpo_d.status == "cancelled", vpo_d.status)
check("vendor-po: decline flags order for human", q_d.status == QuoteStatus.AWAITING_APPROVAL, q_d.status.value)
check("vendor-po: decline flagged action", "flagged_for_human_resource" in r_d["actions_taken"], r_d)
# Vendor CONFIRMS -> status advances, no human flag
q_c, vpo_c, r_c = _vpo_reply("VPO-C", "AE-PO-20260710-9002-1", "confirmed", reason="we confirm")
check("vendor-po: confirm -> confirmed", vpo_c.status == "confirmed", vpo_c.status)
check("vendor-po: confirm not flagged", "flagged_for_human_resource" not in r_c["actions_taken"], r_c)

# ===== VENDOR OFFER (Phase C) — a supplier's stock-offer blast is captured as market intel, no reply =====
from core.database import MarketOffer
gp.claude = SimpleNamespace(extract_vendor_offer=lambda **kw: {"items": [
    {"mpn": "MT41K128M16JT-125AAT:K", "manufacturer": "Micron", "unit_price": 5.2, "currency": "USD",
     "date_code": "25+", "lead_time": "2-3 days", "moq": 2000, "quantity": 6000, "packaging": "Reel"},
    {"mpn": "NOPRICE"}]})  # a line with no price must be skipped
gp._extract_name = lambda e: "HTD"
r_off = gp._handle_vendor_offer(s, SimpleNamespace(status=None),
                                {"gmail_id": "g", "thread_id": "t", "subject": "DRAM Offer",
                                 "from_email": "vendor@example.com", "body_text": "MT41K... $5.2/pc DC25+"})
check("offer: captured 1 market offer", r_off == ["captured_1_market_offers"], r_off)
mo = s.query(MarketOffer).filter_by(mpn="MT41K128M16JT-125AAT:K").first()
check("offer: stored price + date code", mo is not None and abs(mo.unit_price - 5.2) < 1e-6 and mo.date_code == "25+", mo)
check("offer: no-price line skipped", s.query(MarketOffer).filter_by(mpn="NOPRICE").first() is None)
check("offer: normalises MPN upper", mo.mpn == "MT41K128M16JT-125AAT:K")

# ===== AUTONOMOUS NEGOTIATION (#4): margin-trim vs re-source, customer target never shared =====
from core.email_processor import EmailProcessor as _EP
from core.database import NegotiationRound, Vendor
np = _EP.__new__(_EP)
np.account = "main"; np.automatic_mode = False; np.autonomous_enabled = True
np.negotiation_min_margin = 10.0; np.vendor_test_email = ""; np.vendor_max_vendors = 1
np.vendor_rfq_cc = []
np.default_margin = 15.0; np.max_vendor_ask = 20.0
np.finalize_autonomous_quote = lambda session, q: ["gate:stub"]  # stub the send path
class _FakeGmail:
    def send_new_email(self, to_email, subject, body_html, cc_emails=None): return {"threadId": "t1", "id": "m1"}
np.gmail = _FakeGmail()

def make_neg_quote(qn, mpn, landed, cost, margin=15.0):
    q = Quotation(email_id=1, quote_number=qn, account="main", currency="USD", status=QuoteStatus.SENT,
                  line_items=[{"mpn": mpn, "manufacturer": "TE", "quantity": 100, "description": mpn,
                               "landed_cost": landed, "cost_price": cost, "cost_currency": "USD",
                               "margin_percent": margin, "unit_price": round(landed*(1+margin/100), 4),
                               "pricing_status": "priced"}])
    s.add(q); s.flush()
    nr = NegotiationRound(quotation_id=q.id, round_number=1); s.add(nr); s.flush()
    return q, nr

# Case A: target implies 12% margin (>= 10% floor) -> trim margin, quote at target
qA, nrA = make_neg_quote("NEG-A", "NEGP1", landed=100.0, cost=100.0)
actsA = np._negotiate_autonomous(s, SimpleNamespace(status=None), qA, nrA,
                                 [{"mpn": "NEGP1", "target_price": 112.0}], "USD")
lA = qA.line_items[0]
check("neg A: quoted at target 112", abs(lA["unit_price"] - 112.0) < 1e-6, str(lA["unit_price"]))
check("neg A: margin trimmed to ~12%", abs(lA["margin_percent"] - 12.0) < 0.5, str(lA["margin_percent"]))
check("neg A: negotiated flag set", lA.get("negotiated") is True)
check("neg A: met by margin (no re-source)", any("met" in a for a in actsA), str(actsA))
consolidation.consolidate_and_price(qA, s, settings)  # re-consolidation must NOT clobber it
check("neg A: consolidation preserves negotiated price", abs(qA.line_items[0]["unit_price"] - 112.0) < 1e-6)

# Case B: target implies 5% (< 10% floor) -> re-source at OUR target cost, customer target NOT shared
s.add(Vendor(name="ANYbroker", vendor_type="non_authorized", brands=["ANY"], email="v@x.com", active=True)); s.commit()
qB, nrB = make_neg_quote("NEG-B", "NEGP2", landed=100.0, cost=100.0)
actsB = np._negotiate_autonomous(s, SimpleNamespace(status=None), qB, nrB,
                                 [{"mpn": "NEGP2", "target_price": 105.0}], "USD")
lB = qB.line_items[0]
check("neg B: line held for re-source", lB["pricing_status"] == "needs_review", lB["pricing_status"])
check("neg B: re-sourced at our target cost", any("resourced" in a for a in actsB), str(actsB))
check("neg B: back to SOURCING_VENDORS", qB.status == QuoteStatus.SOURCING_VENDORS)
# our target cost = cost * (target_landed/landed) = 100 * (105/1.1)/100 = 95.4545
vq_target = s.query(VendorQuote).filter_by(quotation_id=qB.id).first()  # none yet; check the RFQ carried it
from core.vendor_sourcing import VendorRFQ as _VRFQ
check("neg B: a re-source RFQ was recorded", s.query(_VRFQ).filter_by(quotation_id=qB.id).count() >= 1)

# ===== REVISED QUOTE: no internal cost/margin leak + Last Quoted|Your Target|Revised columns =====
from core.consolidation import is_internal_remark
_leaks = ["Target buy price was USD 11.4740 (please match or beat) — quoted 11.48",
          "Revised to your target (margin now 11%)", "Re-sourcing at our target buy cost USD 42.3"]
check("neg remark: leaked phrases flagged internal", all(is_internal_remark(x) for x in _leaks))
check("neg remark: legit remark not flagged", not is_internal_remark("Quoted Samsung p/n") and not is_internal_remark("Exact P/N"))
# Case A's negotiated line carries prev/target and a customer-safe remark (internal detail off-quote).
check("neg A: prev price captured", abs(lA.get("prev_unit_price", 0) - 115.0) < 1e-6, str(lA.get("prev_unit_price")))
check("neg A: customer target captured", abs(lA.get("customer_target_price", 0) - 112.0) < 1e-6)
check("neg A: remark is customer-safe", lA.get("remark") == "Revised price" and not is_internal_remark(lA.get("remark")))
check("neg A: internal note kept off the customer field", "margin now" in (lA.get("internal_note") or ""))
# Force a leaky remark onto the line and prove the render-time safety net strips it.
_line = dict(lA); _line["remark"] = "Target buy price was USD 95.4545; quoted at target"
_html = np._build_quotation_html(qA, [_line], [], "USD", 15, is_update=True)
_low = _html.lower()
check("revised html: no internal text", not any(m in _low for m in ("target buy", "match or beat", "margin now", "quoted at target", "re-sourcing")), "leak in customer html")
check("revised html: Last Quoted column", "last quoted (usd)" in _low)
check("revised html: Your Target column", "your target (usd)" in _low)
check("revised html: Revised Price header", "revised price (usd)" in _low)
check("revised html: shows last quoted + target values", "USD 115.00" in _html and "USD 112.00" in _html)

# ===== ADAPTIVE NEGOTIATION: proportional + capped vendor ask, then margin flex to our best =====
# We quoted 115 (cost 100 +15%). Customer target 100 (= implies 0% margin < floor) -> re-source,
# asking the vendor for ~the % that keeps our default margin at the target (proportional, ~13%).
qR, nrR = make_neg_quote("NEG-R", "NEGR", landed=100.0, cost=100.0)
np._negotiate_autonomous(s, SimpleNamespace(status=None), qR, nrR, [{"mpn": "NEGR", "target_price": 100.0}], "USD")
lR = qR.line_items[0]
check("neg R: re-sourced (needs_review)", lR["pricing_status"] == "needs_review")
check("neg R: customer target retained", abs(lR.get("customer_target_price", 0) - 100.0) < 1e-6)
check("neg R: proportional vendor ask ~13%", "~13% off" in (lR.get("internal_note") or ""), lR.get("internal_note"))
# BLIND LOWBALL: target 75 (a 35% cut) -> the vendor ask is CAPPED at max_vendor_ask (20%), never 35%.
qL, nrL = make_neg_quote("NEG-L", "NEGL", landed=100.0, cost=100.0)
np._negotiate_autonomous(s, SimpleNamespace(status=None), qL, nrL, [{"mpn": "NEGL", "target_price": 75.0}], "USD")
lL = qL.line_items[0]
check("neg L: blind lowball ask CAPPED at 20%", "~20% off" in (lL.get("internal_note") or ""), lL.get("internal_note"))
# Vendor comes back cheaper (88) -> margin flexes within [10,15] to land AT the customer target.
s.add(VendorQuote(quotation_id=qR.id, vendor_name="V", mpn="NEGR", cost_price=88.0, currency="USD")); s.commit()
consolidation.consolidate_and_price(qR, s, settings)
lR2 = qR.line_items[0]
check("neg R: cheaper cost -> quote AT customer target", abs(lR2["unit_price"] - 100.0) < 1.0, str(lR2["unit_price"]))
check("neg R: margin flexed into 10-15 band", 10.0 <= lR2["margin_percent"] <= 15.0, str(lR2["margin_percent"]))
check("neg R: negotiated line locked", lR2.get("negotiated") is True)
# Lowball vendor best (85) still can't reach 75 -> quote at the FLOOR margin = our best price (>75, <115).
s.add(VendorQuote(quotation_id=qL.id, vendor_name="V", mpn="NEGL", cost_price=85.0, currency="USD")); s.commit()
consolidation.consolidate_and_price(qL, s, settings)
lL2 = qL.line_items[0]
check("neg L: best price at floor margin (10%)", abs(lL2["margin_percent"] - 10.0) < 0.6, str(lL2["margin_percent"]))
check("neg L: best is above target but below old quote", 75.0 < lL2["unit_price"] < 115.0, str(lL2["unit_price"]))

# ===== PARTIAL QUOTE: newly_priced ("New") tracking across consolidation rounds =====
qP = Quotation(email_id=1, quote_number="PARTIAL1", account="main", currency="USD",
               status=QuoteStatus.SOURCING_VENDORS,
               line_items=[{"mpn": "PA", "manufacturer": "TE", "quantity": 100, "description": "a"},
                           {"mpn": "PB", "manufacturer": "TE", "quantity": 100, "description": "b"}])
s.add(qP); s.flush()
from core.database import VendorRFQ as _VRFQ
s.add(VendorQuote(quotation_id=qP.id, vendor_name="V1", mpn="PA", cost_price=1.0, currency="USD"))
rfqB = _VRFQ(quotation_id=qP.id, vendor_name="V2", requested_mpns=["PB"], status="sent")  # PB's vendor silent
s.add(rfqB); s.commit()
consolidation.consolidate_and_price(qP, s, settings)  # round 1: only PA has a cost, PB awaiting vendor
r1 = {it["mpn"]: it for it in qP.line_items}
check("partial R1: PA priced", r1["PA"]["pricing_status"] == "priced")
check("partial R1: PA not tagged New (first round)", not r1["PA"].get("newly_priced"))
check("partial R1: PB pending (awaiting vendor, not NO BID)", r1["PB"]["pricing_status"] == "pending" and r1["PB"]["unit_price"] is None)
rfqB.status = "replied"; s.add(VendorQuote(quotation_id=qP.id, vendor_name="V2", mpn="PB", cost_price=2.0, currency="USD")); s.commit()
consolidation.consolidate_and_price(qP, s, settings)  # round 2: PB's vendor replied late
r2 = {it["mpn"]: it for it in qP.line_items}
check("partial R2: PB now priced", r2["PB"]["pricing_status"] == "priced")
check("partial R2: PB tagged New (late line)", r2["PB"].get("newly_priced") is True)
check("partial R2: PA not tagged New (already priced)", not r2["PA"].get("newly_priced"))

s.close()

# ===== CRASH RECOVERY: resume an inquiry stranded before its RFQs went to vendors (isolated DB) =====
from core.email_processor import EmailProcessor as _EPR
from core.database import Email as _Email, EmailType as _ET, EmailStatus as _ES, VendorRFQ as _VRFQ3
import os as _os
_rdb = DB_FILE.replace(".db", "_recover.db")
if _os.path.exists(_rdb):
    _os.remove(_rdb)
_reng, _RS = init_database("sqlite:///" + _rdb)
rs = _RS()
rp = _EPR.__new__(_EPR)
rp.account = "main"; rp.automatic_mode = False; rp.autonomous_enabled = True
rp.vendor_test_email = ""; rp.vendor_max_vendors = 1
rp.vendor_rfq_cc = []
class _FakeGmail2:
    def send_new_email(self, to_email, subject, body_html, cc_emails=None): return {"threadId": "t9", "id": "m9"}
rp.gmail = _FakeGmail2()
rs.add(Vendor(name="RecovBroker", vendor_type="non_authorized", brands=["ANY"], email="r@x.com", active=True))
# Simulate: email row committed + quotation created (AWAITING_PRICING) but RFQs never sent (power cut)
em = _Email(gmail_id="crash123", account="main", from_email="c@x.com", subject="RFQ",
            email_type=_ET.RFQ, status=_ES.NEW)
rs.add(em); rs.flush()
qc = Quotation(email_id=em.id, quote_number="CRASH-1", account="main", currency="USD",
               status=QuoteStatus.AWAITING_PRICING,
               line_items=[{"mpn": "CR1", "manufacturer": "TE", "quantity": 10, "description": "x"}])
rs.add(qc); rs.commit()
skip, acts = rp._resume_incomplete(em, {"gmail_id": "crash123"}, rs)
check("recovery: does NOT skip a stranded RFQ", skip is False, str(acts))
check("recovery: sent the missing vendor RFQs", any("resumed_sourced" in a for a in acts), str(acts))
check("recovery: status advanced to SOURCING_VENDORS", qc.status == QuoteStatus.SOURCING_VENDORS)
check("recovery: a VendorRFQ now exists", rs.query(_VRFQ3).filter_by(quotation_id=qc.id).count() >= 1)
skip2, acts2 = rp._resume_incomplete(em, {"gmail_id": "crash123"}, rs)  # idempotent: already sourced -> skip
check("recovery: idempotent (2nd pass skips, no dup RFQ)",
      skip2 is True and rs.query(_VRFQ3).filter_by(quotation_id=qc.id).count() == 1, str(acts2))
rs.close(); _reng.dispose()
try:
    _os.remove(_rdb)
except OSError:
    pass

# ===== INTERNAL-TEAM FORWARD: process forwarded customer inquiries; skip genuine chatter =====
_idb = DB_FILE.replace(".db", "_internal.db")
if _os.path.exists(_idb):
    _os.remove(_idb)
_ieng, _ISess = init_database("sqlite:///" + _idb)
_ISess().close()
ip = _EPR.__new__(_EPR)
ip.account = "main"; ip.database_url = "sqlite:///" + _idb
ip.internal_domains = ["company-b.example"]; ip.autonomous_enabled = False
ip.learning = SimpleNamespace(build_classification_context=lambda **k: "")
ip.gmail = SimpleNamespace(mark_as_read=lambda g: None, add_label=lambda g, l: None)
ip.claude = SimpleNamespace(t="other",
                            classify_email=lambda **k: {"type": ip.claude.t, "urgency": "normal", "confidence": 0.9})
ip._find_quotation_for_thread = lambda tid, s: None
ip._extract_and_store_lead = lambda s, db, ed, c: None
ip._extract_name = lambda e: "TeamMember"
_processed = []
ip._handle_rfq = lambda s, db, ed: (_processed.append("rfq") or ["handled_rfq"])
def _ied(frm, gid):
    return {"gmail_id": gid, "thread_id": None, "from_email": frm, "to_email": "pm@company-b.example",
            "subject": "RFQ", "body_text": "please quote PV40-27B24 20pcs", "body_html": "",
            "has_attachments": False, "attachment_names": [], "attachment_refs": []}
ip.claude.t = "other"
rA = ip.process_email(_ied("intl.sales@company-b.example", "g_int_other"))
check("internal chatter ('other') is skipped", rA.get("type") == "internal" and rA.get("actions_taken") == ["skipped_internal_other"], str(rA))
ip.claude.t = "rfq"
rB = ip.process_email(_ied("intl.sales@company-b.example", "g_int_rfq"))
check("internal team-forwarded RFQ is PROCESSED (not dropped)", "rfq" in _processed and rB.get("type") == "rfq", str(rB))
_ieng.dispose()
try:
    _os.remove(_idb)
except OSError:
    pass

# ===== VENDOR REPLY (even from an external gmail) must NOT be processed as a customer email =====
_vdb = DB_FILE.replace(".db", "_vendorreply.db")
if _os.path.exists(_vdb):
    _os.remove(_vdb)
_veng, _VSess = init_database("sqlite:///" + _vdb)
vseed = _VSess()
vseed.add(_VRFQ3(quotation_id=1, vendor_name="ST Micro India Distributor", thread_id="VT1", status="sent"))
vseed.commit(); vseed.close()
vp = _EPR.__new__(_EPR)
vp.account = "main"; vp.database_url = "sqlite:///" + _vdb
class _G3:
    def mark_as_read(self, gid): pass
    def add_label(self, gid, lbl): pass
vp.gmail = _G3()
res = vp.process_email({"gmail_id": "vr1", "thread_id": "VT1", "from_email": "vendor2@example.com",
                        "subject": "Re: RFQ AE-Q-0013 - please find quotation"})
check("vendor reply NOT treated as customer inquiry",
      res.get("type") == "vendor_reply" and res.get("actions_taken") == ["skipped_vendor_reply_handled_by_monitor"],
      str(res))
# A genuinely NEW customer email (unknown thread) is NOT short-circuited by this guard
res2 = None
try:
    vp.process_email({"gmail_id": "cust1", "thread_id": "UNKNOWN", "from_email": "buyer@acme.com", "subject": "RFQ"})
except Exception:
    res2 = "reached_classification"  # expected: no claude stub -> fails AFTER passing the vendor guard
check("unknown-thread email is NOT short-circuited as vendor", res2 == "reached_classification")
_veng.dispose()
try:
    _os.remove(_vdb)
except OSError:
    pass

# 6) CURRENCY INFERENCE (no DB) — v1.1: default USD; detect stated USD/SGD/EUR/INR; no .in->INR.
from core.email_processor import EmailProcessor
ep = EmailProcessor.__new__(EmailProcessor)
ep.account = "main"
def infer(bom, email, cust=None): return ep._infer_quote_currency(bom, email, cust or {})
check("ccy stated USD", infer([{"currency": "USD"}], {"from_email": "x@y.com"})[:2] == ("USD", False))
check("ccy symbol USD", infer([{"target_price": "$1.25"}], {"from_email": "x@y.com"})[:2] == ("USD", False))
check("ccy symbol SGD (S$)", infer([{"target_price": "S$ 4.20"}], {"from_email": "x@y.com"})[:2] == ("SGD", False))
check("ccy symbol EUR (€)", infer([{"target_price": "€ 3.10"}], {"from_email": "x@y.com"})[:2] == ("EUR", False))
check("ccy symbol INR still detected", infer([{"target_price": "₹ 39.60"}], {"from_email": "x@y.com"})[:2] == ("INR", False))
# v1.1: an Indian domain no longer forces INR — International defaults to USD (assumed).
check("ccy .in domain -> USD default", infer([{}], {"from_email": "buyer@acme.co.in"})[:2] == ("USD", True))
check("ccy no signal -> USD default", infer([{}], {"from_email": "buyer@acme.com"})[:2] == ("USD", True))

# 7) ACK authorized-parts block
blk = ep._build_authorized_html([{"mpn": "STTH5L06", "manufacturer": "ST", "quantity": 20}])
check("ack authorized heading", "supply these directly" in blk)
check("ack authorized lists part", "STTH5L06" in blk)
check("ack empty when none", ep._build_authorized_html([]) == "")

# 7b) ACK missing-info questions — ask ONLY for what the customer left out
# Bare part number: no qty, no make -> asks qty + make + lead time (v1.1 does NOT ask currency: USD default)
bare = ep._build_missing_info_html([{"mpn": "PK8071305554400"}], currency_assumed=True)
check("missing: asks quantity", "quantity" in bare.lower())
check("missing: asks make", "manufacturer / make" in bare)
check("missing: does NOT ask currency", "currency" not in bare.lower())
check("missing: asks lead time", "lead time" in bare.lower())
# Fully-specified line + currency stated -> nothing to ask, block is empty
full = ep._build_missing_info_html(
    [{"mpn": "STTH5L06", "manufacturer": "ST", "quantity": 20}], currency_assumed=False)
check("missing: empty when complete", full == "")
# Make already resolved by Mouser, only qty missing -> asks qty (+lead time), NOT make
resolved = ep._build_missing_info_html(
    [{"mpn": "PK8071305554400", "manufacturer": "Intel"}], currency_assumed=False)
check("missing: qty asked when make known", "quantity" in resolved.lower())
check("missing: make NOT re-asked when resolved", "manufacturer / make" not in resolved)
# Qty of 0 counts as missing
zero = ep._build_missing_info_html(
    [{"mpn": "X", "manufacturer": "ST", "quantity": 0}], currency_assumed=False)
check("missing: qty=0 treated as missing", "quantity" in zero.lower())

# 12) EUD keyword screener
from knowledge.compliance_watchlist import check_eud
check("eud: FPGA flagged", check_eud("XC7A100T", "Xilinx", "FPGA")[0] is True)
check("eud: RF amp flagged", check_eud("X", "Qorvo", "RF power amplifier")[0] is True)
check("eud: rad-hard flagged", check_eud("X", "Y", "radiation-tolerant memory")[0] is True)
check("eud: plain resistor clean", check_eud("CFR200J680K", "TE", "Resistor 680k 2W")[0] is False)
check("eud: user keyword via settings",
      check_eud("G1", "Y", "gyroscope module",
                settings=SimpleNamespace(eud_watchlist_keywords="gyroscope"))[0] is True)

# 13) validity parser
from core.vendor_sourcing import _parse_validity
base = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
check("validity 48 hours", _parse_validity("valid 48 hours", base) == base + timedelta(hours=48))
check("validity 7 days", _parse_validity("offer valid for 7 days", base) == base + timedelta(days=7))
check("validity explicit date", _parse_validity("valid till 2026-07-10", base).date() == datetime(2026,7,10).date())
check("validity none", _parse_validity("subject to prior sale", base) is None)

# 14) _expired_quote_lines helper
past = datetime(2020,1,1,tzinfo=timezone.utc).isoformat()
future = datetime(2999,1,1,tzinfo=timezone.utc).isoformat()
qobj = SimpleNamespace(line_items=[{"mpn": "OLD", "quote_valid_until": past},
                                   {"mpn": "NEW", "quote_valid_until": future},
                                   {"mpn": "NONE"}])
exp = ep._expired_quote_lines(qobj)
check("expired-lines helper finds past", "OLD" in exp and "NEW" not in exp and "NONE" not in exp, str(exp))

# 15) LEAD-TIME TIE-BREAK (#3): equal cost -> shortest lead time wins ("2 Days" beats "1 Week")
qLT = add_quote(s, "LEADTIE", "TE", 5000)
vq(s, qLT, "Slow", 0.50, lead_time="1 Week")
vq(s, qLT, "Fast", 0.50, lead_time="2 Days")
vq(s, qLT, "Slower", 0.50, lead_time="2 Weeks")
s.commit(); consolidation.consolidate_and_price(qLT, s, settings)
check("tie-break: shortest lead selected", qLT.line_items[0]["selected_vendor"] == "Fast",
      qLT.line_items[0]["selected_vendor"])
# Displayed lead is now EXW-Singapore = vendor lead + inbound transit (2 Days + 2 wk transit ≈ 2 weeks).
check("tie-break: fast lead + SG transit", qLT.line_items[0]["lead_time"] == "2 weeks",
      qLT.line_items[0]["lead_time"])

# 16) MANUAL VENDOR PIN (#2a): a human pins the pricier vendor -> it's used, not the auto-cheapest
qPin = add_quote(s, "PINME", "TE", 1000)
vq(s, qPin, "Cheap", 1.00)
pricey = vq(s, qPin, "Pricey", 1.50)
s.commit()  # assign VendorQuote ids
consolidation.consolidate_and_price(qPin, s, settings)
check("pin: auto picks cheapest first", qPin.line_items[0]["selected_vendor"] == "Cheap")
li = [dict(it) for it in qPin.line_items]; li[0]["pinned_vendor_quote_id"] = pricey.id
qPin.line_items = li
consolidation.consolidate_and_price(qPin, s, settings)
check("pin: honoured -> pricey selected", qPin.line_items[0]["selected_vendor"] == "Pricey",
      qPin.line_items[0]["selected_vendor"])
check("pin: resale uses pinned cost", abs(qPin.line_items[0]["unit_price"] - round(1.50*1.15, 4)) < 1e-6)
check("pin: flag set", qPin.line_items[0].get("vendor_pinned") is True)

# 17) ALTERNATIVES: no Match % shown (#4)
alt_html = ep._build_alternatives_html([{"original_mpn": "A", "brand": "Hokuriku",
                                         "suggested_mpn": "B", "comparison_notes": "same specs",
                                         "match_percentage": 75}])
check("alt: no 'Match %' text", "Match %" not in alt_html)
check("alt: no 75% shown", "75%" not in alt_html and "(alternative)" not in alt_html)
check("alt: still lists suggestion", "B" in alt_html and "Hokuriku" in alt_html)
check("alt: empty when none", ep._build_alternatives_html([]) == "")

# 17b) CROSS-REFERENCE GATES — the agent once emailed a customer 'B2405LS-1WR3 (CLAF Power)',
# invented by dropping a letter from their Mornsun IB2405LS-1WR3. Three gates now stand in the way:
# brand must be a real authorised line, a series only if catalogue-verified, an MPN only if confirmed.
from knowledge.cross_reference import CrossReference
_cr = CrossReference()
check("xref: catalogue loaded", "CLAF Power" in _cr.brands_with_data(), _cr.brands_with_data())
# GATE 1 — a series must exist in manufacturer-verified data
check("xref: fabricated MPN blocked", not _cr.series_exists("CLAF Power", "B2405LS-1WR3"))
check("xref: real series passes", _cr.series_exists("CLAF Power", "DES1-F"))
check("xref: MPN on a real series passes", _cr.series_exists("CLAF Power", "DES1-F2405"))
check("xref: other maker's series blocked", not _cr.series_exists("Mornsun", "DES1-F"))
check("xref: empty input blocked", not _cr.series_exists("CLAF Power", ""))
# GATE 2 — a specific MPN is only ever emailed once a human confirmed that cross
check("xref: unconfirmed cross returns None",
      _cr.confirmed_cross("IB2405LS-1WR3", "Mornsun") is None)
check("xref: example row is ignored", _cr.confirmed_cross("EXAMPLE-DELETE-ME") is None)
# An unconfirmed hit must go out at SERIES level and must not print a bare part number
_pending = ep._build_alternatives_html([{
    "original_mpn": "IB2405LS-1WR3", "original_manufacturer": "Mornsun",
    "brand": "CLAF Power", "series": "DES1-F", "suggested_mpn": None,
    "fit": "direct", "comparison_notes": "wide 18-36V in, 5V out, 1W, 3kVDC, SIP",
    "needs_mpn_confirmation": True}])
check("xref: pending row shows the series", "DES1-F" in _pending)
check("xref: pending row promises confirmation", "on confirmation" in _pending)
check("xref: pending row never prints the invented MPN", "B2405LS-1WR3</strong>" not in _pending)
# A real authorised brand with no verified series/MPN is now offered at BRAND level (cover EVERY
# line, not just CLAF) — but it must NEVER print a specific part number.
_brand_only = ep._build_alternatives_html([{"original_mpn": "X", "original_manufacturer": "SomeMaker",
                                            "brand": "CLAF Power", "suggested_mpn": None, "series": None,
                                            "brand_level": True, "comparison_notes": "we carry equivalent DC-DC converters"}])
check("xref: brand-only row is shown", _brand_only != "" and "CLAF Power" in _brand_only)
check("xref: brand-only promises confirmation", "on confirmation" in _brand_only)
check("xref: brand-only prints no part number", "Equivalent from our line" in _brand_only)
# The customer's example: a diode from an unauthorised maker → suggested from our diode line (SMC),
# at brand level, no invented part number.
_diode = ep._build_alternatives_html([{"original_mpn": "1N4007-FAKE", "original_manufacturer": "SomeDiodeCo",
                                       "brand": "SMC Diode Solutions", "suggested_mpn": None, "series": None,
                                       "brand_level": True, "comparison_notes": "equivalent rectifier diode"}])
check("xref: diode routed to our diode line", "SMC Diode Solutions" in _diode and _diode != "")
check("xref: diode row has no fabricated MPN", "1N4007-FAKE</strong>" not in _diode)
# A row with NO real brand at all is still not printable
check("xref: no-brand row dropped",
      ep._build_alternatives_html([{"original_mpn": "X", "brand": None,
                                    "suggested_mpn": None, "series": None}]) == "")
# A partial fit must say so in words, not bury it in the notes
_partial = ep._build_alternatives_html([{
    "original_mpn": "IB2405LS-1WR3", "brand": "CLAF Power", "series": "DFLS1",
    "suggested_mpn": None, "fit": "partial", "comparison_notes": "1500VDC vs 3000VDC required",
    "caveats": "confirm isolation rating"}])
check("xref: partial fit flagged in words", "not a drop-in" in _partial.lower())
check("xref: caveats surfaced", "confirm isolation rating" in _partial)

# 18) TARGET-PRICE ACK (#1)
ack = ep._build_target_ack_html("Sanjay Kumar", "INR", [{"mpn": "MBA02040C2202FCT00", "target_price": "25"}])
check("ack: greets first name only", "Sanjay" in ack and "Kumar" not in ack)
check("ack: shows target price", "25" in ack and "INR" in ack)
check("ack: promises a revised quote", "revised quotation" in ack.lower())
check("ack: says we'll match", "match" in ack.lower())

# 19) INR VENDOR COST -> USD SALE (usd_import): an Indian vendor quotes INR on a USD quote — converts
# at FX (usd_cost = 100/83) then margin (expenses=0 in these test settings).
qIU = add_quote(s, "INRUSD", "TE", 1000)          # USD quote (add_quote default currency)
vq(s, qIU, "IndianVendor", 100.0, currency="INR")  # vendor quotes INR
s.commit(); consolidation.consolidate_and_price(qIU, s, settings)  # fx = usd_inr_fx_rate = 83
lineIU = qIU.line_items[0]
check("INR->USD priced (not skipped)", lineIU["pricing_status"] == "priced", lineIU.get("pricing_note"))
check("INR->USD resale in USD magnitude", abs(lineIU["unit_price"] - round((100.0 / 83.0) * 1.15, 4)) < 1e-3,
      lineIU["unit_price"])
check("INR->USD cost kept in INR", lineIU["cost_currency"] == "INR" and abs(lineIU["cost_price"] - 100.0) < 1e-6)

# 20) TOO-CHEAP OUTLIER (counterfeit-risk) — cheapest far below the panel median -> authenticity flag
qAuth = add_quote(s, "AUTHLOW", "TE", 100)
vq(s, qAuth, "Normal1", 10.0); vq(s, qAuth, "Normal2", 10.0); vq(s, qAuth, "SuspectLow", 3.0)
s.commit(); consolidation.consolidate_and_price(qAuth, s, settings)
lineA = qAuth.line_items[0]
check("auth: cheapest suspect selected", lineA["selected_vendor"] == "SuspectLow", lineA["selected_vendor"])
check("auth: flagged for authenticity", lineA.get("authenticity_review") is True, lineA.get("authenticity_review"))
check("auth: remark says verify", "verify" in (lineA.get("remark") or "").lower(), lineA.get("remark"))
# A normal spread (10/11/12) must NOT be flagged
qOK = add_quote(s, "AUTHOK", "TE", 100)
vq(s, qOK, "V1", 10.0); vq(s, qOK, "V2", 11.0); vq(s, qOK, "V3", 12.0)
s.commit(); consolidation.consolidate_and_price(qOK, s, settings)
check("auth: normal spread not flagged", not qOK.line_items[0].get("authenticity_review"))

engine.dispose()
try:
    os.remove(DB_FILE)
except OSError:
    pass

print(f"\n==== {len(passed)} passed, {len(failed)} failed ====")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
