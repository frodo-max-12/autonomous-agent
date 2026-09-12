"""
SemiSales AI Agent - Main Agent Orchestrator
Runs the continuous email monitoring loop and coordinates all modules.
"""

import time
import threading
from datetime import datetime, timezone, timedelta
from loguru import logger

from config.settings import Settings
from core.database import init_database, get_session, Email, Quotation, NegotiationRound, EmailStatus, QuoteStatus, AuditLog
from core.email_processor import EmailProcessor
from core.bom_extractor import BOMExtractor
from gmail.client import GmailClient
from llm.claude_client import ClaudeClient
from knowledge.line_card_manager import LineCardManager


class SemiSalesAgent:
    """Main agent that monitors inboxes and processes emails autonomously."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.running = False

        # Initialize database
        self.engine, self.Session = init_database(settings.database_url)
        logger.info("Database initialized")

        # Initialize Claude client
        self.claude = ClaudeClient(
            api_key=settings.anthropic_api_key,
            model=settings.claude_model,
            max_tokens=settings.claude_max_tokens,
            heavy_model=settings.claude_heavy_model,
            timeout=settings.claude_timeout_seconds,
            effort=getattr(settings, "claude_effort", "high"),
        )
        logger.info(f"Claude client initialized (light: {settings.claude_model}, heavy: {settings.claude_heavy_model}, "
                    f"effort: {getattr(settings, 'claude_effort', 'high')})")

        # Initialize line card
        self.line_card = LineCardManager()
        logger.info(f"Line card loaded: {len(self.line_card.get_all_brands())} brands")

        # Initialize BOM extractor
        self.bom_extractor = BOMExtractor(self.claude)

        # Initialize Gmail clients for each account
        self.gmail_clients: dict[str, GmailClient] = {}
        self.processors: dict[str, EmailProcessor] = {}

    def setup_gmail_account(self, account_name: str, token_file: str):
        """Set up Gmail client for a specific account."""
        client = GmailClient(
            account_name=account_name,
            credentials_file=self.settings.google_credentials_file,
            token_file=token_file,
            scopes=self.settings.gmail_scopes,
        )
        client.authenticate()

        processor = EmailProcessor(
            gmail_client=client,
            claude_client=self.claude,
            line_card=self.line_card,
            bom_extractor=self.bom_extractor,
            database_url=self.settings.database_url,
            account=account_name,
            internal_domains=self.settings.get_internal_domains(),
            purchase_team_emails=self.settings.get_purchase_emails(account_name),
            product_team_emails=self.settings.get_product_emails(account_name),
            csr_team_emails=self.settings.get_csr_emails(account_name),
            agent_mode=self.settings.agent_mode,
            autonomous_enabled=self.settings.autonomous_enabled,
            auto_send_confidence=self.settings.auto_send_confidence_enabled,
            vendor_test_email=self.settings.vendor_test_email,
            vendor_max_vendors=self.settings.vendor_max_vendors,
            vendor_rfq_cc=self.settings.get_vendor_rfq_cc(),
            min_margin_percent=self.settings.min_margin_percent,
            auto_send_max_value=self.settings.auto_send_max_value,
            negotiation_min_margin_percent=self.settings.negotiation_min_margin_percent,
            default_margin_percent=self.settings.default_margin_percent,
            max_vendor_ask_percent=self.settings.max_vendor_ask_percent,
            part_lookup_provider=self.settings.part_lookup_provider,
            part_lookup_api_key=self.settings.part_lookup_api_key,
        )

        self.gmail_clients[account_name] = client
        self.processors[account_name] = processor
        logger.info(f"Gmail account set up: {account_name} ({client.user_email})")

        # Surface the effective mode + team routing so a testing/placeholder config is obvious at startup.
        mode = "AUTOMATIC (will email customers)" if self.settings.is_automatic() else "TESTING (customer emails suppressed)"
        logger.info(f"[{account_name}] Agent mode: {mode}")
        sourcing = "VENDORS (autonomous — RFQs emailed to vendor DB)" if self.settings.autonomous_enabled else "INTERNAL TEAM (manual pricing)"
        logger.info(f"[{account_name}] Pricing source: {sourcing}")
        teams = {
            "purchase": self.settings.get_purchase_emails(account_name),
            "product": self.settings.get_product_emails(account_name),
            "csr": self.settings.get_csr_emails(account_name),
        }
        logger.info(f"[{account_name}] Team routing → purchase={teams['purchase']} product={teams['product']} csr={teams['csr']}")
        for tname, addrs in teams.items():
            if not addrs:
                logger.warning(f"[{account_name}] {tname} team has NO email configured — those items will NOT be forwarded.")
        if teams["purchase"] and teams["purchase"] == teams["product"] == teams["csr"]:
            logger.warning(
                f"[{account_name}] purchase/product/CSR teams are ALL the same address "
                f"{teams['purchase']} — this looks like a TEST/placeholder config. "
                f"Set the real distribution lists in .env before going live."
            )

    def run(self):
        """Start the main monitoring loop."""
        self.running = True
        logger.info("=" * 60)
        logger.info("SemiSales AI Agent - STARTED")
        logger.info(f"Monitoring {len(self.gmail_clients)} inbox(es)")
        logger.info(f"Check interval: {self.settings.agent_check_interval_seconds}s")
        logger.info("=" * 60)

        # Crash recovery: resume any inquiry that was interrupted (e.g. power cut) after its quotation
        # was created but before its vendor RFQs went out, so nothing is silently stranded.
        try:
            self._recover_stranded_quotes()
        except Exception as e:
            logger.error(f"Startup recovery sweep failed: {e}")

        # Catch up on mail we may have MISSED while the machine was off — including mail a human already
        # opened (marked read), which the steady-state unread-only poll would skip forever.
        try:
            self._backfill_missed_mail()
        except Exception as e:
            logger.error(f"Startup missed-mail backfill failed: {e}")

        # Start follow-up checker in background thread
        followup_thread = threading.Thread(target=self._followup_loop, daemon=True)
        followup_thread.start()

        # Start pricing reply monitor in background thread
        pricing_thread = threading.Thread(target=self._pricing_monitor_loop, daemon=True)
        pricing_thread.start()

        while self.running:
            try:
                self._check_all_inboxes()
            except Exception as e:
                logger.error(f"Error in main loop: {e}")

            time.sleep(self.settings.agent_check_interval_seconds)

    def stop(self):
        """Stop the agent."""
        self.running = False
        logger.info("SemiSales AI Agent - STOPPED")

    def _inbox_after_date(self) -> str:
        """Gmail `after:` bound = the newest email we've already processed, so mail received while the
        laptop was OFF (weekend / holiday / shutdown) is caught on restart — not just 'today's'. Floored
        at the go-live date so we never pull the pre-go-live backlog (~900 old unread). YYYY/MM/DD."""
        from core.database import Email, get_session
        floor = str(getattr(self.settings, "agent_backfill_floor", None) or "2026/07/24")
        session = get_session(self.settings.database_url)
        try:
            row = session.query(Email.received_at).order_by(Email.received_at.desc()).first()
        finally:
            session.close()
        if row and row[0]:
            wm = row[0].strftime("%Y/%m/%d")
            return wm if wm > floor else floor
        return floor

    def _check_all_inboxes(self):
        """Check all inboxes for NEW mail — unread since the newest email we already processed (catches
        weekend/shutdown mail on restart), floored at go-live."""
        after = self._inbox_after_date()
        for account_name, processor in self.processors.items():
            try:
                client = self.gmail_clients[account_name]
                emails = client.get_unread_emails(max_results=5, after_date=after)

                if emails:
                    logger.info(f"[{account_name}] Processing {len(emails)} new email(s)...")

                for email_data in emails:
                    try:
                        result = processor.process_email(email_data)
                        logger.info(f"[{account_name}] Processed: {result}")
                    except Exception as e:
                        logger.error(f"[{account_name}] Error processing email {email_data.get('gmail_id')}: {e}")

            except Exception as e:
                logger.error(f"[{account_name}] Error checking inbox: {e}")

    def _backfill_missed_mail(self):
        """One-time start-up sweep: process any INBOX mail we missed while the machine was off —
        INCLUDING mail already marked read (e.g. a human opened an RFQ/quotation). The steady-state
        loop only reads UNREAD mail, so a read mail would otherwise never be handled. Deduped by
        gmail_id (process_email skips anything already handled); floored at go-live so we never pull
        the pre-go-live backlog."""
        after = self._inbox_after_date()
        for account_name, processor in self.processors.items():
            client = self.gmail_clients.get(account_name)
            if not client:
                continue
            try:
                emails = client.get_inbox_emails(after_date=after, max_results=100, include_read=True)
            except Exception as e:
                logger.error(f"[{account_name}] Backfill fetch failed: {e}")
                continue
            if len(emails) >= 100:
                logger.warning(f"[{account_name}] Backfill hit the 100-mail cap since {after} — older "
                               f"missed mail may not be swept; clear the inbox or narrow the gap.")
            for email_data in emails:
                try:
                    result = processor.process_email(email_data)
                    logger.info(f"[{account_name}] Backfill: {result}")
                except Exception as e:
                    logger.error(f"[{account_name}] Backfill error on {email_data.get('gmail_id')}: {e}")
            logger.info(f"[{account_name}] Startup backfill swept {len(emails)} read+unread mail since {after}")

    def _followup_loop(self):
        """Background loop to send follow-up emails on open quotes."""
        while self.running:
            try:
                self._check_followups()
            except Exception as e:
                logger.error(f"Follow-up check error: {e}")

            # Check every hour
            time.sleep(3600)

    def _check_followups(self):
        """Check for quotations needing follow-up."""
        session = get_session(self.settings.database_url)
        now = datetime.now(timezone.utc)

        try:
            # Day 3 follow-ups (both initial and revised quotations)
            if self.settings.followup_day3_enabled:
                day3_quotes = session.query(Quotation).filter(
                    Quotation.status.in_([QuoteStatus.SENT, QuoteStatus.REVISED_SENT]),
                    Quotation.sent_at <= now - timedelta(days=3),
                    Quotation.followup_1_sent_at.is_(None),
                ).all()

                for quote in day3_quotes:
                    self._send_followup(session, quote, followup_number=1)

            # Day 7 follow-ups
            if self.settings.followup_day7_enabled:
                day7_quotes = session.query(Quotation).filter(
                    Quotation.status.in_([QuoteStatus.SENT, QuoteStatus.REVISED_SENT, QuoteStatus.FOLLOW_UP_1]),
                    Quotation.sent_at <= now - timedelta(days=7),
                    Quotation.followup_2_sent_at.is_(None),
                ).all()

                for quote in day7_quotes:
                    self._send_followup(session, quote, followup_number=2)

            session.commit()

        except Exception as e:
            session.rollback()
            logger.error(f"Follow-up processing error: {e}")
        finally:
            session.close()

    def _pricing_monitor_loop(self):
        """Background loop to monitor forwarded email threads for pricing replies from Quote Analyst."""
        while self.running:
            try:
                self._check_pricing_replies()
            except Exception as e:
                logger.error(f"Pricing monitor error: {e}")

            # Check every 2 minutes
            time.sleep(120)

    def _check_pricing_replies(self):
        """Check all AWAITING_PRICING and AWAITING_REVISED_PRICING quotations for replies on their forwarded threads."""
        from core.database import BOMItem
        session = get_session(self.settings.database_url)

        try:
            # Keep the USD->INR rate fresh (only hits the network when actually due).
            if self.settings.autonomous_enabled:
                try:
                    from core import fx as fx_mod
                    fx_mod.ensure_fresh(session, self.settings)
                except Exception as e:
                    logger.warning(f"FX refresh check failed: {e}")

            # Find all quotes waiting for pricing (initial or revised after negotiation)
            awaiting = session.query(Quotation).filter(
                Quotation.status.in_([
                    QuoteStatus.AWAITING_PRICING,
                    QuoteStatus.AWAITING_REVISED_PRICING,
                    QuoteStatus.SOURCING_VENDORS,  # autonomous: waiting for vendor cost replies
                    QuoteStatus.VENDOR_PRICED,     # autonomous: consolidated, retry the confidence gate
                    QuoteStatus.PARTIALLY_PRICED,  # a partial quote went out — keep watching the silent vendors
                ]),
            ).all()

            if not awaiting:
                return

            logger.debug(f"Checking {len(awaiting)} quotations for pricing replies...")

            for quote in awaiting:
                try:
                    if quote.status == QuoteStatus.AWAITING_REVISED_PRICING:
                        self._check_revised_pricing(session, quote)
                    elif quote.status == QuoteStatus.SOURCING_VENDORS:
                        self._check_vendor_replies(session, quote)
                    elif quote.status == QuoteStatus.VENDOR_PRICED:
                        self._finalize_autonomous_quote(session, quote)
                    elif quote.status == QuoteStatus.PARTIALLY_PRICED:
                        self._check_partial_quote(session, quote)
                    else:
                        self._check_single_quote_pricing(session, quote)
                except Exception as e:
                    logger.error(f"Error checking pricing for {quote.quote_number}: {e}")

            session.commit()

        except Exception as e:
            session.rollback()
            logger.error(f"Pricing reply check error: {e}")
        finally:
            session.close()

    def _check_single_quote_pricing(self, session, quote: Quotation):
        """Check a single quotation's forwarded threads for pricing replies."""
        client = self.gmail_clients.get(quote.account)
        if not client:
            return

        # Collect known MPNs for this quote
        known_mpns = []
        if quote.line_items:
            known_mpns = [item.get("mpn", "") for item in quote.line_items if item.get("mpn")]

        if not known_mpns:
            return

        all_pricing_items = []
        detected_currencies: list[str] = []

        # Check product team thread for replies
        if quote.product_thread_id:
            replies = client.get_thread_replies(
                thread_id=quote.product_thread_id,
                after_message_id=quote.product_message_id,
            )
            for reply in replies:
                pricing = self._extract_pricing_from_reply(reply, known_mpns, quote.currency)
                if pricing and pricing.get("has_pricing"):
                    all_pricing_items.extend(pricing["items"])
                    cur = (pricing.get("currency") or "").upper().strip()
                    if cur in ("INR", "USD", "EUR"):
                        detected_currencies.append(cur)
                    logger.info(f"[{quote.quote_number}] Found pricing in product team thread reply from {reply['from_email']} (currency: {cur or 'not detected'})")

        # Check purchase team thread for replies
        if quote.purchase_thread_id:
            replies = client.get_thread_replies(
                thread_id=quote.purchase_thread_id,
                after_message_id=quote.purchase_message_id,
            )
            for reply in replies:
                pricing = self._extract_pricing_from_reply(reply, known_mpns, quote.currency)
                if pricing and pricing.get("has_pricing"):
                    all_pricing_items.extend(pricing["items"])
                    cur = (pricing.get("currency") or "").upper().strip()
                    if cur in ("INR", "USD", "EUR"):
                        detected_currencies.append(cur)
                    logger.info(f"[{quote.quote_number}] Found pricing in purchase team thread reply from {reply['from_email']} (currency: {cur or 'not detected'})")

        if not all_pricing_items:
            return

        # Delegate the merge + partial-quote drafting to the shared, partial-aware handler on
        # the account's EmailProcessor — same logic the inbox reply path uses, so both produce
        # identical partial quotes (priced rows + "Pending" rows) and highlight newly-priced items.
        cur = ""
        if detected_currencies:
            from collections import Counter
            cur = Counter(detected_currencies).most_common(1)[0][0]
        processor = self.processors.get(quote.account)
        if not processor:
            logger.warning(f"[{quote.quote_number}] No processor for account {quote.account}; cannot apply monitor pricing")
            return
        result = processor.apply_team_pricing(session, quote, all_pricing_items, cur, submitted_by="team (thread monitor)")
        logger.info(f"[{quote.quote_number}] Monitor applied pricing: {result}")

    def _check_vendor_replies(self, session, quote: Quotation):
        """Autonomous (Phase 3): parse vendor COST replies on VendorRFQ threads into VendorQuote rows.
        The quote stays in SOURCING_VENDORS; Phase 4 consolidates + prices once enough replies arrive."""
        client = self.gmail_clients.get(quote.account)
        if not client:
            return
        from core.vendor_sourcing import check_vendor_replies
        n = check_vendor_replies(
            quote, session, client, self.claude,
            our_email=getattr(client, "user_email", "") or "",
        )
        if n:
            logger.info(f"[{quote.quote_number}] Parsed {n} vendor cost quote(s) from vendor replies")
        # Phase 4: consolidate + apply margin once all vendors replied OR the wait window has elapsed.
        self._maybe_consolidate(session, quote)

    def _maybe_consolidate(self, session, quote: Quotation):
        """Consolidate vendor costs -> resale when the quote is ready (all vendors replied or timed out)."""
        from datetime import timedelta
        from core.database import VendorRFQ, VendorQuote
        from core.consolidation import consolidate_and_price

        rfqs = session.query(VendorRFQ).filter(VendorRFQ.quotation_id == quote.id).all()
        if not rfqs:
            return
        all_replied = all(r.status == "replied" for r in rfqs)
        now = datetime.now(timezone.utc).replace(tzinfo=None)  # SQLite stores naive datetimes
        sent_times = [r.sent_at for r in rfqs if r.sent_at]
        elapsed = bool(sent_times) and (now - min(sent_times)) > timedelta(hours=self.settings.vendor_reply_wait_hours)
        has_quotes = session.query(VendorQuote).filter(VendorQuote.quotation_id == quote.id).count() > 0

        if (all_replied or elapsed) and has_quotes:
            summary = consolidate_and_price(quote, session, self.settings, claude=self.claude)
            try:
                from core.vendor_scorecard import compute_vendor_scores
                compute_vendor_scores(session)  # wins/replies just changed -> refresh learned priority
            except Exception as e:
                logger.warning(f"Vendor scoring failed: {e}")
            quote.status = QuoteStatus.VENDOR_PRICED  # ready for the Phase 5 confidence gate
            logger.info(f"[{quote.quote_number}] Vendor pricing consolidated (all_replied={all_replied}, timed_out={elapsed}): {summary}")
            # Phase 5: run the confidence gate immediately (auto-send or route to dashboard).
            self._finalize_autonomous_quote(session, quote)
        elif elapsed and not has_quotes:
            # No vendor quoted within the wait window — hand to a human on the dashboard.
            # (ESCALATED is an EmailStatus, not a QuoteStatus; AWAITING_APPROVAL is the "needs human" state.)
            quote.status = QuoteStatus.AWAITING_APPROVAL
            logger.warning(f"[{quote.quote_number}] No vendor quotes within {self.settings.vendor_reply_wait_hours}h - escalated to human (dashboard)")

    def _finalize_autonomous_quote(self, session, quote: Quotation):
        """Phase 5: delegate to the account's EmailProcessor confidence gate (build + send-or-escalate)."""
        processor = self.processors.get(quote.account)
        if not processor:
            logger.warning(f"[{quote.quote_number}] No processor for {quote.account}; cannot finalize quote")
            return
        actions = processor.finalize_autonomous_quote(session, quote)
        logger.info(f"[{quote.quote_number}] Confidence gate: {actions}")

    def _recover_stranded_quotes(self):
        """Resume autonomous quotations that were created but whose vendor RFQs never went out —
        an inquiry interrupted mid-processing (power cut) would otherwise sit forever in
        AWAITING_PRICING with no RFQs, and the read email never comes back to re-trigger it."""
        if not self.settings.autonomous_enabled:
            return
        from core.database import VendorRFQ
        from core.vendor_sourcing import VendorSourcer
        session = get_session(self.settings.database_url)
        recovered = 0
        try:
            stranded = session.query(Quotation).filter(
                Quotation.status == QuoteStatus.AWAITING_PRICING).all()
            for q in stranded:
                n_rfq = session.query(VendorRFQ).filter_by(quotation_id=q.id).count()
                if n_rfq > 0:
                    q.status = QuoteStatus.SOURCING_VENDORS  # RFQs existed; status just wasn't advanced
                    recovered += 1
                    continue
                client = self.gmail_clients.get(q.account)
                if not client:
                    continue
                try:
                    sourcer = VendorSourcer(client, q.account, automatic=self.settings.is_automatic(),
                                            vendor_test_email=self.settings.vendor_test_email,
                                            max_vendors=self.settings.vendor_max_vendors,
                                            rfq_cc=self.settings.get_vendor_rfq_cc())
                    sent, _ = sourcer.send_rfqs(q, q.line_items or [], session)
                    q.status = QuoteStatus.SOURCING_VENDORS
                    recovered += 1
                    logger.warning(f"[recovery] Stranded quote {q.quote_number} — sent {len(sent)} "
                                   f"vendor RFQ(s) that never went out")
                except Exception as e:
                    logger.error(f"[recovery] Failed to resume {q.quote_number}: {e}")
            if recovered:
                session.commit()
                logger.info(f"[recovery] Resumed {recovered} stranded quotation(s) on startup")
        finally:
            session.close()

    def _check_partial_quote(self, session, quote: Quotation):
        """A PARTIAL quote already went to the customer. Keep polling the still-silent vendors; when
        their costs arrive, re-price the remaining lines and send an UPDATED quote (new parts tagged
        'New'). Autonomous only (needs vendor RFQs to poll)."""
        from core.database import VendorRFQ
        rfqs = session.query(VendorRFQ).filter(VendorRFQ.quotation_id == quote.id).all()
        if not rfqs or all(r.status == "replied" for r in rfqs):
            return  # not autonomous, or every vendor already replied — nothing more will arrive
        client = self.gmail_clients.get(quote.account)
        if not client:
            return
        from core.vendor_sourcing import check_vendor_replies
        n = check_vendor_replies(quote, session, client, self.claude,
                                 our_email=getattr(client, "user_email", "") or "")
        if not n:
            return  # no new vendor replies since last check
        from core.consolidation import consolidate_and_price
        consolidate_and_price(quote, session, self.settings, claude=self.claude)
        try:
            from core.vendor_scorecard import compute_vendor_scores
            compute_vendor_scores(session)
        except Exception as e:
            logger.warning(f"Vendor scoring failed: {e}")
        quotable = [it for it in (quote.line_items or []) if it.get("mpn") or it.get("description")]
        still_pending = [it for it in quotable if it.get("unit_price") is None]
        if still_pending:
            logger.info(f"[{quote.quote_number}] Partial update: {n} new vendor quote(s); "
                        f"{len(still_pending)} line(s) still pending — user can send another partial")
            return
        # All lines priced now → send the complete UPDATED quote (is_update → new parts tagged 'New').
        logger.info(f"[{quote.quote_number}] Partial quote now COMPLETE ({n} late quote(s)) → sending updated quote")
        quote.status = QuoteStatus.VENDOR_PRICED
        self._finalize_autonomous_quote(session, quote)

    def _check_revised_pricing(self, session, quote: Quotation):
        """Check a negotiation quote's team threads for revised pricing replies.
        Same logic as _check_single_quote_pricing but updates the negotiation round
        and drafts a REVISED quotation."""
        client = self.gmail_clients.get(quote.account)
        if not client:
            return

        known_mpns = [item.get("mpn", "") for item in (quote.line_items or []) if item.get("mpn")]
        if not known_mpns:
            return

        # Get the current negotiation round to find when we forwarded target prices
        neg_round = session.query(NegotiationRound).filter_by(
            quotation_id=quote.id,
            round_number=quote.negotiation_round,
        ).first()

        if not neg_round or not neg_round.forwarded_at:
            return

        all_pricing_items = []
        detected_currencies: list[str] = []

        # Check product team thread for replies AFTER our negotiation forward
        if quote.product_thread_id:
            replies = client.get_thread_replies(
                thread_id=quote.product_thread_id,
                after_message_id=quote.product_message_id,
            )
            # Only consider replies after the negotiation was forwarded
            for reply in replies:
                pricing = self._extract_pricing_from_reply(reply, known_mpns, quote.currency)
                if pricing and pricing.get("has_pricing"):
                    all_pricing_items.extend(pricing["items"])
                    cur = (pricing.get("currency") or "").upper().strip()
                    if cur in ("INR", "USD", "EUR"):
                        detected_currencies.append(cur)
                    logger.info(f"[{quote.quote_number}] R{quote.negotiation_round}: Found revised pricing in product team reply from {reply['from_email']}")

        # Check purchase team thread for replies
        if quote.purchase_thread_id:
            replies = client.get_thread_replies(
                thread_id=quote.purchase_thread_id,
                after_message_id=quote.purchase_message_id,
            )
            for reply in replies:
                pricing = self._extract_pricing_from_reply(reply, known_mpns, quote.currency)
                if pricing and pricing.get("has_pricing"):
                    all_pricing_items.extend(pricing["items"])
                    cur = (pricing.get("currency") or "").upper().strip()
                    if cur in ("INR", "USD", "EUR"):
                        detected_currencies.append(cur)
                    logger.info(f"[{quote.quote_number}] R{quote.negotiation_round}: Found revised pricing in purchase team reply from {reply['from_email']}")

        # Apply detected currency
        if detected_currencies:
            from collections import Counter
            new_currency = Counter(detected_currencies).most_common(1)[0][0]
            if new_currency != quote.currency:
                logger.info(f"[{quote.quote_number}] Currency updated from {quote.currency} to {new_currency}")
                quote.currency = new_currency

        if not all_pricing_items:
            return

        # Apply revised pricing to quote line items
        logger.info(f"[{quote.quote_number}] R{quote.negotiation_round}: Received revised pricing for {len(all_pricing_items)} items")

        line_items = [dict(it) for it in (quote.line_items or [])]  # copy so JSON change is tracked
        pricing_map = {p.get("mpn", "").upper(): p for p in all_pricing_items}

        items_priced = 0
        for item in line_items:
            mpn_key = (item.get("mpn") or "").upper()
            if mpn_key in pricing_map:
                p = pricing_map[mpn_key]
                item["unit_price"] = p.get("unit_price")
                item["lead_time"] = p.get("lead_time")
                item["moq"] = p.get("moq")
                item["pricing_notes"] = p.get("notes")
                if p.get("unit_price"):
                    items_priced += 1

        if items_priced == 0:
            logger.warning(f"[{quote.quote_number}] Revised pricing reply found but no prices matched known MPNs")
            return

        quote.line_items = line_items

        # Update negotiation round with revised prices
        neg_round.team_revised_prices = all_pricing_items
        neg_round.revised_pricing_at = datetime.now(timezone.utc)

        # Draft revised quotation email
        quote_body = self.claude.draft_revised_quotation_email(
            customer_name=quote.customer_name,
            customer_company=quote.customer_company or "",
            account=quote.account,
            line_items=line_items,
            currency=quote.currency or "USD",
            validity_days=quote.validity_days or 30,
            negotiation_round=quote.negotiation_round,
        )

        quote.draft_email_body = quote_body
        quote.status = QuoteStatus.REVISED_AWAITING_APPROVAL
        quote.pricing_received_at = datetime.now(timezone.utc)

        # Update email status
        email = session.query(Email).filter_by(id=quote.email_id).first()
        if email:
            email.status = EmailStatus.AWAITING_APPROVAL

        session.add(AuditLog(
            action="revised_pricing_received",
            entity_type="quotation",
            entity_id=quote.id,
            details={
                "quote_number": quote.quote_number,
                "round": quote.negotiation_round,
                "items_priced": items_priced,
                "total_items": len(line_items),
            },
            performed_by="agent",
        ))

        logger.info(f"[{quote.quote_number}] R{quote.negotiation_round}: Revised quotation draft created ({items_priced}/{len(line_items)} items) - awaiting approval")

    def _extract_pricing_from_reply(self, reply: dict, known_mpns: list[str], currency: str) -> dict:
        """Use Claude to extract pricing from a thread reply."""
        body = reply.get("body_text", "")
        if not body or len(body.strip()) < 10:
            return {"items": [], "has_pricing": False}

        return self.claude.extract_pricing_from_reply(
            reply_body=body,
            known_mpns=known_mpns,
            currency=currency or "USD",
        )

    def _send_followup(self, session, quote: Quotation, followup_number: int):
        """Send a follow-up email for an open quotation."""
        client = self.gmail_clients.get(quote.account)
        if not client:
            return

        # AGENT_MODE gate: never auto-email a real customer in testing mode.
        if not self.settings.is_automatic():
            logger.info(
                f"[TESTING MODE] Suppressed Day-{'3' if followup_number == 1 else '7'} follow-up "
                f"for {quote.quote_number} to {quote.customer_email}. Set AGENT_MODE=automatic to send."
            )
            return

        # v1.1 — Company B International only.
        entity = "Company B Pte Ltd"

        if followup_number == 1:
            body = f"""<p>Dear {quote.customer_name},</p>
<p>I hope this message finds you well. I wanted to follow up on our quotation <strong>{quote.quote_number}</strong> sent a few days ago.</p>
<p>Please let us know if you have any questions about the pricing, specifications, or lead times. We are happy to discuss further or explore alternative options if needed.</p>
<p>Looking forward to hearing from you.</p>
<p>Best regards,<br>{entity} Sales Team</p>"""
        else:
            body = f"""<p>Dear {quote.customer_name},</p>
<p>Just a gentle reminder regarding our quotation <strong>{quote.quote_number}</strong>. We want to make sure you have everything you need to make a decision.</p>
<p>If the quoted parts or pricing do not meet your requirements, we would be glad to work with you on alternatives or revised pricing.</p>
<p>Please feel free to reach out at any time.</p>
<p>Best regards,<br>{entity} Sales Team</p>"""

        try:
            # Find the original thread
            email = session.query(Email).filter_by(id=quote.email_id).first()
            if email:
                from core.email_processor import _addrs
                fu_cc = _addrs(quote.customer_cc)                       # the customer's own team
                for a in self.settings.get_vendor_rfq_cc():            # + the director's oversight
                    if a and a not in fu_cc:
                        fu_cc.append(a)
                client.send_reply(
                    thread_id=email.thread_id,
                    to_email=quote.customer_email,
                    subject=f"Follow-up: Quotation {quote.quote_number}",
                    body_html=body,
                    cc_emails=fu_cc or None,
                )

                if followup_number == 1:
                    quote.followup_1_sent_at = datetime.now(timezone.utc)
                    quote.status = QuoteStatus.FOLLOW_UP_1
                else:
                    quote.followup_2_sent_at = datetime.now(timezone.utc)
                    quote.status = QuoteStatus.FOLLOW_UP_2

                session.add(AuditLog(
                    action=f"followup_{followup_number}_sent",
                    entity_type="quotation",
                    entity_id=quote.id,
                    details={"customer": quote.customer_email, "quote": quote.quote_number},
                    performed_by="agent",
                ))

                logger.info(f"Follow-up {followup_number} sent for {quote.quote_number} to {quote.customer_email}")

        except Exception as e:
            logger.error(f"Failed to send follow-up for {quote.quote_number}: {e}")
