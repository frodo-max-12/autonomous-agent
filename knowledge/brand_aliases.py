"""
Brand normalization / aliases (E5).

Your vendor list tags brands in short form (STM, TEXAS, MICRON), but a customer BOM says
'STMicroelectronics' or 'Texas Instruments'. This maps every variant to one canonical token so
routing matches. Extend `_GROUPS` as you meet new spellings (safe, additive).
"""

import re

# canonical UPPER token -> variants (matched case-insensitively)
_GROUPS = {
    "STM": ["ST", "STM", "STMICRO", "STMICROELECTRONICS", "ST MICROELECTRONICS", "ST MICRO"],
    "TI": ["TI", "TEXAS", "TEXAS INSTRUMENTS", "TEXAS INSTRUMENT", "BURR BROWN", "NATIONAL SEMICONDUCTOR"],
    "ONSEMI": ["ON", "ONSEMI", "ON SEMI", "ON SEMICONDUCTOR", "ONSEMICONDUCTOR", "FAIRCHILD"],
    "NXP": ["NXP", "NXP SEMICONDUCTORS", "FREESCALE"],
    "INFINEON": ["INFINEON", "IFX", "CYPRESS", "INTERNATIONAL RECTIFIER", "IR"],
    "MICROCHIP": ["MICROCHIP", "ATMEL", "MCHP", "MICROSEMI"],
    "RENESAS": ["RENESAS", "INTERSIL", "IDT", "DIALOG"],
    "ADI": ["ADI", "ANALOG", "ANALOG DEVICES", "MAXIM", "MAXIM INTEGRATED", "LINEAR", "LINEAR TECHNOLOGY", "LTC"],
    "VISHAY": ["VISHAY", "DALE", "SILICONIX"],
    "MICRON": ["MICRON", "MT", "SPECTEK"],
    "SAMSUNG": ["SAMSUNG"],
    "SKHYNIX": ["HYNIX", "SK HYNIX", "SKHYNIX"],
    "TOSHIBA": ["TOSHIBA", "KIOXIA"],
    "MURATA": ["MURATA"],
    "TDK": ["TDK", "EPCOS"],
    "YAGEO": ["YAGEO", "PHYCOMP"],
    "KEMET": ["KEMET"],
    "AVX": ["AVX", "KYOCERA"],
    "BOURNS": ["BOURNS"],
    "PANASONIC": ["PANASONIC", "MATSUSHITA"],
    "NICHICON": ["NICHICON"],
    "ROHM": ["ROHM"],
    "DIODES": ["DIODES", "DIODES INC", "DIODES INCORPORATED", "ZETEX"],
    "WURTH": ["WURTH", "WURTH ELEKTRONIK", "WUERTH"],
    "TE": ["TE", "TE CONNECTIVITY", "TYCO", "AMP"],
    "MOLEX": ["MOLEX"],
    "AMPHENOL": ["AMPHENOL", "FCI"],
    "HIROSE": ["HIROSE", "HRS"],
    "JST": ["JST"],
    "LITTELFUSE": ["LITTELFUSE", "LITTLEFUSE"],
    "MARVELL": ["MARVELL"],
    "BROADCOM": ["BROADCOM", "AVAGO", "LSI"],
    "WESTERN-DIGITAL": ["WD", "WESTERN DIGITAL", "WESTERN-DIGITAL", "SANDISK"],
    "SEAGATE": ["SEAGATE"],
    "INTEL": ["INTEL", "ALTERA"],
    "AMD": ["AMD", "XILINX"],
    "NANJING": ["NANJING"],
    "MORNSUN": ["MORNSUN"],
    "SILERGY": ["SILERGY"],
    "SGMICRO": ["SGMICRO", "SG MICRO"],
    "GOFORD": ["GOFORD"],
    "SMC": ["SMC", "SMC DIODE", "SMC DIODE SOLUTIONS"],
    "COILMASTER": ["COILMASTER"],
    "STANDEX": ["STANDEX", "STANDEX MEDER", "MEDER"],
    "KLS": ["KLS"],
    "EVERLIGHT": ["EVERLIGHT"],
    "NETSOL": ["NETSOL"],
    "CLAFPOWER": ["CLAFPOWER", "CLAF", "CLAF POWER"],
    "ABRACON": ["ABRACON"],
    "BELFUSE": ["BELFUSE", "BEL FUSE"],
    "PULSE": ["PULSE", "PULSE ELECTRONICS"],
    "SUMIDA": ["SUMIDA"],
}

_ALIAS = {}
for _canon, _variants in _GROUPS.items():
    _ALIAS[_canon] = _canon
    for _v in _variants:
        _ALIAS[_v.upper().strip()] = _canon

_SUFFIX = re.compile(
    r"\b(SEMICONDUCTORS?|TECHNOLOG(?:Y|IES)|ELECTRONICS?|MICROELECTRONICS|"
    r"CORP(?:ORATION)?|INC|LTD|LLC|GMBH|CO|COMPANY|INTERNATIONAL|SOLUTIONS?)\b", re.I)


def canonical(brand: str) -> str:
    """Return a canonical UPPER token for a brand / manufacturer string."""
    if not brand:
        return ""
    b = re.sub(r"\s+", " ", str(brand).strip().upper())
    if b in _ALIAS:
        return _ALIAS[b]
    stripped = re.sub(r"\s+", " ", _SUFFIX.sub("", b)).strip(" .,-")
    if stripped in _ALIAS:
        return _ALIAS[stripped]
    tok = stripped.split(" ")[0] if stripped else b
    return _ALIAS.get(tok, stripped or b)


def brand_match(requested: str, vendor_brands) -> bool:
    """True if the requested manufacturer matches any of the vendor's brands after normalization."""
    if not requested:
        return False
    rc = canonical(requested)
    if not rc:
        return False
    return any(canonical(vb) == rc for vb in (vendor_brands or []))
