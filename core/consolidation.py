"""
Consolidation + margin — steps 11-12 of the autonomous pipeline (Phase 4).

Mirrors the manual worksheet (see D:\\Company B\\Inquires\\a real customer worksheet): for each line we
compare every vendor cost (normalised to the sale currency via live FX), pick the cheapest, add
margin, and write cost/vendor/margin/resale back into quotation.line_items — plus a human-readable
Remark (exact / cross-brand / split) and an explicit NO BID when nobody quoted.

Split-sourcing: when the cheapest vendor can only supply PART of the required quantity (its reply
stated an `offered_qty` cap), the balance is filled from the next-cheapest vendor(s) and the line is
priced at the BLENDED landed cost — exactly how a buyer splits a line across suppliers. If the ask
still can't be fully covered, the covered portion is priced but the line is held for review (never
auto-sent as if it were complete).

Lines with no usable vendor cost are left un-priced (NO BID / pending / needs_review) so the Phase 5
confidence gate routes them to a human instead of guessing.
"""

from datetime import datetime, timezone
from loguru import logger
from core.database import VendorQuote
from core import margin_engine


# Phrases that are INTERNAL (our buy cost, our margin, our negotiation instructions). A vendor
# replying on a negotiation re-RFQ often quotes our own "target buy price … match or beat" line back
# to us; that text must NEVER surface on the customer quotation. Any remark/note containing one of
# these is treated as internal-only and dropped from the customer-facing remark.
INTERNAL_REMARK_MARKERS = (
    "target buy", "buy cost", "match or beat", "please match", "margin now", "margin =",
    "quoted at target", "re-sourcing", "re-source", "our target", "target cost",
    "target price was", "target buy price",
)


def is_internal_remark(text) -> bool:
    low = (text or "").lower()
    return any(m in low for m in INTERNAL_REMARK_MARKERS)


def _norm(mpn) -> str:
    return (mpn or "").strip().upper()


def _to_int(v):
    try:
        if v is None or v == "":
            return None
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _match_remark(quote, item) -> str:
    """Human-readable match note for a single-vendor line, echoing the worksheet's Remarks column
    ('Perfect and Complete P/N' / 'Quoted <brand> p/n' / 'Alternate ...')."""
    notes = (quote.notes or "").strip()
    # Drop any vendor note that echoes our internal target-buy-cost / negotiation wording — it must
    # never become the customer's remark. (A genuine alternate/cross note is kept below.)
    if is_internal_remark(notes):
        notes = ""
    low = notes.lower()
    if any(w in low for w in ("alternate", "alternative", "equivalent", "cross", "eol", "verify")):
        return notes[:120]
    # Offered brand differs from the requested manufacturer -> cross reference.
    req = (item.get("manufacturer") or "").strip()
    off = (quote.manufacturer or "").strip()
    if req and off:
        try:
            from knowledge.brand_aliases import brand_match
            if not brand_match(req, [off]):
                return f"Quoted {off} p/n"
        except Exception:
            if req.lower() not in off.lower() and off.lower() not in req.lower():
                return f"Quoted {off} p/n"
    return notes[:120] if notes else "Exact P/N"


def _combined_lead(leads: list) -> str | None:
    """For a split line, the effective lead time is the WORST (longest) leg."""
    vals = [l for l in leads if l and str(l).strip()]
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    try:
        from core.vendor_scorecard import _lead_days
        return max(vals, key=lambda s: (_lead_days(s) or 0))
    except Exception:
        return vals[0]


def _customer_lead(vendor_lead, vendor_country, settings, claude=None) -> str | None:
    """Customer-facing lead time = the vendor's lead + inbound transit to Singapore, because we quote
    EXW Singapore. The transit is the LLM's per-origin judgement (Hong Kong ≈ days, Europe/US ≈ weeks),
    cached; the configured flat value is only a last-resort fallback when no estimate exists and the
    model can't be reached. So a vendor's '1 week EXW China' becomes a realistic EXW-Singapore lead."""
    from core.vendor_scorecard import _lead_days
    from knowledge.transit_lead import transit_days_to_sg
    base = _lead_days(vendor_lead)
    is_sg = bool(vendor_country) and "singapore" in str(vendor_country).lower()
    transit_days = 0.0 if is_sg else transit_days_to_sg(vendor_country, claude)
    if transit_days is None:  # nothing cached and no model to ask → last-resort configured fallback
        transit_days = float(getattr(settings, "transit_weeks_to_sg", 2.0) or 0.0) * 7.0
    if base is None:
        if transit_days <= 0:
            return vendor_lead  # unknown vendor lead, no transit to add — leave as-is
        total = transit_days
    else:
        total = base + transit_days
    if total >= 7:
        wk = max(1, int(round(total / 7.0)))
        return f"{wk} week{'s' if wk != 1 else ''}"
    return f"{int(round(total))} days"


def _norm_pkg(p) -> str | None:
    """Canonicalise a packaging string so split legs can be compared for homogeneity.
    Returns None for unknown/blank (an unknown format never triggers a false mismatch)."""
    if not p or not str(p).strip():
        return None
    s = str(p).strip().lower()
    if "cut" in s and "tape" in s:
        return "cut_tape"
    if any(w in s for w in ("reel", "t&r", "t & r", "tape and reel", "tape&reel", "emboss")):
        return "reel"
    if "tray" in s:
        return "tray"
    if "tube" in s:
        return "tube"
    if any(w in s for w in ("bulk", "loose", "bag")):
        return "bulk"
    if "ammo" in s:
        return "ammo"
    if s in ("tape", "tape only"):
        return "reel"
    return None


def _as_naive(dt):
    """Compare timestamps safely — SQLite returns naive datetimes; strip tzinfo so <,> don't raise."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt


def _market_status(mpn, settings):
    """Raw live market availability dict for the MPN via Mouser ({in_stock, lead_time, shortage}) or
    None. Best-effort; never raises. Used both to feed the LLM planner and for the deterministic note."""
    mpn = (mpn or "").strip()
    if not mpn:
        return None
    try:
        from core.part_lookup import PartLookup
        pl = PartLookup(provider=getattr(settings, "part_lookup_provider", "none"),
                        api_key=getattr(settings, "part_lookup_api_key", ""),
                        database_url=getattr(settings, "database_url", ""), timeout=8)
        if not pl.enabled:
            return None
        return pl.market_status(mpn)
    except Exception:
        return None


def _market_note(mpn, settings) -> str | None:
    """DETERMINISTIC shortage note (used when the LLM planner is off / didn't judge). Returns a short
    CUSTOMER-facing note when Mouser flags the part allocated / on a long factory lead, else None.
    Never mentions a vendor — pricing must not depend on it."""
    st = _market_status(mpn, settings)
    if st and st.get("shortage"):
        lead = st.get("lead_time")
        return f"Market shortage — extended lead time{(' (~' + lead + ')') if lead else ''}"
    return None


def _deterministic_allocation(priced_cands, req_qty):
    """Rules-based allocation (the fallback + flag-off path): prefer the cheapest SINGLE vendor that can
    cover the full qty; only split across vendors (cheapest-first) as a last resort. `priced_cands` is
    already cheapest-first. Returns [(qty_or_None, landed, calc, quote)]."""
    if req_qty <= 0:
        landed, calc, c = priced_cands[0]
        return [(None, landed, calc, c)]  # unknown qty -> single cheapest vendor
    remaining = req_qty
    for landed, calc, c in priced_cands:  # cheapest-first; first vendor covering the full qty wins
        cap = c.offered_qty if (c.offered_qty and c.offered_qty > 0) else None
        if cap is None or cap >= remaining:
            return [(remaining, landed, calc, c)]
    allocations = []  # no single vendor covers it — split across vendors (last resort)
    for landed, calc, c in priced_cands:
        if remaining <= 0:
            break
        cap = c.offered_qty if (c.offered_qty and c.offered_qty > 0) else None
        take = remaining if cap is None else min(remaining, cap)
        if take <= 0:
            continue
        allocations.append((take, landed, calc, c))
        remaining -= take
    return allocations


def _validate_llm_plan(plan, priced_cands, req_qty):
    """Turn an LLM sourcing plan into an allocations list IF it is legal, else None (→ deterministic
    fallback). Gates: each `lot` index exists, no lot repeated, qty>0 and <= that lot's offered cap,
    and the total never exceeds the requirement. The LLM only chose lots+qty — never a price."""
    alloc = plan.get("allocation") if isinstance(plan, dict) else None
    if not isinstance(alloc, list) or not alloc:
        return None
    n = len(priced_cands)
    out, seen_lots, total = [], set(), 0
    for a in alloc:
        if not isinstance(a, dict):
            return None
        li, q = a.get("lot"), a.get("qty")
        if not isinstance(li, int) or li < 0 or li >= n or li in seen_lots:
            return None
        try:
            q = int(q)
        except (TypeError, ValueError):
            return None
        if q <= 0:
            return None
        landed, calc, c = priced_cands[li]
        cap = c.offered_qty if (c.offered_qty and c.offered_qty > 0) else None
        if cap is not None and q > cap:
            return None
        seen_lots.add(li)
        out.append((q, landed, calc, c))
        total += q
    if req_qty > 0 and total > req_qty:
        return None  # never quote more than the customer asked for
    out.sort(key=lambda x: x[1])  # cheapest leg first, so downstream 'primary' picks the main lot
    return out


def consolidate_and_price(quotation, session, settings, claude=None) -> dict:
    """Select cheapest vendor cost per line (splitting across vendors when one can't cover the qty),
    apply margin -> resale, and update line_items. For INR imports, resolves the HSN duty
    (classify + verify-once); unverified HSN -> needs_review.
    Returns {priced, unpriced, no_bid, partial, currency_skipped, total_lines, total_amount}."""
    quote_ccy = (quotation.currency or "USD").upper()

    vquotes = session.query(VendorQuote).filter(VendorQuote.quotation_id == quotation.id).all()
    by_mpn: dict = {}
    for vq in vquotes:
        by_mpn.setdefault(_norm(vq.mpn), []).append(vq)
    for vq in vquotes:
        vq.is_selected = False  # recompute selection each run

    # MPNs still awaiting a vendor: an RFQ was sent for them but that vendor hasn't replied yet. Such a
    # line with no cost is "pending" (a price may still come), NOT "no_bid" (everyone replied, none quoted).
    from core.database import VendorRFQ
    awaited_mpns = set()
    for r in session.query(VendorRFQ).filter(VendorRFQ.quotation_id == quotation.id).all():
        if r.status != "replied":
            for m in (r.requested_mpns or []):
                awaited_mpns.add(_norm(m))

    from core import fx as fx_mod
    fx = fx_mod.get_effective_rate(session, settings)  # USD->INR — converts an Indian vendor's INR cost to USD
    margin_override = getattr(quotation, "margin_percent_override", None)  # flat per-quote margin (e.g. 5%)

    items = [dict(it) for it in (quotation.line_items or [])]  # copy — JSON column mutation isn't tracked in place
    out_items = []  # consolidation may EXPAND a line into several (one per date-code lot) — build fresh
    # Vendor countries → customer-facing lead time (vendor lead + inbound transit to Singapore).
    from core.database import Vendor as _Vendor
    _vids = {vq.vendor_id for vq in vquotes if vq.vendor_id}
    country_by_vid = ({v.id: v.country for v in session.query(_Vendor).filter(_Vendor.id.in_(_vids)).all()}
                      if _vids else {})
    priced = unpriced = currency_skipped = no_bid = partial = 0
    pkg_mismatch_ct = expired_ct = eud_ct = split_review_ct = 0
    # For partial → updated quotes: a line that gains a price on a LATER consolidation (some lines were
    # already priced) is tagged "newly_priced" so the updated customer quote flags it green "New".
    prior_priced = {_norm(it.get("mpn")) for it in items if it.get("unit_price") is not None}
    is_followup = bool(prior_priced)

    for it in items:
        # A line whose price was set by autonomous negotiation (margin trimmed to meet the customer's
        # target) must NOT be re-priced back to the default margin when vendor replies re-trigger us.
        if it.get("negotiated") and it.get("unit_price") is not None:
            priced += 1
            out_items.append(it)
            continue

        req_qty = _to_int(it.get("quantity")) or 0
        candidates = [c for c in by_mpn.get(_norm(it.get("mpn")), []) if c.cost_price]

        # Manual vendor override (#2a): a human picked a specific vendor for this line on the
        # dashboard. Honour it — quote from that vendor only — instead of auto-selecting the cheapest.
        pinned_id = it.get("pinned_vendor_quote_id")
        if pinned_id is not None:
            pinned = [c for c in candidates if c.id == pinned_id]
            if pinned:
                candidates = pinned
                it["vendor_pinned"] = True
            else:
                it.pop("pinned_vendor_quote_id", None)  # pinned quote gone (re-sourced) → fall back to auto
                it["vendor_pinned"] = False

        # No vendor cost for this part yet. If a vendor we asked hasn't replied → PENDING (a price may
        # still come; shown "Pending" on a partial quote). If everyone replied and none quoted it → NO BID.
        if not candidates:
            if _norm(it.get("mpn")) in awaited_mpns:
                it.update(unit_price=None, cost_price=None, selected_vendor=None, fulfillment=None,
                          pricing_status="pending", pricing_note="awaiting vendor reply",
                          remark="Pending", newly_priced=False)
            else:
                it.update(unit_price=None, cost_price=None, selected_vendor=None, fulfillment=None,
                          pricing_status="no_bid", pricing_note="NO BID — no vendor quoted this part",
                          remark="NO BID", newly_priced=False)
                no_bid += 1
            unpriced += 1
            out_items.append(it)
            continue

        # Price every candidate to a USD landed cost so mixed-currency vendor quotes compare on equal
        # footing. A USD cost is used as-is; an Indian vendor's INR cost is converted to USD via FX; a
        # cost in any other non-USD currency (rare) has no rate here, so the engine blocks it -> review.
        priced_cands = []  # (landed, calc, quote)
        blocked_reason = None
        for c in candidates:
            c_ccy = (c.currency or quote_ccy).upper()
            cand_fx = fx if c_ccy == "INR" else None   # only INR is auto-converted to USD
            calc = margin_engine.compute_resale(
                c.cost_price, settings,
                cost_currency=c_ccy, sale_currency=quote_ccy,
                fx_rate=cand_fx, brand=it.get("manufacturer"),
                margin_percent_override=margin_override,
            )
            if not calc or calc.get("blocked"):
                blocked_reason = (calc.get("reason") if calc else "invalid vendor cost")
                continue
            landed = calc.get("landed_cost")
            if landed is None:
                landed = calc.get("cost_usd", calc.get("cost"))
            priced_cands.append((landed, calc, c))

        if not priced_cands:
            it.update(unit_price=None, cost_price=None, selected_vendor=None, fulfillment=None,
                      pricing_status="needs_review",
                      pricing_note=blocked_reason or "vendor currency needs FX/review",
                      remark="needs review")
            currency_skipped += 1
            unpriced += 1
            out_items.append(it)
            continue

        # Cheapest landed cost first; on a TIE, the shorter lead time wins (units normalised — a
        # "2 Days" quote beats a "1 Week" quote even though 2 > 1). Rounded so float noise never
        # flips a genuine tie. Unknown lead time sorts last.
        from core.vendor_scorecard import _lead_days
        def _lead_key(c):
            d = _lead_days(getattr(c, "lead_time", None))
            return d if d is not None else 10 ** 9
        priced_cands.sort(key=lambda x: (round(x[0], 4), _lead_key(x[2])))

        # "Too cheap = counterfeit" — the desk's real rule (INTL_SALES_STUDY.md). If the cheapest cost
        # is far below the panel median (needs >=3 quotes to be meaningful), don't trust it silently:
        # flag for an authenticity check (CoC / stock-label photo) — the confidence gate holds it.
        auth_flag = False
        if len(priced_cands) >= 3:
            landeds = sorted(x[0] for x in priced_cands)
            m = len(landeds) // 2
            median = landeds[m] if len(landeds) % 2 else (landeds[m - 1] + landeds[m]) / 2.0
            if median > 0 and priced_cands[0][0] < 0.6 * median:
                auth_flag = True

        # --- Allocate the required qty ---------------------------------------------------------
        # NON-HARDCODED PATH: let the AI BUYER decide the sourcing plan (which vendor lot(s) + how much,
        # single-vs-split, hold, shortage) from the GROUNDED lots. The code then VALIDATES the plan
        # (indices exist, qty <= offered, total <= required) and computes every price itself — the model
        # never states a number. An unsafe/over-allocated plan or an unreachable model falls straight
        # back to the deterministic rules (prefer single full-qty vendor; split only as a last resort).
        allocations = None
        plan_hold = False
        plan_shortage = None  # None = not judged by the LLM → use the deterministic Mouser note
        use_llm = (claude is not None and getattr(settings, "consolidation_llm_planning", True)
                   and len(priced_cands) > 1 and req_qty > 0 and not it.get("customer_target_price"))
        if use_llm:
            try:
                mkt = _market_status(it.get("mpn"), settings)
                lots = [{"lot": i, "cost_usd": round(landed, 4),
                         "offered_qty": (c.offered_qty if (c.offered_qty and c.offered_qty > 0) else None),
                         "date_code": c.date_code, "packaging": c.packaging,
                         "lead_time": c.lead_time, "vendor": c.vendor_name}
                        for i, (landed, calc, c) in enumerate(priced_cands)]
                plan = claude.plan_sourcing(it.get("mpn") or "", it.get("description") or "",
                                            it.get("manufacturer") or "", req_qty, lots, mkt)
                validated = _validate_llm_plan(plan, priced_cands, req_qty) if plan else None
                if validated:
                    allocations = validated
                    plan_hold = bool(plan.get("hold_for_review"))
                    plan_shortage = bool(plan.get("shortage"))
                    logger.info(f"[{quotation.quote_number}] AI sourcing plan for {it.get('mpn')}: "
                                f"{len(allocations)} lot(s), hold={plan_hold}, shortage={plan_shortage} — "
                                f"{str(plan.get('reason'))[:120]}")
                elif plan is not None:
                    logger.warning(f"[{quotation.quote_number}] AI sourcing plan for {it.get('mpn')} failed "
                                   f"validation — falling back to deterministic rules")
            except Exception as e:
                logger.warning(f"[{quotation.quote_number}] AI planning error for {it.get('mpn')}: {e} "
                               f"— falling back to deterministic rules")
        if allocations is None:
            allocations = _deterministic_allocation(priced_cands, req_qty)

        allocated_qty = sum(q for q, _, _, _ in allocations if q)
        shortfall = (req_qty - allocated_qty) if (req_qty > 0 and allocated_qty < req_qty) else 0
        covered = None if req_qty <= 0 else (req_qty - shortfall)
        # Distinct suppliers in the allocation. >1 = a cross-vendor split → priced but held for human
        # review. (A single-vendor multi-date-code line is NOT multi-vendor.) Key on vendor_id, falling
        # back to vendor_name when the id is missing.
        multi_vendor = len({(c.vendor_id if c.vendor_id is not None else c.vendor_name)
                            for _, _, _, c in allocations}) > 1

        # EUD / dual-use screening (per part — same for every date-code lot of this MPN).
        from knowledge.compliance_watchlist import check_eud
        eud_flag, eud_reason = check_eud(it.get("mpn"), it.get("manufacturer"),
                                         it.get("description"), it.get("hsn_code"), settings)
        if eud_flag:
            eud_ct += 1

        # DATE-CODE SPLIT — when the covered qty comes from lots with DIFFERENT date codes, quote each lot
        # as its OWN customer line (its qty / date code / price), never one blended line with a single date
        # code. That is how a human trader quotes: the customer prices and buys by date code.
        distinct_dcs = {(c.date_code or "").strip() for _, _, _, c in allocations if (c.date_code or "").strip()}
        if len(allocations) > 1 and len(distinct_dcs) > 1 and not it.get("customer_target_price"):
            for _, _, _, c in allocations:
                c.is_selected = True
            n_lots = len(allocations)
            for li, (q, landed, calc, c) in enumerate(allocations, 1):
                lot = dict(it)
                lot_margin = calc.get("margin_percent") or 0.0
                # Customer-facing remark must NOT name our vendor — the supplier is recorded in
                # `fulfillment` (dashboard only), never shown to the customer.
                lot_remark = f"Lot {li} of {n_lots} — date code {c.date_code or 'n/a'}"
                if auth_flag:
                    lot_remark += " · cost far below market — verify CoC / stock-label"
                lot.update(
                    quantity=(int(q) if q else req_qty),
                    cost_price=calc.get("cost_usd", calc.get("cost")),
                    cost_currency=(c.currency or quote_ccy),
                    selected_vendor=c.vendor_name,
                    fulfillment=[{"vendor": c.vendor_name, "qty": (int(q) if q else None),
                                  "landed_cost": round(landed, 4)}],
                    margin_percent=lot_margin,
                    unit_price=round(landed * (1 + lot_margin / 100.0), 4),
                    landed_cost=round(landed, 4),
                    fx_rate=calc.get("fx_rate"),
                    pricing_mode=calc.get("mode"),
                    lead_time=_customer_lead(c.lead_time, country_by_vid.get(c.vendor_id), settings, claude),
                    moq=(c.moq or it.get("moq")),
                    spq=(c.spq or it.get("spq")),
                    packaging=_norm_pkg(c.packaging),
                    date_code=c.date_code,
                    quote_valid_until=(_as_naive(c.valid_until).isoformat() if c.valid_until else None),
                    validity_raw=(c.validity_raw or None),
                    eud_required=bool(eud_flag), eud_reason=eud_reason,
                    authenticity_review=bool(auth_flag),
                    pricing_status="priced", pricing_note=lot_remark, remark=lot_remark,
                    newly_priced=False, date_code_lot=True,
                )
                out_items.append(lot)
                priced += 1
            if shortfall > 0:  # can't fully cover even across date codes → hold the group for review
                bal = (f"Only {int(covered):,} of {int(req_qty):,} available across date codes "
                       f"({int(shortfall):,} short) — confirm the balance")
                out_items[-1]["remark"] = out_items[-1]["remark"] + " · " + bal
                out_items[-1]["pricing_status"] = "needs_review"
                out_items[-1]["pricing_note"] = bal
                partial += 1
            elif multi_vendor or plan_hold:  # cross-supplier lots (or AI asked to hold) → human confirms
                mv = (f"Multiple suppliers needed for {int(req_qty):,} pcs — confirm sourcing before order"
                      if multi_vendor else "Held for buyer review before quoting")
                out_items[-1]["remark"] = out_items[-1]["remark"] + " · " + mv
                out_items[-1]["pricing_status"] = "needs_review"
                out_items[-1]["pricing_note"] = mv
                split_review_ct += 1
            continue

        # Blended landed cost across the allocation (single-vendor is the degenerate case).
        if covered and covered > 0:
            blended_landed = sum((q or 0) * l for q, l, _, _ in allocations) / covered
        else:
            blended_landed = allocations[0][1]

        primary_calc = allocations[0][2]
        primary_quote = allocations[0][3]
        margin_pct = primary_calc.get("margin_percent") or 0.0
        resale = round(blended_landed * (1 + margin_pct / 100.0), 4)

        # --- Negotiation best-price -------------------------------------------------------------
        # A line carrying a customer target price (set by _negotiate_autonomous when it re-sourced
        # this line for a sharper vendor cost) is priced by FLEXING our margin within
        # [negotiation floor, our normal margin] to get as close to the customer's target as we can:
        #   vendor came down enough  -> margin lands in-band -> we quote AT the target,
        #   vendor didn't move enough -> margin clamps to the floor -> that IS our best price,
        #   target is generous        -> margin caps at our normal margin (never overcharge).
        # This is the human "give our best price at 10-15% margin" step. Locks the line so later
        # consolidation rounds don't reset it to the default margin.
        ctp = it.get("customer_target_price")
        if ctp and blended_landed > 0:
            floor_m = getattr(settings, "negotiation_min_margin_percent", 10.0) or 10.0
            margin_to_hit = (ctp / blended_landed - 1.0) * 100.0
            margin_pct = round(max(float(floor_m), min(margin_to_hit, margin_pct)), 2)
            resale = round(blended_landed * (1 + margin_pct / 100.0), 4)
            it["negotiated"] = True

        for _, _, _, c in allocations:
            c.is_selected = True

        is_split = len(allocations) > 1
        fulfillment = [{"vendor": c.vendor_name, "qty": (int(q) if q else None),
                        "landed_cost": round(l, 4)} for q, l, _, c in allocations]
        if is_split:
            # Customer-facing remark must NOT reveal our vendors — the per-supplier breakdown lives in
            # `fulfillment` (dashboard only). This line is held for review below anyway (multi_vendor).
            remark = f"Consolidated from {len(allocations)} sources"
        else:
            remark = _match_remark(primary_quote, it)

        raw_lead = _combined_lead([c.lead_time for _, _, _, c in allocations]) or it.get("lead_time")
        lead = _customer_lead(raw_lead, country_by_vid.get(primary_quote.vendor_id), settings, claude) or raw_lead

        # Packaging homogeneity — a split must not blend incompatible formats (T&R + Bulk/Tray breaks
        # an assembly line). Unknown/blank packaging never triggers a false mismatch.
        pkgs = {_norm_pkg(c.packaging) for _, _, _, c in allocations if _norm_pkg(c.packaging)}
        pkg_mismatch = is_split and len(pkgs) > 1
        line_packaging = _norm_pkg(primary_quote.packaging) or (next(iter(pkgs)) if pkgs else None)

        # Quote validity — the binding expiry is the EARLIEST across the legs actually used.
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        valids = [_as_naive(c.valid_until) for _, _, _, c in allocations if c.valid_until]
        earliest_valid = min(valids) if valids else None
        expired = bool(earliest_valid and earliest_valid < now_naive)

        if auth_flag:
            remark = (remark + " · " if remark and remark != "Exact P/N" else "") + \
                     "cost far below market — verify CoC / stock-label"

        # Market-shortage signal for the customer. If the AI planner judged shortage (from live stock +
        # lead + part type), use its judgement; otherwise fall back to the deterministic Mouser note.
        # Looked up once per line (cached on the item).
        if not it.get("market_checked"):
            if plan_shortage is not None:
                it["market_note"] = "Market shortage — extended lead time" if plan_shortage else None
            else:
                it["market_note"] = _market_note(it.get("mpn"), settings)
            it["market_checked"] = True
        if it.get("market_note"):
            remark = (remark + " · " + it["market_note"]) if (remark and remark != "Exact P/N") else it["market_note"]

        it.update(
            cost_price=primary_calc.get("cost_usd", primary_calc.get("cost")),
            cost_currency=primary_quote.currency or quote_ccy,
            selected_vendor=primary_quote.vendor_name,
            fulfillment=fulfillment,
            margin_percent=margin_pct,
            unit_price=resale,                 # RESALE (ex-GST) shown to the customer
            landed_cost=round(blended_landed, 4),
            fx_rate=primary_calc.get("fx_rate"),
            pricing_mode=primary_calc.get("mode"),
            lead_time=lead,
            moq=primary_quote.moq or it.get("moq"),
            spq=primary_quote.spq or it.get("spq"),
            packaging=line_packaging,
            date_code=(primary_quote.date_code or it.get("date_code")),  # carry YY+ into the quote
            quote_valid_until=(earliest_valid.isoformat() if earliest_valid else None),
            validity_raw=(primary_quote.validity_raw or None),
            eud_required=bool(eud_flag),
            eud_reason=eud_reason,
            authenticity_review=bool(auth_flag),  # too-cheap → confidence gate holds for human check
            remark=remark,
        )

        # Status precedence: shortfall -> packaging mismatch -> expired quote -> cross-vendor split -> priced.
        if shortfall > 0:
            # Even after splitting, vendors can't cover the whole ask -> price the covered portion but
            # hold for a human (confirm the balance / lead time). Never auto-sent as complete.
            avail_note = (f"Only {int(covered):,} of {int(req_qty):,} available "
                          f"({int(shortfall):,} short) — confirm the balance")
            it.update(pricing_status="needs_review", pricing_note=avail_note, remark=avail_note)
            partial += 1
            unpriced += 1
        elif pkg_mismatch:
            pkg_note = ("Packaging mismatch across vendors (" + " vs ".join(sorted(pkgs)) +
                        ") — confirm before order")
            it.update(pricing_status="needs_review", pricing_note=pkg_note,
                      remark=f"{remark} | ⚠ {pkg_note}")
            pkg_mismatch_ct += 1
            unpriced += 1
        elif expired:
            exp_note = f"Vendor quote expired ({earliest_valid.date()}) — re-source before quoting"
            it.update(pricing_status="needs_review", pricing_note=exp_note, remark=exp_note)
            expired_ct += 1
            unpriced += 1
        elif multi_vendor or plan_hold:
            # A cross-vendor split (or the AI asked to hold) → price the covered amount but HOLD for a
            # human to confirm the sourcing before it goes to the customer.
            hv_note = (f"Multiple suppliers needed for {int(req_qty):,} pcs — confirm sourcing before order"
                       if multi_vendor else "Held for buyer review before quoting")
            it.update(pricing_status="needs_review", pricing_note=hv_note, remark=hv_note)
            split_review_ct += 1
            unpriced += 1
        else:
            it.update(pricing_status="priced",
                      pricing_note=(remark if is_split else None),
                      newly_priced=is_followup and (_norm(it.get("mpn")) not in prior_priced))
            priced += 1
        out_items.append(it)

    quotation.line_items = out_items  # reassign so SQLAlchemy persists the JSON change
    total = sum((it.get("unit_price") or 0) * (it.get("quantity") or 0)
                for it in items if it.get("unit_price"))
    quotation.total_amount = total
    quotation.pricing_received_at = datetime.now(timezone.utc)
    quotation.pricing_submitted_by = "autonomous consolidation"

    summary = {"priced": priced, "unpriced": unpriced, "no_bid": no_bid, "partial": partial,
               "packaging_mismatch": pkg_mismatch_ct, "expired": expired_ct, "eud_flagged": eud_ct,
               "split_review": split_review_ct,
               "currency_skipped": currency_skipped, "total_lines": len(items),
               "total_amount": round(total, 2)}
    logger.info(f"[{quotation.quote_number}] Consolidation: {summary}")
    return summary
