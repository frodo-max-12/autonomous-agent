"""
Order processing (Order module) — the final leg: Sales -> Purchase -> ORDER.

When the customer sends a PO for a quoted deal, place a purchase order to each vendor that WON
its lines during consolidation (VendorQuote.is_selected), at the agreed cost.

SAFETY: a PO is a real financial commitment, so it carries the SAME test-mode precaution as RFQs
— in testing mode NO real vendor is ever emailed (redirect to a test inbox, or suppress). The end
customer's identity is never revealed to vendors.
"""

import html
from datetime import datetime, timezone
from loguru import logger
from core.database import Vendor, VendorQuote, VendorPO


def _norm(mpn) -> str:
    return (mpn or "").strip().upper()


class OrderProcessor:
    def __init__(self, gmail_client, account: str, automatic: bool = True, vendor_test_email: str = "",
                 oversight_cc: list = None):
        self.gmail = gmail_client
        self.account = account
        self.entity = "Company B International"  # v1.1 — Company B International only
        self.automatic = bool(automatic)
        self.vendor_test_email = (vendor_test_email or "").strip()
        # the director — CC'd on every REAL vendor PO (oversight); never on test redirects.
        self.oversight_cc = [e for e in (oversight_cc or []) if e]

    def place_vendor_pos(self, quotation, session, customer_po_ref: str = None) -> tuple[list, str | None]:
        """Place one PO per winning vendor. Returns ([VendorPO recorded], error_code or None)."""
        selected = session.query(VendorQuote).filter_by(quotation_id=quotation.id, is_selected=True).all()
        if not selected:
            return [], "no_selected_vendor_quotes"

        # quantity + description per part from the quotation lines
        qty_by, desc_by = {}, {}
        for it in (quotation.line_items or []):
            qty_by[_norm(it.get("mpn"))] = it.get("quantity")
            desc_by[_norm(it.get("mpn"))] = it.get("description")

        groups: dict = {}
        for vq in selected:
            groups.setdefault(vq.vendor_id or ("name:" + (vq.vendor_name or "?")), []).append(vq)

        prefix = "AE"  # v1.1 — Company B International only
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        placed = []
        seq = 0
        for key, vqs in groups.items():
            seq += 1
            vendor = session.get(Vendor, vqs[0].vendor_id) if vqs[0].vendor_id else None
            real_email = (vendor.email or "").strip() if vendor and vendor.email else ""
            po_number = f"{prefix}-PO-{stamp}-{quotation.id:04d}-{seq}"
            currency = vqs[0].currency or quotation.currency or "USD"

            lines, total = [], 0.0
            for vq in vqs:
                qty = qty_by.get(_norm(vq.mpn)) or 0
                cost = vq.cost_price or 0
                total += cost * (qty or 0)
                lines.append({"mpn": vq.mpn, "description": desc_by.get(_norm(vq.mpn)),
                              "quantity": qty, "unit_cost": cost,
                              "currency": vq.currency or currency, "lead_time": vq.lead_time})

            body = self._build_po_html(vqs[0].vendor_name, po_number, lines, total, currency, customer_po_ref)
            subject = f"Purchase Order {po_number} - {self.entity}"

            to, status = None, "placed"
            if self.automatic:
                if not real_email:
                    status = "no_contact"
                else:
                    to = real_email.replace(";", ",")
            else:
                subject = "[TEST] " + subject
                body = self._test_banner(vqs[0].vendor_name, real_email) + body
                if self.vendor_test_email:
                    to, status = self.vendor_test_email, "test_sent"
                else:
                    status = "suppressed_testing"

            thread_id = message_id = None
            try:
                if to:
                    res = self.gmail.send_new_email(to_email=to, subject=subject, body_html=body,
                                                    cc_emails=(self.oversight_cc if self.automatic else None))
                    thread_id, message_id = res.get("threadId"), res.get("id")
            except Exception as e:
                logger.error(f"Failed to send vendor PO to {vqs[0].vendor_name}: {e}")
                status = "send_failed"

            vpo = VendorPO(
                quotation_id=quotation.id, vendor_id=vqs[0].vendor_id, vendor_name=vqs[0].vendor_name,
                vendor_email=real_email or None, po_number=po_number, customer_po_ref=customer_po_ref,
                line_items=lines, total_cost=round(total, 2), currency=currency, status=status,
                thread_id=thread_id, message_id=message_id,
            )
            session.add(vpo)
            placed.append(vpo)
            logger.info(f"Vendor PO [{status}] {po_number} -> {vqs[0].vendor_name} ({to or 'not sent'}) "
                        f"{len(lines)} line(s), total {currency} {round(total,2)}")

        return placed, None

    def _test_banner(self, vendor_name, real_email):
        return (f'<div style="background:#ffebee;border:2px solid #c62828;padding:10px;margin-bottom:12px;'
                f'font-family:Arial,sans-serif;font-size:13px;">'
                f'<strong>&#9888; TEST MODE</strong> &mdash; this PURCHASE ORDER was NOT placed with the vendor. '
                f'Intended recipient: <strong>{html.escape(vendor_name or "-")}</strong> '
                f'&lt;{html.escape(real_email or "no email on file")}&gt;.</div>')

    def _build_po_html(self, vendor_name, po_number, lines, total, currency, customer_po_ref):
        cell = 'padding:6px;border:1px solid #dddddd;'
        th = 'padding:6px;border:1px solid #dddddd;text-align:left;'
        rows = ""
        for ln in lines:
            line_total = (ln.get("unit_cost") or 0) * (ln.get("quantity") or 0)
            rows += (
                f'<tr>'
                f'<td style="{cell}"><strong>{html.escape(str(ln.get("mpn") or "-"))}</strong></td>'
                f'<td style="{cell}">{html.escape(str(ln.get("description") or "-"))}</td>'
                f'<td style="{cell}text-align:right;">{html.escape(str(ln.get("quantity") or "-"))}</td>'
                f'<td style="{cell}text-align:right;">{ln.get("currency") or currency} {ln.get("unit_cost")}</td>'
                f'<td style="{cell}text-align:right;">{currency} {round(line_total,2)}</td>'
                f'<td style="{cell}">{html.escape(str(ln.get("lead_time") or "-"))}</td>'
                f'</tr>'
            )
        ref = f'<br><strong>Ref (customer PO):</strong> {html.escape(customer_po_ref)}' if customer_po_ref else ''
        return f"""
        <div style="font-family:Arial,sans-serif;font-size:14px;color:#222;">
        <p><strong>PURCHASE ORDER</strong> &mdash; {self.entity}</p>
        <p><strong>PO No:</strong> {html.escape(po_number)}{ref}</p>
        <p>Dear {html.escape(vendor_name or 'Supplier')},<br>Please supply the following against this Purchase Order:</p>
        <table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px;">
        <tr style="background:#0d47a1;color:#ffffff;">
        <th style="{th}">MPN</th><th style="{th}">Description</th><th style="{th}">Qty</th>
        <th style="{th}">Unit Cost</th><th style="{th}">Line Total</th><th style="{th}">Lead Time</th></tr>
        {rows}</table>
        <p style="margin-top:10px;"><strong>Order total: {currency} {round(total,2)}</strong></p>
        <p style="margin-top:14px;padding:12px;background:#e8f5e9;border-left:4px solid #2e7d32;">
        Please <strong>confirm acceptance</strong>, unit prices, lead time and dispatch schedule by return.
        Advise date code, packaging and country of origin, and &mdash; for traceability &mdash; share a
        <strong>photo of the stock label</strong> (date code + packing) and the manufacturer
        <strong>CoC / authorised-distributor packing list</strong>. Reply on this same thread.</p>
        <p>Best regards,<br>{self.entity} &mdash; Purchase</p>
        </div>"""
