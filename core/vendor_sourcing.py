"""
Vendor sourcing — the "Purchase" role of the autonomous pipeline (Phase 2).

For each BOM part, routes to the matching vendors (brand match + open-market brokers),
groups parts per vendor, and emails EACH vendor one RFQ asking for their best COST price.
Each send is tracked as a VendorRFQ row so Phase 3 can match the vendor's reply.

Note: vendor emails deliberately do NOT reveal the end customer's identity — a distributor
protects the customer relationship. Only the internal quote number is referenced.
"""

import html
import re
from datetime import datetime, timezone, timedelta
from loguru import logger
from core.database import Vendor, VendorRFQ, VendorQuote


def _to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


_EMAIL_ADDR_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _addrs(raw) -> list:
    """All email addresses in a raw header string, lowercased, de-duped, order-preserving."""
    out = []
    for a in _EMAIL_ADDR_RE.findall(str(raw or "")):
        a = a.lower()
        if a not in out:
            out.append(a)
    return out


# Automated / bounce senders — a delivery-failure ("Mail Delivery Subsystem") or no-reply address is
# never a real vendor contact, so it must never be learned onto a vendor's row.
_SYSTEM_LOCALPARTS = {"mailer-daemon", "mailerdaemon", "postmaster", "no-reply", "noreply",
                      "no_reply", "do-not-reply", "donotreply", "bounce", "bounces"}


def _is_system_addr(a: str) -> bool:
    """True for an automated/bounce/no-reply sender (not a contactable person)."""
    if not a or "@" not in a:
        return True
    return a.split("@", 1)[0] in _SYSTEM_LOCALPARTS


def _parse_validity(text, base_dt):
    """Turn a vendor validity phrase into an absolute expiry timestamp.
    Handles relative ("48 hours", "7 days", "2 weeks") and absolute ("till 2026-07-10") forms.
    Returns None if nothing parseable (an open-ended / no-validity quote)."""
    if not text or not str(text).strip():
        return None
    s = str(text).strip().lower()
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)  # explicit date
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 23, 59, tzinfo=timezone.utc)
        except ValueError:
            pass
    m = re.search(r"(\d+(?:\.\d+)?)\s*(hour|hr|day|week|month)", s)  # relative window
    if m:
        n = float(m.group(1))
        unit = m.group(2)
        hours = {"hour": 1, "hr": 1, "day": 24, "week": 168, "month": 720}[unit]
        return base_dt + timedelta(hours=n * hours)
    return None


class VendorSourcer:
    def __init__(self, gmail_client, account: str, automatic: bool = True,
                 vendor_test_email: str = "", max_vendors: int = 8, rfq_cc: list = None):
        self.gmail = gmail_client
        self.account = account
        self.entity = "Company B International"  # v1.1 — Company B International only
        self.currency = "USD"
        self.automatic = bool(automatic)                    # False = testing: never email real vendors
        self.vendor_test_email = (vendor_test_email or "").strip()
        self.max_vendors = int(max_vendors) if max_vendors else 0  # cap vendors emailed per part (0 = no cap)
        # Internal oversight addresses CC'd on every REAL vendor RFQ. Applied only on real sends —
        # NOT on test redirects/suppressed sends, so testing never emails the oversight team.
        self.rfq_cc = [e for e in (rfq_cc or []) if e]

    def _cc(self):
        """CC list for a send — the oversight addresses, but only when actually emailing a real vendor."""
        return self.rfq_cc if (self.automatic and self.rfq_cc) else None

    @staticmethod
    def _brands_of(v: Vendor) -> list[str]:
        return [b.upper() for b in (v.brands or [])]

    def route(self, bom_items: list, session, exclude_customer_email: str = "") -> tuple[dict, list]:
        """Map parts to vendors.
        Returns ({vendor_id: [vendor, [parts]]}, [unsourced_parts]).
        Each part goes to its brand-matching vendors (normalized via brand aliases) PLUS every
        ANY/open-market broker, capped to the top `max_vendors` (brand-matched first, by priority).
        `exclude_customer_email`: never RFQ the inquiring customer — in this trading model the same
        company is often both a customer AND a vendor, but we must not ask the buyer to quote us the
        very part they asked us to quote."""
        from knowledge.brand_aliases import brand_match
        from core.vendor_scorecard import effective_priority
        vendors = session.query(Vendor).filter(Vendor.active == True).all()  # noqa: E712
        cust_dom = (exclude_customer_email or "").lower().split("@")[-1].replace(">", "").strip()
        if cust_dom:
            kept = [v for v in vendors if not any(a.split("@")[-1] == cust_dom for a in _addrs(v.email))]
            if len(kept) != len(vendors):
                logger.info(f"route: not RFQ-ing vendor(s) on the customer's own domain ({cust_dom}) — "
                            f"the buyer is not a source for their own inquiry")
            vendors = kept
        brokers = [v for v in vendors
                   if "ANY" in self._brands_of(v) or "OPEN-MARKET" in self._brands_of(v)]

        def _rank(v):
            # manual priority (if set) else learned dynamic priority; higher score breaks ties
            return (effective_priority(v), -(v.score or 0))

        assignments: dict = {}
        unsourced: list = []
        for item in bom_items:
            mfr = (item.get("manufacturer") or "").strip()
            matched = [v for v in vendors if mfr and brand_match(mfr, v.brands)]
            matched_ids = {v.id for v in matched}
            # brand-matched vendors first (best rank), then brokers (best rank)
            ordered = sorted(matched, key=_rank) + sorted(
                [b for b in brokers if b.id not in matched_ids], key=_rank)
            part_vendors = ordered[:self.max_vendors] if self.max_vendors else ordered
            if not part_vendors:
                unsourced.append(item)
                continue
            for v in part_vendors:
                assignments.setdefault(v.id, [v, []])[1].append(item)
        return assignments, unsourced

    def _test_banner(self, vendor, real_email: str) -> str:
        return (f'<div style="background:#ffebee;border:2px solid #c62828;padding:10px;margin-bottom:12px;'
                f'font-family:Arial,sans-serif;font-size:13px;">'
                f'<strong>&#9888; TEST MODE</strong> &mdash; this vendor RFQ was NOT sent to the vendor. '
                f'Intended recipient: <strong>{html.escape(vendor.name)}</strong> '
                f'&lt;{html.escape(real_email or "no email on file")}&gt;.</div>')

    def send_rfqs(self, quotation, bom_items: list, session) -> tuple[list, list]:
        """Send one RFQ per matching vendor. Returns ([VendorRFQ recorded], [unsourced parts]).

        SAFETY: in testing mode (automatic=False) NO real vendor is ever emailed — RFQs are either
        redirected to `vendor_test_email` (with a red TEST banner) or just recorded and not sent.
        In automatic mode, a vendor with no email on file is skipped (can't contact) and reported."""
        assignments, unsourced = self.route(bom_items, session,
                                            exclude_customer_email=getattr(quotation, "customer_email", ""))
        sent = []
        no_email = []
        for vendor, parts in assignments.values():
            real_email = (vendor.email or "").strip()
            body = self._build_rfq_html(vendor, parts, quotation)
            subject = f"RFQ {quotation.quote_number} - {len(parts)} item(s) - {self.entity}"
            to = None
            status = "sent"

            if self.automatic:
                if not real_email:
                    no_email.append(vendor.name)
                    continue                                  # never send a blank-address email
                to = real_email.replace(";", ",")
            else:
                # TESTING PRECAUTION — never reach a real vendor.
                subject = "[TEST] " + subject
                body = self._test_banner(vendor, real_email) + body
                if self.vendor_test_email:
                    to = self.vendor_test_email
                    status = "test_sent"
                else:
                    status = "suppressed_testing"             # record only, send nothing

            try:
                thread_id = message_id = None
                if to:
                    result = self.gmail.send_new_email(to_email=to, subject=subject, body_html=body,
                                                       cc_emails=self._cc())
                    thread_id = result.get("threadId")
                    message_id = result.get("id")
                vrfq = VendorRFQ(
                    quotation_id=quotation.id, vendor_id=vendor.id, vendor_name=vendor.name,
                    vendor_email=real_email or None, thread_id=thread_id, message_id=message_id,
                    requested_mpns=[(p.get("mpn") or p.get("description") or "") for p in parts],
                    status=status,
                )
                session.add(vrfq)
                sent.append(vrfq)
                logger.info(f"Vendor RFQ [{status}] {vendor.name} -> {to or '(not sent)'} for {len(parts)} part(s)")
            except Exception as e:
                logger.error(f"Failed vendor RFQ to {vendor.name}: {e}")

        if no_email:
            logger.warning(f"{len(no_email)} matched vendor(s) skipped - no email on file: {no_email[:10]}")
        if unsourced:
            labels = [(p.get("mpn") or p.get("description") or "?") for p in unsourced]
            logger.warning(f"{len(unsourced)} part(s) had NO matching vendor: {labels}")
        return sent, unsourced

    def resend_target_cost(self, quotation, bom_items: list, session) -> tuple[list, list]:
        """Negotiation re-RFQ: ask vendors to match/beat OUR target buy cost, REPLYING on each
        vendor's existing quote thread so the whole exchange stays in one mail trail (instead of
        opening a fresh thread each round). Falls back to a new email for any vendor with no prior
        thread. The vendor's original VendorRFQ row is reused (status reset to 'sent') so Phase 3
        re-reads the thread for the new reply. Same testing-mode safety as send_rfqs."""
        assignments, unsourced = self.route(bom_items, session,
                                            exclude_customer_email=getattr(quotation, "customer_email", ""))
        sent = []
        no_email = []
        now = datetime.now(timezone.utc)
        for vendor, parts in assignments.values():
            real_email = (vendor.email or "").strip()
            body = self._build_rfq_html(vendor, parts, quotation)
            mpns = [(p.get("mpn") or p.get("description") or "") for p in parts]

            # The most recent prior RFQ to this vendor on this quote carries the thread to reply on.
            prior = (session.query(VendorRFQ)
                     .filter(VendorRFQ.quotation_id == quotation.id,
                             VendorRFQ.vendor_id == vendor.id,
                             VendorRFQ.thread_id.isnot(None))
                     .order_by(VendorRFQ.id.desc()).first())
            subject = f"RFQ {quotation.quote_number} - revised target - {self.entity}"

            to = None
            status = "sent"
            if self.automatic:
                if not real_email:
                    no_email.append(vendor.name)
                    continue
                to = real_email.replace(";", ",")
            else:
                subject = "[TEST] " + subject
                body = self._test_banner(vendor, real_email) + body
                if self.vendor_test_email:
                    to = self.vendor_test_email
                    status = "test_sent"
                else:
                    status = "suppressed_testing"

            try:
                if to and prior and prior.thread_id:
                    # Reply IN-THREAD on the vendor's existing conversation, and reuse their RFQ row.
                    result = self.gmail.send_reply(
                        thread_id=prior.thread_id, to_email=to, subject=subject,
                        body_html=body, in_reply_to=prior.message_id, cc_emails=self._cc())
                    prior.message_id = result.get("id") or prior.message_id
                    prior.requested_mpns = mpns
                    prior.status = status          # back to 'sent' → Phase 3 re-reads for the new reply
                    prior.sent_at = now
                    prior.replied_at = None
                    sent.append(prior)
                    logger.info(f"Vendor re-RFQ [{status}] {vendor.name} → replied in-thread for {len(parts)} part(s)")
                else:
                    thread_id = message_id = None
                    if to:
                        result = self.gmail.send_new_email(to_email=to, subject=subject, body_html=body,
                                                           cc_emails=self._cc())
                        thread_id = result.get("threadId")
                        message_id = result.get("id")
                    vrfq = VendorRFQ(
                        quotation_id=quotation.id, vendor_id=vendor.id, vendor_name=vendor.name,
                        vendor_email=real_email or None, thread_id=thread_id, message_id=message_id,
                        requested_mpns=mpns, status=status,
                    )
                    session.add(vrfq)
                    sent.append(vrfq)
                    logger.info(f"Vendor re-RFQ [{status}] {vendor.name} → new thread (no prior) for {len(parts)} part(s)")
            except Exception as e:
                logger.error(f"Failed vendor re-RFQ to {vendor.name}: {e}")

        if no_email:
            logger.warning(f"{len(no_email)} matched vendor(s) skipped on re-RFQ - no email: {no_email[:10]}")
        return sent, unsourced

    def _build_rfq_html(self, vendor: Vendor, parts: list, quotation) -> str:
        cur = self.currency
        cell = 'padding:6px;border:1px solid #dddddd;'
        th = 'padding:6px;border:1px solid #dddddd;text-align:left;'
        rows = ""
        has_target = False
        for p in parts:
            # OUR target buy price (set by the negotiation re-source path). This is OUR cost target,
            # NOT the customer's price — the customer's target is never shared with a vendor.
            tc = p.get("_target_cost")
            if tc:
                has_target = True
                ask_pct = p.get("_ask_pct")
                prev = p.get("_prev_vendor_cost")
                # Frame the ask on the VENDOR'S OWN last cost (their number) — never mention the
                # customer's price or our margin.
                ask_txt = (f" (about {float(ask_pct):.0f}% below your last {cur} {float(prev):.4f})"
                           if (ask_pct and prev) else "")
                remark = (f'<span style="color:#c62828;">Please improve to ~{cur} '
                          f'{float(tc):.4f}{ask_txt} — match or beat to win this order</span>')
            else:
                remark = ""
            rows += (
                f'<tr>'
                f'<td style="{cell}">{html.escape(str(p.get("mpn") or "(to identify)"))}</td>'
                f'<td style="{cell}">{html.escape(str(p.get("description") or ""))}</td>'
                f'<td style="{cell}">{html.escape(str(p.get("manufacturer") or ""))}</td>'
                f'<td style="{cell}text-align:right;">{html.escape(str(p.get("quantity") or ""))}</td>'
                f'<td style="{cell}"></td><td style="{cell}"></td><td style="{cell}"></td>'
                f'<td style="{cell}"></td><td style="{cell}">{remark}</td>'
                f'</tr>'
            )
        self._rfq_has_target = has_target
        header = (
            f'<tr style="background:#0d47a1;color:#ffffff;">'
            f'<th style="{th}">MPN</th><th style="{th}">Description</th><th style="{th}">Make</th>'
            f'<th style="{th}">Quantity</th><th style="{th}">Unit Cost ({cur})</th><th style="{th}">SPQ</th>'
            f'<th style="{th}">MOQ</th><th style="{th}">Lead Time</th><th style="{th}">Remark</th></tr>'
        )
        # A re-negotiation RFQ (has_target) is a FOLLOW-UP on the vendor's earlier quote — ask them to
        # sharpen their cost. A first-time RFQ just asks for their best price.
        if has_target:
            intro = (f'<p>Thank you for your earlier quotation (Ref: <strong>{html.escape(quotation.quote_number)}</strong>). '
                     f'This enquiry has become <strong>price-sensitive</strong> and we are keen to place the order with you '
                     f'&mdash; please see the <em>Remark</em> column for the improved cost we need to win it, and reply with '
                     f'your <strong>best firm price</strong>:</p>')
        else:
            intro = (f'<p>We have a <strong>firm customer enquiry</strong> for the part(s) below '
                     f'(Ref: <strong>{html.escape(quotation.quote_number)}</strong>). Please quote your <strong>best firm price</strong>:</p>')
        return f"""
        <div style="font-family:Arial,sans-serif;font-size:14px;color:#222;">
        <p>Dear {html.escape(vendor.name)},</p>
        {intro}
        <table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px;">
        {header}{rows}</table>
        <p style="margin-top:16px;padding:12px;background:#fff3e0;border-left:4px solid #f57c00;">
        <strong>Please advise, for each line item above:</strong><br>
        1. Available Quantity / Stock<br>
        2. Firm Unit Price ({cur})<br>
        3. Date Code (YY+)<br>
        4. Lead Time<br>
        5. MOQ &amp; SPQ<br>
        6. Condition, Packaging (Tray/Reel/Tube) &amp; COO (Country of Origin)<br><br>
        Terms: <strong>{cur}, EXW Hong Kong, T/T</strong> unless stated otherwise. Where available, please also share a
        <strong>photo of the stock label</strong> (date code + packing).<br>
        Kindly reply on <strong>this same email thread</strong> with your best pricing.</p>
        <p>Best regards,<br>{self.entity} &mdash; Sourcing<br>
        <span style="color:#888888;font-size:12px;">Kindly reply on this same thread with your best pricing.</span></p>
        </div>"""


def check_vendor_replies(quotation, session, gmail, claude, our_email: str = "") -> int:
    """Phase 3 — parse vendor COST replies on each VendorRFQ thread into VendorQuote rows.

    For every VendorRFQ of this quotation that hasn't replied yet, fetch new messages on its
    thread, skip our own messages, extract per-part cost pricing via the LLM, and store a
    VendorQuote per priced line (cost_price = the vendor's unit price). First reply wins per
    vendor (the RFQ is marked 'replied' so it isn't re-parsed every monitor cycle).

    Returns the number of new VendorQuote rows created.
    """
    known_mpns = [it.get("mpn", "") for it in (quotation.line_items or []) if it.get("mpn")]
    rfqs = session.query(VendorRFQ).filter(VendorRFQ.quotation_id == quotation.id).all()
    our = (our_email or "").lower()
    our_dom = our.split("@")[-1] if "@" in our else ""  # our own domain — never learn our own / oversight addrs
    new_count = 0

    for vrfq in rfqs:
        # Keep polling a vendor thread until EVERY requested part has been quoted — a vendor often sends
        # part 1's price first and part 2's in a LATER reply on the same thread. Only "replied" (all
        # parts in) or "no_bid" (declined) is terminal; a "partial"/"sent" RFQ is re-read every cycle.
        if vrfq.status in ("replied", "no_bid") or not vrfq.thread_id:
            continue

        vendor = session.query(Vendor).get(vrfq.vendor_id) if vrfq.vendor_id else None
        cur_hint = (vendor.currency if vendor else None) or quotation.currency or "USD"

        try:
            replies = gmail.get_thread_replies(thread_id=vrfq.thread_id, after_message_id=vrfq.message_id)
        except Exception as e:
            logger.error(f"[{quotation.quote_number}] Could not fetch vendor thread for {vrfq.vendor_name}: {e}")
            continue

        # What this vendor was asked for, and what it has ALREADY quoted (across earlier replies) — so a
        # re-read of the same thread never records the same part twice.
        requested = {(m or "").strip().upper() for m in (vrfq.requested_mpns or []) if m}
        already = {(q.mpn or "").strip().upper()
                   for q in session.query(VendorQuote).filter_by(vendor_rfq_id=vrfq.id).all()}

        parsed_any = False
        declined_any = False
        for reply in replies:
            frm = (reply.get("from_email") or "").lower()
            if our and our in frm:
                continue  # skip our own RFQ / any message we sent on the thread

            # LEARN the vendor's team: whoever they replied from + everyone they CC'd (their colleagues),
            # so the NEXT RFQ to this vendor reaches all of them automatically. Excludes our own side
            # (mailbox + the director's oversight — all on our domain) and anything already on file.
            # Runs on ANY vendor reply, even a plain acknowledgement with no pricing.
            if vendor:
                learned = [a for a in (_addrs(reply.get("from_email")) + _addrs(reply.get("cc")))
                           if not (our_dom and a.endswith("@" + our_dom)) and not _is_system_addr(a)]
                if learned:
                    have = _addrs(vendor.email)
                    fresh = [a for a in learned if a not in have]
                    if fresh:
                        vendor.email = "; ".join(have + fresh)
                        logger.info(f"[{quotation.quote_number}] Learned {len(fresh)} new contact(s) for "
                                    f"{vendor.name} from their reply: {fresh}")

            body = reply.get("body_text", "")
            if not body or len(body.strip()) < 10:
                continue
            # A decline / no-bid (no price) — record it so the vendor shows No-Bid, not a silent "sent".
            _low = body.lower()
            if any(w in _low for w in ("no bid", "no-bid", "not authorised", "not authorized", "no stock",
                                       "cannot quote", "can't quote", "unable to quote", "no offer",
                                       "we decline", "not able to")):
                declined_any = True
            try:
                pricing = claude.extract_pricing_from_reply(reply_body=body, known_mpns=known_mpns, currency=cur_hint)
            except Exception as e:
                logger.error(f"[{quotation.quote_number}] Pricing extract failed for {vrfq.vendor_name}: {e}")
                continue
            if not (pricing and pricing.get("has_pricing")):
                continue

            det_cur = (pricing.get("currency") or cur_hint or "").upper() or None
            reply_dt = reply.get("date") or datetime.now(timezone.utc)  # base for relative validity
            for it in pricing.get("items", []):
                cost = _to_float(it.get("unit_price"))
                if cost is None:
                    continue  # a line with no cost is not a quote
                mpn_key = (it.get("mpn") or "").strip().upper()
                if mpn_key and mpn_key in already:
                    continue  # this part was already recorded from an earlier reply — don't duplicate
                validity_raw = it.get("validity") or pricing.get("pricing_notes")
                session.add(VendorQuote(
                    quotation_id=quotation.id,
                    vendor_rfq_id=vrfq.id,
                    vendor_id=vrfq.vendor_id,
                    vendor_name=vrfq.vendor_name,
                    mpn=it.get("mpn"),
                    cost_price=cost,
                    currency=det_cur,
                    moq=_to_int(it.get("moq")),
                    spq=_to_int(it.get("spq")),
                    offered_qty=_to_int(it.get("available_qty")),  # supply cap -> drives split-sourcing
                    lead_time=it.get("lead_time"),
                    packaging=(it.get("packaging") or None),        # drives split homogeneity check
                    date_code=(it.get("date_code") or None),
                    valid_until=_parse_validity(it.get("validity"), reply_dt),  # quote expiry guard
                    validity_raw=(str(validity_raw)[:120] if validity_raw else None),
                    notes=it.get("notes"),
                ))
                if mpn_key:
                    already.add(mpn_key)
                new_count += 1
                parsed_any = True

        # Terminal ("replied") only when every requested part is now covered; otherwise stay "partial"
        # and keep polling for the parts the vendor hasn't priced yet (consolidation still fires on the
        # wait-window timeout even if the vendor never sends the rest).
        now = datetime.now(timezone.utc)
        if parsed_any and (not requested or requested.issubset(already)):
            vrfq.status = "replied"
            vrfq.replied_at = now
            logger.info(f"[{quotation.quote_number}] Vendor {vrfq.vendor_name} replied — all "
                        f"{len(requested) or len(already)} part(s) quoted")
        elif already:
            vrfq.status = "partial"
            vrfq.replied_at = now
            missing = len(requested - already) if requested else 0
            logger.info(f"[{quotation.quote_number}] Vendor {vrfq.vendor_name} partial — "
                        f"{len(already)} part(s) quoted, {missing} still awaited; keep polling this thread")
        elif declined_any:
            vrfq.status = "no_bid"
            vrfq.replied_at = now
            logger.info(f"[{quotation.quote_number}] Vendor {vrfq.vendor_name} declined — No-Bid recorded")

    return new_count
