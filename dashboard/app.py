"""
SemiSales AI Agent - Dashboard API
FastAPI application for human approval workflow, monitoring, and agent control.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from loguru import logger
import io

import base64 as _b64
import secrets as _secrets
from starlette.responses import Response as _Response

from core.database import (
    get_session, Email, BOMItem, Quotation, AuditLog, Lead, CustomerHistory, NegotiationRound,
    Vendor, VendorRFQ, VendorQuote, VendorPO,
    EmailType, EmailStatus, QuoteStatus, coerce_email_type,
)
from core.lead_manager import LeadManager
from core.learning import LearningManager

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

app = FastAPI(title="SemiSales AI Agent", version="1.0.0")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Will be set by main.py at startup
DATABASE_URL: str = ""
GMAIL_CLIENTS: dict = {}
AUTH_USER: str = ""
AUTH_PASSWORD: str = ""


def set_config(database_url: str, gmail_clients: dict = None,
               auth_user: str = "", auth_password: str = ""):
    global DATABASE_URL, GMAIL_CLIENTS, AUTH_USER, AUTH_PASSWORD
    DATABASE_URL = database_url
    if gmail_clients:
        GMAIL_CLIENTS = gmail_clients
    AUTH_USER = auth_user or ""
    AUTH_PASSWORD = auth_password or ""


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    """HTTP Basic auth guard. Active only when dashboard credentials are configured.
    Without it, anyone who can reach this port could send customer email / export all leads."""
    if AUTH_USER and AUTH_PASSWORD:
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                decoded = _b64.b64decode(header[6:]).decode("utf-8", "replace")
                user, _, pwd = decoded.partition(":")
                ok = (_secrets.compare_digest(user, AUTH_USER)
                      and _secrets.compare_digest(pwd, AUTH_PASSWORD))
            except Exception:
                ok = False
        if not ok:
            return _Response(
                "Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="SemiSales"'},
            )
    return await call_next(request)


# ─── API Models ──────────────────────────────────────────

class QuoteApproval(BaseModel):
    approved_email_body: Optional[str] = None
    action: str  # "approve", "reject", "edit"


class QuoteSend(BaseModel):
    quote_id: int


class PricingItem(BaseModel):
    mpn: str
    unit_price: Optional[float] = None
    lead_time: Optional[str] = None
    moq: Optional[int] = None
    spq: Optional[int] = None
    notes: Optional[str] = None


class SubmitPricing(BaseModel):
    items: list[PricingItem]
    pricing_notes: Optional[str] = ""
    submitted_by: Optional[str] = "purchase_team"
    currency: Optional[str] = None  # "INR" or "USD" - overrides default if provided


class LeadCreate(BaseModel):
    name: Optional[str] = None
    company: Optional[str] = None
    designation: Optional[str] = None
    email: str
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = "India"
    pincode: Optional[str] = None
    website: Optional[str] = None
    application: Optional[str] = None
    industry: Optional[str] = None
    notes: Optional[str] = None


class ClassificationCorrection(BaseModel):
    email_id: int
    correct_type: str
    reason: Optional[str] = ""


# ─── Dashboard Pages ────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    session = get_session(DATABASE_URL)
    try:
        total_emails = session.query(Email).count()
        awaiting_pricing = session.query(Quotation).filter(
            Quotation.status.in_([QuoteStatus.AWAITING_PRICING, QuoteStatus.AWAITING_REVISED_PRICING])
        ).count()
        pending_approvals = session.query(Quotation).filter(
            Quotation.status.in_([QuoteStatus.AWAITING_APPROVAL, QuoteStatus.REVISED_AWAITING_APPROVAL, QuoteStatus.PARTIALLY_PRICED])
        ).count()
        rfq_count = session.query(Email).filter(Email.email_type == EmailType.RFQ).count()
        escalated = session.query(Email).filter(Email.status == EmailStatus.ESCALATED).count()
        in_negotiation = session.query(Quotation).filter(
            Quotation.status.in_([
                QuoteStatus.NEGOTIATION_FORWARDED,
                QuoteStatus.AWAITING_REVISED_PRICING,
                QuoteStatus.REVISED_AWAITING_APPROVAL,
            ])
        ).count()
        po_received = session.query(Quotation).filter(
            Quotation.status.in_([QuoteStatus.PO_RECEIVED, QuoteStatus.PO_FORWARDED])
        ).count()

        recent_emails = session.query(Email).order_by(Email.created_at.desc()).limit(20).all()
        pending_quotes = session.query(Quotation).filter(
            Quotation.status.in_([QuoteStatus.AWAITING_APPROVAL, QuoteStatus.REVISED_AWAITING_APPROVAL, QuoteStatus.PARTIALLY_PRICED])
        ).order_by(Quotation.created_at.desc()).all()
        pricing_quotes = session.query(Quotation).filter(
            Quotation.status.in_([QuoteStatus.AWAITING_PRICING, QuoteStatus.AWAITING_REVISED_PRICING])
        ).order_by(Quotation.created_at.desc()).all()

        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "total_emails": total_emails,
                "awaiting_pricing": awaiting_pricing,
                "pending_approvals": pending_approvals,
                "rfq_count": rfq_count,
                "escalated": escalated,
                "in_negotiation": in_negotiation,
                "po_received": po_received,
                "recent_emails": recent_emails,
                "pending_quotes": pending_quotes,
                "pricing_quotes": pricing_quotes,
            },
        )
    finally:
        session.close()


@app.get("/emails", response_class=HTMLResponse)
async def emails_page(request: Request, email_type: Optional[str] = None):
    session = get_session(DATABASE_URL)
    try:
        query = session.query(Email).order_by(Email.created_at.desc())
        if email_type:
            query = query.filter(Email.email_type == coerce_email_type(email_type))
        emails = query.limit(100).all()

        return templates.TemplateResponse(
            request=request,
            name="emails.html",
            context={
                "emails": emails,
                "filter_type": email_type,
            },
        )
    finally:
        session.close()


@app.get("/email/{email_id}", response_class=HTMLResponse)
async def email_detail(request: Request, email_id: int):
    session = get_session(DATABASE_URL)
    try:
        email = session.query(Email).filter_by(id=email_id).first()
        if not email:
            raise HTTPException(status_code=404, detail="Email not found")

        bom_items = session.query(BOMItem).filter_by(email_id=email_id).all()
        quotations = session.query(Quotation).filter_by(email_id=email_id).all()

        return templates.TemplateResponse(
            request=request,
            name="email_detail.html",
            context={
                "email": email,
                "bom_items": bom_items,
                "quotations": quotations,
            },
        )
    finally:
        session.close()


@app.get("/quotations", response_class=HTMLResponse)
async def quotations_page(request: Request):
    session = get_session(DATABASE_URL)
    try:
        quotes = session.query(Quotation).order_by(Quotation.created_at.desc()).limit(100).all()
        return templates.TemplateResponse(
            request=request,
            name="quotations.html",
            context={
                "quotations": quotes,
            },
        )
    finally:
        session.close()


@app.get("/quote/{quote_id}", response_class=HTMLResponse)
async def quote_detail(request: Request, quote_id: int):
    session = get_session(DATABASE_URL)
    try:
        quote = session.query(Quotation).filter_by(id=quote_id).first()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")

        email = session.query(Email).filter_by(id=quote.email_id).first()
        bom_items = session.query(BOMItem).filter_by(email_id=quote.email_id).all()
        negotiations = session.query(NegotiationRound).filter_by(
            quotation_id=quote.id
        ).order_by(NegotiationRound.round_number).all()
        # Vendor sourcing for THIS enquiry: which vendors we sent the RFQ to, and the cost quotes
        # they returned — so the salesperson can see, from the quotation, which vendors are behind
        # each part's price (the user asked to see this after the quote is sent).
        rfqs = session.query(VendorRFQ).filter_by(quotation_id=quote.id).order_by(VendorRFQ.sent_at).all()
        vquotes = session.query(VendorQuote).filter_by(quotation_id=quote.id).order_by(
            VendorQuote.mpn, VendorQuote.cost_price).all()

        return templates.TemplateResponse(
            request=request,
            name="quote_detail.html",
            context={
                "quote": quote,
                "email": email,
                "bom_items": bom_items,
                "negotiations": negotiations,
                "rfqs": rfqs,
                "vquotes": vquotes,
            },
        )
    finally:
        session.close()


# ─── Autonomous: Vendor management + Sourcing review ─────

class VendorIn(BaseModel):
    name: str
    email: str
    brands: str = ""        # comma-separated; "ANY" for open-market broker
    categories: str = ""    # comma-separated
    currency: str = "USD"
    country: str = ""
    priority: int = 3
    active: bool = True
    notes: str = ""


def _csv_list(val: str, upper: bool = False) -> list[str]:
    parts = [p.strip() for p in (val or "").replace(";", ",").split(",") if p.strip()]
    return [p.upper() for p in parts] if upper else parts


@app.get("/vendors", response_class=HTMLResponse)
async def vendors_page(request: Request):
    session = get_session(DATABASE_URL)
    try:
        vendors = session.query(Vendor).order_by(Vendor.active.desc(), Vendor.priority, Vendor.name).all()
        return templates.TemplateResponse(request=request, name="vendors.html",
                                          context={"vendors": vendors})
    finally:
        session.close()


@app.post("/api/vendor")
async def create_vendor(v: VendorIn):
    session = get_session(DATABASE_URL)
    try:
        vendor = Vendor(
            name=v.name.strip(), email=v.email.strip(),
            brands=_csv_list(v.brands, upper=True), categories=_csv_list(v.categories),
            currency=(v.currency or "USD").upper(), country=v.country.strip() or None,
            priority=int(v.priority or 3), active=bool(v.active), notes=v.notes.strip() or None,
        )
        session.add(vendor); session.commit()
        return {"status": "ok", "vendor_id": vendor.id}
    except Exception as e:
        session.rollback(); raise HTTPException(status_code=400, detail=str(e))
    finally:
        session.close()


@app.post("/api/vendor/{vendor_id}/update")
async def update_vendor(vendor_id: int, v: VendorIn):
    session = get_session(DATABASE_URL)
    try:
        vendor = session.query(Vendor).filter_by(id=vendor_id).first()
        if not vendor:
            raise HTTPException(status_code=404, detail="Vendor not found")
        vendor.name = v.name.strip(); vendor.email = v.email.strip()
        vendor.brands = _csv_list(v.brands, upper=True); vendor.categories = _csv_list(v.categories)
        vendor.currency = (v.currency or "USD").upper(); vendor.country = v.country.strip() or None
        vendor.priority = int(v.priority or 3); vendor.active = bool(v.active)
        vendor.notes = v.notes.strip() or None
        session.commit()
        return {"status": "ok", "vendor_id": vendor.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback(); raise HTTPException(status_code=400, detail=str(e))
    finally:
        session.close()


@app.post("/api/vendor/{vendor_id}/toggle")
async def toggle_vendor(vendor_id: int):
    session = get_session(DATABASE_URL)
    try:
        vendor = session.query(Vendor).filter_by(id=vendor_id).first()
        if not vendor:
            raise HTTPException(status_code=404, detail="Vendor not found")
        vendor.active = not bool(vendor.active); session.commit()
        return {"status": "ok", "active": vendor.active}
    finally:
        session.close()


@app.delete("/api/vendor/{vendor_id}")
async def delete_vendor(vendor_id: int):
    session = get_session(DATABASE_URL)
    try:
        vendor = session.query(Vendor).filter_by(id=vendor_id).first()
        if not vendor:
            raise HTTPException(status_code=404, detail="Vendor not found")
        session.delete(vendor); session.commit()
        return {"status": "deleted"}
    finally:
        session.close()


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    """Order tracking — customer POs and the vendor POs placed against them."""
    session = get_session(DATABASE_URL)
    try:
        quotes = session.query(Quotation).filter(
            Quotation.status.in_([QuoteStatus.PO_RECEIVED, QuoteStatus.PO_FORWARDED, QuoteStatus.WON])
        ).order_by(Quotation.updated_at.desc()).limit(100).all()
        orders = []
        for q in quotes:
            pos = session.query(VendorPO).filter_by(quotation_id=q.id).order_by(VendorPO.po_number).all()
            orders.append({"quote": q, "pos": pos})
        return templates.TemplateResponse(request=request, name="orders.html", context={"orders": orders})
    finally:
        session.close()


class POStatusIn(BaseModel):
    status: str


@app.post("/api/vendorpo/{po_id}/status")
async def update_vendorpo_status(po_id: int, s: POStatusIn):
    allowed = {"placed", "confirmed", "shipped", "received", "cancelled"}
    if s.status not in allowed:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(allowed)}")
    session = get_session(DATABASE_URL)
    try:
        po = session.query(VendorPO).filter_by(id=po_id).first()
        if not po:
            raise HTTPException(status_code=404, detail="Vendor PO not found")
        po.status = s.status
        session.commit()
        return {"status": "ok", "po_status": po.status}
    finally:
        session.close()


@app.post("/api/vendors/rescore")
async def rescore_vendors():
    from core.vendor_scorecard import compute_vendor_scores
    session = get_session(DATABASE_URL)
    try:
        n = compute_vendor_scores(session)
        return {"status": "ok", "scored": n}
    finally:
        session.close()


@app.get("/sourcing/{quote_id}", response_class=HTMLResponse)
async def sourcing_review(request: Request, quote_id: int):
    """Internal review of an autonomous quote — vendor RFQs, competing cost quotes, and the
    consolidated cost/margin/resale per line (the data hidden from the customer)."""
    session = get_session(DATABASE_URL)
    try:
        quote = session.query(Quotation).filter_by(id=quote_id).first()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")
        rfqs = session.query(VendorRFQ).filter_by(quotation_id=quote.id).all()
        vquotes = session.query(VendorQuote).filter_by(quotation_id=quote.id).order_by(
            VendorQuote.mpn, VendorQuote.cost_price).all()
        return templates.TemplateResponse(request=request, name="sourcing.html",
                                          context={"quote": quote, "rfqs": rfqs, "vquotes": vquotes,
                                                   "line_items": quote.line_items or []})
    finally:
        session.close()


def _reprice_quote_now(session, quote):
    """Re-consolidate a quote IMMEDIATELY (currency/margin just changed) so the dashboard shows the
    correct FX-converted resale right away, instead of a stale figure until the monitor catches up.
    If no vendor costs have arrived yet, leave it in SOURCING_VENDORS to be priced on reply."""
    from core.database import VendorQuote
    from core.consolidation import consolidate_and_price
    from config.settings import get_settings
    has_quotes = session.query(VendorQuote).filter(VendorQuote.quotation_id == quote.id).count() > 0
    if has_quotes:
        consolidate_and_price(quote, session, get_settings(), claude=None)
        quote.approved_email_body = None  # pricing changed → void stale approval (finalize rebuilds draft)
        quote.status = QuoteStatus.VENDOR_PRICED  # priced now; the confidence gate re-runs next tick
    else:
        quote.status = QuoteStatus.SOURCING_VENDORS


@app.post("/api/quotation/{quote_id}/currency")
async def confirm_currency(quote_id: int, currency: str = Form(...)):
    """Confirm the quote currency when it was inferred (currency_assumed). Clears the flag; if the
    currency actually changed, the resale math is now stale, so the quote is put back to
    SOURCING_VENDORS for the monitor to re-consolidate at the new currency."""
    from fastapi.responses import RedirectResponse
    session = get_session(DATABASE_URL)
    try:
        quote = session.query(Quotation).filter_by(id=quote_id).first()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")
        cur = (currency or "").strip().upper()
        if cur not in ("USD", "INR"):
            raise HTTPException(status_code=400, detail="currency must be USD or INR")
        changed = (quote.currency or "").upper() != cur
        quote.currency = cur
        quote.currency_assumed = False
        quote.currency_source = "confirmed"
        if changed:
            _reprice_quote_now(session, quote)  # re-consolidate now → resale is correct immediately
        session.commit()
        return RedirectResponse(url=f"/sourcing/{quote_id}", status_code=303)
    finally:
        session.close()


@app.post("/api/quotation/{quote_id}/margin")
async def set_quote_margin(quote_id: int, margin_percent: str = Form(...)):
    """Set (or clear) a flat per-quote margin — e.g. 5% on a distributor BOM instead of the 15%
    default. Blank clears the override. Re-consolidates at the new margin via SOURCING_VENDORS."""
    from fastapi.responses import RedirectResponse
    session = get_session(DATABASE_URL)
    try:
        quote = session.query(Quotation).filter_by(id=quote_id).first()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")
        raw = (margin_percent or "").strip()
        if raw == "":
            quote.margin_percent_override = None
        else:
            try:
                val = float(raw)
            except ValueError:
                raise HTTPException(status_code=400, detail="margin_percent must be a number")
            if val < 0 or val > 100:
                raise HTTPException(status_code=400, detail="margin_percent must be 0–100")
            quote.margin_percent_override = val
        _reprice_quote_now(session, quote)  # re-consolidate now at the new margin
        session.commit()
        return RedirectResponse(url=f"/sourcing/{quote_id}", status_code=303)
    finally:
        session.close()


@app.post("/api/quotation/{quote_id}/select-vendor")
async def select_vendor(quote_id: int, vendor_quote_id: str = Form(""), mpn: str = Form("")):
    """Human override (#2a): pin a specific vendor quote for a line instead of the auto-selected
    cheapest, then re-consolidate so the resale/lead-time reflect that vendor. An empty
    vendor_quote_id clears the pin and returns the line to automatic selection. Both fields default
    to '' so a missing/blank field redirects harmlessly instead of returning a 422."""
    from fastapi.responses import RedirectResponse
    session = get_session(DATABASE_URL)
    try:
        quote = session.query(Quotation).filter_by(id=quote_id).first()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")
        vqid = (vendor_quote_id or "").strip()
        try:
            pin = int(vqid) if vqid else None       # empty -> clear the pin; bad value -> treat as clear
        except ValueError:
            pin = None
        target = (mpn or "").strip().upper()
        # If mpn wasn't supplied but the pin is, derive the line from the vendor quote itself.
        if not target and pin is not None:
            vq_row = session.query(VendorQuote).filter_by(id=pin, quotation_id=quote.id).first()
            target = (vq_row.mpn or "").strip().upper() if vq_row else ""
        items = [dict(it) for it in (quote.line_items or [])]
        matched = False
        for it in items:
            if target and (it.get("mpn") or "").strip().upper() == target:
                if pin is None:
                    it.pop("pinned_vendor_quote_id", None)
                else:
                    it["pinned_vendor_quote_id"] = pin
                matched = True
        if matched:
            quote.line_items = items
            _reprice_quote_now(session, quote)  # honour the pin + refresh resale
            session.add(AuditLog(action="vendor_selected", entity_type="quotation", entity_id=quote.id,
                                 details={"mpn": target, "vendor_quote_id": pin}, performed_by="human"))
            session.commit()
        return RedirectResponse(url=f"/sourcing/{quote_id}", status_code=303)
    finally:
        session.close()


@app.post("/api/quotation/{quote_id}/send-partial")
async def send_partial_quote(quote_id: int):
    """Send the customer a PARTIAL quotation NOW — the lines priced so far, with the rest shown as
    'Pending'. Consolidates whatever vendor prices have arrived (does NOT wait for silent vendors) and
    keeps the quote open as PARTIALLY_PRICED, so late vendor replies auto-produce an updated quote
    (with the new parts tagged 'New'). If everything is already priced, it just sends the full quote."""
    from fastapi.responses import RedirectResponse
    from core.consolidation import consolidate_and_price
    from core.email_processor import EmailProcessor
    from config.settings import get_settings
    session = get_session(DATABASE_URL)
    try:
        quote = session.query(Quotation).filter_by(id=quote_id).first()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")
        # Price whatever vendor costs have arrived so far (don't wait for the silent vendors).
        consolidate_and_price(quote, session, get_settings(), claude=None)
        session.flush()
        quotable = [it for it in (quote.line_items or []) if it.get("mpn") or it.get("description")]
        priced = [it for it in quotable if it.get("unit_price") is not None]
        if not priced:
            raise HTTPException(status_code=400, detail="No vendor prices yet — wait for at least one vendor reply.")
        pending_n = sum(1 for it in quotable if it.get("unit_price") is None)

        email = session.query(Email).filter_by(id=quote.email_id).first()
        gmail = GMAIL_CLIENTS.get(quote.account)
        if not gmail or not email:
            raise HTTPException(status_code=500, detail="Gmail client / original email not available")

        # Build the customer quote (priced rows + 'Pending' rows); mark as an update if re-sent.
        proc = EmailProcessor.__new__(EmailProcessor)
        body = proc._build_quotation_html(quote, quote.line_items, [], quote.currency or "USD",
                                          quote.validity_days or 30, is_update=quote.sent_at is not None)
        quote.draft_email_body = body
        label = "updated" if quote.sent_at else ("partial" if pending_n else "")
        # No internal quote_number (AE-Q-...) in the CUSTOMER subject — they mistake it for a part number.
        gmail.send_reply(thread_id=email.thread_id, to_email=quote.customer_email,
                         subject=f"Quotation{(' (' + label + ')') if label else ''} - {email.subject}",
                         body_html=body, cc_emails=_customer_quote_cc(quote) or None)
        quote.status = QuoteStatus.PARTIALLY_PRICED if pending_n else QuoteStatus.SENT
        quote.sent_at = datetime.now(timezone.utc)
        email.status = EmailStatus.SENT
        session.add(AuditLog(action="partial_quote_sent", entity_type="quotation", entity_id=quote.id,
                             details={"quote_number": quote.quote_number, "priced": len(priced),
                                      "pending": pending_n}, performed_by="human (dashboard)"))
        session.commit()
        return RedirectResponse(url=f"/sourcing/{quote_id}", status_code=303)
    finally:
        session.close()


# ─── Autonomous: FX rate ─────────────────
# v1.1 (Company B International) prices in USD. FX (USD->INR) is kept ONLY to convert an Indian vendor's
# INR cost to USD; the India HSN/customs-duty pages were removed (no INR sales in this build).

@app.post("/api/fx/refresh")
async def refresh_fx():
    from core import fx as fx_mod
    from config.settings import get_settings
    session = get_session(DATABASE_URL)
    try:
        row = fx_mod.update_fx_rate(session, get_settings())
        if not row:
            raise HTTPException(status_code=502, detail="FX fetch failed from all sources")
        return {"status": "ok", "base": row.base_rate, "effective": row.effective_rate}
    finally:
        session.close()


# ─── API Endpoints ──────────────────────────────────────

@app.post("/api/quote/{quote_id}/submit-pricing")
async def submit_pricing(quote_id: int, pricing: SubmitPricing):
    """Purchase team / Quote Analyst submits pricing for a quotation.
    This triggers the AI to draft the quotation email."""
    session = get_session(DATABASE_URL)
    try:
        quote = session.query(Quotation).filter_by(id=quote_id).first()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")

        if quote.status not in (QuoteStatus.AWAITING_PRICING, QuoteStatus.PARTIALLY_PRICED):
            raise HTTPException(status_code=400, detail="Quotation is not awaiting pricing")

        if pricing.pricing_notes:
            quote.notes = pricing.pricing_notes

        # Build a pricing batch in the same shape a team email reply produces, then delegate to
        # the shared partial-aware drafter so the manual quote uses the identical standard table
        # (MPN | Description | Make | Qty | Price | SPQ | MOQ | Lead Time | Remark), SPQ, partial
        # handling and green-"New" highlight as the automatic path.
        pricing_items = [{
            "mpn": p.mpn,
            "unit_price": p.unit_price,
            "lead_time": p.lead_time,
            "moq": p.moq,
            "spq": p.spq,
            "notes": p.notes,
        } for p in pricing.items]
        currency = (pricing.currency or "").upper() if pricing.currency else ""

        # apply_team_pricing builds the quote in code (no LLM) and needs no gmail/claude/line_card
        # deps, so construct a lightweight processor without spawning the Claude CLI.
        from core.email_processor import EmailProcessor
        processor = EmailProcessor(
            gmail_client=GMAIL_CLIENTS.get(quote.account),
            claude_client=None, line_card=None, bom_extractor=None,
            database_url=DATABASE_URL, account=quote.account,
        )
        actions = processor.apply_team_pricing(
            session, quote, pricing_items, currency,
            submitted_by=pricing.submitted_by or "purchase_team (manual)",
        )

        session.commit()
        return {
            "status": "ok",
            "quote_status": quote.status.value,
            "actions": actions,
            "message": "Pricing submitted. Quotation drafted and ready for salesperson approval.",
        }

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.error(f"Failed to submit pricing: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@app.post("/api/quote/{quote_id}/approve")
async def approve_quotation(quote_id: int, approval: QuoteApproval):
    """Human approves or edits a quotation draft before sending (initial or revised)."""
    session = get_session(DATABASE_URL)
    try:
        quote = session.query(Quotation).filter_by(id=quote_id).first()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")

        is_revised = quote.status == QuoteStatus.REVISED_AWAITING_APPROVAL

        if approval.action == "approve":
            quote.status = QuoteStatus.APPROVED
            if approval.approved_email_body:
                quote.approved_email_body = approval.approved_email_body
            else:
                quote.approved_email_body = quote.draft_email_body

            session.add(AuditLog(
                action="revised_quotation_approved" if is_revised else "quotation_approved",
                entity_type="quotation",
                entity_id=quote.id,
                details={
                    "quote_number": quote.quote_number,
                    "negotiation_round": quote.negotiation_round or 0,
                },
                performed_by="human",
            ))

        elif approval.action == "reject":
            quote.status = QuoteStatus.DRAFT
            session.add(AuditLog(
                action="quotation_rejected",
                entity_type="quotation",
                entity_id=quote.id,
                details={"quote_number": quote.quote_number},
                performed_by="human",
            ))

        elif approval.action == "edit":
            if approval.approved_email_body:
                quote.approved_email_body = approval.approved_email_body
            session.add(AuditLog(
                action="quotation_edited",
                entity_type="quotation",
                entity_id=quote.id,
                performed_by="human",
            ))

        session.commit()
        return {"status": "ok", "quote_status": quote.status.value}

    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


def _customer_quote_cc(quote) -> list:
    """CC for a customer-facing quote sent from the dashboard: the customer's OWN team (persisted on
    the quote from the original inquiry) + the internal oversight pair (the director). De-duped.
    The dashboard send is a deliberate human action, so oversight is always included."""
    from core.email_processor import _addrs
    from config.settings import get_settings
    cc = _addrs(getattr(quote, "customer_cc", None))
    for a in get_settings().get_vendor_rfq_cc():
        if a and a not in cc:
            cc.append(a)
    return cc


@app.post("/api/quote/{quote_id}/send")
async def send_quotation(quote_id: int):
    """Send an approved quotation to the customer via Gmail."""
    session = get_session(DATABASE_URL)
    try:
        quote = session.query(Quotation).filter_by(id=quote_id).first()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")

        if quote.status != QuoteStatus.APPROVED:
            raise HTTPException(status_code=400, detail="Quotation must be approved before sending")

        email = session.query(Email).filter_by(id=quote.email_id).first()
        if not email:
            raise HTTPException(status_code=404, detail="Original email not found")

        gmail_client = GMAIL_CLIENTS.get(quote.account)
        if not gmail_client:
            raise HTTPException(status_code=500, detail=f"Gmail client not configured for {quote.account}")

        body_to_send = quote.approved_email_body or quote.draft_email_body

        is_revised = (quote.negotiation_round or 0) > 0
        subject_prefix = f"Revised Quotation (R{quote.negotiation_round})" if is_revised else "Quotation"

        # NOTE: never put the internal quote_number (AE-Q-...) in the CUSTOMER subject — customers
        # mistake it for a part number. Reply threads on the customer's own thread_id, so matching a
        # later customer reply back to this quote is done by thread_id, not the subject code.
        gmail_client.send_reply(
            thread_id=email.thread_id,
            to_email=quote.customer_email,
            subject=f"{subject_prefix} - {email.subject}",
            body_html=body_to_send,
            cc_emails=_customer_quote_cc(quote) or None,  # their team + the director oversight
        )

        quote.status = QuoteStatus.REVISED_SENT if is_revised else QuoteStatus.SENT
        quote.sent_at = datetime.now(timezone.utc)
        # Reset follow-up timers on revised sends so follow-ups restart from new sent_at
        if is_revised:
            quote.followup_1_sent_at = None
            quote.followup_2_sent_at = None
            # Update the negotiation round record
            neg_round = session.query(NegotiationRound).filter_by(
                quotation_id=quote.id,
                round_number=quote.negotiation_round,
            ).first()
            if neg_round:
                neg_round.revised_quote_sent_at = datetime.now(timezone.utc)
        email.status = EmailStatus.SENT

        session.add(AuditLog(
            action="revised_quotation_sent" if is_revised else "quotation_sent",
            entity_type="quotation",
            entity_id=quote.id,
            details={
                "to": quote.customer_email,
                "quote_number": quote.quote_number,
                "negotiation_round": quote.negotiation_round or 0,
            },
            performed_by="human",
        ))

        session.commit()
        return {"status": "sent", "quote_number": quote.quote_number}

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.error(f"Failed to send quotation: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@app.get("/api/stats")
async def get_stats():
    """Get agent statistics."""
    session = get_session(DATABASE_URL)
    try:
        return {
            "total_emails": session.query(Email).count(),
            "rfq_count": session.query(Email).filter(Email.email_type == EmailType.RFQ).count(),
            "po_count": session.query(Email).filter(Email.email_type == EmailType.PO).count(),
            "negotiation_count": session.query(Email).filter(Email.email_type == EmailType.NEGOTIATION).count(),
            "awaiting_pricing": session.query(Quotation).filter(
                Quotation.status.in_([QuoteStatus.AWAITING_PRICING, QuoteStatus.AWAITING_REVISED_PRICING])
            ).count(),
            "pending_approvals": session.query(Quotation).filter(
                Quotation.status.in_([QuoteStatus.AWAITING_APPROVAL, QuoteStatus.REVISED_AWAITING_APPROVAL, QuoteStatus.PARTIALLY_PRICED])
            ).count(),
            "in_negotiation": session.query(Quotation).filter(
                Quotation.status.in_([
                    QuoteStatus.NEGOTIATION_FORWARDED,
                    QuoteStatus.AWAITING_REVISED_PRICING,
                    QuoteStatus.REVISED_AWAITING_APPROVAL,
                ])
            ).count(),
            "quotes_sent": session.query(Quotation).filter(
                Quotation.status.in_([QuoteStatus.SENT, QuoteStatus.REVISED_SENT])
            ).count(),
            "po_received": session.query(Quotation).filter(
                Quotation.status.in_([QuoteStatus.PO_RECEIVED, QuoteStatus.PO_FORWARDED])
            ).count(),
            "escalated": session.query(Email).filter(
                Email.status == EmailStatus.ESCALATED
            ).count(),
        }
    finally:
        session.close()


# ─── Leads Pages & API ──────────────────────────────────

@app.get("/leads", response_class=HTMLResponse)
async def leads_page(request: Request, status: Optional[str] = None, country: Optional[str] = None):
    manager = LeadManager(DATABASE_URL)
    leads = manager.list_leads(status=status, country=country, limit=500)
    counts = manager.count_leads()

    return templates.TemplateResponse(
        request=request,
        name="leads.html",
        context={
            "leads": leads,
            "counts": counts,
            "filter_status": status,
            "filter_country": country,
        },
    )


@app.get("/lead/{lead_id}", response_class=HTMLResponse)
async def lead_detail(request: Request, lead_id: int):
    manager = LeadManager(DATABASE_URL)
    lead = manager.get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    history = manager.get_customer_history(lead.email, limit=30)

    return templates.TemplateResponse(
        request=request,
        name="lead_detail.html",
        context={
            "lead": lead,
            "history": history,
        },
    )


@app.post("/api/lead")
async def create_lead(lead_data: LeadCreate):
    manager = LeadManager(DATABASE_URL)
    try:
        lead = manager.add_lead(lead_data.model_dump())
        return {"status": "ok", "lead_id": lead.id, "email": lead.email}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/lead/{lead_id}/update")
async def update_lead_api(lead_id: int, updates: dict):
    manager = LeadManager(DATABASE_URL)
    lead = manager.update_lead(lead_id, updates)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"status": "ok", "lead_id": lead.id}


@app.delete("/api/lead/{lead_id}")
async def delete_lead_api(lead_id: int):
    manager = LeadManager(DATABASE_URL)
    if not manager.delete_lead(lead_id):
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"status": "deleted"}


@app.post("/api/leads/import")
async def import_leads(file: UploadFile = File(...)):
    manager = LeadManager(DATABASE_URL)
    content = await file.read()

    filename = file.filename.lower()
    if filename.endswith(".csv"):
        result = manager.import_from_csv(content)
    elif filename.endswith((".xlsx", ".xls")):
        result = manager.import_from_excel(content)
    else:
        raise HTTPException(status_code=400, detail="File must be CSV or Excel")

    return result


@app.get("/api/leads/export")
async def export_leads():
    """Export all leads to Excel and download."""
    manager = LeadManager(DATABASE_URL)
    file_bytes = manager.export_to_excel()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"semisales_leads_{timestamp}.xlsx"

    return StreamingResponse(
        io.BytesIO(file_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/leads/count")
async def leads_count():
    manager = LeadManager(DATABASE_URL)
    return manager.count_leads()


# ─── Classification Correction (Auto-Learning) ──────────

@app.post("/api/email/{email_id}/correct")
async def correct_classification(email_id: int, correction: ClassificationCorrection):
    """Record a human correction to improve future classifications."""
    session = get_session(DATABASE_URL)
    try:
        email = session.query(Email).filter_by(id=email_id).first()
        if not email:
            raise HTTPException(status_code=404, detail="Email not found")

        agent_class = email.email_type.value if email.email_type else "other"

        learning = LearningManager(DATABASE_URL)
        success = learning.record_correction(
            email_id=email_id,
            agent_classification=agent_class,
            human_correction=correction.correct_type,
            reason=correction.reason or "",
        )

        if not success:
            raise HTTPException(status_code=500, detail="Failed to record correction")

        return {"status": "ok", "message": "Correction recorded - agent will learn from this"}
    finally:
        session.close()


@app.get("/api/emails")
async def list_emails(limit: int = 50, email_type: Optional[str] = None):
    """List emails via API."""
    session = get_session(DATABASE_URL)
    try:
        query = session.query(Email).order_by(Email.created_at.desc())
        if email_type:
            query = query.filter(Email.email_type == coerce_email_type(email_type))
        emails = query.limit(limit).all()

        return [{
            "id": e.id,
            "from": e.from_email,
            "subject": e.subject,
            "type": e.email_type.value if e.email_type else None,
            "status": e.status.value if e.status else None,
            "urgency": e.urgency,
            "received_at": str(e.received_at),
        } for e in emails]
    finally:
        session.close()
