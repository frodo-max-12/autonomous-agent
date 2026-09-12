"""
Dual-use / export-control watchlist — flags parts that may require an End-User Declaration (EUD).

Certain semiconductors (FPGAs, high-speed data converters, RF/microwave, rad-hard / space / MIL
grade, cryptographic devices) fall under dual-use export controls (India SCOMET / Wassenaar, US
EAR/ITAR). A trader must obtain an End-User Declaration before shipping these. This is a *screening*
aid — it raises a flag for a human to confirm, it is NOT a legal determination.

Detection is keyword-based over the MPN / manufacturer / description (extendable via settings).
"""

import re

# category -> list of lowercase keyword/regex fragments that suggest it
_RULES = {
    "FPGA / programmable logic": [
        r"\bfpga\b", r"\bcpld\b", r"field[- ]programmable", r"\bsom\b.*fpga",
        r"\bzynq\b", r"\bvirtex\b", r"\bkintex\b", r"\bartix\b", r"\bstratix\b",
        r"\barria\b", r"\bcyclone\b", r"\bagilex\b",
    ],
    "High-speed data converter": [
        r"high[- ]speed\s+(adc|dac)", r"\bgsps\b", r"\bgsa?/s\b",
        r"\d+\s*gsps", r"rf\s+(adc|dac)",
    ],
    "RF / microwave": [
        r"\brf\b\s*(power\s*)?(amp|amplifier|transceiver|frontend|front[- ]end)",
        r"microwave", r"\bgan\b", r"\bgaas\b", r"\bmmic\b", r"\bpll\b.*rf",
        r"transceiver", r"power\s+amplifier",
    ],
    "Rad-hard / space / military grade": [
        r"rad[- ]?hard", r"radiation[- ]tolerant", r"space[- ]grade", r"\bqml\b",
        r"mil[- ]?spec", r"mil[- ]?std", r"\bmilitary\b", r"aerospace",
    ],
    "Cryptographic / security": [
        r"crypto", r"cryptographic", r"secure\s+element", r"\bhsm\b",
        r"\btpm\b", r"encryption",
    ],
}


def _extra_keywords(settings):
    """Optional user-added keywords: EUD_WATCHLIST_KEYWORDS='keyword:category,...' or plain 'kw,kw'."""
    raw = getattr(settings, "eud_watchlist_keywords", "") if settings else ""
    out = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            kw, cat = chunk.split(":", 1)
            out[kw.strip().lower()] = cat.strip() or "User watchlist"
        else:
            out[chunk.lower()] = "User watchlist"
    return out


def check_eud(mpn: str = "", manufacturer: str = "", description: str = "",
              hsn: str = None, settings=None):
    """Return (flagged: bool, reason: str|None). Reason names the dual-use category matched."""
    blob = " ".join(str(x or "") for x in (mpn, manufacturer, description)).lower()
    if not blob.strip():
        return False, None
    for category, patterns in _RULES.items():
        for pat in patterns:
            if re.search(pat, blob):
                return True, f"Requires End-User Declaration — {category}"
    for kw, cat in _extra_keywords(settings).items():
        if kw and kw in blob:
            return True, f"Requires End-User Declaration — {cat}"
    return False, None
