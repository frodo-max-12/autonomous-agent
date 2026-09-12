"""
SemiSales AI Agent - Cross Reference

Supplies REAL catalogue data about our authorised brands so the LLM can cross-reference against
facts instead of recalling part numbers it was never given.

DIVISION OF LABOUR (deliberate -- do not add domain rules to this file)
----------------------------------------------------------------------
  LLM  : decodes the customer's part, reads our real catalogue, judges which of our parts fits,
         and explains the trade-offs. All engineering judgement lives in the model. Nothing here
         scores, ranks, or decides what makes a good cross -- that would hardcode one family's
         reasoning (DC-DC) onto 54 brands of components it doesn't describe.
  CODE : retrieval (hand the model the right slice of the catalogue) and ONE safety gate
         (an MPN in a customer email must exist in verified data).

WHY THE GATE STAYS
------------------
The agent once turned a customer's Mornsun 'IB2405LS-1WR3' into 'B2405LS-1WR3 (CLAF Power)' -- a
part that does not exist -- by dropping a letter, and emailed it to a customer. That was not a
reasoning failure the model could have thought its way out of: it was asked for a catalogue it
never had. Freeing the model to think is right; letting it invent a part number is not.

A parametric fit gets us to the right SERIES, not to a guaranteed orderable MPN -- a catalogue
table says "DES1-F is 18-36Vin 1W 3kVDC SIP", not "DES1-F2405 is a real orderable part". So a
series-level judgement is offered to the customer at BRAND level and raised on the dashboard for a
human to pin the exact MPN. Once pinned it lands in `confirmed_crosses` and is automatic after that.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CROSS_REF_PATH = Path(__file__).parent / "cross_reference.json"

# Keys that are bookkeeping rather than electrical specs -- skipped when rendering a series for the
# model, so the prompt shows engineering data and nothing else.
_NON_SPEC_KEYS = {"series", "source_url", "family", "_comment", "_example"}


def _norm_mpn(mpn: str) -> str:
    """'IB2405LS-1WR3' -> 'IB2405LS1WR3'. Used only for equality, never for deriving a part number."""
    if not mpn:
        return ""
    return re.sub(r"[^A-Z0-9]+", "", str(mpn).upper())


def _norm_brand(name: str) -> str:
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


def _render_value(v) -> str:
    if isinstance(v, list):
        return "/".join(f"{x:g}" if isinstance(x, (int, float)) else str(x) for x in v)
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


class CrossReference:
    def __init__(self, path: str = None):
        self._path = Path(path) if path else CROSS_REF_PATH
        self._data = {"brands": {}, "confirmed_crosses": []}
        self._load()

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except FileNotFoundError:
            logger.warning(f"cross_reference.json not found at {self._path}; "
                           "no verified catalogue -- suggestions stay brand-level")
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"cross_reference.json is invalid ({e}); refusing to operate on "
                         "unverified data -- suggestions stay brand-level")

    # ---------------------------------------------------------------- brands

    def brands_with_data(self) -> list[str]:
        return list(self._data.get("brands", {}).keys())

    def has_brand_data(self, brand: str) -> bool:
        """True once a brand's catalogue has been harvested from the manufacturer's own site.
        False for the brands not yet done -- those degrade to brand-level suggestions rather than
        letting the model fill the gap from memory."""
        target = _norm_brand(brand)
        return any(_norm_brand(b) == target for b in self._data.get("brands", {}))

    # ------------------------------------------------------------- retrieval

    def catalogue_text(self, family: str = None, brands: list[str] = None,
                       max_series: int = 120) -> str:
        """Render our verified catalogue as text for the model to reason over.

        Filtering is coarse on purpose -- by product family only. Narrowing further (by voltage,
        package, power) would mean encoding what counts as 'close', which is the model's call, and
        an over-tight filter silently hides the option a human would have picked.
        """
        wanted_brands = {_norm_brand(b) for b in brands} if brands else None
        lines: list[str] = []
        count = 0
        for brand, binfo in self._data.get("brands", {}).items():
            if wanted_brands and _norm_brand(brand) not in wanted_brands:
                continue
            series_list = [s for s in binfo.get("series", [])
                           if not family or not s.get("family") or s.get("family") == family]
            if not series_list:
                continue
            lines.append(f"\n## {brand}  (source: {binfo.get('manufacturer_site', 'n/a')}, "
                         f"verified {binfo.get('verified_on', 'n/a')})")
            for s in series_list:
                if count >= max_series:
                    lines.append("  ... (catalogue truncated)")
                    break
                specs = ", ".join(f"{k}={_render_value(v)}"
                                  for k, v in s.items()
                                  if k not in _NON_SPEC_KEYS and v not in (None, ""))
                lines.append(f"  - {s.get('series', '?')}: {specs}")
                count += 1
        return "\n".join(lines) if lines else ""

    def known_series(self, brand: str = None) -> list[str]:
        out = []
        target = _norm_brand(brand) if brand else None
        for b, binfo in self._data.get("brands", {}).items():
            if target and _norm_brand(b) != target:
                continue
            out.extend(s.get("series", "") for s in binfo.get("series", []) if s.get("series"))
        return [s for s in out if s]

    # ------------------------------------------------------------ the gate

    def series_exists(self, brand: str, series_or_mpn: str) -> bool:
        """The one hard check: does this actually appear in manufacturer-verified data?

        Accepts an exact series name, or an MPN that starts with a known series (DES1-F2405 ->
        DES1-F). Deliberately NOT fuzzy: 'close enough' is how a fabricated part number gets through.
        """
        if not series_or_mpn:
            return False
        candidate = _norm_mpn(series_or_mpn)
        for s in self.known_series(brand):
            ns = _norm_mpn(s)
            if ns and (candidate == ns or candidate.startswith(ns)):
                return True
        return False

    # ------------------------------------------------------- confirmed cross

    def confirmed_cross(self, requested_mpn: str, requested_manufacturer: str = "") -> Optional[dict]:
        """A human-approved (their part -> our part) pair -- the ONLY source of a specific MPN in a
        customer email. None until somebody has confirmed this cross."""
        want = _norm_mpn(requested_mpn)
        if not want:
            return None
        want_mfr = _norm_brand(requested_manufacturer)
        for row in self._data.get("confirmed_crosses", []):
            if row.get("_example"):
                continue
            if _norm_mpn(row.get("requested_mpn")) != want:
                continue
            row_mfr = _norm_brand(row.get("requested_manufacturer"))
            if row_mfr and want_mfr and row_mfr != want_mfr:
                continue  # same MPN string can exist at two makers
            return row
        return None

    def record_confirmed_cross(self, requested_mpn: str, requested_manufacturer: str,
                               our_brand: str, our_mpn: str, confirmed_by: str,
                               notes: str = "") -> bool:
        """Persist a human-approved cross so it is automatic next time.

        Visible and undoable by design (the standing rule for anything the agent 'learns'): a plain
        row in a JSON file that can be read, edited or deleted.
        """
        if not (requested_mpn and our_brand and our_mpn and confirmed_by):
            return False
        existing = self.confirmed_cross(requested_mpn, requested_manufacturer)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if existing:
            existing.update(our_brand=our_brand, our_mpn=our_mpn, confirmed_by=confirmed_by,
                            confirmed_on=today, notes=notes or existing.get("notes", ""))
        else:
            self._data.setdefault("confirmed_crosses", []).append({
                "requested_mpn": requested_mpn,
                "requested_manufacturer": requested_manufacturer or "",
                "our_brand": our_brand,
                "our_mpn": our_mpn,
                "confirmed_by": confirmed_by,
                "confirmed_on": today,
                "notes": notes or "",
            })
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            logger.info(f"Confirmed cross recorded: {requested_mpn} -> {our_brand} {our_mpn} "
                        f"(by {confirmed_by})")
            return True
        except OSError as e:
            logger.error(f"Could not write cross_reference.json: {e}")
            return False
