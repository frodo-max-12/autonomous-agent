"""
Inbound transit lead time from a vendor's origin to Singapore — the leg we add to a vendor's quoted
lead so the CUSTOMER quote is EXW Singapore.

This is deliberately NOT a hardcoded constant. A Singapore trader knows Hong Kong is a few days by air
while Europe/US is weeks by sea — so the estimate is the LLM's judgement about the origin, cached per
country in `transit_to_sg.json` (visible + hand-editable, like any learned fact). Code only stores and
looks it up; the thinking is the model's. A configured flat value is used ONLY as a last resort when no
estimate exists yet and the model can't be reached.
"""
import json
import re
from pathlib import Path
from loguru import logger

_PATH = Path(__file__).parent / "transit_to_sg.json"


class TransitEstimator:
    """Singleton-ish cache of origin-country → transit DAYS to Singapore, filled by the LLM on first
    sight of an origin and persisted to disk."""

    def __init__(self):
        self._cache = {}
        try:
            self._cache = json.loads(_PATH.read_text(encoding="utf-8"))
        except Exception:
            self._cache = {}

    @staticmethod
    def _norm(c: str) -> str:
        return re.sub(r"[^a-z]", "", (c or "").lower())

    def days(self, country: str, claude=None):
        """Estimated inbound transit DAYS from `country` to Singapore. Singapore → 0. Returns None if we
        have no cached estimate and no model to ask (caller then falls back)."""
        if not country:
            return None
        key = self._norm(country)
        if not key:
            return None
        if "singapore" in key:
            return 0.0
        if key in self._cache:
            return self._cache[key]
        if claude is None:
            return None  # nothing cached yet and no model to ask — caller uses its fallback
        try:
            est = claude.estimate_transit_days(country)
        except Exception as e:
            logger.warning(f"transit estimate for '{country}' failed: {e}")
            return None
        if est is not None:
            self._cache[key] = est
            try:
                _PATH.write_text(json.dumps(self._cache, indent=2), encoding="utf-8")
            except Exception as e:
                logger.warning(f"could not persist transit cache: {e}")
        return est


# module-level instance so the JSON is read once per process
_ESTIMATOR = TransitEstimator()


def transit_days_to_sg(country: str, claude=None):
    return _ESTIMATOR.days(country, claude)
