"""
Vendor scorecard (E6) — learned priority from real outcomes.

Recomputes, per vendor with history, a 0-100 score blended from:
  - win rate      (how often their cost was the lowest selected)   weight 0.35
  - reply rate    (how often they answer an RFQ)                    weight 0.30
  - response speed(how fast they reply)                             weight 0.20
  - lead time     (how short their quoted lead time is)            weight 0.15

The score maps to a `dynamic_priority` (1 best .. 5). Routing uses your MANUAL priority when you
set one, else this learned priority — so the agent self-ranks the 264 vendors you haven't ranked,
and you can always override.
"""

import re
from datetime import datetime, timezone
from loguru import logger
from core.database import Vendor, VendorRFQ, VendorQuote

_RESP_CAP_H = 72.0    # a reply within ~3 days scores full marks; slower decays to 0
_LEAD_CAP_D = 84.0    # a ~12-week lead scores 0; shorter scores higher


def _lead_days(text):
    """Parse a lead-time string ('8 weeks', '4-6 wks', '10 days', 'ex-stock') to days, or None."""
    if not text:
        return None
    t = str(text).strip().lower()
    if any(w in t for w in ("stock", "ready", "immediate", "available")):
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?)\s*)?(week|wk|day|month|mon)", t)
    if not m:
        return None
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else lo
    val = (lo + hi) / 2.0
    unit = m.group(3)
    if unit.startswith("day"):
        return val
    if unit.startswith("mon"):
        return val * 30.0
    return val * 7.0  # week / wk


def effective_priority(v) -> int:
    """Routing priority: your MANUAL priority if set, else the learned dynamic_priority, else 3."""
    if v.priority is not None:
        return v.priority
    if v.dynamic_priority is not None:
        return v.dynamic_priority
    return 3


def compute_vendor_scores(session) -> int:
    """Recompute the scorecard for every vendor that has RFQ/quote history. Returns count scored."""
    vendor_ids = set()
    for (vid,) in session.query(VendorRFQ.vendor_id).distinct():
        if vid:
            vendor_ids.add(vid)
    for (vid,) in session.query(VendorQuote.vendor_id).distinct():
        if vid:
            vendor_ids.add(vid)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    scored = 0
    for vid in vendor_ids:
        v = session.get(Vendor, vid)
        if not v:
            continue
        rfqs = session.query(VendorRFQ).filter_by(vendor_id=vid).all()
        quotes = session.query(VendorQuote).filter_by(vendor_id=vid).all()

        attempts = [r for r in rfqs if r.status in ("sent", "test_sent", "replied")]
        replied = [r for r in rfqs if r.status == "replied" or r.replied_at is not None]
        sent_n, replied_n = len(attempts), len(replied)
        reply_rate = (replied_n / sent_n) if sent_n else 0.0

        resp = [((r.replied_at - r.sent_at).total_seconds() / 3600.0)
                for r in replied if r.sent_at and r.replied_at
                and r.replied_at >= r.sent_at]
        avg_resp = (sum(resp) / len(resp)) if resp else None

        q_n = len(quotes)
        won_n = sum(1 for q in quotes if q.is_selected)
        win_rate = (won_n / q_n) if q_n else 0.0

        leads = [d for d in (_lead_days(q.lead_time) for q in quotes) if d is not None]
        avg_lead = (sum(leads) / len(leads)) if leads else None

        if sent_n > 0 or q_n > 0:
            s_resp = max(0.0, 1 - avg_resp / _RESP_CAP_H) if avg_resp is not None else 0.5
            s_lead = max(0.0, 1 - avg_lead / _LEAD_CAP_D) if avg_lead is not None else 0.5
            score = round(100 * (0.35 * win_rate + 0.30 * reply_rate + 0.20 * s_resp + 0.15 * s_lead), 1)
            dyn = 1 if score >= 80 else 2 if score >= 60 else 3 if score >= 40 else 4 if score >= 20 else 5
        else:
            score, dyn = None, None

        v.rfqs_sent, v.rfqs_replied = sent_n, replied_n
        v.quotes_received, v.quotes_won = q_n, won_n
        v.avg_response_hours = round(avg_resp, 1) if avg_resp is not None else None
        v.avg_lead_days = round(avg_lead, 1) if avg_lead is not None else None
        v.score, v.dynamic_priority, v.last_scored_at = score, dyn, now
        scored += 1

    session.commit()
    logger.info(f"Vendor scorecard recomputed for {scored} vendor(s)")
    return scored
