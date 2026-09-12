"""
SemiSales AI Agent - BOM Extractor
Extracts part numbers from email text, Excel files, and PDF attachments.
"""

import io
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

import pandas as pd
import pdfplumber
from loguru import logger

from llm.claude_client import ClaudeClient


class _HTMLTableExtractor(HTMLParser):
    """Parses HTML and emits clean text + markdown-style tables.
    Helps small LLMs (gemma3:4b) read tabular RFQ data reliably."""

    # Tags whose CONTENT is code/metadata, not prose. Their text must never reach the LLM: a vendor
    # stock-offer blast carries a ~15KB <style> block ahead of the parts table, and emitting it as
    # "body text" both burned the token budget and pushed the actual rows past every downstream cap.
    #
    # ONLY tags that have a real closing tag belong here. Void elements (<meta>, <link>, <br>) never
    # emit an end tag, so counting them would leave the skip depth stuck above zero and silently
    # swallow the entire rest of the document.
    _SKIP_CONTENT_TAGS = {"style", "script", "title"}

    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self._in_table = False
        self._in_cell = False
        self._cell_buf: list[str] = []
        self._row_cells: list[str] = []
        self._is_header_row = False
        self._table_rows: list[list[str]] = []
        self._skip_depth = 0          # >0 while inside a code-bearing tag

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self._SKIP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if t == "table":
            self._in_table = True
            self._table_rows = []
        elif t == "tr" and self._in_table:
            self._row_cells = []
            self._is_header_row = False
        elif t in ("td", "th") and self._in_table:
            self._in_cell = True
            self._cell_buf = []
            if t == "th":
                self._is_header_row = True
        elif t in ("br", "p", "div", "li"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self._SKIP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if t == "table" and self._in_table:
            self._in_table = False
            if self._table_rows:
                self.out.append("\n")
                # Markdown-style table for clarity. Real-world HTML tables (esp. Gmail) are JAGGED —
                # rows have different cell counts (colspans, empty cells). Pad every row to the widest
                # so per-column width indexing can't go out of range (was: 'list index out of range',
                # which dumped raw bloated HTML to the LLM and truncated large BOMs to a few rows).
                ncols = max(len(r) for r in self._table_rows)
                padded = [list(r) + [""] * (ncols - len(r)) for r in self._table_rows]
                widths = [max(len(str(row[j])) for row in padded) for j in range(ncols)]
                for i, row in enumerate(padded):
                    line = " | ".join(str(c).ljust(widths[j]) for j, c in enumerate(row))
                    self.out.append(line + "\n")
                    if i == 0 and len(padded) > 1:
                        self.out.append(" | ".join("-" * w for w in widths) + "\n")
                self.out.append("\n")
        elif t == "tr" and self._in_table:
            if self._row_cells:
                self._table_rows.append(self._row_cells)
        elif t in ("td", "th") and self._in_table:
            self._in_cell = False
            text = " ".join("".join(self._cell_buf).split())
            self._row_cells.append(text)

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_cell:
            self._cell_buf.append(data)
        elif not self._in_table:
            self.out.append(data)

    def get_text(self) -> str:
        return "".join(self.out)


def html_to_clean_text(html: str) -> str:
    """Convert HTML email body to clean plain text with markdown-style tables."""
    if not html or "<" not in html:
        return html or ""
    try:
        parser = _HTMLTableExtractor()
        parser.feed(html)
        text = parser.get_text()
        # Collapse whitespace
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
        return text.strip()
    except Exception as e:
        logger.warning(f"HTML cleaning failed, falling back to raw: {e}")
        return html


# Common MPN patterns (alphanumeric with dashes, slashes, hashes)
MPN_PATTERN = re.compile(
    r'\b[A-Z]{1,5}[0-9][A-Z0-9\-/]{3,30}\b',
    re.IGNORECASE
)

# Known manufacturer prefixes for MPN inference
MPN_PREFIXES = {
    "STM": "STMicroelectronics", "ST": "STMicroelectronics",
    "TI": "Texas Instruments", "LM": "Texas Instruments", "TPS": "Texas Instruments",
    "AD": "Analog Devices", "ADP": "Analog Devices",
    "NXP": "NXP Semiconductors", "MK": "NXP Semiconductors",
    "IRF": "Infineon", "IRS": "Infineon",
    "ON": "ON Semiconductor", "NCV": "ON Semiconductor",
    "MUR": "Vishay", "VS-": "Vishay", "SI": "Vishay",
    "GD": "GigaDevice", "GD32": "GigaDevice",
    "FM": "Fudan Micro",
    "MBRS": "ON Semiconductor",
    "BAT": "Vishay",
    "BAS": "NXP Semiconductors",
}


class BOMExtractor:
    """Extracts BOM/MPN data from email text and attachments."""

    def __init__(self, claude_client: ClaudeClient):
        self.claude = claude_client

    def extract_from_email(self, subject: str, body: str, our_domains: list = None) -> list[dict]:
        """Use Claude to intelligently extract BOM from email text.
        If body looks like HTML, convert tables to markdown format first so the
        local LLM can read the row structure clearly.
        `our_domains` (our internal email domains) is passed to the model as context so it can tell,
        by its own reading, when a customer has replied ON TOP of an offer WE sent — and extract only
        what they are actually asking for, not the parts/prices we quoted in the trail below."""
        cleaned = html_to_clean_text(body) if body and "<" in body and ">" in body else body
        return self.claude.extract_bom(subject, cleaned, our_domains=our_domains)

    def _excel_to_text(self, file_bytes: bytes) -> str:
        """Convert EVERY sheet of a workbook to a plain-text table (all non-empty rows),
        so the LLM can locate the real header row and non-standard column names itself.
        Handles modern .xlsx (openpyxl) AND legacy .xls (xlrd)."""
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
            parts = []
            for name in wb.sheetnames:
                ws = wb[name]
                rows = []
                for row in ws.iter_rows(values_only=True):
                    cells = ["" if c is None else str(c).strip() for c in row]
                    if any(cells):
                        rows.append(" | ".join(cells))
                if rows:
                    parts.append(f"### Sheet: {name}\n" + "\n".join(rows))
            try:
                wb.close()
            except Exception:
                pass
            if parts:
                return "\n\n".join(parts)
        except Exception as e:
            logger.info(f"openpyxl couldn't read workbook ({e}); trying legacy .xls reader")
        return self._xls_to_text(file_bytes)

    def _xls_to_text(self, file_bytes: bytes) -> str:
        """Legacy .xls (BIFF) reader — openpyxl only supports .xlsx, so old .xls files need xlrd."""
        try:
            import xlrd
        except ImportError:
            logger.warning("xlrd not installed — cannot read legacy .xls files. `pip install xlrd`.")
            return ""
        book = xlrd.open_workbook(file_contents=file_bytes)
        parts = []
        for sh in book.sheets():
            rows = []
            for r in range(sh.nrows):
                cells = []
                for c in range(sh.ncols):
                    v = sh.cell_value(r, c)
                    cells.append("" if v is None else str(v).strip())
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"### Sheet: {sh.name}\n" + "\n".join(rows))
        return "\n\n".join(parts)

    def extract_from_excel(self, file_bytes: bytes, filename: str) -> list[dict]:
        """Extract BOM from an Excel attachment.
        Real-world BOMs vary wildly — title/metadata rows above the header, non-standard column
        names ('Stock Code', 'Value', 'Category', 'PCB Package'), multiple sheets. Rigid column
        matching is fragile (and mis-picks the title cell), so we convert the whole workbook to
        text and let the LLM extract it (same approach as PDF). Falls back to a pandas parse only
        if the LLM path yields nothing."""
        try:
            text = self._excel_to_text(file_bytes)
            if text.strip():
                items = self.claude.extract_bom(f"Excel BOM attachment: {filename}", text)
                if items:
                    logger.info(f"Excel {filename}: LLM extracted {len(items)} items")
                    return items
                logger.warning(f"Excel {filename}: LLM found no BOM, trying pandas fallback")
        except Exception as e:
            logger.warning(f"Excel {filename}: text conversion failed ({e}), trying pandas fallback")
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
            return self._parse_dataframe(df)
        except Exception as e:
            logger.error(f"Error parsing Excel {filename}: {e}")
            return []

    def extract_from_csv(self, file_bytes: bytes, filename: str) -> list[dict]:
        """Extract BOM from a CSV attachment via the LLM (robust to title rows / odd headers),
        with a pandas fallback."""
        try:
            text = file_bytes.decode("utf-8", errors="replace")
            if text.strip():
                items = self.claude.extract_bom(f"CSV BOM attachment: {filename}", text)
                if items:
                    logger.info(f"CSV {filename}: LLM extracted {len(items)} items")
                    return items
        except Exception as e:
            logger.warning(f"CSV {filename}: LLM path failed ({e}), trying pandas fallback")
        try:
            df = pd.read_csv(io.BytesIO(file_bytes))
            return self._parse_dataframe(df)
        except Exception as e:
            logger.error(f"Error parsing CSV {filename}: {e}")
            return []

    def extract_from_pdf(self, file_bytes: bytes, filename: str) -> list[dict]:
        """Extract BOM data from a PDF attachment."""
        try:
            text = ""
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"

                    # Also try extracting tables
                    tables = page.extract_tables()
                    for table in tables:
                        for row in table:
                            text += "\t".join(str(cell) if cell else "" for cell in row) + "\n"

            if text.strip():
                return self.claude.extract_bom(f"PDF: {filename}", text)
            return []

        except Exception as e:
            logger.error(f"Error parsing PDF {filename}: {e}")
            return []

    def extract_from_attachment(self, file_bytes: bytes, filename: str, mime_type: str) -> list[dict]:
        """Route attachment to the correct parser based on type."""
        filename_lower = filename.lower()

        if filename_lower.endswith((".xlsx", ".xls")):
            return self.extract_from_excel(file_bytes, filename)
        elif filename_lower.endswith(".csv"):
            return self.extract_from_csv(file_bytes, filename)
        elif filename_lower.endswith(".pdf"):
            return self.extract_from_pdf(file_bytes, filename)
        else:
            logger.info(f"Unsupported attachment type: {filename} ({mime_type})")
            return []

    def _parse_dataframe(self, df: pd.DataFrame) -> list[dict]:
        """Parse a pandas DataFrame to extract BOM items."""
        items = []
        # Normalize column names
        df.columns = [str(c).lower().strip() for c in df.columns]

        # Find relevant columns by common names
        mpn_cols = self._find_columns(df, [
            "mpn", "part number", "part no", "part#", "p/n", "pn",
            "mfr part", "mfr p/n", "mfr pn", "manufacturer part", "manufacturer part no",
            "manufacturer part number", "cpn", "item no", "item number",
            "component no", "ic part no", "device", "model", "model no",
            "cat no", "catalog no", "order code", "product code", "stock no",
            "material no", "material",
        ])
        mfr_cols = self._find_columns(df, [
            "manufacturer", "mfr", "mfg", "brand", "vendor", "make",
            "maker", "oem", "supplier", "origin", "source", "producer",
        ])
        qty_cols = self._find_columns(df, [
            "qty", "quantity", "pieces", "pcs", "nos",
            "order qty", "required qty", "req qty", "need qty", "demand", "required",
        ])
        price_cols = self._find_columns(df, [
            "price", "target price", "unit price", "target", "budget",
            "budget price", "expected price", "last price", "ref price",
        ])
        desc_cols = self._find_columns(df, [
            "description", "desc", "item description", "part description",
            "component description", "specification", "specs", "details", "remarks",
        ])
        pkg_cols = self._find_columns(df, [
            "package", "pkg", "case", "case size", "footprint",
            "case/package", "smd package", "package type", "housing",
        ])
        annual_cols = self._find_columns(df, [
            "annual", "annual qty", "annual quantity", "annual volume",
            "yearly", "yearly qty", "eau", "annual usage", "forecast",
        ])

        mpn_col = mpn_cols[0] if mpn_cols else None
        mfr_col = mfr_cols[0] if mfr_cols else None
        qty_col = qty_cols[0] if qty_cols else None
        price_col = price_cols[0] if price_cols else None
        desc_col = desc_cols[0] if desc_cols else None
        pkg_col = pkg_cols[0] if pkg_cols else None
        annual_col = annual_cols[0] if annual_cols else None

        if not mpn_col:
            # Try to find any column with MPN-like values
            for col in df.columns:
                sample = df[col].dropna().astype(str).head(5)
                if any(MPN_PATTERN.match(str(v)) for v in sample):
                    mpn_col = col
                    break

        # Allow parsing even without MPN column if we have description column
        if not mpn_col and not desc_col:
            logger.warning("Could not identify MPN or Description column in spreadsheet")
            return items

        for _, row in df.iterrows():
            mpn = str(row.get(mpn_col, "")).strip() if mpn_col else ""
            desc = str(row.get(desc_col, "")).strip() if desc_col else ""
            if mpn in ("", "nan") and desc in ("", "nan"):
                continue  # skip completely empty rows

            has_mpn = mpn and mpn != "nan"
            item = {
                "mpn": mpn if has_mpn else None,
                "manufacturer": str(row.get(mfr_col, "")).strip() if mfr_col else (self._infer_manufacturer(mpn) if has_mpn else None),
                "quantity": self._parse_quantity(row.get(qty_col)) if qty_col else None,
                "package": str(row.get(pkg_col, "")).strip() if pkg_col else None,
                "annual_quantity": self._parse_quantity(row.get(annual_col)) if annual_col else None,
                "needs_identification": not has_mpn,
                "target_price": self._parse_price(row.get(price_col)) if price_col else None,
                "currency": None,
                "required_date": None,
                "special_requirements": None,
                "description": desc if desc and desc != "nan" else None,
            }

            # Clean up "nan" strings
            for key in item:
                if item[key] == "nan" or item[key] == "":
                    item[key] = None

            items.append(item)

        logger.info(f"Extracted {len(items)} BOM items from spreadsheet")
        return items

    def _find_columns(self, df: pd.DataFrame, keywords: list[str]) -> list[str]:
        """Find DataFrame columns matching any of the keywords.
        Skips very long headers (>40 chars) — those are title/merged cells like
        'Bill Of Materials for ...', not real column headers (avoids matching 'material')."""
        matches = []
        for col in df.columns:
            col_lower = str(col).lower().strip()
            if len(col_lower) > 40:
                continue
            for keyword in keywords:
                if keyword in col_lower:
                    matches.append(col)
                    break
        return matches

    def _infer_manufacturer(self, mpn: str) -> Optional[str]:
        """Try to infer manufacturer from MPN prefix."""
        mpn_upper = mpn.upper()
        for prefix, manufacturer in sorted(MPN_PREFIXES.items(), key=lambda x: -len(x[0])):
            if mpn_upper.startswith(prefix):
                return manufacturer
        return None

    def _parse_quantity(self, value) -> Optional[int]:
        """Parse quantity from various formats."""
        if value is None or str(value).strip() in ("", "nan"):
            return None
        text = str(value).upper().replace(",", "").strip()
        # Handle K suffix (1K = 1000)
        match = re.match(r'(\d+(?:\.\d+)?)\s*K', text)
        if match:
            return int(float(match.group(1)) * 1000)
        try:
            return int(float(text))
        except ValueError:
            return None

    def _parse_price(self, value) -> Optional[float]:
        """Parse price from various formats."""
        if value is None or str(value).strip() in ("", "nan"):
            return None
        text = str(value).replace(",", "").replace("$", "").replace("Rs", "").replace("INR", "").replace("USD", "").strip()
        try:
            return round(float(text), 4)
        except ValueError:
            return None
