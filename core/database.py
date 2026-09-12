"""
SemiSales AI Agent - Database Models
Stores emails, quotations, BOM data, and approval workflows.
"""

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, Boolean,
    Float, ForeignKey, Enum as SQLEnum, JSON
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime, timezone
import enum

Base = declarative_base()


class EmailType(str, enum.Enum):
    RFQ = "rfq"
    PO = "po"
    NEGOTIATION = "negotiation"
    VENDOR_OFFER = "vendor_offer"        # a supplier pushing an unsolicited stock/price list AT us (market intel)
    DELIVERY_QUERY = "delivery_query"
    TECHNICAL = "technical"
    COMPLAINT = "complaint"
    SPAM = "spam"
    OTHER = "other"


def coerce_email_type(value) -> "EmailType":
    """Safely map an arbitrary classifier/filter string to an EmailType.
    Never raises — unknown or malformed values fall back to EmailType.OTHER.
    Prevents a stray value like 'RFQ' / 'inquiry' from crashing email processing
    (which would leave the email unread and reprocessed on every cycle forever)."""
    try:
        return EmailType((value or "other").strip().lower())
    except (ValueError, AttributeError):
        return EmailType.OTHER


class EmailStatus(str, enum.Enum):
    NEW = "new"
    ACKNOWLEDGED = "acknowledged"
    PROCESSING = "processing"
    DRAFT_READY = "draft_ready"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SENT = "sent"
    ESCALATED = "escalated"
    ARCHIVED = "archived"


class QuoteStatus(str, enum.Enum):
    AWAITING_PRICING = "awaiting_pricing"  # Waiting for purchase team / quote analyst to provide pricing
    SOURCING_VENDORS = "sourcing_vendors"  # Autonomous: vendor RFQs sent, awaiting vendor cost replies
    VENDOR_PRICED = "vendor_priced"        # Autonomous: vendor costs consolidated + margin applied, ready for confidence gate
    PARTIALLY_PRICED = "partially_priced"  # Some items priced (partial quote drafted); still awaiting the rest
    DRAFT = "draft"                        # Pricing received, quotation email drafted
    AWAITING_APPROVAL = "awaiting_approval"  # Draft ready for salesperson review
    APPROVED = "approved"
    SENT = "sent"
    FOLLOW_UP_1 = "follow_up_1"
    FOLLOW_UP_2 = "follow_up_2"
    # Negotiation cycle (can repeat multiple rounds on same team threads)
    NEGOTIATION_FORWARDED = "negotiation_forwarded"          # Customer target prices forwarded to same team threads
    AWAITING_REVISED_PRICING = "awaiting_revised_pricing"    # Waiting for team's revised prices on same threads
    REVISED_AWAITING_APPROVAL = "revised_awaiting_approval"  # Revised draft ready for approval
    REVISED_SENT = "revised_sent"                            # Revised quotation sent to customer
    # PO stage
    PO_RECEIVED = "po_received"              # Customer sent PO on this quote
    PO_FORWARDED = "po_forwarded"            # PO forwarded to purchase/product team for processing
    WON = "won"
    LOST = "lost"
    EXPIRED = "expired"


class Email(Base):
    __tablename__ = "emails"

    id = Column(Integer, primary_key=True, autoincrement=True)
    gmail_id = Column(String(255), unique=True, nullable=False)
    thread_id = Column(String(255))
    account = Column(String(255), nullable=False)  # which inbox this row came from
    from_email = Column(String(255))
    from_name = Column(String(255))
    to_email = Column(String(255))
    cc = Column(Text)  # raw Cc header of the inbound email — who else the sender looped in (their team)
    subject = Column(Text)
    body_text = Column(Text)
    body_html = Column(Text)
    received_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    email_type = Column(SQLEnum(EmailType), default=EmailType.OTHER)
    status = Column(SQLEnum(EmailStatus), default=EmailStatus.NEW)
    urgency = Column(String(20), default="normal")  # normal, high, critical, priority (RFQ=high, Negotiation=critical, PO=priority)
    has_attachments = Column(Boolean, default=False)
    attachment_names = Column(JSON, default=list)
    extracted_data = Column(JSON, default=dict)  # structured extraction results
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    bom_items = relationship("BOMItem", back_populates="email", cascade="all, delete-orphan")
    quotations = relationship("Quotation", back_populates="email", cascade="all, delete-orphan")


class BOMItem(Base):
    __tablename__ = "bom_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email_id = Column(Integer, ForeignKey("emails.id"), nullable=False)
    mpn = Column(String(255), nullable=True)  # Manufacturer Part Number (nullable for description-only items)
    manufacturer = Column(String(255))
    description = Column(Text)
    quantity = Column(Integer)
    package = Column(String(100))  # Package type: SOD-123F, SOT-23-3, QFN-48, etc.
    annual_quantity = Column(Integer)  # Annual volume (for volume discount decisions)
    needs_identification = Column(Boolean, default=False)  # True when customer gave description but no MPN
    target_price = Column(Float)
    target_currency = Column(String(10))
    required_date = Column(String(100))
    special_requirements = Column(Text)  # RoHS, AEC-Q100, etc.

    # Line card check results
    is_authorized = Column(Boolean)  # Is the manufacturer on our line card?
    authorized_brand = Column(String(255))  # If alternative found, which brand
    alternative_mpn = Column(String(255))  # Alternative part number
    alternative_notes = Column(Text)  # Spec comparison notes
    line_status = Column(String(20))  # green / amber / red

    email = relationship("Email", back_populates="bom_items")


class Quotation(Base):
    __tablename__ = "quotations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email_id = Column(Integer, ForeignKey("emails.id"), nullable=False)
    quote_number = Column(String(50), unique=True)
    customer_name = Column(String(255))
    customer_email = Column(String(255))
    customer_cc = Column(Text)  # the customer's own team (from the inquiry Cc) — kept in CC on every reply for this quote
    customer_company = Column(String(255))
    account = Column(String(50))  # which inbox this row came from
    currency = Column(String(10))
    currency_assumed = Column(Boolean, default=False)  # True = inferred, not stated by customer -> confirm on dashboard
    currency_source = Column(String(80))               # how currency was decided (symbol / domain / account default)
    margin_percent_override = Column(Float)            # per-quote margin (e.g. 5% for a distributor BOM); None = use settings default/overrides
    status = Column(SQLEnum(QuoteStatus), default=QuoteStatus.DRAFT)
    line_items = Column(JSON, default=list)  # list of quoted items with prices
    draft_email_body = Column(Text)  # AI-drafted email body
    approved_email_body = Column(Text)  # Human-edited/approved body
    validity_days = Column(Integer, default=30)
    total_amount = Column(Float)
    notes = Column(Text)

    # Internal forwarding thread tracking (same email trail)
    # Product team thread (authorized brands)
    product_thread_id = Column(String(255))    # Gmail thread ID of forwarded email to product team
    product_message_id = Column(String(255))   # Gmail message ID
    product_forward_items = Column(JSON)       # List of MPNs sent to product team
    # Purchase team thread (unauthorized brands - sourcing/trading)
    purchase_thread_id = Column(String(255))   # Gmail thread ID of forwarded email to purchase team
    purchase_message_id = Column(String(255))  # Gmail message ID
    purchase_forward_items = Column(JSON)      # List of MPNs sent to purchase team
    # Pricing tracking
    pricing_received_at = Column(DateTime)     # When Quote Analyst submitted pricing
    pricing_submitted_by = Column(String(255)) # Who submitted pricing

    # Negotiation tracking
    negotiation_round = Column(Integer, default=0)  # Current round (0 = no negotiation yet)

    # Tracking
    sent_at = Column(DateTime)
    followup_1_sent_at = Column(DateTime)
    followup_2_sent_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    email = relationship("Email", back_populates="quotations")
    negotiations = relationship("NegotiationRound", back_populates="quotation", cascade="all, delete-orphan")


class NegotiationRound(Base):
    """Tracks each negotiation round — customer target prices and team revised prices."""
    __tablename__ = "negotiation_rounds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    quotation_id = Column(Integer, ForeignKey("quotations.id"), nullable=False)
    round_number = Column(Integer, nullable=False)  # 1, 2, 3, ...
    customer_email_id = Column(Integer, ForeignKey("emails.id"), nullable=True)  # The negotiation email from customer

    # Customer's target prices for this round
    customer_target_prices = Column(JSON, default=list)  # [{mpn, target_price, notes}, ...]

    # Team's revised prices for this round
    team_revised_prices = Column(JSON, default=list)  # [{mpn, unit_price, lead_time, moq, notes}, ...]

    # Forwarding tracking (replies go on SAME threads as original RFQ forward)
    forwarded_at = Column(DateTime)          # When target prices were forwarded to team
    revised_pricing_at = Column(DateTime)    # When team replied with revised prices
    revised_quote_sent_at = Column(DateTime) # When revised quotation was sent to customer

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    quotation = relationship("Quotation", back_populates="negotiations")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    action = Column(String(100), nullable=False)
    entity_type = Column(String(50))  # email, quotation, bom_item
    entity_id = Column(Integer)
    details = Column(JSON)
    performed_by = Column(String(50), default="agent")  # agent or human
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Lead(Base):
    """Customer leads database - for lead generation and tracking."""
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255))
    company = Column(String(255), index=True)
    designation = Column(String(255))
    email = Column(String(255), unique=True, index=True, nullable=False)
    phone = Column(String(100))
    alternate_phone = Column(String(100))
    address = Column(Text)
    city = Column(String(100))
    state = Column(String(100))
    country = Column(String(100), default="India")
    pincode = Column(String(20))
    website = Column(String(255))

    # Business intelligence
    application = Column(Text)  # What they make / what the parts are for
    industry = Column(String(100))  # Automotive, IoT, Medical, Industrial, etc.
    annual_volume_estimate = Column(String(100))  # e.g., "low", "medium", "high" or value
    typical_brands_used = Column(JSON, default=list)  # Brands they've mentioned buying
    product_categories = Column(JSON, default=list)  # Categories they've shown interest in

    # Lead tracking
    source = Column(String(50), default="email")  # email, manual, csv_import, web, directory
    source_details = Column(String(255))  # specific source info
    status = Column(String(50), default="new")  # new, contacted, qualified, customer, inactive
    tags = Column(JSON, default=list)
    notes = Column(Text)

    # Interaction tracking
    first_contact_at = Column(DateTime)
    last_contact_at = Column(DateTime)
    total_inquiries = Column(Integer, default=0)
    total_quotes_sent = Column(Integer, default=0)
    total_orders = Column(Integer, default=0)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class CustomerHistory(Base):
    """Tracks past interactions per customer email for learning customer preferences."""
    __tablename__ = "customer_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_email = Column(String(255), index=True, nullable=False)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True)
    email_id = Column(Integer, ForeignKey("emails.id"), nullable=True)
    interaction_type = Column(String(50))  # inquiry, quote_sent, order, complaint
    summary = Column(Text)
    mpns_requested = Column(JSON, default=list)
    brands_requested = Column(JSON, default=list)
    extracted_data = Column(JSON, default=dict)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ClassificationFeedback(Base):
    """Stores human corrections to agent classifications for auto-learning."""
    __tablename__ = "classification_feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email_id = Column(Integer, ForeignKey("emails.id"), nullable=False)
    subject = Column(Text)
    body_snippet = Column(Text)  # first 500 chars
    from_email = Column(String(255))
    agent_classification = Column(String(50))
    human_correction = Column(String(50))
    reason = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ============================================================================
# AUTONOMOUS PIPELINE — Vendor sourcing, cost quotes, margin (Phase 1 data model)
# ============================================================================

class Vendor(Base):
    """Vendor / supplier database — WHO to source pricing from, routed by brand.
    Loaded from vendors_template.xlsx via scripts/import_vendors.py."""
    __tablename__ = "vendors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    email = Column(String(500))                   # one or more, comma/semicolon separated (may be blank until sourced)
    vendor_type = Column(String(30), default="non_authorized")  # non_authorized | authorized
    brands = Column(JSON, default=list)           # brand names this vendor supplies; ["ANY"] = open-market broker
    categories = Column(JSON, default=list)       # product-of-interest (IC/MCU, Passives, Connectors, Active...)
    currency = Column(String(10), default="USD")  # currency this vendor quotes in
    country = Column(String(100))
    priority = Column(Integer)                    # 1 = preferred (manual; blank = let the scorecard decide)
    active = Column(Boolean, default=True)        # False = keep on file but don't email
    notes = Column(Text)
    # Learned scorecard (E6) — recomputed from real RFQ/quote outcomes
    score = Column(Float)                         # 0-100 blended performance
    dynamic_priority = Column(Integer)            # derived from score (1 best .. 5 worst)
    rfqs_sent = Column(Integer, default=0)
    rfqs_replied = Column(Integer, default=0)
    quotes_received = Column(Integer, default=0)
    quotes_won = Column(Integer, default=0)       # times selected as the lowest landed cost
    avg_response_hours = Column(Float)
    avg_lead_days = Column(Float)
    last_scored_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class VendorRFQ(Base):
    """One row per vendor we emailed for a given quotation — tracks the vendor RFQ thread."""
    __tablename__ = "vendor_rfqs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    quotation_id = Column(Integer, ForeignKey("quotations.id"), nullable=False)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True)
    vendor_name = Column(String(255))
    vendor_email = Column(String(500))
    thread_id = Column(String(255))               # Gmail thread of the vendor RFQ
    message_id = Column(String(255))
    requested_mpns = Column(JSON, default=list)   # MPNs (or descriptions) asked of this vendor
    status = Column(String(30), default="sent")   # sent, replied, no_reply
    sent_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    replied_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class VendorQuote(Base):
    """A COST price for one part from one vendor (parsed from the vendor's reply).
    Consolidation picks the lowest landed cost per part; the margin engine turns it into resale."""
    __tablename__ = "vendor_quotes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    quotation_id = Column(Integer, ForeignKey("quotations.id"), nullable=False)
    vendor_rfq_id = Column(Integer, ForeignKey("vendor_rfqs.id"), nullable=True)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True)
    vendor_name = Column(String(255))
    mpn = Column(String(255))
    manufacturer = Column(String(255))
    cost_price = Column(Float)                     # per-unit COST quoted by the vendor
    currency = Column(String(10))
    moq = Column(Integer)
    spq = Column(Integer)
    offered_qty = Column(Integer)                  # qty this vendor can actually supply (cap); None = full/unlimited
    lead_time = Column(String(100))
    stock = Column(Integer)                        # available stock if quoted
    date_code = Column(String(100))
    packaging = Column(String(60))                 # Tape&Reel / Tray / Bulk / Tube / Cut Tape — drives split homogeneity check
    valid_until = Column(DateTime)                 # quote expiry (parsed from "valid 48h"); PO after this -> re-source
    validity_raw = Column(String(120))             # the raw validity text the vendor stated
    notes = Column(Text)
    is_selected = Column(Boolean, default=False)   # chosen as the lowest landed cost for its part
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class HSNDuty(Base):
    """India customs duty per HSN code — the VERIFIED reference the landed-cost engine reads.
    The agent classifies each part to an HSN and suggests a rate; a human confirms it ONCE
    (verified=True) and it's reused for every future part with that HSN."""
    __tablename__ = "hsn_duties"

    id = Column(Integer, primary_key=True, autoincrement=True)
    hsn_code = Column(String(20), unique=True, index=True)  # e.g. "8542"
    description = Column(String(255))
    bcd_percent = Column(Float)                # Basic Customs Duty % (null until a human sets it)
    sws_percent = Column(Float, default=10.0)  # Social Welfare Surcharge, as % of BCD
    suggested_bcd = Column(Float)              # the agent's suggestion, shown for confirmation
    verified = Column(Boolean, default=False)  # True once a human confirms the rate
    source = Column(String(50))                # seed | llm | user
    notes = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class FxRate(Base):
    """Stored USD->INR exchange rate (E4). Auto-fetched daily from a free currency API; the
    landed-cost engine reads `effective_rate` = fetched rate + a safety buffer for volatility."""
    __tablename__ = "fx_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair = Column(String(10), default="USDINR", index=True)
    base_rate = Column(Float)                  # raw rate from the API
    buffer_percent = Column(Float, default=0.0)
    effective_rate = Column(Float)             # base_rate x (1 + buffer%) — what pricing uses
    source = Column(String(50))                # api name / manual
    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class PartInfo(Base):
    """Cache of MPN → real manufacturer + description, resolved from a live part-search API (Mouser,
    etc.). GROUNDS part identification in a real distributor database instead of the LLM guessing.
    A row with blank manufacturer+description is a 'looked up, not found' marker (so we don't re-query)."""
    __tablename__ = "part_info"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mpn = Column(String(255), unique=True, index=True)  # stored UPPER
    manufacturer = Column(String(255))
    description = Column(Text)
    category = Column(String(255))
    datasheet_url = Column(String(500))
    source = Column(String(50))                # mouser | digikey | octopart | manual
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class VendorPO(Base):
    """A purchase order WE place to a vendor after the customer's PO (Order module).
    One row per vendor, for the lines that vendor won during consolidation."""
    __tablename__ = "vendor_pos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    quotation_id = Column(Integer, ForeignKey("quotations.id"), nullable=False)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True)
    vendor_name = Column(String(255))
    vendor_email = Column(String(500))
    po_number = Column(String(50))              # our PO number to the vendor
    customer_po_ref = Column(String(200))       # the customer's PO reference
    line_items = Column(JSON, default=list)     # [{mpn, description, quantity, unit_cost, currency, lead_time}]
    total_cost = Column(Float)
    currency = Column(String(10))
    # placed | confirmed | shipped | received | cancelled | test_sent | suppressed_testing | no_contact
    status = Column(String(30), default="placed")
    thread_id = Column(String(255))
    message_id = Column(String(255))
    placed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class MarketOffer(Base):
    """A standing vendor STOCK OFFER captured from an unsolicited 'offer / available stock' blast
    (not tied to any customer RFQ). This is grounded market intel — real vendor prices over time —
    surfaced when a customer later asks for a matching part. One row per offered line."""
    __tablename__ = "market_offers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mpn = Column(String(255), index=True)         # normalised MPN we match customer RFQs against
    manufacturer = Column(String(255))
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True)
    vendor_name = Column(String(255))
    vendor_email = Column(String(500))
    unit_price = Column(Float)                     # the vendor's offered COST
    currency = Column(String(10), default="USD")
    date_code = Column(String(30))                # "YY+"
    lead_time = Column(String(100))
    moq = Column(Integer)
    offered_qty = Column(Integer)                 # quantity available
    packaging = Column(String(60))
    source_gmail_id = Column(String(255))
    source_thread_id = Column(String(255))
    received_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    active = Column(Boolean, default=True)        # superseded offers can be deactivated
    notes = Column(Text)


def init_database(database_url: str):
    engine = create_engine(database_url, echo=False)
    Base.metadata.create_all(engine)

    # Migrate existing databases: add new columns if they don't exist yet.
    # SQLite silently errors if column already exists — safe to run every startup.
    from sqlalchemy import text
    with engine.connect() as conn:
        for stmt in [
            "ALTER TABLE bom_items ADD COLUMN package VARCHAR(100)",
            "ALTER TABLE bom_items ADD COLUMN annual_quantity INTEGER",
            "ALTER TABLE bom_items ADD COLUMN needs_identification BOOLEAN DEFAULT 0",
            "ALTER TABLE quotations ADD COLUMN negotiation_round INTEGER DEFAULT 0",
            "ALTER TABLE vendors ADD COLUMN vendor_type VARCHAR(30) DEFAULT 'non_authorized'",
            "ALTER TABLE vendors ADD COLUMN score FLOAT",
            "ALTER TABLE vendors ADD COLUMN dynamic_priority INTEGER",
            "ALTER TABLE vendors ADD COLUMN rfqs_sent INTEGER DEFAULT 0",
            "ALTER TABLE vendors ADD COLUMN rfqs_replied INTEGER DEFAULT 0",
            "ALTER TABLE vendors ADD COLUMN quotes_received INTEGER DEFAULT 0",
            "ALTER TABLE vendors ADD COLUMN quotes_won INTEGER DEFAULT 0",
            "ALTER TABLE vendors ADD COLUMN avg_response_hours FLOAT",
            "ALTER TABLE vendors ADD COLUMN avg_lead_days FLOAT",
            "ALTER TABLE vendors ADD COLUMN last_scored_at DATETIME",
            "ALTER TABLE quotations ADD COLUMN currency_assumed BOOLEAN DEFAULT 0",
            "ALTER TABLE quotations ADD COLUMN currency_source VARCHAR(80)",
            "ALTER TABLE quotations ADD COLUMN margin_percent_override FLOAT",
            "ALTER TABLE vendor_quotes ADD COLUMN offered_qty INTEGER",
            "ALTER TABLE vendor_quotes ADD COLUMN packaging VARCHAR(60)",
            "ALTER TABLE vendor_quotes ADD COLUMN valid_until DATETIME",
            "ALTER TABLE vendor_quotes ADD COLUMN validity_raw VARCHAR(120)",
            "ALTER TABLE emails ADD COLUMN cc TEXT",
            "ALTER TABLE quotations ADD COLUMN customer_cc TEXT",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass  # Column already exists

    Session = sessionmaker(bind=engine)
    return engine, Session


def get_session(database_url: str):
    engine = create_engine(database_url, echo=False)
    Session = sessionmaker(bind=engine)
    return Session()
