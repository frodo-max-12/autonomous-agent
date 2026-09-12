"""Resolve a bare manufacturer part number (MPN) to its REAL manufacturer + description via a live
part-search API, so identification is grounded in a distributor database instead of the LLM guessing
(which hallucinates for obscure MPNs — e.g. it invented 'CLB2424S-50WR3').

Used to enrich a customer inquiry that gives only a part number BEFORE we RFQ vendors, so the vendor
sees MPN + description + make, and the acknowledgement can tell the customer what the part is.

Provider: Mouser Search API (free with a Mouser account — one API key, simple POST, no OAuth).
Digi-Key / Octopart(Nexar) can be added later (they need OAuth). Results are cached in `part_info`
so the same MPN is never looked up twice.
"""

import json
import re
import urllib.request
from loguru import logger

from core.database import PartInfo, get_session

MOUSER_URL = "https://api.mouser.com/api/v1/search/partnumber?apiKey={key}"


class PartLookup:
    def __init__(self, provider: str = "none", api_key: str = "", database_url: str = "", timeout: int = 15):
        self.provider = (provider or "none").strip().lower()
        self.api_key = (api_key or "").strip()
        self.database_url = database_url
        self.timeout = timeout
        # Enabled only when a real provider + key are configured. Otherwise resolve() is a no-op
        # (the agent keeps working; parts just aren't auto-identified).
        self.enabled = self.provider not in ("", "none") and bool(self.api_key)

    def resolve(self, mpn: str) -> dict | None:
        """Return {manufacturer, description, category, datasheet_url, source} for the MPN, or None.
        Checks the local cache first; only hits the API for an MPN we've never resolved."""
        mpn = (mpn or "").strip()
        if not mpn:
            return None
        cached = self._from_cache(mpn)
        if cached is not None:                 # cache hit (may be an empty {} 'not found' marker)
            return cached or None
        if not self.enabled:
            return None
        try:
            info = self._mouser(mpn) if self.provider == "mouser" else None
            if info is None and self.provider != "mouser":
                logger.warning(f"Part-lookup provider '{self.provider}' not implemented yet (use 'mouser').")
        except Exception as e:
            logger.warning(f"Part lookup failed for {mpn}: {e}")
            return None
        self._to_cache(mpn, info)              # cache the result (or the 'not found')
        if info:
            logger.info(f"Part {mpn} identified: {info.get('manufacturer')} — {(info.get('description') or '')[:60]}")
        return info

    def market_status(self, mpn: str) -> dict | None:
        """Live availability for the MPN via Mouser: {in_stock:int, lead_time:str|None, shortage:bool}.
        shortage = zero distributor stock OR a long factory lead (>= ~12 weeks). Best-effort and
        UNCACHED — stock is time-sensitive, unlike the static manufacturer/description in resolve().
        Returns None on any error / not-found so the caller can silently skip the market note."""
        mpn = (mpn or "").strip()
        if not mpn or not self.enabled or self.provider != "mouser":
            return None
        try:
            return self._parse_availability(self._mouser_post(mpn), mpn)
        except Exception as e:
            logger.warning(f"Market-status lookup failed for {mpn}: {e}")
            return None

    # ---- providers ----
    def _mouser_post(self, mpn: str) -> dict:
        """Raw Mouser part-number search → parsed JSON (shared by identification + availability)."""
        body = json.dumps({"SearchByPartRequest": {"mouserPartNumber": mpn}}).encode("utf-8")
        req = urllib.request.Request(MOUSER_URL.format(key=self.api_key), data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def _mouser(self, mpn: str) -> dict | None:
        return self._parse_mouser(self._mouser_post(mpn), mpn)

    @staticmethod
    def _parse_availability(data: dict, mpn: str) -> dict | None:
        parts = ((data or {}).get("SearchResults") or {}).get("Parts") or []
        if not parts:
            return None
        target = PartLookup._norm_mpn(mpn)
        best = next((p for p in parts
                     if PartLookup._norm_mpn(p.get("ManufacturerPartNumber")) == target), None)
        if not best:
            return None
        avail = (best.get("Availability") or "").strip()      # e.g. "1234 In Stock" / "None"
        lead = (best.get("LeadTime") or "").strip() or None   # e.g. "42 Days"
        m = re.search(r"[\d,]+", avail)
        in_stock = int(m.group(0).replace(",", "")) if m else 0
        long_lead = False
        lm = re.search(r"(\d+)\s*(day|week)", (lead or "").lower())
        if lm:
            days = int(lm.group(1)) * (7 if lm.group(2) == "week" else 1)
            long_lead = days >= 84                            # ~12 weeks = allocation territory
        return {"in_stock": in_stock, "lead_time": lead, "shortage": (in_stock == 0) or long_lead}

    @staticmethod
    def _norm_mpn(s: str) -> str:
        return (s or "").strip().upper().replace("-", "").replace(" ", "").replace("_", "")

    @staticmethod
    def _parse_mouser(data: dict, mpn: str) -> dict | None:
        parts = ((data or {}).get("SearchResults") or {}).get("Parts") or []
        if not parts:
            return None
        # EXACT match only (ignoring dashes/spaces). If Mouser doesn't stock this exact part, return
        # None rather than a near-match — mis-identifying the manufacturer is worse than "not found".
        target = PartLookup._norm_mpn(mpn)
        best = next((p for p in parts
                     if PartLookup._norm_mpn(p.get("ManufacturerPartNumber")) == target), None)
        if not best:
            return None
        mfr = (best.get("Manufacturer") or "").strip()
        desc = (best.get("Description") or "").strip()
        if not mfr and not desc:
            return None
        return {
            "manufacturer": mfr or None,
            "description": desc or None,
            "category": (best.get("Category") or "").strip() or None,
            "datasheet_url": (best.get("DataSheetUrl") or "").strip() or None,
            "source": "mouser",
        }

    # ---- cache ----
    def _from_cache(self, mpn: str):
        session = get_session(self.database_url)
        try:
            row = session.query(PartInfo).filter_by(mpn=mpn.upper()).first()
            if not row:
                return None
            if not row.manufacturer and not row.description:
                return {}   # 'looked up, not found' — don't re-query
            return {"manufacturer": row.manufacturer, "description": row.description,
                    "category": row.category, "datasheet_url": row.datasheet_url, "source": row.source}
        finally:
            session.close()

    def _to_cache(self, mpn: str, info: dict | None):
        session = get_session(self.database_url)
        try:
            key = mpn.upper()
            row = session.query(PartInfo).filter_by(mpn=key).first() or PartInfo(mpn=key)
            row.manufacturer = (info or {}).get("manufacturer")
            row.description = (info or {}).get("description")
            row.category = (info or {}).get("category")
            row.datasheet_url = (info or {}).get("datasheet_url")
            row.source = (info or {}).get("source") or self.provider
            session.add(row)
            session.commit()
        except Exception as e:
            session.rollback()
            logger.warning(f"Could not cache part info for {mpn}: {e}")
        finally:
            session.close()
