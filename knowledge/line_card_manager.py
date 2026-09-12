"""
SemiSales AI Agent - Line Card Manager
Loads and queries the authorized line card for brand/product lookups and alternative suggestions.
"""

import json
import re
from pathlib import Path
from typing import Optional


LINE_CARD_PATH = Path(__file__).parent / "line_card.json"


def _normalize(name: str) -> str:
    """Normalize a brand name for fuzzy matching.
    Lowercases, strips whitespace/punctuation, collapses to letters+digits only.
    Examples: 'CLAF Power' -> 'clafpower', 'Claf-Power' -> 'clafpower',
              'claf power inc.' -> 'clafpowerinc', 'ST Micro' -> 'stmicro'."""
    if not name:
        return ""
    s = name.lower().strip()
    # Strip common corporate suffixes BEFORE stripping punctuation
    s = re.sub(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|gmbh|ag|sa|plc|pvt|private)\.?\b", "", s)
    # Keep only letters and digits
    return re.sub(r"[^a-z0-9]+", "", s)


def _distinctive_first_word(name: str) -> Optional[str]:
    """Return the first word of a multi-word brand if it's long enough to be distinctive.
    'CLAF Power' -> 'claf', 'STMicroelectronics' -> None (single word),
    'ON Semiconductor' -> None (first word too short/generic)."""
    if not name or not isinstance(name, str):
        return None
    words = re.split(r"[\s\-_/]+", name.strip())
    if len(words) < 2:
        return None
    first = re.sub(r"[^a-z0-9]+", "", words[0].lower())
    # Require >=4 chars AND not a generic word
    generic = {"the", "new", "old", "pro", "sun", "eco", "off", "and", "for"}
    if len(first) >= 4 and first not in generic:
        return first
    return None


class LineCardManager:
    def __init__(self, line_card_path: str = None):
        path = Path(line_card_path) if line_card_path else LINE_CARD_PATH
        with open(path, "r", encoding="utf-8") as f:
            self._data = json.load(f)

        # Build lookup indexes
        self._brand_index: dict[str, dict] = {}  # brand_name_lower -> brand info
        self._alias_index: dict[str, dict] = {}  # normalized alias -> brand info (fuzzy match)
        self._product_index: dict[str, list[dict]] = {}  # product_keyword_lower -> list of brands
        self._all_brands: list[str] = []

        self._build_indexes()

    def _build_indexes(self):
        for category_key, category in self._data["categories"].items():
            for brand in category["brands"]:
                brand_name = brand["name"]
                brand_lower = brand_name.lower()
                brand_entry = {
                    "name": brand_name,
                    "category": category["display_name"],
                    "category_key": category_key,
                    "products": brand["products"],
                }
                self._brand_index[brand_lower] = brand_entry
                self._all_brands.append(brand_name)

                # Build fuzzy aliases: full-normalized form + distinctive first word +
                # parenthetical parts. Handles ALL variants like:
                #  'CLAF Power' == 'claf power' == 'clafpower' == 'Claf-Power' == 'claf'
                #  'UTC (unisonic)' == 'utc' == 'unisonic' == 'utcunisonic'
                normalized = _normalize(brand_name)
                if normalized:
                    self._alias_index.setdefault(normalized, brand_entry)
                first_word = _distinctive_first_word(brand_name)
                if first_word:
                    self._alias_index.setdefault(first_word, brand_entry)
                # Handle parenthetical names like "UTC (unisonic)" -> aliases: "utc", "unisonic"
                paren_matches = re.findall(r"\(([^)]+)\)", brand_name)
                outside_paren = re.sub(r"\([^)]*\)", "", brand_name).strip()
                for part in [outside_paren] + paren_matches:
                    part_norm = _normalize(part)
                    if part_norm and len(part_norm) >= 3:
                        self._alias_index.setdefault(part_norm, brand_entry)

                # Index by product keywords
                for product in brand["products"]:
                    for keyword in self._extract_keywords(product):
                        if keyword not in self._product_index:
                            self._product_index[keyword] = []
                        self._product_index[keyword].append(brand_entry)

    def _extract_keywords(self, product_name: str) -> list[str]:
        keywords = [product_name.lower()]
        # Split multi-word products into individual keywords
        words = product_name.lower().replace("/", " ").replace("-", " ").replace("&", " ").split()
        for word in words:
            word = word.strip()
            if len(word) > 2:  # skip tiny words
                keywords.append(word)
        return keywords

    def is_authorized_brand(self, manufacturer: str) -> bool:
        """Fuzzy match against line card. Handles casing, spaces, punctuation,
        corporate suffixes, and first-word aliases.
        'claf', 'CLAF POWER', 'clafpower', 'Claf-Power', 'claf power inc' all match 'CLAF Power'."""
        return self.get_brand_info(manufacturer) is not None

    def get_brand_info(self, manufacturer: str) -> Optional[dict]:
        """Fuzzy lookup — see is_authorized_brand for match rules."""
        if not manufacturer:
            return None
        raw = manufacturer.lower().strip()
        # 1. Exact lowercase match (fastest path)
        if raw in self._brand_index:
            return self._brand_index[raw]
        # 2. Fully normalized match (strips spaces, punctuation, corporate suffixes)
        normalized = _normalize(manufacturer)
        if normalized and normalized in self._alias_index:
            return self._alias_index[normalized]
        # 3. Distinctive-first-word match (customer wrote just "claf" for "CLAF Power")
        first = _distinctive_first_word(manufacturer) or normalized
        if first and first in self._alias_index:
            return self._alias_index[first]
        return None

    def get_all_brands(self) -> list[str]:
        return self._all_brands.copy()

    def find_alternatives_by_product(self, product_keywords: list[str]) -> list[dict]:
        """Find brands that carry products matching the given keywords."""
        matches: dict[str, dict] = {}
        scores: dict[str, int] = {}

        for keyword in product_keywords:
            keyword_lower = keyword.lower().strip()
            for stored_keyword, brands in self._product_index.items():
                if keyword_lower in stored_keyword or stored_keyword in keyword_lower:
                    for brand in brands:
                        key = brand["name"]
                        if key not in matches:
                            matches[key] = brand
                            scores[key] = 0
                        scores[key] += 1

        # Sort by relevance score
        sorted_brands = sorted(matches.keys(), key=lambda k: scores[k], reverse=True)
        return [matches[b] for b in sorted_brands]

    def find_alternatives_by_category(self, category_key: str, exclude_brand: str = None) -> list[dict]:
        """Find all brands in the same category."""
        category = self._data["categories"].get(category_key)
        if not category:
            return []

        results = []
        for brand in category["brands"]:
            if exclude_brand and brand["name"].lower() == exclude_brand.lower():
                continue
            results.append({
                "name": brand["name"],
                "category": category["display_name"],
                "category_key": category_key,
                "products": brand["products"],
            })
        return results

    def check_and_suggest(self, manufacturer: str, product_description: str = "") -> dict:
        """
        Main method: Check if manufacturer is authorized, and suggest alternatives if not.
        Returns a dict with:
          - is_authorized: bool
          - brand_info: dict or None
          - alternatives: list of alternative brands with relevance
          - product_keywords: extracted keywords used for matching
        """
        result = {
            "is_authorized": False,
            "brand_info": None,
            "alternatives": [],
            "product_keywords": [],
        }

        # Check if authorized
        if self.is_authorized_brand(manufacturer):
            result["is_authorized"] = True
            result["brand_info"] = self.get_brand_info(manufacturer)
            return result

        # Not authorized - find alternatives
        keywords = []
        if product_description:
            keywords = self._extract_keywords(product_description)
        result["product_keywords"] = keywords

        if keywords:
            result["alternatives"] = self.find_alternatives_by_product(keywords)

        return result

    def generate_line_card_summary(self) -> str:
        """Generate a text summary of the entire line card for LLM context."""
        lines = ["AUTHORIZED LINE CARD - Company B & Company A\n"]
        for category_key, category in self._data["categories"].items():
            lines.append(f"\n## {category['display_name']}")
            for brand in category["brands"]:
                products = ", ".join(brand["products"])
                lines.append(f"  - {brand['name']}: {products}")
        return "\n".join(lines)
