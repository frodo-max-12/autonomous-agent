"""
Margin engine — the "Quote Analyst" role of the autonomous pipeline.

v1.1 — Company B Pte Ltd (Singapore), INTERNATIONAL sales, priced in USD:

    cost_usd    = vendor cost converted to USD (via FX if the vendor quoted a non-USD currency)
    landed_cost = cost_usd * (1 + expenses%)      # freight / handling
    resale      = landed_cost * (1 + margin%)     # our margin

There is NO India import duty here (that was the India/Company A v1.0 path — removed). We AUTO-price USD
sales only; a customer who wants SGD/EUR is held for human review upstream. Margin % is chosen per
line: a category override wins, else a brand override, else the default. All rates come from settings.
"""

from __future__ import annotations
import math


def resolve_margin_percent(settings, category: str | None = None, brand: str | None = None) -> tuple[float, str]:
    """Return (margin_percent, reason). Category override > brand override > default."""
    overrides = settings.get_margin_overrides()
    if category and category.strip().lower() in overrides:
        return overrides[category.strip().lower()], f"category:{category}"
    if brand and brand.strip().lower() in overrides:
        return overrides[brand.strip().lower()], f"brand:{brand}"
    return float(settings.default_margin_percent), "default"


def compute_resale(cost, settings, *, cost_currency: str = "USD", sale_currency: str = "USD",
                   fx_rate=None, freight_percent=None, category=None, brand=None,
                   margin_percent_override=None, round_to: int = 4, **_ignored) -> dict | None:
    """Vendor COST -> customer RESALE in USD (ex-tax). A non-USD vendor cost (e.g. an Indian vendor
    quoting INR) is converted to USD at FX first. Non-USD SALE currencies (SGD/EUR) are NOT priced
    here — they return a 'blocked' so the caller holds the line for a human.

    Returns:
      - None                 if `cost` is missing/invalid,
      - {"blocked": ...}     if it can't price safely (non-USD sale, or no FX for a non-USD cost) —
                             the caller marks the line needs_review (never auto-sent),
      - a full breakdown     otherwise (includes `resale`).
    """
    try:
        cost = float(cost)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(cost) or cost <= 0:
        return None

    cost_currency = (cost_currency or "USD").upper()
    sale_currency = (sale_currency or "USD").upper()
    # A per-quote override (e.g. a flat 5% on a distributor BOM) wins over the settings default/overrides.
    if margin_percent_override is not None:
        try:
            margin_pct, margin_reason = float(margin_percent_override), "quote-override"
        except (TypeError, ValueError):
            margin_pct, margin_reason = resolve_margin_percent(settings, category=category, brand=brand)
    else:
        margin_pct, margin_reason = resolve_margin_percent(settings, category=category, brand=brand)
    _exp = getattr(settings, "default_expenses_percent", None)
    if _exp is None:
        _exp = getattr(settings, "default_freight_percent", 0)   # back-compat fallback
    expenses_pct = float(freight_percent if freight_percent is not None else (_exp or 0))

    # We AUTO-price USD sales only. SGD / EUR / any other sale currency is held for a human review.
    if sale_currency != "USD":
        return {"blocked": "currency",
                "reason": f"{sale_currency} sale — auto-pricing is USD only (SGD/EUR need human review)"}

    # USD vendor cost -> USD sale: expenses + margin, no conversion.
    if cost_currency == "USD":
        landed = cost * (1 + expenses_pct / 100.0)
        resale = landed * (1 + margin_pct / 100.0)
        return {"mode": "usd_domestic", "currency": "USD", "cost": round(cost, round_to),
                "expenses_percent": expenses_pct, "landed_cost": round(landed, round_to),
                "margin_percent": margin_pct, "margin_reason": margin_reason,
                "resale": round(resale, round_to)}

    # Non-USD vendor cost (e.g. INR from an Indian vendor) -> convert to USD, then expenses + margin.
    # fx_rate is USD->cost_currency (e.g. USD->INR ~83), so usd_cost = cost / fx_rate.
    if not fx_rate:
        return {"blocked": "no_fx", "reason": f"{cost_currency}->USD rate unavailable"}
    usd_cost = cost / float(fx_rate)
    landed = usd_cost * (1 + expenses_pct / 100.0)
    resale = landed * (1 + margin_pct / 100.0)
    return {"mode": "usd_import", "currency": "USD", "cost": round(cost, round_to),  # cost in its own ccy
            "cost_currency": cost_currency, "fx_rate": float(fx_rate),
            "usd_cost": round(usd_cost, round_to), "expenses_percent": expenses_pct,
            "landed_cost": round(landed, round_to), "margin_percent": margin_pct,
            "margin_reason": margin_reason, "resale": round(resale, round_to)}
