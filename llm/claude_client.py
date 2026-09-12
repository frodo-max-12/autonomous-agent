"""
SemiSales AI Agent - Claude Code SDK LLM Client
Uses the Claude Code CLI (covered by your $200/month Max plan) for email
classification, BOM extraction, alternative suggestion, and quotation drafting.
No separate API key needed - runs through your existing Claude Code subscription.
"""

import json
import re
import subprocess
import shutil
from loguru import logger
from typing import Optional

# Agent knowledge distilled from the real intl.sales@ mailbox study (INTL_SALES_STUDY.md) — injected
# into the prompts so the agent reasons like Company B's real trading desk.
from knowledge.intl_sales_playbook import classification_context, extraction_context, reply_style_context


# Find claude CLI path
_CLAUDE_PATH = shutil.which("claude") or "claude"


class ClaudeClient:
    """Wrapper for Claude Code CLI calls.
    Class name kept as ClaudeClient to avoid changes across all other files.
    """

    def __init__(self, api_key: str = "", model: str = "claude-sonnet-5",
                 max_tokens: int = 4096, heavy_model: str = None, timeout: int = 600,
                 effort: str = "high"):
        self.model = model                              # light model: classification, drafting
        self.heavy_model = heavy_model or model         # heavy model: BOM/pricing/alt extraction (accuracy)
        self.max_tokens = max_tokens
        self.timeout = timeout                          # per-call CLI timeout (heavy BOM calls can be slow)
        # Reasoning effort passed to the CLI (--effort): low|medium|high|xhigh|max. Higher = more thinking
        # = more accurate but slower (raise `timeout` to match, or it will time out = the "stuck" symptom).
        self.effort = (effort or "").strip().lower() or None
        # Verify claude CLI is available
        try:
            result = subprocess.run(
                [_CLAUDE_PATH, "--version"],
                capture_output=True, text=True, timeout=10,
            )
            logger.info(f"Claude Code SDK connected: {result.stdout.strip()} "
                        f"(light={self.model}, heavy={self.heavy_model})")
        except Exception as e:
            logger.error(f"Claude Code CLI not found. Install it or check PATH. Error: {e}")

    def _call(self, system_prompt: str, user_message: str, max_tokens: int = None, model: str = None) -> str:
        """Call Claude via the Claude Code CLI subprocess.
        model: override the model for this call (defaults to the light self.model)."""
        call_model = model or self.model
        max_retries = 2
        combined_prompt = f"""INSTRUCTIONS (follow exactly):
{system_prompt}

---

INPUT:
{user_message}"""

        argv = [
            _CLAUDE_PATH,
            "-p", combined_prompt,
            "--model", call_model,
            "--output-format", "json",
            "--max-turns", "1",
        ]
        if self.effort:
            argv += ["--effort", self.effort]           # more thinking = more accurate (slower)

        for attempt in range(max_retries):
            try:
                result = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    encoding="utf-8",
                    errors="replace",
                )

                if result.returncode != 0:
                    logger.error(f"Claude CLI error (exit {result.returncode}): {result.stderr[:500]}")
                    if attempt < max_retries - 1:
                        continue
                    raise Exception(f"Claude CLI exited with code {result.returncode}")

                # Parse the outer JSON envelope from --output-format json
                outer = json.loads(result.stdout)

                # Guard against error/usage-limit envelopes (returncode can still be 0). Without this,
                # an error string like "Usage limit reached" would be returned as `result` and could end
                # up as the HTML body of a customer email from the draft_* methods.
                if outer.get("is_error") is True:
                    err = outer.get("result") or outer.get("subtype") or "unknown error"
                    logger.error(f"Claude CLI returned an error envelope: {str(err)[:300]}")
                    if attempt < max_retries - 1:
                        continue
                    raise Exception(f"Claude CLI error envelope: {str(err)[:200]}")

                text = outer.get("result", "")

                if not text:
                    logger.warning(f"Claude CLI returned empty result")
                    if attempt < max_retries - 1:
                        continue
                    return ""

                logger.debug(f"Claude response ({len(text)} chars, "
                             f"cost: ${outer.get('total_cost_usd', 0):.4f}, "
                             f"duration: {outer.get('duration_ms', 0)}ms)")
                return text

            except subprocess.TimeoutExpired:
                logger.warning(f"Claude CLI timeout ({self.timeout}s) on model {call_model}, retrying ({attempt + 1}/{max_retries})...")
            except json.JSONDecodeError:
                # CLI might return plain text without JSON envelope in some modes
                if result.stdout.strip():
                    return result.stdout.strip()
                logger.warning(f"Failed to parse Claude CLI output, retrying ({attempt + 1}/{max_retries})...")
            except Exception as e:
                logger.error(f"Claude CLI error: {e}")
                if attempt < max_retries - 1:
                    continue
                raise

        raise Exception("Claude CLI: max retries exceeded")

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Strip a leading ```html / ``` fence and trailing ``` from drafted email bodies.
        The model sometimes wraps HTML output in a markdown code fence, which would otherwise
        be sent to the customer literally."""
        if not text:
            return text or ""
        t = text.strip()
        if t.startswith("```"):
            # drop the opening fence line (``` or ```html)
            t = t.split("\n", 1)[1] if "\n" in t else ""
            t = t.rstrip()
            if t.endswith("```"):
                t = t[:-3]
        return t.strip()

    def _parse_json_response(self, text: str) -> Optional[dict | list]:
        """Parse JSON from Claude's response, tolerating prose and markdown fences around it.
        The model sometimes prefaces the JSON with a sentence (e.g. 'This is a bounce notification…')
        or wraps it in a ```json fence — extract the JSON regardless, for both objects and arrays."""
        text = (text or "").strip()
        # 1. Prefer a ```json … ``` (or ``` … ```) fence anywhere in the text.
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
        else:
            # 2. No fence — trim any prose before the first { or [ and after its matching close char,
            #    so a model that wraps the JSON in explanation still parses.
            opens = [i for i in (text.find("{"), text.find("[")) if i != -1]
            if opens:
                start = min(opens)
                close = "}" if text[start] == "{" else "]"
                end = text.rfind(close)
                if end > start:
                    text = text[start:end + 1]
        return json.loads(text)

    def classify_email(self, subject: str, body: str, from_email: str, learned_context: str = "") -> dict:
        """
        Classify an incoming email into one of the defined types.
        Returns: {type, urgency, confidence, reasoning}
        """
        system_prompt = """You are an email classifier for Company B International (Singapore), a semiconductor component distributor.

Classify the email into EXACTLY ONE of these types:
- rfq: Request for Quotation / Inquiry / Enquiry - customer asking for pricing/availability of parts. This includes emails with part numbers, descriptions of components, tables of items, or requests like "please quote", "send pricing", "need quotation". Even if only component DESCRIPTIONS are given (no part numbers), it is still an rfq if the customer is asking us to quote.
- po: Purchase Order - customer sending a PO to proceed with an order
- negotiation: Price negotiation - customer countering price or asking for discount
- vendor_offer: A SUPPLIER pushing an UNSOLICITED stock/price OFFER at us - a vendor's "offer" / "available stock" / "in stock" / daily "DRAM offer" / "hot memory" / "stock list" blast that CONTAINS prices/quantities they are selling. This is market intel FROM a supplier, NOT a customer asking us to quote. Key tell: the email OFFERS parts with prices (they are selling), rather than ASKING us for a price. Classify as vendor_offer even if it lists many parts.
- delivery_query: Delivery/shipment status inquiry
- technical: Technical question about parts, datasheets, specifications
- complaint: Complaint, quality issue, return request
- spam: Marketing emails, promotional content, newsletters, generic mass mail
- other: Internal company emails, system notifications, automated reports, generic FYI emails

SUBJECT LINE CLUES (use together with body content to classify):
These subjects STRONGLY suggest rfq: "RFQ", "Request for Quotation", "Inquiry", "Enquiry", "Inq", "INQ", "Requirement", "Quote Request", "Price Request", "Pricing Request", "Please Quote", "Need Quotation", "RFQ for semiconductor", "Component requirement"

IMPORTANT RULES:
- CRITICAL: Look at BOTH the subject line AND body content. If the subject says "Inquiry" or "RFQ" and the body has a table of items (even descriptions only, no part numbers) = rfq.
- CRITICAL: Customers can send emails from ANY email address. The sender address does NOT indicate classification.
- CRITICAL: If the email body references our quote number (AE-Q- or CA-Q-), says "thank you for the quotation", mentions target price, or replies to our pricing - this is a CUSTOMER REPLY. NEVER classify as spam. Classify as: negotiation (if they mention target price, counter price, discount), po (if they confirm order), or rfq (if they add new parts).
- If the body contains part numbers, component descriptions, or a table of items AND is asking for pricing/availability = rfq
- DESCRIPTION-ONLY ITEMS: If the customer lists component descriptions like "DIODE GEN PURP 1KV" or "CMC 10MH 200MA" without specific part numbers, this is STILL an rfq — the customer wants us to identify and quote the parts.
- Customer replies with target prices or counter-offers on our quotation = negotiation (NOT spam)
- Emails with NO specific part numbers AND no component descriptions AND just asking about our services generally = other
- Only classify as spam if the body is clearly: marketing/promotional content, newsletters, web design/training offers, "Offer List" bulk stock lists from distributors, or generic mass mail with no specific inquiry
- Daily reports, BI tools, dashboards = other

Also determine urgency:
- critical: "production stopped", "urgent", "ASAP", "line down"
- high: "urgent", "rush", short deadlines
- normal: Standard business inquiry

Respond in this EXACT JSON format only, no other text:
{"type": "<type>", "urgency": "<urgency>", "confidence": <0.0-1.0>, "reasoning": "<brief reason>"}"""

        user_message = f"""From: {from_email}
Subject: {subject}

Body:
{body[:3000]}"""

        if learned_context:
            user_message = learned_context + "\n\n" + user_message

        # Inject the real business context so the classifier can tell a customer RFQ from a vendor
        # stock-offer blast (a big share of this desk's inbox).
        system_prompt = system_prompt + "\n\nBUSINESS CONTEXT (learned from our real mailbox):\n" + classification_context()
        result = self._call(system_prompt, user_message, max_tokens=200)

        try:
            return self._parse_json_response(result)
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"Failed to parse classification response: {result[:200]}")
            return {"type": "other", "urgency": "normal", "confidence": 0.0, "reasoning": "Parse error"}

    def classify_po_response(self, subject: str, body: str, po_number: str = "") -> dict:
        """Classify a VENDOR's reply to a purchase order WE (the buyer) sent them.
        Returns {"status": confirmed|dispatched|delivered|declined|unclear, "reason": "<short>"}."""
        system_prompt = f"""You are reading a VENDOR's reply to a PURCHASE ORDER that WE (the buyer) sent them.
This is a SUPPLIER responding to OUR order {po_number} — it is NOT a customer placing an order with us.

Decide how the vendor is responding and return exactly ONE status:
- "confirmed": accepts / confirms / acknowledges the order and will supply it.
- "dispatched": has shipped / dispatched / handed over the goods (mentions courier, tracking, AWB, invoice for dispatch).
- "delivered": says the goods have been delivered / received.
- "declined": CANNOT supply — out of stock / no stock, discontinued / EOL, MOQ not met, price changed and won't honour, or they refuse / cancel the order.
- "unclear": a question, a request for info, partial, or anything you cannot confidently map above.

Respond ONLY as JSON:
{{"status": "confirmed|dispatched|delivered|declined|unclear", "reason": "<short paraphrase of the vendor's words>"}}"""
        user_message = f"Subject: {subject}\n\nVendor's reply:\n{body[:3000]}"
        result = self._call(system_prompt, user_message, max_tokens=300, model=self.heavy_model)
        try:
            data = self._parse_json_response(result) or {}
            status = (data.get("status") or "unclear").strip().lower()
            if status not in ("confirmed", "dispatched", "delivered", "declined", "unclear"):
                status = "unclear"
            return {"status": status, "reason": (data.get("reason") or "").strip()}
        except (json.JSONDecodeError, ValueError, AttributeError):
            logger.warning(f"Failed to parse PO-response classification: {str(result)[:200]}")
            return {"status": "unclear", "reason": ""}

    def extract_vendor_offer(self, subject: str, body: str) -> dict:
        """Extract the offered lines from a vendor's unsolicited STOCK OFFER / price-list blast
        (a supplier SELLING parts, with prices). Returns {"items": [{mpn, manufacturer, unit_price,
        currency, date_code, lead_time, moq, quantity, packaging}]}."""
        system_prompt = f"""You are reading a SUPPLIER's unsolicited STOCK OFFER email — a vendor pushing
parts they have TO SELL, WITH prices. Extract every offered line (there may be many).

{extraction_context()}

For each offered part extract:
- mpn: the manufacturer part number (exact)
- manufacturer: brand / maker if stated
- unit_price: offered price PER PIECE (number only)
- currency: USD or EUR (default USD)
- date_code: date code if stated (e.g. "25+")
- lead_time: if stated
- moq: minimum order qty if stated (integer)
- quantity: quantity available / offered (integer)
- packaging: Tray / Reel / Tube if stated

Respond ONLY as JSON:
{{"items": [{{"mpn": "MT41K128M16JT-125AAT:K", "manufacturer": "Micron", "unit_price": 5.20, "currency": "USD", "date_code": "25+", "lead_time": "2-3 days", "moq": 2000, "quantity": 6000, "packaging": "Reel"}}]}}
If this is NOT actually an offer that lists parts with prices, respond {{"items": []}}."""
        user_message = f"Subject: {subject}\n\nVendor offer email:\n{body[:8000]}"
        result = self._call(system_prompt, user_message, max_tokens=4000, model=self.heavy_model)
        try:
            data = self._parse_json_response(result) or {}
            return {"items": data.get("items") or []}
        except (json.JSONDecodeError, ValueError, AttributeError):
            return {"items": []}

    def extract_bom(self, subject: str, body: str, our_domains: list = None) -> list[dict]:
        """
        Extract BOM (Bill of Materials) data from email text.
        Returns list of: {mpn, manufacturer, quantity, target_price, currency, required_date, special_requirements}
        `our_domains` is passed to the model purely as CONTEXT (a fact), so it can judge for itself
        when a customer has replied on top of an offer WE sent and extract only their real request.
        """
        system_prompt = """You are a BOM (Bill of Materials) extraction engine for a semiconductor distributor.
Extract ALL part numbers and related information from the email. Tables may be in HTML, plain text, or markdown.

FIELD SYNONYMS - customer column headers vary globally. ALL of these mean the same field:
- MPN: "Part No", "Part No.", "Part Number", "Part#", "P/N", "PN", "MPN", "Mfr Part", "Mfr P/N", "Mfr PN", "Manufacturer Part", "Manufacturer Part No", "Manufacturer Part Number", "Mfr Part Number", "CPN" (Customer Part Number), "Item No", "Item Number", "Item", "Component No", "Component", "IC Part No", "Device", "Model", "Model No", "Cat No", "Catalog No", "Order Code", "Product Code", "Stock No", "Stock Code", "Stock", "Item Code", "Material No", "Material"
- MANUFACTURER: "Make", "Mfr", "Mfr.", "MFR", "MFG", "Manufacturer", "Brand", "Vendor", "Maker", "OEM", "Supplier", "Company", "Origin", "Source", "Producer"
- QUANTITY: "Qty", "QTY", "Qty.", "Quantity", "Pieces", "Pcs", "Nos", "Nos.", "Numbers", "Order Qty", "Required Qty", "Req Qty", "Need Qty", "Demand", "MOQ", "Required"
- PACKAGE: "Package", "Pkg", "Case", "Case Size", "Footprint", "PCB Package", "PCB Footprint", "Case/Package", "SMD Package", "Package Type", "Encapsulation", "Housing", "Body Size"
- ANNUAL: "Annual", "Annual Qty", "Annual Quantity", "Annual Volume", "Yearly", "Yearly Qty", "EAU", "Annual Usage", "Forecast", "Annual Forecast", "Year Vol"
- DESCRIPTION: "Description", "Desc", "Desc.", "Item Description", "Part Description", "Component Description", "Specification", "Specs", "Details", "Remarks", "Parameters"
- TARGET PRICE: "Price", "Target", "Target Price", "Budget", "Budget Price", "Unit Price", "Expected Price", "Last Price", "Ref Price"
- CURRENCY: "Currency", "Curr", "CCY"
- DELIVERY: "Delivery", "Required Date", "Need Date", "Lead Time", "ETA", "Delivery Date", "Schedule"

CRITICAL RULES:
1. EXTRACT EVERY ROW. If a table has 2 rows, return 2 items. If 10 rows, return 10 items. NEVER skip a row.
2. NEVER HALLUCINATE A MANUFACTURER. Use the EXACT text the customer wrote in the Make/Mfr/Brand/Manufacturer column. If the customer writes "Mornsun", you MUST output "Mornsun" - NOT "Würth", NOT "Vishay", NOT any other brand. If the customer writes "SMC Diode Solutions", output "SMC Diode Solutions" exactly.
3. NEVER CHANGE QUANTITY. If the customer writes "10000", output 10000 (NOT 1000). If "12000", output 12000. Count zeros carefully. "10K"=10000, "1K"=1000.
4. Only infer manufacturer from the MPN prefix if the Make/Mfr column is completely empty AND no manufacturer is mentioned anywhere else.
5. DESCRIPTION-ONLY ITEMS: If a row has a Description but NO MPN/Part Number, set mpn to null and needs_identification to true. NEVER skip a row just because it has no part number. The purchase team will identify the correct part.
6. ANNUAL vs QUANTITY: "Quantity" is the immediate order quantity. "Annual" is the yearly volume (used for volume discounts). They are DIFFERENT fields — extract both separately.
7. SPREADSHEET / BOM STRUCTURE: A spreadsheet BOM often has TITLE and METADATA rows ABOVE the real header row — e.g. "Bill Of Materials for ...", a company name (e.g. "ARYA SYSTEMS"), "Design Title", "Author", "Document Number", "Revision", "Design Created", "Design Last Modified", "Total Parts In Design". IGNORE all of those. Find the real column-header row (the row containing headers like Category/Qty/Value/Stock Code/Part No/Description/Package) and extract ONLY the actual component rows below it. Also IGNORE any side "Totals" / summary table (e.g. a small "Category vs Quantity" count table). NEVER output a metadata label or a category name (like "Capacitors", "Resistors", "ARYA SYSTEMS") as an MPN.
8. NON-STANDARD PART-NUMBER COLUMNS: The part number may be under a header such as "Stock Code", "Stock No", or (when there is no dedicated part-number column) the "Value" column. When a row has a "Category" (e.g. Capacitors, Resistors, ICs, Diodes) plus a "Value" (e.g. 100n, 470uF, 10k) plus a "Stock Code", use the Stock Code as the mpn and build the description from Category + Value (+ Package). If a row has only a Category + Value and no real part number, set mpn to null, needs_identification=true, and put "Category Value" (e.g. "Capacitor 100n 1206") in description.
9. PART NUMBER CAN APPEAR ANYWHERE — recognise it. An MPN is an alphanumeric code: letters+digits, often with dashes/slashes (e.g. "1N4007", "BC337-25BK", "CC1206KRX7R9BB104", "TAJC106K016RNJ", "XY129", "IRF1407", "MAL210213472E3", "0603B105K160CT"). If there is no dedicated MPN column, look in the "Stock Code" / "Value" columns AND inside the Description/Remarks text and extract that code as the mpn. Distinguish it from a plain component VALUE — "100n", "10k", "470uF", "22pF", "10R", "10OHM" are values (→ description), NOT part numbers. Only set mpn=null (needs_identification=true) when the row genuinely has NO code-like part number anywhere (e.g. "LCD Display 16x2", "BURG STRIPE 1x20 male", "IC Socket 8 pin").
10. REPLIES & FORWARDS — READ THE WHOLE THREAD, BUT TELL WHOSE PARTS ARE WHOSE. An email may quote or forward earlier messages below the newest one. Decide, by reading, which of two cases this is:
    (a) The real request sits in a FORWARDED or QUOTED message from the CUSTOMER or a third party (a colleague forwards a customer's RFQ; the customer quotes their own BOM). Then the parts further down ARE what to extract — don't stop just because the top note is short.
    (b) The customer is REPLYING on top of an OFFER WE ourselves sent them (a stock / availability list we emailed, now quoted underneath their reply — you can recognise it because the quoted message's "From:" is one of OUR OWN email domains, given in the message below). Those quoted parts and prices are OURS, not their request. Extract ONLY the part(s) the customer is now actually asking us to quote or buy in their own words (e.g. "please quote EXOS-X16, 1000 pcs"); IGNORE the parts in our quoted offer, and NEVER treat a price from our own quoted offer as the customer's target price.
    Use your OWN judgement about which case applies — from who sent the quoted text and what the sender is asking for. Do not mechanically pull every part number in the body.

WORKED EXAMPLE (6-column RFQ table):
Input table:
Description                        | Manufacturer Part Number | Manufacturer              | Quantity | Package   | Annual
DIODE GEN PURP 1KV 1A SOD123FL    | 1N4007FL                 | SMC Diode Solutions        | 2200     | SOD-123F  | 2000000
DIODE ARRAY SCHOTTKY 30V SOT23    | BAT54C                   | SMC Diode Solutions        | 3000     | SOT-23-3  | 3000000
CMC 10MH 200MA 2LN TH             |                          | Sumida America Components  | 100      | TH        | 10000

Correct output:
[
  {"mpn": "1N4007FL", "manufacturer": "SMC Diode Solutions", "quantity": 2200, "package": "SOD-123F", "annual_quantity": 2000000, "needs_identification": false, "target_price": null, "currency": null, "required_date": null, "special_requirements": null, "description": "DIODE GEN PURP 1KV 1A SOD123FL"},
  {"mpn": "BAT54C", "manufacturer": "SMC Diode Solutions", "quantity": 3000, "package": "SOT-23-3", "annual_quantity": 3000000, "needs_identification": false, "target_price": null, "currency": null, "required_date": null, "special_requirements": null, "description": "DIODE ARRAY SCHOTTKY 30V SOT23"},
  {"mpn": null, "manufacturer": "Sumida America Components", "quantity": 100, "package": "TH", "annual_quantity": 10000, "needs_identification": true, "target_price": null, "currency": null, "required_date": null, "special_requirements": null, "description": "CMC 10MH 200MA 2LN TH"}
]

Note: ALL 3 rows extracted. Row 3 has no MPN so mpn=null and needs_identification=true. Annual quantities preserved exactly.

For each part, extract these fields:
- mpn: exact alphanumeric code as written (null if not provided)
- manufacturer: EXACT text from Make/Mfr/Brand/Manufacturer column
- quantity: immediate order quantity (integer, preserve all digits exactly)
- package: package type (SOD-123F, SOT-23-3, QFN-48, TH, etc.)
- annual_quantity: annual/yearly volume if provided (integer)
- needs_identification: true if no MPN given (description-only item), false otherwise
- target_price: per-unit price if mentioned (number only)
- currency: USD, INR, EUR, etc.
- required_date: delivery date if mentioned
- special_requirements: RoHS, AEC-Q100, automotive grade, etc.
- description: part description (EXACT text from Description column if available, otherwise brief part-type description)

Respond with a JSON array ONLY, no other text, no markdown fences:
[{"mpn": "...", "manufacturer": "...", "quantity": ..., "package": "...", "annual_quantity": ..., "needs_identification": false, "target_price": null, "currency": null, "required_date": null, "special_requirements": null, "description": "..."}]

If no parts found, return: []"""

        # Cap generously so large BOMs (Excel / PDF / long tables) are NOT truncated before the model
        # sees them. Sonnet 5 has a 1M context window; 100k chars ≈ ~1,200 table rows of input.
        body_for_model = body[:100000]
        if len(body) > 100000:
            logger.warning(f"BOM content is {len(body)} chars — truncated to 100000 for extraction (very large BOM).")
        our_note = ""
        if our_domains:
            doms = ", ".join(d for d in our_domains if d)
            if doms:
                our_note = (f"\nOUR OWN email domains: {doms}. (Per rule 10: if this is the customer replying "
                            f"on top of an offer WE sent — a quoted message whose From: is one of these — extract "
                            f"only the part(s) they now ask for and ignore the parts/prices in our quoted offer.)\n")
        user_message = f"""Subject: {subject}
{our_note}
Content (email body or attachment text) — extract EVERY row, do not stop early:
{body_for_model}"""

        result = self._call(system_prompt, user_message, max_tokens=16000, model=self.heavy_model)

        try:
            return self._parse_json_response(result)
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"Failed to parse BOM extraction: {result[:200]}")
            return []

    def cross_reference_batch(self, items: list[dict], catalogue_text: str,
                              line_card_summary: str = "") -> dict:
        """Cross-reference requested parts against our REAL catalogue, in one call.

        Replaces the old suggest_alternatives_batch, which handed the model nothing but brand names
        and product categories ("CLAF Power: DC-DC Converters") and then asked it for "the
        equivalent part number from that brand". With no catalogue to read, it improvised: a
        customer's Mornsun 'IB2405LS-1WR3' came back as 'B2405LS-1WR3 (CLAF Power)' -- a part that
        does not exist -- and went out in a customer email.

        The difference now is the input, not the instructions: `catalogue_text` carries real series
        and real specs read off the manufacturers' own sites. The engineering judgement is entirely
        the model's -- how to weigh isolation against package, when a near-miss is still worth
        offering, when to decline. We deliberately do NOT tell it what makes a good cross, because
        that reasoning differs for a DC-DC converter, a MOSFET and a connector, and hardcoding one
        family's rules onto 54 brands would be worse than letting it think.

        The model may suggest from ANY of our authorised lines (all 54 are given in the line-card
        summary), not just the brands with catalogue data. It works at two levels: a catalogue-backed
        brand gets an exact `series`; every other authorised brand is suggested at BRAND level with
        `series=""` and no part number. The caller re-checks: the brand must be a real authorised
        brand, a `series` is shown only if it exists in verified catalogue data, and a specific MPN
        only if a human confirmed it.

        Returns {index_str: {decoded, brand, series, fit, reasoning, caveats, confidence}}.
        """
        if not items:
            return {}
        parts_text = ""
        for i, it in enumerate(items):
            parts_text += (f"{i}. MPN={it.get('mpn') or '(none)'} | "
                           f"Manufacturer={it.get('manufacturer') or '(none)'} | "
                           f"Description={it.get('description') or ''}\n")

        if catalogue_text.strip():
            catalogue_block = f"""OUR VERIFIED CATALOGUE (read off each manufacturer's own website):
{catalogue_text[:12000]}"""
        else:
            catalogue_block = ("OUR VERIFIED CATALOGUE: (empty -- we have not yet harvested "
                               "catalogue data for any relevant brand)")

        system_prompt = f"""You are a component cross-reference engineer for Company B International and Company A.

The customer has asked for parts from makers we are not authorised to supply. Decide, for each one,
whether anything in OUR catalogue below is a genuine engineering substitute.

{catalogue_block}

These are ALL the brands we are authorised to supply, with the product types each one covers. Use
this to pick which of OUR lines makes this kind of component -- a rectifier diode maps to one of our
diode makers, a DC-DC converter to our converter maker, a connector to a connector maker, and so on:
{line_card_summary[:4000]}

HOW TO WORK
1. Decode the requested part -- what does it actually specify electrically (voltage, current, power,
   package, tolerance, isolation, speed, etc.)? Show your working in `decoded`.
2. Choose the best-fit brand from OUR authorised list above, using your own judgement about which of
   our lines makes that kind of part. Consider every relevant line, not just one.
3. Judge how good the fit is and explain it honestly in `reasoning`, mismatches included.

TWO KINDS OF SUGGESTION -- this distinction is the whole point:
- If the brand you pick HAS catalogue data above: name the exact matching `series`, copied VERBATIM
  from the catalogue, and compare parameter by parameter.
- If the brand you pick has NO catalogue data (most do not): suggest it at BRAND level -- set
  `series` to "" (empty string). Justify the fit by the product type and the parameters the customer
  needs. We would far rather tell the customer "we carry equivalent rectifiers from <brand>, exact
  part to follow on confirmation" than guess a part number.

ABSOLUTE RULE (this once went wrong and reached a customer): never construct, extend, adapt, complete
or invent a part number in ANY field, including `reasoning`. A `series` is allowed ONLY when copied
verbatim from the catalogue above. For a brand with no catalogue, describe the fit by SPECIFICATION,
never by a specific part number you were not given.

Fields per part:
- decoded: what the requested part number encodes, e.g. "I=wide 2:1 in (18-36V), 24->5V out, 1W, R3=3kVDC, SIP"
- brand: the best-fit authorised brand, exact name as written in the list above
- series: the exact catalogue series IF that brand has catalogue data, otherwise "" (empty)
- fit: "direct" (substitutable) | "partial" (works with a caveat) | "none" (we make nothing comparable)
- reasoning: why this line fits, parameter by parameter; state mismatches; NO invented part numbers
- caveats: what the customer's engineer must confirm
- confidence: "high" | "medium" | "low" -- be honest; lower it when you have no catalogue specs to check against

Respond ONLY with a JSON object keyed by part index as a string, no markdown, no other text:
{{"0": {{"decoded": "...", "brand": "...", "series": "...", "fit": "direct",
 "reasoning": "...", "caveats": "...", "confidence": "high"}}}}
Omit an index ONLY if we genuinely make nothing comparable on any of our lines."""

        user_message = f"Cross-reference these requested parts:\n{parts_text}"
        result = self._call(system_prompt, user_message, max_tokens=8000, model=self.heavy_model)
        try:
            parsed = self._parse_json_response(result)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"Failed to parse cross-reference batch: {result[:200]}")
            return {}

    def estimate_transit_days(self, country: str):
        """Estimate the TYPICAL inbound freight transit (in DAYS) from `country` to Singapore, the way a
        Singapore trader would judge it: air freight for nearby regions (Hong Kong/China/India ≈ a few
        days to ~2 weeks), sea / consolidated for far ones (Europe / US ≈ several weeks). Singapore = 0.
        Returns a number of days (or None if unparseable)."""
        system = ("You are a Singapore-based electronics trading desk sourcing on cost. For the given "
                  "ORIGIN, give the realistic DOOR-TO-DOOR inbound transit to Singapore in DAYS using our "
                  "usual COST-EFFECTIVE freight (sea / consolidated LCL is the default for cost-sensitive "
                  "orders, air only when nothing else works), and INCLUDE booking, port handling, sailing "
                  "and customs clearance. As a calibration anchor, a China origin is about 14 days "
                  "door-to-door this way; nearer origins (Hong Kong) are a bit less, far ones (Europe / "
                  "US) more. Singapore itself is 0. Answer with ONLY an integer number of days — no words, "
                  "no range, no units.")
        out = self._call(system, f"Origin country: {country}\nInbound transit to Singapore (days):",
                         max_tokens=200)
        m = re.search(r"\d+(?:\.\d+)?", out or "")
        return float(m.group(0)) if m else None

    def match_market_offers(self, items: list[dict], offers: list[dict]) -> dict:
        """Given a customer's inquiry and the stock vendors have recently OFFERED us, work out who
        to approach first — the way a trader who reads the daily offer sheets would.

        Deliberately not a lookup. The previous version filtered `MarketOffer.mpn == mpn`, which
        only ever fires on a byte-identical part number, so it missed everything a human catches:
        the same die in a different grade suffix ('MT41K128M16JT-125 IT:K' vs '...-125AAT:K'), an
        equivalent from another maker (Nanya NT5AD512M16C4-JR against a Samsung K4A8G165WC-BCWE
        requirement), and — most valuable — knowing that the vendor who blasts DRAM every morning
        is worth asking even when today's sheet doesn't list this exact line.

        No rules are given about what counts as a match; that judgement is the model's. Returns
        {index_str: {offers: [{vendor, mpn, relation, why}], approach_first: [vendor,...], note}}
        with relation one of "same_part" | "equivalent" | "same_vendor_category". Omits an index
        when nothing in the offer history is worth acting on.
        """
        if not items or not offers:
            return {}
        parts_text = ""
        for i, it in enumerate(items):
            parts_text += (f"{i}. MPN={it.get('mpn') or '(none)'} | "
                           f"Manufacturer={it.get('manufacturer') or '(none)'} | "
                           f"Qty={it.get('quantity') or '?'} | "
                           f"Description={it.get('description') or ''}\n")

        offers_text = ""
        for o in offers:
            age = o.get("age_days")
            offers_text += (f"- {o.get('vendor')}: {o.get('mpn')} "
                            f"[{o.get('manufacturer') or '?'}] "
                            f"qty {o.get('qty') or '?'} @ {o.get('currency') or 'USD'} {o.get('price')} "
                            f"D/C {o.get('date_code') or '?'} lead {o.get('lead_time') or '?'} "
                            f"({age} days ago)\n" if age is not None else
                            f"- {o.get('vendor')}: {o.get('mpn')} @ {o.get('price')}\n")

        system_prompt = f"""You are a spot-market trader at Company B International.

Every day your vendor panel emails you stock offer sheets. A customer enquiry has just come in.
Before you blast an RFQ to the whole panel, you check what you have ALREADY been offered — because
a vendor who offered the part last week very likely still has it, at a price you already know.

STOCK RECENTLY OFFERED TO US:
{offers_text[:9000]}

Work through each enquiry line and decide whether anything above is worth acting on. Use your own
judgement about what counts as a usable match — you know how part numbering, grade suffixes, speed
bins, packaging codes and second-source equivalents work in this market, and you know which vendors
specialise in what. A vendor who sends a DRAM sheet every morning is a good first call for a DRAM
part even if today's sheet doesn't show it.

Be honest about staleness: spot prices move, and an offer more than a few weeks old is a lead to
re-confirm, not a price to quote. Say so in `why` when it applies.

Per enquiry line:
- offers: the useful ones, each {{vendor, mpn, relation, why}}
    relation = "same_part" (this is the part) | "equivalent" (different MPN, does the job)
             | "same_vendor_category" (not this part, but this vendor trades in it — ask them)
- approach_first: vendor names to RFQ ahead of the wider panel, best first
- note: one line for the salesperson

Respond ONLY with a JSON object keyed by the enquiry index as a string, no markdown, no other text:
{{"0": {{"offers": [{{"vendor": "...", "mpn": "...", "relation": "same_part", "why": "..."}}],
 "approach_first": ["..."], "note": "..."}}}}
Omit any index where the offer history holds nothing genuinely useful. Do not stretch for a match."""

        user_message = f"Customer enquiry lines:\n{parts_text}"
        result = self._call(system_prompt, user_message, max_tokens=6000, model=self.heavy_model)
        try:
            parsed = self._parse_json_response(result)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"Failed to parse market-offer match: {result[:200]}")
            return {}

    def suggest_hsn(self, mpn, description, manufacturer) -> dict:
        """Suggest an Indian HSN heading (4-digit) + a rough BCD% for a component (LIGHT model).
        Returns {hsn, description, bcd_percent}. The bcd_percent is a SUGGESTION to be human-verified."""
        system_prompt = """You classify an electronic component to its Indian customs HSN code (a 4-digit heading is fine) and estimate the Basic Customs Duty (BCD) %.
Common headings: 8542 ICs/MCU/memory/processors; 8541 diodes/transistors/LEDs/crystals; 8532 capacitors; 8533 resistors; 8504 converters/inductors/transformers; 8536 connectors/switches/relays/fuses; 8534 PCB; 8544 wire/cable; 8471 HDD/SSD; 8517 comms modules.
The bcd_percent is your best-effort estimate ONLY (it will be human-verified before use). If unsure, use 0.
Respond ONLY as JSON, no other text: {"hsn": "8542", "description": "Electronic integrated circuits", "bcd_percent": 0}"""
        user_message = f"Component: MPN={mpn or '(none)'} | Manufacturer={manufacturer or '(none)'} | Description={description or ''}"
        result = self._call(system_prompt, user_message, max_tokens=200)  # light model
        try:
            parsed = self._parse_json_response(result)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}

    def plan_sourcing(self, mpn: str, description: str, manufacturer: str, req_qty: int,
                      lots: list[dict], market: dict | None = None) -> Optional[dict]:
        """Decide the SOURCING PLAN for ONE part the way a human buyer would — which vendor lot(s) to
        use, how much from each, whether to hold for review, and whether the part is in market shortage.

        Reasons over GROUNDED facts only (the exact lots the code found + live market data) and returns
        ONLY lot indices + quantities + flags — it NEVER states a price. The caller then VALIDATES the
        plan (indices exist, qty <= offered, sum <= required) and falls back to the deterministic rules
        if it doesn't hold. Returns the parsed dict, or None on any failure.

        `lots`: [{"lot": i, "cost_usd": float, "offered_qty": int|None, "date_code": str,
                  "packaging": str, "lead_time": str, "vendor": str}]  (indices 0..n-1)
        """
        if not lots:
            return None
        lines = []
        for l in lots:
            cap = l.get("offered_qty")
            lines.append(
                f'Lot {l["lot"]}: cost USD {l.get("cost_usd")}/pc, '
                f'can supply {cap if cap else "the full quantity"}, '
                f'date code {l.get("date_code") or "n/a"}, packaging {l.get("packaging") or "n/a"}, '
                f'lead {l.get("lead_time") or "n/a"}'
            )
        mkt = ""
        if market:
            mkt = (f'\nLive market (distributor): {market.get("in_stock", "?")} in stock, '
                   f'factory lead {market.get("lead_time") or "n/a"}.')
        system_prompt = """You are an experienced semiconductor-distribution BUYER deciding how to source ONE part for a customer order. You are given the EXACT vendor lots available (already cost-normalised to USD) and the quantity needed. Decide the best sourcing plan the way a real buyer would.

Principles:
- Prefer buying the whole quantity from ONE vendor (a single clean PO). BUT if the cheapest single vendor that can cover the full quantity is MATERIALLY more expensive than combining a couple of cheaper lots, it is fine to use more than one lot to save real money — use your judgement on whether the premium is worth avoiding a split.
- You may ONLY use the lots listed. You may NOT invent lots, you may NOT exceed a lot's available quantity, and you may NOT state any price — you only choose lot numbers and quantities.
- If your plan uses more than one different SUPPLIER (vendor), set hold_for_review to true (a human confirms a multi-supplier buy).
- Judge "shortage" from the live market data and the kind of part — do NOT use a fixed rule. A part that is out of stock everywhere or on a very long factory lead is in shortage.

Return ONLY strict JSON, no prose:
{"allocation": [{"lot": <int>, "qty": <int>}, ...], "hold_for_review": <bool>, "shortage": <bool>, "reason": "<one short sentence>"}"""
        user_message = (f"Part: {mpn} — {description or ''} ({manufacturer or 'make n/a'})\n"
                        f"Quantity needed: {req_qty}\n\nAvailable vendor lots:\n" + "\n".join(lines) + mkt +
                        "\n\nReturn the sourcing plan JSON.")
        try:
            plan = self._parse_json_response(self._call(system_prompt, user_message, max_tokens=500))
            return plan if isinstance(plan, dict) and plan.get("allocation") else None
        except Exception as e:
            logger.warning(f"plan_sourcing failed for {mpn}: {e}")
            return None

    def draft_acknowledgement(self, customer_name: str, customer_company: str, items_summary: str, account: str) -> str:
        """Draft a professional acknowledgement email."""
        entity = "Company B International" if account == "main" else "Company A"

        system_prompt = f"""You are drafting a professional acknowledgement email on behalf of {entity}, a semiconductor component distributor.

{reply_style_context()}

Rules:
- Be warm, professional, and concise
- Acknowledge receipt of their inquiry
- List the items they requested (briefly)
- Promise a response with pricing shortly (we're confirming with our sources)
- Sign off professionally
- Do NOT include any pricing or availability information
- Do NOT make promises about stock availability (be honest about supply, as in the style above)
- Write entirely as a human member of the {entity} sales team. NEVER state, hint, or imply this email was written or sent by an AI, bot, agent, assistant, or automation, and never add "automated message", "auto-generated", or "do not reply" notes.
- Output HTML formatted email body only (no subject line, no headers)"""

        user_message = f"""Customer: {customer_name} from {customer_company}
Items requested:
{items_summary}

Draft the acknowledgement email body in HTML."""

        return self._strip_fences(self._call(system_prompt, user_message, max_tokens=800))

    def draft_quotation_email(
        self,
        customer_name: str,
        customer_company: str,
        account: str,
        line_items: list[dict],
        alternatives: list[dict],
        currency: str,
        validity_days: int,
    ) -> str:
        """Draft a quotation email for human review."""
        entity = "Company B International" if account == "main" else "Company A"

        items_text = ""
        has_pricing = any(item.get("unit_price") for item in line_items)
        for i, item in enumerate(line_items, 1):
            line = f"{i}. {item.get('mpn', 'N/A')} - {item.get('manufacturer', 'N/A')} - Qty: {item.get('quantity', 'N/A')}"
            if has_pricing and item.get("unit_price"):
                line += f" - Unit Price: {currency} {item['unit_price']}"
                if item.get("lead_time"):
                    line += f" - Lead Time: {item['lead_time']}"
                if item.get("moq"):
                    line += f" - MOQ: {item['moq']}"
            items_text += line + "\n"

        alt_text = ""
        if alternatives:
            alt_text = "\nAlternative suggestions from our authorized line:\n"
            for alt in alternatives:
                alt_text += f"- Instead of {alt.get('original_mpn', 'N/A')}: {alt.get('brand', 'N/A')} {alt.get('suggested_mpn', 'N/A')} ({alt.get('match_percentage', 'N/A')}% match) - {alt.get('comparison_notes', '')}\n"

        system_prompt = f"""You are drafting a professional quotation email on behalf of {entity}.

Rules:
- Professional, relationship-oriented tone
- Present line items in a clear HTML table format
- Include quotation validity period
- If there are alternative suggestions, present them clearly with spec comparisons
- Mention that datasheets can be provided on request
- Include a clear call to action
- IMPORTANT: If unit prices are provided in the items, use the EXACT prices given - calculate line totals (qty x unit price) and grand total
- If NO prices are provided, leave placeholder markers [UNIT_PRICE] and [TOTAL] where prices go
- Currency: {currency}
- Quote validity: {validity_days} days
- Write entirely as a human member of the {entity} sales team. NEVER state, hint, or imply this email was written or sent by an AI, bot, agent, assistant, or automation, and never add "automated message", "auto-generated", or "do not reply" notes.
- Output HTML formatted email body only"""

        user_message = f"""Customer: {customer_name} from {customer_company}

Requested items:
{items_text}
{alt_text}
Draft the quotation email body in HTML with price placeholders."""

        return self._strip_fences(self._call(system_prompt, user_message, max_tokens=2000))

    def extract_customer_info(self, from_field: str, body: str, signature: str = "") -> dict:
        """Extract customer name and company from email fields."""
        system_prompt = """Extract the customer's name and company from the email information provided.

Respond in this EXACT JSON format only:
{"name": "First Last", "company": "Company Name", "designation": "Title if found"}

If you cannot determine a field, use null."""

        user_message = f"""From field: {from_field}
Email body (first 500 chars): {body[:500]}
Signature: {signature}"""

        result = self._call(system_prompt, user_message, max_tokens=150)

        try:
            return self._parse_json_response(result)
        except (json.JSONDecodeError, ValueError):
            return {"name": None, "company": None, "designation": None}

    def extract_target_prices(self, subject: str, body: str, known_mpns: list[str], currency: str = "USD") -> dict:
        """Extract customer's target prices from a negotiation email.
        Returns: {items: [{mpn, current_price, target_price, notes}], has_targets: bool, currency}
        """
        system_prompt = f"""You are a pricing extraction assistant for a semiconductor distributor.
You are reading a CUSTOMER email where they are negotiating/countering our quotation prices.
The customer may provide target prices in various formats: tables, lists, inline text, or HTML tables.

For each part number, extract:
- mpn: the part number
- current_price: the price WE quoted (if the customer mentions it)
- target_price: the price the CUSTOMER wants (their target/counter price)
- quantity: quantity if mentioned (customer may have changed qty)
- notes: any remarks from the customer about this item

Known MPNs from our quotation: {', '.join(known_mpns)}

ALSO detect the CURRENCY from the email. Look for: "INR", "Rs.", "Rs ", "₹", "USD", "$", or context clues.
Default currency: {currency}

IMPORTANT:
- Match MPNs to the known list even if slightly different in the email
- Customer might say "target price", "our budget", "expected price", "best price", "last buy price"
- Extract ALL items the customer mentions, not just ones with explicit target prices
- If customer says "please check" or "can you match" next to a price, that IS the target price

Respond ONLY with this JSON format:
{{
    "items": [
        {{"mpn": "PART123", "current_price": 12.0, "target_price": 10.5, "quantity": 2200, "notes": "customer says last buy was 10.5"}},
        ...
    ],
    "has_targets": true,
    "currency": "INR",
    "customer_message": "brief summary of what customer is asking"
}}

If no target prices found:
{{"items": [], "has_targets": false, "currency": null, "customer_message": "summary"}}"""

        user_message = f"""Subject: {subject}

Customer's negotiation email:
{body[:3000]}

Extract target prices for these MPNs: {', '.join(known_mpns)}"""

        result = self._call(system_prompt, user_message, max_tokens=2000, model=self.heavy_model)

        try:
            return self._parse_json_response(result)
        except (json.JSONDecodeError, ValueError):
            logger.error(f"Failed to parse target price extraction: {result[:200]}")
            return {"items": [], "has_targets": False, "customer_message": "Failed to parse"}

    def draft_revised_quotation_email(
        self,
        customer_name: str,
        customer_company: str,
        account: str,
        line_items: list[dict],
        currency: str,
        validity_days: int,
        negotiation_round: int,
    ) -> str:
        """Draft a revised quotation email after negotiation, with updated prices from team."""
        entity = "Company B International" if account == "main" else "Company A"

        items_text = ""
        for i, item in enumerate(line_items, 1):
            line = f"{i}. {item.get('mpn', 'N/A')} - {item.get('manufacturer', 'N/A')} - Qty: {item.get('quantity', 'N/A')}"
            if item.get("unit_price"):
                line += f" - Revised Price: {currency} {item['unit_price']}"
                if item.get("lead_time"):
                    line += f" - Lead Time: {item['lead_time']}"
                if item.get("moq"):
                    line += f" - MOQ: {item['moq']}"
            items_text += line + "\n"

        system_prompt = f"""You are drafting a REVISED quotation email on behalf of {entity}, a semiconductor component distributor.

This is negotiation round {negotiation_round} — the customer requested better pricing and our team has provided revised prices.

Rules:
- Professional, relationship-oriented tone
- Acknowledge that this is a revised offer based on their feedback
- Present items in a clear HTML table with revised prices
- Calculate line totals (qty x unit price) and grand total
- Include quotation validity period ({validity_days} days)
- Mention we have worked to accommodate their target pricing
- Include a call to action (request PO / confirmation)
- Do NOT apologize excessively — be confident about the revised pricing
- Currency: {currency}
- Write entirely as a human member of the {entity} sales team. NEVER state, hint, or imply this email was written or sent by an AI, bot, agent, assistant, or automation, and never add "automated message", "auto-generated", or "do not reply" notes.
- Output HTML formatted email body only"""

        user_message = f"""Customer: {customer_name} from {customer_company}
Negotiation round: {negotiation_round}

Revised items:
{items_text}

Draft the revised quotation email body in HTML."""

        return self._strip_fences(self._call(system_prompt, user_message, max_tokens=2000))

    def extract_pricing_from_reply(
        self,
        reply_body: str,
        known_mpns: list[str],
        currency: str = "USD",
    ) -> dict:
        """Extract pricing data from Quote Analyst's email reply."""
        system_prompt = f"""You are a pricing data extraction assistant for a semiconductor distributor.
You are reading an internal email from a Quote Analyst / Purchase team / Product team who is providing selling prices for a customer quotation.

The analyst may format the pricing in various ways: tables, lists, inline text, Excel-style columns, etc.

For each part number, extract:
- mpn: the part number
- unit_price: the SELLING price (the final price to quote the customer, including margin)
- lead_time: delivery lead time if mentioned
- moq: minimum order quantity if mentioned
- spq: standard pack / packing quantity (SPQ) if mentioned
- available_qty: the quantity the sender can actually SUPPLY / OFFER / QUOTE for this part — the SUPPLY CAP.
  Vendors label this MANY ways — treat ALL of these as the offered supply quantity:
  "Offered Qty", "Quoted Qty", "Quoted Quantity", "Supported Qty", "Supported Quantity", "Available",
  "Available Qty", "In Stock", "Stock", "We can offer/supply", "Can support", and bare "Qty", "Quantity",
  "Pcs", "Pieces", "Items", "Nos", "Units" next to a number.
  Read the NUMBER, stripping commas and unit words (e.g. "8,000 pcs" -> 8000, "10k" / "10K nos" -> 10000).
  If they quote LESS than the requested quantity ("8000 against your 10000", "only 8000 available"), that
  8000 IS the available_qty. Null ONLY if they can cover the full requested quantity or state no limit.
  This is a SUPPLY CAP — do NOT confuse it with MOQ (a minimum to order) or SPQ (a pack multiple).
- packaging: the packing format if stated — e.g. "Tape & Reel", "T&R", "Reel", "Cut Tape", "Tray", "Tube", "Bulk". Null if not mentioned.
- date_code: the date code / DC if stated (e.g. "DC 2340", "2023+"). Null if not mentioned.
- validity: how long THIS price is valid, if stated — e.g. "48 hours", "7 days", "valid till 2026-07-10", "subject to prior sale". Null if not mentioned.
- notes: any conditions, stock status, or remarks

ALSO detect the CURRENCY from the reply. The team always mentions INR or USD somewhere in the email.
Look for: "INR", "Rs.", "Rs ", "₹", "USD", "$", "USDollar", or phrases like "Resale price in INR", "Quoted in USD".
Report the detected currency at the top level. If currency is unclear, use "{currency}" as default.

Known MPNs from the original inquiry: {', '.join(known_mpns)}

IMPORTANT: The analyst provides the FINAL SELLING PRICE (cost + margin + expenses already added).
Match MPNs to the known list - they may appear slightly different in the reply.

Respond ONLY with this JSON format:
{{
    "items": [
        {{"mpn": "PART123", "unit_price": 1.25, "lead_time": "4-6 weeks", "moq": 1000, "spq": 500, "available_qty": null, "packaging": "Tape & Reel", "date_code": "2340", "validity": "48 hours", "notes": "ex-stock"}},
        ...
    ],
    "has_pricing": true,
    "currency": "INR" or "USD",
    "pricing_notes": "any overall notes from the analyst"
}}

If the email does NOT contain pricing (e.g., just a status update, question, or FYI), respond:
{{"items": [], "has_pricing": false, "currency": null, "pricing_notes": "reason why no pricing found"}}"""

        user_message = f"""Quote Analyst's email reply:

{reply_body[:3000]}

Extract the pricing for these MPNs: {', '.join(known_mpns)}
Currency: {currency}"""

        # Inject the real market conventions so date code (YY+), EXW/T-T, condition and blunt "no stock"
        # declines are read the way our desk reads them.
        system_prompt = system_prompt + "\n\nMARKET CONVENTIONS (learned from our real mailbox):\n" + extraction_context()
        result = self._call(system_prompt, user_message, max_tokens=2000, model=self.heavy_model)

        try:
            return self._parse_json_response(result)
        except (json.JSONDecodeError, ValueError):
            # Non-fatal: usually a non-pricing reply (bounce/FYI). Treat as "no pricing", don't alarm.
            logger.warning(f"No parseable pricing in reply (treated as no-pricing): {result[:150]}")
            return {"items": [], "has_pricing": False, "pricing_notes": "no pricing content"}
