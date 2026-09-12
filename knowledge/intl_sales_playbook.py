"""
Agent knowledge distilled from the REAL intl.sales@ mailbox study (see INTL_SALES_STUDY.md — the
human-readable source of truth). These durable conventions are injected into the LLM prompts so the
agent reasons like Company B's International trading desk instead of from generic assumptions.

When the playbook changes, edit INTL_SALES_STUDY.md and mirror the durable rules here.
"""

# --- What the business actually is (injected into classification + drafting) ---
BUSINESS_CONTEXT = """\
Company B Pte Ltd (Singapore) is an independent semiconductor-component
distributor/broker running a TWO-SIDED SPOT-TRADING desk:
- SOURCING: when a customer needs a part, we fan the SAME RFQ across a panel of vendors (HK / China /
  Singapore / India / EU traders + authorised distributors such as Arrow, Avnet, Ingram Micro),
  collect their COST quotes, and buy the cheapest TRUSTWORTHY stock.
- SELLING: we also broadcast our own spot-stock offers (CPUs, DRAM, SSD/HDD, GPUs) to buyers.
The SAME counterparties often appear on BOTH sides ("we have a customer who needs..."). Trade is in
USD per piece; most parts are memory (DRAM / DDR3 / DDR4), Intel/AMD CPUs, SSD/HDD, FPGAs and broadline ICs."""

# --- How the spot market quotes (injected into vendor-reply extraction + drafting) ---
TRADE_CONVENTIONS = """\
Real trade conventions of the semiconductor spot market:
- Currency is USD, priced PER PIECE. Some EU vendors quote EUR.
- DATE CODE is a first-class field, written "YY+" meaning "that year or newer" (e.g. "25+" = 2025 or
  newer). A newer date code is preferred and commands a premium. Always capture/ask for it.
- LEAD TIME signals stock: "2-3 days" / "ex-stock" = in stock; "3-4 weeks" / "allocation" = spot or
  factory allocation (uncertain).
- MOQ and SPQ matter (e.g. "SPQ 2K/reel", "MOQ 100 pcs"); higher quantity often unlocks better price
  tiers, so quantity is stated explicitly.
- Default incoterm is EXW (Ex-Works Hong Kong); default payment is T/T. Country of Origin (COO) and
  packaging (Tray / Reel / Tube) are commonly stated.
- Condition / traceability phrases: "100% new & original", "factory sealed", "Pb-free / RoHS".
- Vendors DECLINE bluntly: "no stock", "no carry", "no bid", "under allocation", "not authorised".
- AUTHENTICITY: a suspiciously LOW price is a COUNTERFEIT red flag, NOT a win — the desk asks for a
  stock-label photo (showing date code + packing) and/or a manufacturer CoC / authorised-distributor
  packing list before committing. Vendors often refuse until a PO is placed."""


def classification_context() -> str:
    """Business context that helps the classifier tell a customer RFQ from a vendor stock-offer blast."""
    return (BUSINESS_CONTEXT + "\n\n"
            "Classification hints from the real mailbox:\n"
            "- A VENDOR 'offer' / 'available stock' / daily 'DRAM offer' blast (a supplier pushing an\n"
            "  unsolicited price list AT us) is vendor market intel, not a customer rfq.\n"
            "- A CUSTOMER rfq leads with a specific part number + firm quantity and asks US to quote\n"
            "  (price / stock / lead time / date code); it may reply into one of our own offer threads.")


def extraction_context() -> str:
    """Conventions that help extract vendor COST replies accurately (date code, EXW, condition, decline)."""
    return TRADE_CONVENTIONS


def sourcing_context() -> str:
    """Full playbook slice for RFQ drafting / sourcing prompts."""
    return BUSINESS_CONTEXT + "\n\n" + TRADE_CONVENTIONS


# --- How the desk writes to CUSTOMERS (injected into acknowledgement / reply drafting) ---
REPLY_STYLE = """\
How the International desk writes to customers — match this tone:
- Brief, polite business English ("Hope you're well", "Kindly revert", "Please can you...").
- Mirror the customer's OWN numbered questions and answer them inline, in the same order.
- Be HONEST about supply — never over-promise stock: e.g. "no solid lead time until we get the
  allocation; usually ~4-6 weeks, subject to vendor stock availability."
- Keep the customer warm while we source: "Price noted — we're confirming with our sources and will revert."
- When a part is tight or unavailable, proactively offer a cross-reference / equivalent from our line."""


def reply_style_context() -> str:
    """Tone + phrasing guidance for customer-facing reply drafting."""
    return REPLY_STYLE
