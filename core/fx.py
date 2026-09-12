"""
FX auto-fetch (E4) — daily USD->INR from a free currency API, with a safety buffer + stale fallback.

The landed-cost engine reads `get_effective_rate()`:
  effective = fetched_rate x (1 + fx_buffer_percent/100)   [buffer covers volatility]
If no rate is stored (or a fetch fails), it falls back to the manual `usd_inr_fx_rate` setting.

Uses only the Python stdlib (urllib) — no extra dependency, and NOT the Claude API.
"""

import json
import urllib.request
from datetime import datetime, timezone, timedelta
from loguru import logger
from core.database import FxRate

# free, key-less endpoints (primary first, fallback second)
_ENDPOINTS = [
    ("open.er-api.com", "https://open.er-api.com/v6/latest/USD",
     lambda d: (d.get("rates") or {}).get("INR")),
    ("frankfurter", "https://api.frankfurter.app/latest?from=USD&to=INR",
     lambda d: (d.get("rates") or {}).get("INR")),
]


def _now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # SQLite stores naive datetimes


def fetch_usd_inr(timeout: int = 10):
    """Fetch the raw USD->INR rate from a free API. Returns float, or None if all sources fail."""
    for name, url, extract in _ENDPOINTS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SemiSales/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            rate = extract(data)
            if rate and float(rate) > 0:
                logger.info(f"FX USD->INR = {rate} (from {name})")
                return float(rate)
        except Exception as e:
            logger.warning(f"FX fetch failed from {name}: {e}")
    logger.error("FX fetch failed from all sources")
    return None


def _latest(session):
    return (session.query(FxRate).filter_by(pair="USDINR")
            .order_by(FxRate.fetched_at.desc()).first())


def update_fx_rate(session, settings, fetcher=None):
    """Fetch a fresh rate, apply the buffer, and store it. Returns the FxRate row or None."""
    fetcher = fetcher or fetch_usd_inr
    raw = fetcher()
    if not raw:
        return None
    buf = float(getattr(settings, "fx_buffer_percent", 0) or 0)
    row = FxRate(pair="USDINR", base_rate=round(raw, 4), buffer_percent=buf,
                 effective_rate=round(raw * (1 + buf / 100.0), 4), source="api",
                 fetched_at=_now_naive())
    session.add(row)
    session.commit()
    logger.info(f"FX stored: base {row.base_rate} +{buf}% -> effective {row.effective_rate}")
    return row


def get_effective_rate(session, settings) -> float:
    """The USD->INR rate pricing should use: latest stored effective rate, else the manual fallback."""
    row = _latest(session)
    if row and row.effective_rate:
        return float(row.effective_rate)
    return float(getattr(settings, "usd_inr_fx_rate", 83.0))


def status(session, settings) -> dict:
    """Current FX status for the dashboard."""
    row = _latest(session)
    if not row:
        return {"effective": float(getattr(settings, "usd_inr_fx_rate", 83.0)),
                "base": None, "buffer": getattr(settings, "fx_buffer_percent", 0),
                "fetched_at": None, "source": "manual fallback", "stale": True}
    age_h = (_now_naive() - row.fetched_at).total_seconds() / 3600 if row.fetched_at else 1e9
    return {"effective": row.effective_rate, "base": row.base_rate, "buffer": row.buffer_percent,
            "fetched_at": row.fetched_at, "source": row.source,
            "stale": age_h > float(getattr(settings, "fx_max_age_hours", 48))}


def ensure_fresh(session, settings):
    """Fetch a new rate if auto-fetch is on and the stored one is missing / older than fx_refresh_hours.
    Cheap to call every monitor cycle — it only hits the network when actually due."""
    if not getattr(settings, "fx_auto_fetch", True):
        return
    row = _latest(session)
    due = row is None or row.fetched_at is None
    if not due:
        age_h = (_now_naive() - row.fetched_at).total_seconds() / 3600
        due = age_h > float(getattr(settings, "fx_refresh_hours", 20))
    if due:
        update_fx_rate(session, settings)
