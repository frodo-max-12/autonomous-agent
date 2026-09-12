"""
SemiSales AI Agent - Email Processor
Orchestrates the processing of each incoming email through the full Phase 1 pipeline:
1. Classify email
2. Extract BOM/MPN data
3. Check line card & suggest alternatives
4. Draft acknowledgement
5. Draft quotation (for human approval)
"""

import html
from datetime import datetime, timezone
from loguru import logger

from core.database import (
    Email, BOMItem, Quotation, AuditLog, Lead, CustomerHistory, NegotiationRound,
    EmailType, EmailStatus, QuoteStatus,
    get_session, coerce_email_type,
)
from core.bom_extractor import BOMExtractor
from core.learning import LearningManager
from gmail.client import GmailClient
from llm.claude_client import ClaudeClient
from knowledge.line_card_manager import LineCardManager
from knowledge.cross_reference import CrossReference

import re as _re_mod
_EMAIL_ADDR_RE = _re_mod.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _addrs(raw) -> list:
    """All email addresses in a raw header string (To/Cc/From), lowercased, de-duped, order-preserving."""
    out = []
    for a in _EMAIL_ADDR_RE.findall(str(raw or "")):
        a = a.lower()
        if a not in out:
            out.append(a)
    return out


class EmailProcessor:
    """Processes incoming emails through the SemiSales pipeline."""

    def __init__(
        self,
        gmail_client: GmailClient,
        claude_client: ClaudeClient,
        line_card: LineCardManager,
        bom_extractor: BOMExtractor,
        database_url: str,
        account: str,  # which mailbox this send belongs to; only "main" is wired up
        internal_domains: list[str] = None,
        purchase_team_emails: list[str] = None,
        product_team_emails: list[str] = None,
        csr_team_emails: list[str] = None,
        agent_mode: str = "testing",
        autonomous_enabled: bool = False,
        auto_send_confidence: bool = True,
        vendor_test_email: str = "",
        vendor_max_vendors: int = 8,
        vendor_rfq_cc: list[str] = None,
        min_margin_percent: float = 8.0,
        auto_send_max_value: float = 0.0,
        negotiation_min_margin_percent: float = 10.0,
        default_margin_percent: float = 15.0,
        max_vendor_ask_percent: float = 20.0,
        part_lookup_provider: str = "none",
        part_lookup_api_key: str = "",
    ):
        self.gmail = gmail_client
        self.claude = claude_client
        self.line_card = line_card
        # Manufacturer-verified catalogue data. Loads empty and degrades to brand-level suggestions
        # if the file is missing/invalid, which is the safe direction.
        self.cross_ref = CrossReference()
        self.bom_extractor = bom_extractor
        self.database_url = database_url
        self.account = account
        # "automatic" => customer-facing emails (ack / PO-ack / complaint reply) send automatically.
        # Anything else ("testing") => those sends are SUPPRESSED (drafted only). Fail closed.
        self.automatic_mode = (agent_mode or "").strip().lower() == "automatic"
        self.internal_domains = [d.lower() for d in (internal_domains or [])]
        self.purchase_team_emails = purchase_team_emails or []    # Sourcing/Trading - unauthorized brands
        self.product_team_emails = product_team_emails or []      # Authorized brands - 50+ line card
        self.csr_team_emails = csr_team_emails or []              # CSR team - creates Sales Orders from POs
        # Autonomous mode: source pricing from external VENDORS (by brand) instead of the internal team.
        self.autonomous_enabled = bool(autonomous_enabled)
        # Confidence gate: auto-send a customer quote only when every line is confidently priced.
        self.auto_send_confidence = bool(auto_send_confidence)
        # Vendor-send safety: in testing, redirect RFQs here (or suppress); cap vendors per part.
        self.vendor_test_email = vendor_test_email or ""
        # Internal oversight addresses CC'd on every real vendor RFQ (sourcing visibility for the director).
        self.vendor_rfq_cc = vendor_rfq_cc or []
        # NOTE: 0 means "no cap" (email every matching vendor). 0 is falsy, so guard on
        # None/"" only -- a plain `if vendor_max_vendors else 8` would turn 0 back into 8.
        self.vendor_max_vendors = int(vendor_max_vendors) if vendor_max_vendors not in (None, "") else 8
        # Confidence-gate guardrails: thin-margin floor and a per-quote auto-send value ceiling.
        self.min_margin_percent = float(min_margin_percent) if min_margin_percent is not None else 0.0
        self.auto_send_max_value = float(auto_send_max_value) if auto_send_max_value else 0.0
        # Negotiation: how low margin may go to meet a customer's target before we re-source instead,
        # our normal (default) margin = the ceiling of the negotiation band, and the human cap on how
        # much cheaper we ask a vendor to go in one round.
        self.negotiation_min_margin = float(negotiation_min_margin_percent) if negotiation_min_margin_percent is not None else 10.0
        self.default_margin = float(default_margin_percent) if default_margin_percent is not None else 15.0
        self.max_vendor_ask = float(max_vendor_ask_percent) if max_vendor_ask_percent is not None else 20.0
        # Part identification: resolve a bare MPN -> real make + description via a live API (cached).
        from core.part_lookup import PartLookup
        self.part_lookup = PartLookup(provider=part_lookup_provider, api_key=part_lookup_api_key,
                                      database_url=database_url)
        self.learning = LearningManager(database_url)

    def _is_internal_email(self, from_email: str) -> bool:
        """Check if email is from our own team (internal domain)."""
        if not from_email:
            return False
        email_lower = from_email.lower()
        if "@" not in email_lower:
            return False
        domain_part = email_lower.split("@")[-1].replace(">", "").strip()
        for internal in self.internal_domains:
            if internal in domain_part:
                return True
        return False

    def _oversight_cc(self) -> list:
        """the director — CC'd on EVERY real outbound mail (customer + vendor), for sourcing oversight.
        Empty in testing mode so test runs never reach them."""
        return list(self.vendor_rfq_cc) if self.automatic_mode else []

    def _customer_team_addrs(self, email_data: dict) -> list:
        """The customer's OWN colleagues on the inquiry (its To + Cc), minus our own addresses and the
        sender (who is already the To of our reply). Kept in CC so their whole team stays in the loop."""
        sender = _addrs(email_data.get("from_email"))
        team = []
        for a in _addrs(email_data.get("to")) + _addrs(email_data.get("cc")):
            if self._is_internal_email(a):   # that's us (our domain), not the customer's side
                continue
            if a in sender:                  # the sender is already the To of our reply
                continue
            if a not in team:
                team.append(a)
        return team

    def _send_customer_reply(self, thread_id: str, to_email: str, subject: str, body_html: str, kind: str,
                             cc_emails: list = None) -> bool:
        """Send a CUSTOMER-facing reply, gated by AGENT_MODE.
        In 'testing' mode the send is suppressed (the draft/record is still kept) so the agent
        can run safely against the live inbox without emailing real customers.
        `cc_emails` = the customer's own team to keep in CC; the internal oversight pair
        (the director) is always appended on real sends. Returns True if sent, False if suppressed."""
        if not self.automatic_mode:
            logger.warning(
                f"[TESTING MODE] Suppressed customer {kind} to {to_email} "
                f"(subject: {subject[:60]!r}). Set AGENT_MODE=automatic in .env to send for real."
            )
            return False
        final_cc = []
        for a in list(cc_emails or []) + self._oversight_cc():
            if a and a not in final_cc:
                final_cc.append(a)
        self.gmail.send_reply(
            thread_id=thread_id,
            to_email=to_email,
            subject=subject,
            body_html=body_html,
            cc_emails=final_cc or None,
        )
        return True

    def _find_quotation_for_thread(self, thread_id: str, session) -> Quotation:
        """Find the quotation linked to a team thread (product or purchase)."""
        if not thread_id:
            return None
        return session.query(Quotation).filter(
            (Quotation.product_thread_id == thread_id) |
            (Quotation.purchase_thread_id == thread_id)
        ).first()

    def _has_pending_items(self, quotation: Quotation) -> bool:
        """True if the quote still has quotable line items with no unit price yet
        (i.e., a team hasn't priced them). Used to keep accepting team pricing replies
        for split-BOM quotes even after a partial quote has been drafted/sent."""
        for it in (quotation.line_items or []):
            if it.get("unit_price") is None and (it.get("mpn") or it.get("description")):
                return True
        return False

    def process_email(self, email_data: dict) -> dict:
        """
        Process a single incoming email through the full pipeline.
        Returns a summary dict of actions taken.
        """
        session = get_session(self.database_url)
        actions = {"email_id": None, "type": None, "actions_taken": []}

        try:
            # Step 0: Already-seen? Skip if the earlier run FINISHED; RESUME if it was interrupted
            # (e.g. power cut) after this row was committed but before the RFQs went to vendors.
            existing = session.query(Email).filter_by(gmail_id=email_data["gmail_id"]).first()
            if existing:
                skip, acts = self._resume_incomplete(existing, email_data, session)
                if skip:
                    logger.info(f"Email {email_data['gmail_id']} already processed, skipping")
                    return {"email_id": existing.id, "type": str(existing.email_type), "actions_taken": acts}
                # Resumed the missing work — mark read + persist, then we're done for this email.
                try:
                    self.gmail.mark_as_read(email_data["gmail_id"])
                except Exception as e:
                    logger.warning(f"Could not mark resumed email as read: {e}")
                session.commit()
                logger.info(f"Resumed interrupted email {email_data['gmail_id']}: {acts}")
                return {"email_id": existing.id, "type": str(existing.email_type), "actions_taken": acts}

            # Step 0.25: Is this a VENDOR REPLY on one of our RFQ threads? A vendor can be ANY address
            # (incl. an external gmail), so this must NOT rely on the internal-domain check. The
            # vendor-reply monitor (check_vendor_replies) already parses these into VendorQuotes from
            # the thread — here we just make sure the main loop does NOT misprocess a supplier's
            # quotation as a customer inquiry / negotiation (which created bogus leads + target prices).
            if email_data.get("thread_id"):
                from core.database import VendorRFQ
                on_vendor_thread = session.query(VendorRFQ).filter_by(
                    thread_id=email_data["thread_id"]).first()
                if on_vendor_thread:
                    logger.info(f"Vendor reply on RFQ thread from {email_data['from_email']} "
                                f"(quote {on_vendor_thread.quotation_id}) — left for the vendor-reply "
                                f"monitor; NOT a customer email")
                    try:
                        self.gmail.mark_as_read(email_data["gmail_id"])
                        self.gmail.add_label(email_data["gmail_id"], "SemiSales/Quotation")
                    except Exception as e:
                        logger.warning(f"Could not label vendor reply: {e}")
                    return {"email_id": None, "type": "vendor_reply",
                            "actions_taken": ["skipped_vendor_reply_handled_by_monitor"]}

            # Step 0.26: Is this a VENDOR'S REPLY to one of OUR purchase orders? WE send the PO to the
            # vendor, so their reply ("confirmed" / "sorry, no stock — can't process your PO") must NOT
            # be read as a CUSTOMER placing a PO (which wrongly sent a "thank you for your PO" back to
            # the supplier). Update the vendor-PO status instead — like a human reading the reply. This
            # runs BEFORE the internal-domain check, because our test vendors can be on our own domain.
            if email_data.get("thread_id"):
                from core.database import VendorPO
                vpo = session.query(VendorPO).filter_by(thread_id=email_data["thread_id"]).first()
                if not vpo:
                    import re as _re
                    m = _re.search(r'(?:AE|Company A)-PO-\d{8}-\d{4}(?:-\d+)?', email_data.get("subject", "") or "")
                    if m:
                        vpo = session.query(VendorPO).filter_by(po_number=m.group(0)).first()
                if vpo:
                    return self._handle_vendor_po_reply(vpo, email_data, session)

            # Step 0.27: Is this a VENDOR'S QUOTE for one of our RFQs, arriving OFF-THREAD? Vendors often
            # reply from a different colleague / a fresh email ("RE: RFQ AE-Q-…") that is NOT threaded to
            # our original RFQ, so Step 0.25's thread match misses it and the classifier would wrongly call
            # it a "vendor_offer" (real case: Kimter/WT). Match by the QUOTE NUMBER in the subject → parse
            # it as a vendor quote (price → VendorQuote; decline → No-Bid) and label it Quotation, so we
            # never lose a quote or a No-Bid just because it came on a new thread.
            import re as _re27
            _mq = _re27.search(r'AE-Q-\d{8}-\d{4}', email_data.get("subject", "") or "")
            if _mq and not self._is_internal_email(email_data["from_email"]):
                _quote = session.query(Quotation).filter_by(quote_number=_mq.group(0)).first()
                if _quote:
                    def _dom(e):
                        return (e or "").lower().split("@")[-1].replace(">", "").strip()
                    # A reply from the CUSTOMER's own domain is a customer follow-up, not a vendor quote.
                    if _dom(email_data.get("from_email")) != _dom(_quote.customer_email):
                        return self._handle_vendor_quote_reply(_quote, email_data, session)

            # Step 0.5: Handle internal emails
            # Internal emails from our own domain should generally be skipped.
            # BUT: (a) team members reply on tracked quotation threads with pricing — processed below;
            # (b) team members FORWARD customer inquiries / POs / negotiations from our own domain —
            # those must be processed too, not dropped. So an internal email that isn't a pricing reply
            # falls through to classification, and we skip only genuine internal chatter afterwards.
            internal_forward = False
            if self._is_internal_email(email_data["from_email"]):
                quotation_on_thread = self._find_quotation_for_thread(email_data.get("thread_id"), session)

                if quotation_on_thread:
                    # Team reply on a quotation thread — process pricing directly
                    logger.info(f"Internal team reply from {email_data['from_email']} on thread for {quotation_on_thread.quote_number} (status: {quotation_on_thread.status.value})")
                    try:
                        self.gmail.mark_as_read(email_data["gmail_id"])
                        self.gmail.add_label(email_data["gmail_id"], "SemiSales/Internal")
                    except Exception as e:
                        logger.warning(f"Could not label internal email: {e}")

                    # Process pricing from this reply whenever the quote is still accepting prices.
                    # This includes AWAITING_PRICING, AWAITING_REVISED_PRICING, a PARTIALLY_PRICED
                    # quote, OR any quote that still has un-priced items (e.g. a partial quote already
                    # drafted/sent while the second team's reply comes in) — so split-BOM prices are
                    # never dropped.
                    accepting = (
                        quotation_on_thread.status in (
                            QuoteStatus.AWAITING_PRICING,
                            QuoteStatus.AWAITING_REVISED_PRICING,
                            QuoteStatus.PARTIALLY_PRICED,
                        )
                        or self._has_pending_items(quotation_on_thread)
                    )
                    if accepting:
                        result = self._handle_team_pricing_reply(session, quotation_on_thread, email_data)
                        session.commit()
                        return {"email_id": None, "type": "team_pricing_reply", "actions_taken": result}
                    else:
                        logger.info(f"Quotation {quotation_on_thread.quote_number} is in status {quotation_on_thread.status.value} — fully priced, no pricing action needed, marking read")
                        return {"email_id": None, "type": "internal_team_reply", "actions_taken": ["team_reply_no_action_needed"]}
                else:
                    # Internal, but NOT a reply on a tracked quotation thread. A team member may be
                    # FORWARDING a customer inquiry / PO / negotiation — classify it below and process it
                    # if it's business; only genuine internal chatter is skipped (after classification).
                    internal_forward = True

            # Step 1: Classify the email (with learned context from past corrections)
            # Use HTML body (cleaned) when plain text is too short OR has no part numbers.
            # HTML emails often have part numbers only inside <table> tags that get stripped
            # from the plain text, so greeting text like "Hi Sir, please quote..." passes the
            # length check but contains zero part numbers → misclassified as "other".
            logger.info(f"Classifying email: {email_data['subject'][:60]}...")
            from core.bom_extractor import html_to_clean_text, MPN_PATTERN
            body_for_classify = email_data["body_text"] or ""
            html_body_raw = email_data.get("body_html") or ""
            has_mpns_in_text = bool(MPN_PATTERN.search(body_for_classify))
            # Also check if HTML has a <table> — strong signal of an embedded parts list
            has_html_table = "<table" in html_body_raw.lower()
            if (len(body_for_classify.strip()) < 50 or not has_mpns_in_text or has_html_table) and html_body_raw:
                cleaned_html = html_to_clean_text(html_body_raw)
                if len(cleaned_html) > len(body_for_classify) or MPN_PATTERN.search(cleaned_html) or has_html_table:
                    body_for_classify = cleaned_html
                    logger.info("Using cleaned HTML body for classification (plain text missing part data or HTML table detected)")

            learned_context = self.learning.build_classification_context(
                subject=email_data["subject"],
                body=body_for_classify,
                from_email=email_data["from_email"],
            )
            classification = self.claude.classify_email(
                subject=email_data["subject"],
                body=body_for_classify,
                from_email=email_data["from_email"],
                learned_context=learned_context,
            )
            # Normalize + validate the classifier output. An out-of-enum value (e.g. "RFQ",
            # "inquiry", "order") must NOT reach EmailType(...) directly — that raises ValueError,
            # the email is never marked read, and it gets reprocessed (billed) on every cycle forever.
            email_type_enum = coerce_email_type(classification.get("type"))
            email_type = email_type_enum.value
            # Business-rule urgency (deterministic, overrides the classifier's guess for these types):
            # RFQ = high, Negotiation = critical (extra-high), PO = priority. Other types keep the LLM urgency.
            _URGENCY_BY_TYPE = {"rfq": "high", "negotiation": "critical", "po": "priority"}
            urgency = _URGENCY_BY_TYPE.get(email_type, classification.get("urgency", "normal"))
            logger.info(f"Classification: {email_type} | Urgency: {urgency} | Confidence: {classification.get('confidence', 0)}")

            # Internal email that ISN'T customer business (rfq/po/negotiation) — e.g. genuine team
            # chatter, an FYI, a newsletter forwarded internally — skip it (mark read so it doesn't
            # come back). A team member forwarding a real inquiry/PO/negotiation falls through and is
            # processed normally, with the team member as the contact.
            if internal_forward and email_type not in ("rfq", "po", "negotiation"):
                logger.info(f"SKIPPING internal email from {email_data['from_email']} — classified '{email_type}', not customer business")
                try:
                    self.gmail.mark_as_read(email_data["gmail_id"])
                    self.gmail.add_label(email_data["gmail_id"], "SemiSales/Internal")
                except Exception as e:
                    logger.warning(f"Could not label internal email: {e}")
                return {"email_id": None, "type": "internal", "actions_taken": [f"skipped_internal_{email_type}"]}
            if internal_forward:
                logger.info(f"Internal team FORWARD of a customer {email_type} from {email_data['from_email']} — processing (contact = the forwarding team member)")

            # Step 2: Save email to database
            db_email = Email(
                gmail_id=email_data["gmail_id"],
                thread_id=email_data["thread_id"],
                account=self.account,
                from_email=email_data["from_email"],
                from_name=self._extract_name(email_data["from_email"]),
                to_email=email_data["to_email"],
                cc=email_data.get("cc"),
                subject=email_data["subject"],
                body_text=email_data["body_text"],
                body_html=email_data["body_html"],
                received_at=datetime.now(timezone.utc),
                email_type=email_type_enum,
                status=EmailStatus.NEW,
                urgency=urgency,
                has_attachments=email_data["has_attachments"],
                attachment_names=email_data["attachment_names"],
                extracted_data=classification,
            )
            session.add(db_email)
            session.flush()
            actions["email_id"] = db_email.id
            actions["type"] = email_type

            # Label the email in Gmail
            label_map = {
                "rfq": "SemiSales/RFQ",
                "po": "SemiSales/PO",
                "negotiation": "SemiSales/Negotiation",
                "complaint": "SemiSales/Escalate",
                "spam": "SemiSales/Spam",
            }
            label = label_map.get(email_type, "SemiSales/Other")
            try:
                self.gmail.add_label(email_data["gmail_id"], label)
            except Exception as e:
                logger.warning(f"Could not add label: {e}")

            # Step 2.5: Extract lead info for every external email (lead generation)
            if email_type not in ("spam", "internal"):
                try:
                    self._extract_and_store_lead(session, db_email, email_data, classification)
                except Exception as e:
                    logger.warning(f"Lead extraction failed: {e}")

            # Commit the idempotency marker (this Email row, keyed by unique gmail_id) BEFORE any
            # handler runs — handlers send customer/team email, and if a later step throws we must
            # NOT roll this row back, or the next poll would re-fetch the same gmail_id and re-send
            # a duplicate acknowledgement + a second quote number. Committing here makes the dedup
            # check at the top of process_email authoritative even on partial failure.
            session.commit()

            # Step 3: Handle based on email type
            if email_type == "rfq":
                actions["actions_taken"] = self._handle_rfq(session, db_email, email_data)
            elif email_type == "po":
                actions["actions_taken"] = self._handle_po(session, db_email, email_data)
            elif email_type == "complaint":
                actions["actions_taken"] = self._handle_complaint(session, db_email, email_data)
            elif email_type == "negotiation":
                actions["actions_taken"] = self._handle_negotiation(session, db_email, email_data)
            elif email_type == "vendor_offer":
                actions["actions_taken"] = self._handle_vendor_offer(session, db_email, email_data)
            elif email_type == "spam":
                db_email.status = EmailStatus.ARCHIVED
                actions["actions_taken"] = ["archived_spam"]
            else:
                db_email.status = EmailStatus.PROCESSING
                actions["actions_taken"] = ["classified_as_other"]

            # Audit log
            session.add(AuditLog(
                action="email_processed",
                entity_type="email",
                entity_id=db_email.id,
                details=actions,
                performed_by="agent",
            ))

            session.commit()  # durably persist ALL handler work (quotation, RFQs, status) first

            # Mark as read ONLY after the commit. If we crash before this line, the email stays unread
            # and is RESUMED next cycle (_resume_incomplete finishes the missing RFQs); if we crash
            # after, the dedup sees a finished quotation and skips. No lost work, no double-send.
            try:
                self.gmail.mark_as_read(email_data["gmail_id"])
            except Exception as e:
                logger.warning(f"Could not mark email as read: {e}")

            logger.info(f"Email processed: {actions}")
            return actions

        except Exception as e:
            session.rollback()
            logger.error(f"Error processing email: {e}")
            raise
        finally:
            session.close()

    def _resume_incomplete(self, existing: Email, email_data: dict, session) -> tuple[bool, list]:
        """A row for this email already exists from a prior run. If that run FINISHED, skip (return
        True). If it was interrupted (power cut) after the dedup row was committed but before the work
        completed, RESUME only the missing steps idempotently (return False + actions).

        Focus on the autonomous RFQ path — the common interruption is 'quotation created / lead
        captured, but the RFQs never reached the vendors'. Each resume step checks DB state first so
        it never double-sends what already went out."""
        et = existing.email_type.value if hasattr(existing.email_type, "value") else str(existing.email_type)
        if et != "rfq":
            return True, ["skipped_duplicate"]  # non-RFQ types: keep the plain skip behaviour

        quotation = session.query(Quotation).filter_by(email_id=existing.id).first()
        if quotation is None:
            # Interrupted before the quotation was created -> re-run the RFQ handler fully.
            logger.warning(f"Resuming interrupted RFQ {email_data['gmail_id']} — no quotation yet, re-running handler")
            actions = self._handle_rfq(session, existing, email_data)
            return False, actions + ["resumed_from_scratch"]

        if not self.autonomous_enabled:
            return True, ["skipped_duplicate"]  # internal-team flow has its own thread monitor

        if quotation.status == QuoteStatus.AWAITING_PRICING:
            from core.vendor_sourcing import VendorSourcer
            from core.database import VendorRFQ
            n_rfq = session.query(VendorRFQ).filter_by(quotation_id=quotation.id).count()
            if n_rfq == 0:
                # Quotation exists but RFQs never went out (the exact power-cut case) -> send them now.
                logger.warning(f"Resuming {quotation.quote_number} — quotation exists but RFQs were never sent to vendors")
                sourcer = VendorSourcer(self.gmail, self.account, automatic=self.automatic_mode,
                                        vendor_test_email=self.vendor_test_email, max_vendors=self.vendor_max_vendors,
                                        rfq_cc=self.vendor_rfq_cc)
                sent, unsourced = sourcer.send_rfqs(quotation, quotation.line_items or [], session)
                quotation.status = QuoteStatus.SOURCING_VENDORS
                acts = [f"resumed_sourced_to_{len(sent)}_vendors"]
                if unsourced:
                    acts.append(f"{len(unsourced)}_parts_unsourced_no_vendor")
                return False, acts
            # RFQs were actually sent but the status update didn't commit -> just fix the status.
            quotation.status = QuoteStatus.SOURCING_VENDORS
            return False, ["resumed_status_fixed"]

        return True, ["skipped_duplicate"]  # already sourced / priced / sent -> genuinely done

    def _existing_quote_for_thread(self, session, thread_id, exclude_email_id=None):
        """The most recent quotation already raised on this email thread (i.e. an earlier inquiry the
        customer is now following up on), excluding the current email's own row. None if none."""
        if not thread_id:
            return None
        q = (session.query(Quotation).join(Email, Quotation.email_id == Email.id)
             .filter(Email.thread_id == thread_id))
        if exclude_email_id:
            q = q.filter(Email.id != exclude_email_id)
        return q.order_by(Quotation.id.desc()).first()

    def _apply_customer_revision(self, session, quotation, db_email, email_data, bom_items) -> list[str]:
        """Customer replied on an existing quote thread with a revision (typically a changed quantity).
        Update the EXISTING quotation's line quantities from the new mail, re-source the changed lines,
        and acknowledge — never create a duplicate quotation."""
        actions = ["customer_revision_detected", f"revising_{quotation.quote_number}"]
        db_email.status = EmailStatus.PROCESSING
        lines = [dict(it) for it in (quotation.line_items or [])]
        by_mpn = {(l.get("mpn") or "").strip().upper(): l for l in lines}
        changed = 0
        for item in bom_items:
            mpn = (item.get("mpn") or "").strip().upper()
            newq = item.get("quantity")
            if not (mpn and newq):
                continue
            if mpn in by_mpn:
                if by_mpn[mpn].get("quantity") != newq:
                    by_mpn[mpn]["quantity"] = newq
                    by_mpn[mpn].update(unit_price=None, cost_price=None, selected_vendor=None,
                                       fulfillment=None, pricing_status=None, remark=None)
                    changed += 1
            else:
                lines.append({"mpn": item.get("mpn"), "manufacturer": item.get("manufacturer"),
                              "quantity": newq, "description": item.get("description"),
                              "unit_price": None, "lead_time": None})
                changed += 1
        quotation.line_items = lines
        if changed:
            quotation.status = QuoteStatus.SOURCING_VENDORS
        actions.append(f"updated_{changed}_line(s)")

        if self.autonomous_enabled and changed:
            try:
                from core.vendor_sourcing import VendorSourcer
                sourcer = VendorSourcer(self.gmail, self.account, automatic=self.automatic_mode,
                                        vendor_test_email=self.vendor_test_email,
                                        max_vendors=self.vendor_max_vendors, rfq_cc=self.vendor_rfq_cc)
                sent, _ = sourcer.send_rfqs(quotation, quotation.line_items, session)
                actions.append(f"re_sourced_to_{len(sent)}_vendors")
            except Exception as e:
                logger.error(f"Revision re-source failed: {e}")
                actions.append("revision_resource_failed")

        try:
            ack = (f'<p>Dear {html.escape(quotation.customer_name or "Customer")},</p>'
                   f'<p>Thank you — we have noted your revised requirement and are re-checking pricing '
                   f'with our sources. A revised quotation will follow shortly.</p>'
                   f'<p>Best regards,<br>{self._entity_for_currency(quotation.currency)} Sales Team</p>')
            if self._send_customer_reply(thread_id=email_data["thread_id"], to_email=email_data["from_email"],
                                         subject=email_data["subject"], body_html=ack, kind="revision ack",
                                         cc_emails=self._customer_team_addrs(email_data)):
                actions.append("sent_revision_ack")
        except Exception as e:
            logger.error(f"Revision ack failed: {e}")
        logger.info(f"[{quotation.quote_number}] Customer REVISION applied — {changed} line(s) changed, re-sourced")
        return actions

    def _handle_rfq(self, session, db_email: Email, email_data: dict) -> list[str]:
        """Handle RFQ emails: extract BOM, check line card, draft ack + quotation."""
        actions = []

        # Extract BOM from the body + attachments, then merge.
        # Prefer the HTML body (it preserves the table structure — cleaned to a markdown table).
        # Only fall back to the plain-text body if HTML yielded nothing, so we don't fire a second,
        # redundant (and slow — it was timing out ~10 min) LLM call on every table-based inquiry.
        plain_body = email_data["body_text"] or ""
        html_body = email_data.get("body_html") or ""

        collected = []  # raw items from every source, merged below
        html_items = []
        if html_body:
            try:
                html_items = self.bom_extractor.extract_from_email(subject=email_data["subject"], body=html_body,
                                                                   our_domains=self.internal_domains)
                logger.info(f"BOM from HTML body: {len(html_items)} items")
                collected.extend(html_items)
            except Exception as e:
                logger.warning(f"HTML BOM extraction failed: {e}")
        if not html_items and plain_body.strip() and len(plain_body.strip()) >= 20:
            try:
                items = self.bom_extractor.extract_from_email(subject=email_data["subject"], body=plain_body,
                                                              our_domains=self.internal_domains)
                logger.info(f"BOM from plain text: {len(items)} items")
                collected.extend(items)
            except Exception as e:
                logger.warning(f"Plain-text BOM extraction failed: {e}")
        if email_data["has_attachments"]:
            for att in email_data.get("attachment_refs", []):
                if att.get("attachment_id"):
                    try:
                        file_bytes = self.gmail.download_attachment(email_data["gmail_id"], att["attachment_id"])
                        items = self.bom_extractor.extract_from_attachment(file_bytes, att["filename"], att["mime_type"])
                        logger.info(f"BOM from attachment {att['filename']}: {len(items)} items")
                        collected.extend(items)
                    except Exception as e:
                        logger.warning(f"Error processing attachment {att['filename']}: {e}")

        # Merge/dedup on a COMPOSITE key (mpn|description|package) so DISTINCT rows are never
        # dropped: a row with no MPN, or a repeated MPN with a different description/package, is
        # kept. Only exact duplicates (e.g. the same email parsed as both plain + HTML) collapse,
        # and their missing fields are filled from each other. (Fixes the old MPN-only merge that
        # silently dropped description-only rows and same-MPN variants — e.g. 27 in, 20 out.)
        def _row_key(it):
            return (
                (it.get("mpn") or "").strip().upper(),
                (it.get("description") or "").strip().upper(),
                (it.get("package") or "").strip().upper(),
            )
        merged = {}
        bom_items = []
        for it in collected:
            k = _row_key(it)
            if k == ("", "", ""):
                continue  # completely empty row — nothing to quote
            if k not in merged:
                merged[k] = it
                bom_items.append(it)
            else:
                ex = merged[k]
                for fld in ("manufacturer", "quantity", "target_price", "currency", "required_date",
                            "special_requirements", "annual_quantity", "package", "mpn", "description"):
                    if not ex.get(fld) and it.get(fld):
                        ex[fld] = it[fld]

        logger.info(f"Extracted {len(bom_items)} unique BOM items (from {len(collected)} raw across all sources)")

        # If no BOM items found, escalate to human instead of creating empty quote
        if not bom_items:
            logger.warning("RFQ classified but no BOM items extracted - escalating to human")
            db_email.status = EmailStatus.ESCALATED
            actions.append("no_bom_extracted_escalated")
            return actions

        # REVISION: the customer is replying on a thread that ALREADY has a quotation (e.g. "revised
        # quantity 10,000") — update THAT quote and re-source it, never create a second quotation.
        _existing_q = self._existing_quote_for_thread(session, email_data.get("thread_id"), db_email.id)
        if _existing_q is not None:
            logger.info(f"Customer follow-up on existing quote {_existing_q.quote_number} — treating as a "
                        f"REVISION, not a new RFQ")
            return self._apply_customer_revision(session, _existing_q, db_email, email_data, bom_items)

        # Identify bare part numbers: if the customer gave only an MPN (no make/description), resolve
        # the REAL manufacturer + description from a live part-search API (cached). Grounds the vendor
        # RFQ + line-card check + ack in real data instead of the LLM guessing the manufacturer.
        if getattr(self, "part_lookup", None) and self.part_lookup.enabled:
            identified = 0
            for item in bom_items:
                mpn = (item.get("mpn") or "").strip()
                if mpn and (not item.get("manufacturer") or not item.get("description")):
                    info = self.part_lookup.resolve(mpn)
                    if info:
                        if not item.get("manufacturer") and info.get("manufacturer"):
                            item["manufacturer"] = info["manufacturer"]
                        if not item.get("description") and info.get("description"):
                            item["description"] = info["description"]
                        item["identified_by"] = info.get("source")
                        item["datasheet_url"] = info.get("datasheet_url")
                        identified += 1
            if identified:
                logger.info(f"Part lookup identified {identified} bare part number(s) → make + description")
                actions.append(f"identified_{identified}_parts_via_lookup")

        # Market intel (Phase C): if a vendor recently pushed a standing STOCK OFFER for a part the
        # customer is now asking about, attach it — we already know a live cost / lead / date code
        # (grounded, from a real vendor, not guessed). Surfaced on the line + dashboard.
        try:
            from core.database import MarketOffer
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            recent = now - timedelta(days=90)
            # RETRIEVAL ONLY — hand the model a slice of the offer book and let it do the matching.
            # We deliberately do NOT filter by MPN here: an equality filter is what made this feature
            # dead code (it fires only on byte-identical part numbers, so an equivalent part or the
            # right vendor with the wrong line was invisible). Recency + a cap is a retrieval limit,
            # not a judgement about what matches.
            rows = (session.query(MarketOffer)
                    .filter(MarketOffer.active.is_(True), MarketOffer.received_at >= recent)
                    .order_by(MarketOffer.received_at.desc()).limit(400).all())
            if rows:
                offers = [{"vendor": o.vendor_name, "vendor_email": o.vendor_email, "mpn": o.mpn,
                           "manufacturer": o.manufacturer, "price": o.unit_price,
                           "currency": o.currency, "date_code": o.date_code,
                           "lead_time": o.lead_time, "qty": o.offered_qty,
                           "age_days": ((now - o.received_at).days
                                        if o.received_at else None)}
                          for o in rows]
                by_vendor = {}
                for o in offers:
                    by_vendor.setdefault((o["vendor"] or "").lower(), o.get("vendor_email"))
                matches = self.claude.match_market_offers(items=bom_items, offers=offers)
                matched_offers = 0
                for idx, item in enumerate(bom_items):
                    m = matches.get(str(idx)) or matches.get(idx)
                    if not m or not (m.get("offers") or m.get("approach_first")):
                        continue
                    item["market_offers"] = m.get("offers") or []
                    item["approach_first"] = [
                        {"vendor": v, "vendor_email": by_vendor.get((v or "").lower())}
                        for v in (m.get("approach_first") or [])]
                    item["market_note"] = m.get("note")
                    matched_offers += 1
                if matched_offers:
                    logger.info(f"Market intel: {matched_offers} inquired part(s) matched against "
                                f"{len(rows)} recent vendor offers")
                    actions.append(f"market_offers_matched_{matched_offers}")
        except Exception as e:
            logger.warning(f"Market-offer lookup failed: {e}")

        # First pass: persist each BOM item + classify authorized vs unauthorized.
        alternatives = []
        unauth = []  # (item, db_item) pairs that need an alternative suggestion
        authorized_for_ack = []  # inquired parts on OUR authorised line -> "we supply directly" ACK block
        for item in bom_items:
            manufacturer = item.get("manufacturer", "")
            description = item.get("description", "")
            line_check = self.line_card.check_and_suggest(manufacturer, description)

            db_item = BOMItem(
                email_id=db_email.id,
                mpn=item.get("mpn") or None,
                manufacturer=manufacturer,
                description=description,
                quantity=item.get("quantity"),
                package=item.get("package"),
                annual_quantity=item.get("annual_quantity"),
                needs_identification=item.get("needs_identification", False),
                target_price=item.get("target_price"),
                target_currency=item.get("currency"),
                required_date=item.get("required_date"),
                special_requirements=item.get("special_requirements"),
                is_authorized=line_check["is_authorized"],
            )
            if line_check["is_authorized"]:
                db_item.line_status = "green"
                authorized_for_ack.append(item)
            else:
                db_item.line_status = "amber" if line_check["alternatives"] else "red"
                unauth.append((item, db_item))
            session.add(db_item)

        # Second pass: ONE batched cross-reference for ALL unauthorized parts.
        #
        # The model decodes each requested part and picks the best-fit brand from our REAL authorised
        # line card — ANY of our 54 lines, not just the ones with catalogue data (a diode → our diode
        # makers, a converter → our converter maker). All the engineering judgement is its own. Three
        # gates stand between its answer and the customer's inbox, each on a FACT not a judgement:
        #   1. the brand must be a real authorised brand (checked against the line card) — a brand the
        #      model invented is dropped;
        #   2. a specific SERIES is shown only if it exists in verified catalogue data (currently CLAF);
        #      for every other authorised line the suggestion stays at BRAND level with NO part number;
        #   3. a specific orderable MPN is emailed ONLY if a human confirmed that exact cross before.
        # This lets us say "we carry equivalent rectifiers from <brand>, exact part to follow" for any
        # of our lines, while never printing a part number we have not verified.
        if unauth:
            try:
                batch = self.claude.cross_reference_batch(
                    items=[{"mpn": it.get("mpn"), "manufacturer": it.get("manufacturer"),
                            "description": it.get("description")} for it, _ in unauth],
                    catalogue_text=self.cross_ref.catalogue_text(),
                    line_card_summary=self.line_card.generate_line_card_summary(),
                )
                for idx, (item, db_item) in enumerate(unauth):
                    alt = batch.get(str(idx)) or batch.get(idx)
                    if not alt or (alt.get("fit") or "none") == "none":
                        continue
                    brand, series = alt.get("brand"), alt.get("series")
                    if not brand:
                        continue
                    # GATE 1 (brand) — must be a brand we are genuinely authorised for, per the real
                    # line card (covers all lines) or verified catalogue. A made-up brand is dropped.
                    if not (self.line_card.is_authorized_brand(brand)
                            or self.cross_ref.has_brand_data(brand)):
                        logger.warning(f"Dropped cross for {item.get('mpn')}: "
                                       f"'{brand}' is not one of our authorised brands")
                        actions.append("cross_ref_dropped_unknown_brand")
                        continue
                    # GATE 2 (series) — show a specific series ONLY if it is in verified catalogue data;
                    # otherwise keep the suggestion at brand level (no fabricated part number).
                    verified_series = series if (series and self.cross_ref.series_exists(brand, series)) else None
                    db_item.authorized_brand = brand
                    db_item.alternative_notes = alt.get("reasoning")
                    if db_item.line_status == "red":
                        db_item.line_status = "amber"
                    entry = {
                        "original_mpn": item.get("mpn"),
                        "original_manufacturer": item.get("manufacturer"),
                        "brand": brand,
                        "series": verified_series,
                        "decoded": alt.get("decoded"),
                        "fit": alt.get("fit"),
                        "comparison_notes": alt.get("reasoning"),
                        "caveats": alt.get("caveats"),
                        "confidence": alt.get("confidence"),
                    }
                    # GATE 3 (exact MPN) — a specific orderable part number needs a human's prior
                    # confirmation; without it the suggestion stays at series or brand level.
                    confirmed = self.cross_ref.confirmed_cross(item.get("mpn"),
                                                               item.get("manufacturer"))
                    if confirmed and confirmed.get("our_mpn"):
                        db_item.alternative_mpn = confirmed["our_mpn"]
                        entry["suggested_mpn"] = confirmed["our_mpn"]
                        entry["confirmed_by"] = confirmed.get("confirmed_by")
                    else:
                        db_item.alternative_mpn = None
                        entry["suggested_mpn"] = None
                        if verified_series is None:
                            entry["brand_level"] = True          # authorised line, exact part on confirmation
                        else:
                            entry["needs_mpn_confirmation"] = True
                            actions.append("cross_ref_needs_mpn_confirmation")
                    alternatives.append(entry)
                logger.info(f"Cross-reference: {len(alternatives)} of {len(unauth)} unauthorised "
                            f"parts matched to an authorised line")
            except Exception as e:
                logger.warning(f"Batch cross-reference failed: {e}")

        actions.append(f"extracted_{len(bom_items)}_bom_items")

        # Reuse the customer info already extracted during lead capture (memoized) —
        # avoids a second identical LLM call per RFQ.
        customer_info = self._get_customer_info(email_data)
        customer_name = customer_info.get("name") or self._extract_name(email_data["from_email"])
        customer_company = customer_info.get("company") or ""

        # Draft acknowledgement email
        def _item_label(item):
            mpn = item.get('mpn')
            desc = item.get('description', '')
            # Precedence: without the parens this was `(mpn or "[desc]") if desc else "N/A"`, so a
            # line with an MPN but NO description wrongly showed "N/A" (hit when the inquiry table has
            # no Description column). MPN must win whenever it's present.
            label = mpn or (f"[{desc}]" if desc else "N/A")
            mfr = item.get('manufacturer', 'Unknown')
            qty = item.get('quantity', '?')
            pkg = f" ({item['package']})" if item.get('package') else ""
            return f"- {label} ({mfr}) x {qty} pcs{pkg}"

        items_summary = "\n".join(_item_label(item) for item in bom_items)

        # Infer the quote currency now (reused for the quotation below) so the acknowledgement can ASK
        # the customer which currency to quote in when they didn't state one.
        currency, currency_assumed, currency_source = self._infer_quote_currency(
            bom_items, email_data, customer_info)

        ack_body = self.claude.draft_acknowledgement(
            customer_name=customer_name,
            customer_company=customer_company,
            items_summary=items_summary,
            account=self.account,
        )
        # Ask for anything the customer didn't provide — quantity, make, currency (INR/USD), lead time —
        # so we can quote accurately (only shown when something is actually missing).
        missing_html = self._build_missing_info_html(bom_items, currency_assumed)
        if missing_html:
            ack_body = (ack_body or "") + missing_html
        # Then: which requested parts are on OUR authorised lines (we supply directly), and the Match %
        # alternatives for parts outside our range. Both go to the CUSTOMER here, not the team.
        if authorized_for_ack:
            ack_body = (ack_body or "") + self._build_authorized_html(authorized_for_ack)
        if alternatives:
            ack_body = (ack_body or "") + self._build_alternatives_html(alternatives)

        # Send acknowledgement to the customer (gated by AGENT_MODE — suppressed in testing mode)
        try:
            if self._send_customer_reply(
                thread_id=email_data["thread_id"],
                to_email=email_data["from_email"],
                subject=email_data["subject"],
                body_html=ack_body,
                kind="acknowledgement",
                cc_emails=self._customer_team_addrs(email_data),
            ):
                db_email.status = EmailStatus.ACKNOWLEDGED
                actions.append("sent_acknowledgement")
            else:
                actions.append("ack_suppressed_testing_mode")
        except Exception as e:
            logger.error(f"Failed to send acknowledgement: {e}")
            actions.append("ack_send_failed")

        # Create quotation record in AWAITING_PRICING status. Currency was inferred above and reused
        # here; if it was assumed (customer didn't state INR/USD) the acknowledgement already asked
        # them to confirm, and the quote is flagged for a human on the dashboard.
        if currency_assumed:
            logger.info(f"Currency for {customer_company or customer_name}: assumed {currency} ({currency_source})")
        quote_items = []
        for item in bom_items:
            quote_items.append({
                "mpn": item.get("mpn"),
                "manufacturer": item.get("manufacturer"),
                "quantity": item.get("quantity"),
                "description": item.get("description"),
                "package": item.get("package"),
                "annual_quantity": item.get("annual_quantity"),
                "needs_identification": item.get("needs_identification", False),
                "line_status": "green" if self.line_card.is_authorized_brand(item.get("manufacturer", "")) else "amber",
                "unit_price": None,  # To be filled by purchase team / quote analyst
                "lead_time": None,   # To be filled by purchase team
                "spq": None,         # Standard pack qty — filled from team pricing reply
                "moq": None,         # Filled from team pricing reply
                "pricing_notes": None,
            })

        # Generate quote number (v1.1 — Company B International only, "AE" prefix)
        quote_number = f"AE-Q-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{db_email.id:04d}"

        quotation = Quotation(
            email_id=db_email.id,
            quote_number=quote_number,
            customer_name=customer_name,
            customer_email=email_data["from_email"],
            customer_cc="; ".join(self._customer_team_addrs(email_data)),  # their team → CC on every reply for this quote
            customer_company=customer_company,
            account=self.account,
            currency=currency,
            currency_assumed=currency_assumed,
            currency_source=currency_source,
            status=QuoteStatus.AWAITING_PRICING,  # Wait for purchase team pricing
            line_items=quote_items,
            draft_email_body=None,  # No draft yet - will be created after pricing received
            validity_days=30,
        )
        session.add(quotation)

        db_email.status = EmailStatus.PROCESSING  # Not awaiting_approval yet, waiting for pricing
        actions.append("quotation_awaiting_pricing")
        actions.append(f"quote_number_{quote_number}")

        # ---- AUTONOMOUS: source pricing from external VENDORS (Phase 2) instead of internal teams ----
        if self.autonomous_enabled:
            try:
                from core.vendor_sourcing import VendorSourcer
                sourcer = VendorSourcer(self.gmail, self.account,
                                        automatic=self.automatic_mode,
                                        vendor_test_email=self.vendor_test_email,
                                        max_vendors=self.vendor_max_vendors,
                                        rfq_cc=self.vendor_rfq_cc)
                sent, unsourced = sourcer.send_rfqs(quotation, bom_items, session)
                quotation.status = QuoteStatus.SOURCING_VENDORS
                actions.append(f"sourced_to_{len(sent)}_vendors")
                if unsourced:
                    actions.append(f"{len(unsourced)}_parts_unsourced_no_vendor")
            except Exception as e:
                logger.error(f"Vendor sourcing failed: {e}")
                actions.append("vendor_sourcing_failed")
            return actions

        # Split BOM items into authorized (Product Team) and unauthorized (Purchase Team)
        # For description-only items (no manufacturer), check if the description matches
        # any product category in our line card (e.g., "DC-DC converter" → CLAF Power).
        authorized_items = []
        unauthorized_items = []
        for item in bom_items:
            manufacturer = item.get("manufacturer", "")
            description = item.get("description", "")

            if manufacturer and self.line_card.is_authorized_brand(manufacturer):
                authorized_items.append(item)
            elif not manufacturer and description:
                # Description-only item: check if description matches a line card product category.
                # If it does, route to product team (they can identify the exact part).
                product_matches = self.line_card.find_alternatives_by_product(
                    self.line_card._extract_keywords(description)
                )
                if product_matches:
                    item["_matched_brands"] = [m["name"] for m in product_matches[:3]]
                    authorized_items.append(item)
                    logger.info(f"Description-only item '{description[:50]}' matched line card brands: {item['_matched_brands']}")
                else:
                    unauthorized_items.append(item)
            else:
                unauthorized_items.append(item)

        logger.info(f"BOM split: {len(authorized_items)} authorized → Product Team, {len(unauthorized_items)} unauthorized → Purchase Team")

        # Forward AUTHORIZED items to Product Team
        if authorized_items and self.product_team_emails:
            try:
                result = self._forward_to_team(
                    team_type="product",
                    team_emails=self.product_team_emails,
                    customer_name=customer_name,
                    customer_company=customer_company,
                    customer_email=email_data["from_email"],
                    bom_items=authorized_items,
                    alternatives=[],
                    original_subject=email_data["subject"],
                    quote_number=quote_number,
                )
                quotation.product_thread_id = result.get("thread_id")
                quotation.product_message_id = result.get("message_id")
                quotation.product_forward_items = [i.get("mpn") for i in authorized_items]
                actions.append(f"forwarded_{len(authorized_items)}_authorized_to_product_team")
            except Exception as e:
                logger.error(f"Failed to forward to product team: {e}")
                actions.append("product_forward_failed")

        # Forward UNAUTHORIZED items to Purchase Team (Sourcing/Trading)
        if unauthorized_items and self.purchase_team_emails:
            try:
                result = self._forward_to_team(
                    team_type="purchase",
                    team_emails=self.purchase_team_emails,
                    customer_name=customer_name,
                    customer_company=customer_company,
                    customer_email=email_data["from_email"],
                    bom_items=unauthorized_items,
                    alternatives=alternatives,
                    original_subject=email_data["subject"],
                    quote_number=quote_number,
                )
                quotation.purchase_thread_id = result.get("thread_id")
                quotation.purchase_message_id = result.get("message_id")
                quotation.purchase_forward_items = [i.get("mpn") for i in unauthorized_items]
                actions.append(f"forwarded_{len(unauthorized_items)}_unauthorized_to_purchase_team")
            except Exception as e:
                logger.error(f"Failed to forward to purchase team: {e}")
                actions.append("purchase_forward_failed")

        # If all items are authorized but no product team configured, forward everything to purchase team
        if authorized_items and not self.product_team_emails and self.purchase_team_emails:
            try:
                result = self._forward_to_team(
                    team_type="purchase",
                    team_emails=self.purchase_team_emails,
                    customer_name=customer_name,
                    customer_company=customer_company,
                    customer_email=email_data["from_email"],
                    bom_items=bom_items,
                    alternatives=alternatives,
                    original_subject=email_data["subject"],
                    quote_number=quote_number,
                )
                quotation.purchase_thread_id = result.get("thread_id")
                quotation.purchase_message_id = result.get("message_id")
                quotation.purchase_forward_items = [i.get("mpn") for i in bom_items]
                actions.append("forwarded_all_to_purchase_team_no_product_team_configured")
            except Exception as e:
                logger.error(f"Failed to forward to purchase team: {e}")

        return actions

    def _forward_to_team(
        self,
        team_type: str,  # "product" or "purchase"
        team_emails: list[str],
        customer_name: str,
        customer_company: str,
        customer_email: str,
        bom_items: list,
        alternatives: list,
        original_subject: str,
        quote_number: str,
    ) -> dict:
        """Forward RFQ items to the appropriate team.
        - product team: authorized brands (50+ line card vendors)
        - purchase team: unauthorized brands (sourcing/trading)
        Returns dict with thread_id and message_id for tracking replies."""
        entity = "Company B International"

        if team_type == "product":
            team_label = "PRODUCT TEAM (Authorized Line)"
            action_text = "Get pricing from our authorized principals/vendors for these items."
            tag = "AUTHORIZED"
        else:
            team_label = "PURCHASE TEAM (Sourcing/Trading)"
            action_text = "Source pricing from open-market vendors for these items (not on our authorized line)."
            tag = "SOURCING"

        cur = "USD"
        cell = 'padding:6px;border:1px solid #dddddd;'
        th = 'padding:6px;border:1px solid #dddddd;text-align:left;'

        # Standard inquiry table:
        # MPN | Description | Make | Quantity | Price (cur) | SPQ | MOQ | Lead Time | Remark
        # (Price shows the customer's target if given; SPQ/MOQ/Lead Time are for the team to fill.)
        items_html = ""
        for item in bom_items:
            mpn_display = f'<strong>{html.escape(item.get("mpn"))}</strong>' if item.get("mpn") \
                else '<em style="color:#e65100;">(MPN to be identified)</em>'
            desc = html.escape(str(item.get("description") or "-"))
            make = html.escape(str(item.get("manufacturer") or "-"))
            qty = html.escape(str(item.get("quantity") or "-"))
            tp = item.get("target_price")
            price_cell = f'{cur} {html.escape(str(tp))}' if tp not in (None, "") else "-"
            remarks = []
            if item.get("annual_quantity"):
                remarks.append(f"Annual qty: {html.escape(str(item.get('annual_quantity')))}")
            if item.get("package"):
                remarks.append(f"Pkg: {html.escape(str(item.get('package')))}")
            if item.get("special_requirements"):
                remarks.append(html.escape(str(item.get("special_requirements"))))
            remark = "; ".join(remarks) or "-"
            items_html += (
                f'<tr>'
                f'<td style="{cell}">{mpn_display}</td>'
                f'<td style="{cell}">{desc}</td>'
                f'<td style="{cell}">{make}</td>'
                f'<td style="{cell}text-align:right;">{qty}</td>'
                f'<td style="{cell}text-align:right;">{price_cell}</td>'
                f'<td style="{cell}"></td>'   # SPQ — team to advise
                f'<td style="{cell}"></td>'   # MOQ — team to advise
                f'<td style="{cell}"></td>'   # Lead Time — team to advise
                f'<td style="{cell}">{remark}</td>'
                f'</tr>'
            )

        header = (
            f'<tr style="background:#0d47a1;color:#ffffff;">'
            f'<th style="{th}">MPN</th><th style="{th}">Description</th><th style="{th}">Make</th>'
            f'<th style="{th}">Quantity</th><th style="{th}">Price ({cur})</th><th style="{th}">SPQ</th>'
            f'<th style="{th}">MOQ</th><th style="{th}">Lead Time</th><th style="{th}">Remark</th></tr>'
        )

        # NOTE: "Suggested alternatives" intentionally NOT included here — those go to the
        # CUSTOMER in the acknowledgement email (see _build_alternatives_html), not to the team.

        body = f"""
        <div style="font-family:Arial,sans-serif;font-size:14px;color:#222;">
        <p><strong>{team_label} &mdash; RFQ auto-forwarded by SemiSales AI Agent</strong></p>
        <p><strong>Quote Reference:</strong> {html.escape(quote_number)}<br>
        <strong>Customer:</strong> {html.escape(customer_name)} ({html.escape(customer_email)})<br>
        <strong>Company:</strong> {html.escape(customer_company or 'Not specified')}<br>
        <strong>Original Subject:</strong> {html.escape(original_subject)}<br>
        <strong>Entity:</strong> {entity}</p>

        <table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px;">
        {header}{items_html}</table>

        <p style="margin-top:18px;padding:12px;background:#fff3e0;border-left:4px solid #f57c00;">
        <strong>ACTION REQUIRED &mdash; {action_text}</strong><br><br>
        <strong>Please advise, for each line item above:</strong><br>
        1. Available Quantity<br>
        2. Unit Price ({cur})<br>
        3. Lead Time<br>
        4. Date Code, Packaging, COO (Country of Origin)<br><br>
        Reply on <strong>this same email thread</strong> with the final selling price (cost + margin + expenses),
        plus SPQ, MOQ and Lead Time per MPN. The AI agent will pick up your reply and draft the customer
        quotation automatically.
        </p>

        <p style="color:#666;font-size:12px;">Automated message from the SemiSales AI Agent.
        Do not reply to the customer directly from this email.</p>
        </div>
        """

        subject = f"[{tag}] {quote_number} - {customer_company or customer_name} - {len(bom_items)} items"

        # Send ONE email with all team members (first in TO, rest in CC)
        all_recipients = ", ".join(team_emails)
        result = self.gmail.send_new_email(
            to_email=all_recipients,
            subject=subject,
            body_html=body,
        )
        thread_id = result.get("threadId")
        message_id = result.get("id")
        logger.info(f"Forwarded RFQ to {team_type} team (1 email, {len(team_emails)} recipients): {all_recipients} (thread: {thread_id})")

        return {"thread_id": thread_id, "message_id": message_id}

    def _get_customer_info(self, email_data: dict) -> dict:
        """Extract customer name/company/designation ONCE per email, memoized on email_data.
        The lead-capture step and the RFQ/PO handlers both need this; without caching each
        email pays two identical LLM calls (~11s wasted). Uses the HTML body as a fallback
        when plain text is too short, so every caller gets the better extraction."""
        cached = email_data.get("_customer_info")
        if cached is not None:
            return cached
        body = email_data.get("body_text") or ""
        if len(body.strip()) < 20:
            body = email_data.get("body_html") or body
        info = self.claude.extract_customer_info(
            from_field=email_data["from_email"],
            body=body,
        )
        email_data["_customer_info"] = info
        return info

    def _extract_and_store_lead(self, session, db_email: Email, email_data: dict, classification: dict):
        """Extract customer info and upsert into leads table."""
        from_email = email_data["from_email"]

        # Extract email address only
        if "<" in from_email:
            email_addr = from_email.split("<")[-1].replace(">", "").strip().lower()
        else:
            email_addr = from_email.strip().lower()

        if "@" not in email_addr:
            return

        # Extract customer info (memoized — one LLM call per email, shared with the RFQ/PO handlers)
        customer_info = self._get_customer_info(email_data)

        # Check if lead already exists
        lead = session.query(Lead).filter_by(email=email_addr).first()
        now = datetime.now(timezone.utc)

        if lead:
            # Update existing lead
            lead.last_contact_at = now
            lead.total_inquiries = (lead.total_inquiries or 0) + 1
            if customer_info.get("name") and not lead.name:
                lead.name = customer_info.get("name")
            if customer_info.get("company") and not lead.company:
                lead.company = customer_info.get("company")
            if customer_info.get("designation") and not lead.designation:
                lead.designation = customer_info.get("designation")
        else:
            # Create new lead
            # Infer country from domain
            country = "India"
            if email_addr.endswith(".sg") or email_addr.endswith(".com.sg"):
                country = "Singapore"
            elif email_addr.endswith(".com") and "india" not in email_addr:
                country = "Unknown"

            lead = Lead(
                name=customer_info.get("name"),
                company=customer_info.get("company"),
                designation=customer_info.get("designation"),
                email=email_addr,
                country=country,
                source="email",
                source_details=f"Auto-extracted from {self.account} inbox",
                first_contact_at=now,
                last_contact_at=now,
                total_inquiries=1,
                status="new",
            )
            session.add(lead)
            session.flush()
            logger.info(f"New lead created: {email_addr} ({customer_info.get('company', 'Unknown')})")

        # Store customer history entry
        history = CustomerHistory(
            customer_email=email_addr,
            lead_id=lead.id,
            email_id=db_email.id,
            interaction_type=classification.get("type", "other"),
            summary=email_data["subject"][:500],
            extracted_data=classification,
        )
        session.add(history)

    def _handle_team_pricing_reply(self, session, quotation: Quotation, email_data: dict) -> list[str]:
        """Handle a team member's reply on a tracked quotation thread.
        Extracts pricing directly from the reply body and drafts quotation immediately.
        Works for both initial pricing (AWAITING_PRICING) and revised pricing (AWAITING_REVISED_PRICING)."""
        actions = []
        is_revised = quotation.status == QuoteStatus.AWAITING_REVISED_PRICING

        logger.info(f"[{quotation.quote_number}] Processing team pricing reply ({'revised R' + str(quotation.negotiation_round) if is_revised else 'initial'}) from {email_data['from_email']}")

        # Get known MPNs from quotation
        known_mpns = [item.get("mpn", "") for item in (quotation.line_items or []) if item.get("mpn")]
        if not known_mpns:
            logger.warning(f"[{quotation.quote_number}] No known MPNs in quotation — cannot extract pricing")
            actions.append("no_known_mpns")
            return actions

        # Extract pricing from this email body
        body = email_data.get("body_text", "")
        html_body = email_data.get("body_html", "")

        # Use HTML body if plain text is too short or has table
        if (len(body.strip()) < 30 or "<table" in html_body.lower()) and html_body:
            from core.bom_extractor import html_to_clean_text
            cleaned = html_to_clean_text(html_body)
            if len(cleaned) > len(body):
                body = cleaned

        if not body or len(body.strip()) < 10:
            logger.warning(f"[{quotation.quote_number}] Team reply body too short — no pricing to extract")
            actions.append("reply_body_too_short")
            return actions

        pricing = self.claude.extract_pricing_from_reply(
            reply_body=body,
            known_mpns=known_mpns,
            currency=quotation.currency or "USD",
        )

        if not pricing or not pricing.get("has_pricing"):
            logger.info(f"[{quotation.quote_number}] Team reply has no pricing data (informational message)")
            actions.append("no_pricing_in_reply")
            return actions

        detected_currency = (pricing.get("currency") or "").upper().strip()

        # Initial / partial / additional pricing → shared partial-aware handler.
        # (Handles split-BOM: 3 items priced now, 2 pending; then the remaining 2 later.)
        if not is_revised:
            return self.apply_team_pricing(
                session, quotation, pricing.get("items", []),
                detected_currency, submitted_by=email_data.get("from_email", "team"),
            )

        # ---- Revised (negotiation) pricing path ----
        if detected_currency in ("INR", "USD", "EUR") and detected_currency != quotation.currency:
            logger.info(f"[{quotation.quote_number}] Currency updated from {quotation.currency} to {detected_currency}")
            quotation.currency = detected_currency

        line_items = [dict(it) for it in (quotation.line_items or [])]  # copy so JSON change is tracked
        pricing_map = {(p.get("mpn") or "").upper(): p for p in pricing.get("items", []) if p.get("mpn")}
        items_priced = 0
        for item in line_items:
            mpn_key = (item.get("mpn") or "").upper()
            if mpn_key in pricing_map:
                p = pricing_map[mpn_key]
                item["unit_price"] = p.get("unit_price")
                item["lead_time"] = p.get("lead_time")
                item["moq"] = p.get("moq")
                item["pricing_notes"] = p.get("notes")
                if p.get("unit_price") is not None:
                    items_priced += 1

        if items_priced == 0:
            logger.warning(f"[{quotation.quote_number}] Revised pricing extracted but no MPNs matched")
            actions.append("pricing_no_mpn_match")
            return actions

        quotation.line_items = line_items
        quotation.pricing_received_at = datetime.now(timezone.utc)
        quotation.pricing_submitted_by = email_data.get("from_email", "team")

        neg_round = session.query(NegotiationRound).filter_by(
            quotation_id=quotation.id,
            round_number=quotation.negotiation_round,
        ).first()
        if neg_round:
            neg_round.team_revised_prices = pricing.get("items", [])
            neg_round.revised_pricing_at = datetime.now(timezone.utc)

        quote_body = self.claude.draft_revised_quotation_email(
            customer_name=quotation.customer_name,
            customer_company=quotation.customer_company or "",
            account=quotation.account,
            line_items=line_items,
            currency=quotation.currency or "USD",
            validity_days=quotation.validity_days or 30,
            negotiation_round=quotation.negotiation_round,
        )
        quotation.draft_email_body = quote_body
        quotation.status = QuoteStatus.REVISED_AWAITING_APPROVAL
        actions.append(f"revised_pricing_R{quotation.negotiation_round}_{items_priced}_items")
        actions.append("revised_quotation_drafted")

        email_record = session.query(Email).filter_by(id=quotation.email_id).first()
        if email_record:
            email_record.status = EmailStatus.AWAITING_APPROVAL
        session.add(AuditLog(
            action="revised_pricing_received",
            entity_type="quotation",
            entity_id=quotation.id,
            details={
                "quote_number": quotation.quote_number,
                "items_priced": items_priced,
                "round": quotation.negotiation_round or 0,
                "from": email_data.get("from_email"),
            },
            performed_by="agent",
        ))
        logger.info(f"[{quotation.quote_number}] R{quotation.negotiation_round}: Revised quotation drafted ({items_priced} items) — awaiting approval")
        return actions

    def apply_team_pricing(self, session, quotation: Quotation, pricing_items: list,
                           detected_currency: str = "", submitted_by: str = "team") -> list:
        """Merge a batch of team pricing onto a quotation — PARTIAL-AWARE.

        - Applies unit_price/lead_time/moq/notes to matched line items.
        - Items priced in THIS round that were previously pending are flagged `newly_priced`
          so the drafted quote highlights them (green + "New") on an updated quote.
        - If some quotable items still have no price → status PARTIALLY_PRICED (partial quote,
          rest marked "Pending"); else → AWAITING_APPROVAL (complete quote).
        - Drafts the customer quotation HTML deterministically in code (correct prices,
          pending markers, and highlight — no LLM guessing). Sets email status + audit.

        Shared by the inbox reply handler and the background pricing monitor, so both behave
        identically. Idempotent: a reply that adds no new price leaves the quote unchanged.
        Returns a list of action strings.
        """
        actions = []
        if detected_currency in ("INR", "USD", "EUR") and detected_currency != quotation.currency:
            logger.info(f"[{quotation.quote_number}] Currency updated from {quotation.currency} to {detected_currency}")
            quotation.currency = detected_currency

        line_items = [dict(it) for it in (quotation.line_items or [])]  # copy so JSON change is tracked
        pricing_map = {(p.get("mpn") or "").upper(): p for p in (pricing_items or []) if p.get("mpn")}

        # A prior pricing round already happened → this is an UPDATE (highlight the new rows).
        is_update = quotation.pricing_received_at is not None

        for it in line_items:            # clear last round's highlight
            it["newly_priced"] = False

        priced_this_round = 0
        for it in line_items:
            key = (it.get("mpn") or "").upper()
            p = pricing_map.get(key)
            if p and p.get("unit_price") is not None:
                was_pending = it.get("unit_price") is None
                it["unit_price"] = p.get("unit_price")
                it["lead_time"] = p.get("lead_time")
                it["moq"] = p.get("moq")
                it["spq"] = p.get("spq")
                it["pricing_notes"] = p.get("notes")
                if was_pending:
                    priced_this_round += 1
                    if is_update:
                        it["newly_priced"] = True

        if priced_this_round == 0:
            logger.info(f"[{quotation.quote_number}] Pricing reply added no new prices (already priced / no MPN match) — no change")
            actions.append("pricing_no_new_items")
            return actions

        quotation.line_items = line_items   # reassign so SQLAlchemy tracks the JSON change
        quotation.pricing_received_at = datetime.now(timezone.utc)
        quotation.pricing_submitted_by = submitted_by

        quotable = [it for it in line_items if (it.get("mpn") or it.get("description"))]
        priced_total = sum(1 for it in quotable if it.get("unit_price") is not None)
        pending = [it for it in quotable if it.get("unit_price") is None]

        # Alternatives from BOM items (for the "suggested from our line" section)
        alternatives = []
        for bi in session.query(BOMItem).filter_by(email_id=quotation.email_id).all():
            if bi.authorized_brand and bi.alternative_mpn:
                alternatives.append({
                    "original_mpn": bi.mpn,
                    "brand": bi.authorized_brand,
                    "suggested_mpn": bi.alternative_mpn,
                    "comparison_notes": bi.alternative_notes or "",
                })

        quotation.draft_email_body = self._build_quotation_html(
            quotation=quotation,
            line_items=line_items,
            alternatives=alternatives,
            currency=quotation.currency or "USD",
            validity_days=quotation.validity_days or 30,
            is_update=is_update,
        )
        quotation.approved_email_body = None  # pricing changed → void any stale approval (see finalize)

        if pending:
            quotation.status = QuoteStatus.PARTIALLY_PRICED
            actions.append(f"partial_quotation_{priced_total}_of_{len(quotable)}_priced_{len(pending)}_pending")
        else:
            quotation.status = QuoteStatus.AWAITING_APPROVAL
            actions.append(f"quotation_{priced_total}_items_priced")
        actions.append("updated_quotation_drafted" if is_update else "quotation_drafted")

        email_record = session.query(Email).filter_by(id=quotation.email_id).first()
        if email_record:
            email_record.status = EmailStatus.AWAITING_APPROVAL

        session.add(AuditLog(
            action="pricing_received",
            entity_type="quotation",
            entity_id=quotation.id,
            details={
                "quote_number": quotation.quote_number,
                "priced_total": priced_total,
                "priced_this_round": priced_this_round,
                "pending": len(pending),
                "total_quotable": len(quotable),
                "is_update": is_update,
                "from": submitted_by,
            },
            performed_by="agent",
        ))
        logger.info(f"[{quotation.quote_number}] Pricing applied: {priced_total}/{len(quotable)} priced, "
                    f"{len(pending)} pending, {priced_this_round} new this round → status={quotation.status.value}")
        return actions

    def finalize_autonomous_quote(self, session, quotation: Quotation) -> list:
        """Phase 5 — confidence gate. Build the customer quotation from the consolidated line_items.
        If EVERY quotable line is confidently priced -> auto-send to the customer (AGENT_MODE-gated).
        If ANY line is pending / needs_review -> draft it and route to the dashboard for a human.
        Also respects auto_send_confidence (when off, even a full quote waits for human approval)."""
        actions = []
        line_items = [dict(it) for it in (quotation.line_items or [])]
        quotable = [it for it in line_items if it.get("mpn") or it.get("description")]
        priced = [it for it in quotable
                  if it.get("unit_price") is not None and it.get("pricing_status") == "priced"]
        unpriced = [it for it in quotable if it not in priced]

        # Confidence-gate guardrails — ANY of these holds the quote on the dashboard (never auto-send).
        # (Pending / needs-review / NO BID / packaging-mismatch / expired all land in `unpriced` already.)
        hold_reasons = []
        if unpriced:
            hold_reasons.append(f"{len(unpriced)} line(s) not confidently priced")
        # v1.1 — we AUTO-price USD only. A non-USD quote (SGD / EUR / INR) is held for a human; an
        # assumed-USD is fine to auto-send (USD is the standing International default).
        if (quotation.currency or "USD").upper() != "USD":
            hold_reasons.append(f"non-USD currency ({quotation.currency}) — auto-pricing is USD only; needs review")
        eud_lines = [it for it in quotable if it.get("eud_required")]
        if eud_lines:
            hold_reasons.append(f"{len(eud_lines)} line(s) require an End-User Declaration (dual-use)")
        auth_lines = [it for it in quotable if it.get("authenticity_review")]
        if auth_lines:
            hold_reasons.append(f"{len(auth_lines)} line(s) priced far below market — verify authenticity "
                                f"(CoC / stock-label) before sending")
        thin = [it for it in priced if it.get("margin_percent") is not None
                and it.get("margin_percent") < self.min_margin_percent]
        if thin:
            hold_reasons.append(f"{len(thin)} line(s) below the {self.min_margin_percent:.0f}% margin floor")
        total_val = sum((it.get("unit_price") or 0) * (it.get("quantity") or 0) for it in priced)
        if self.auto_send_max_value and total_val > self.auto_send_max_value:
            hold_reasons.append(f"quote value {total_val:,.0f} exceeds the auto-send ceiling "
                                f"{self.auto_send_max_value:,.0f}")

        all_confident = bool(quotable) and not hold_reasons

        # Suggested alternatives (from BOM items) — same section the manual path adds.
        alternatives = []
        for bi in session.query(BOMItem).filter_by(email_id=quotation.email_id).all():
            if bi.authorized_brand and bi.alternative_mpn:
                alternatives.append({
                    "original_mpn": bi.mpn,
                    "brand": bi.authorized_brand,
                    "suggested_mpn": bi.alternative_mpn,
                    "comparison_notes": bi.alternative_notes or "",
                })

        quotation.draft_email_body = self._build_quotation_html(
            quotation=quotation,
            line_items=line_items,
            alternatives=alternatives,
            currency=quotation.currency or "USD",
            validity_days=quotation.validity_days or 30,
            is_update=quotation.sent_at is not None,  # already sent once (e.g. a partial) → mark as an update
        )
        # The pricing just changed (re-sourced / re-consolidated / negotiated). Any earlier human
        # approval was for the OLD price, so void it — otherwise the dashboard send falls back to the
        # stale approved_email_body and the customer receives the previous (wrong) price. Forcing
        # re-approval guarantees they see and confirm the new figure. (Root cause of the R1 bug.)
        quotation.approved_email_body = None

        email_record = session.query(Email).filter_by(id=quotation.email_id).first()
        thread_id = email_record.thread_id if email_record else None

        if all_confident and self.auto_send_confidence:
            # Attempt auto-send (itself suppressed in AGENT_MODE=testing).
            sent = self._send_customer_reply(
                thread_id=thread_id,
                # Customer subject must NOT carry the internal quote_number (AE-Q-...) — they confuse
                # it with a part number. Reuse their own subject; reply threads by thread_id.
                to_email=quotation.customer_email,
                subject=(email_record.subject if email_record and email_record.subject else "Quotation"),
                body_html=quotation.draft_email_body,
                kind="quotation",
                cc_emails=_addrs(quotation.customer_cc),  # their team, persisted from the original inquiry
            )
            if sent:
                quotation.status = QuoteStatus.SENT
                quotation.sent_at = datetime.now(timezone.utc)
                if email_record:
                    email_record.status = EmailStatus.SENT
                actions.append("quote_auto_sent_confidence_gate")
                logger.info(f"[{quotation.quote_number}] CONFIDENCE GATE PASS ({len(priced)} lines) → auto-sent to customer")
            else:
                quotation.status = QuoteStatus.AWAITING_APPROVAL
                if email_record:
                    email_record.status = EmailStatus.AWAITING_APPROVAL
                actions.append("quote_ready_send_suppressed_testing_mode")
                logger.info(f"[{quotation.quote_number}] Confident quote ready but AGENT_MODE=testing → dashboard")
        else:
            quotation.status = QuoteStatus.AWAITING_APPROVAL
            if email_record:
                email_record.status = EmailStatus.AWAITING_APPROVAL
            if hold_reasons:
                actions.append("escalated_to_dashboard: " + "; ".join(hold_reasons))
                logger.info(f"[{quotation.quote_number}] CONFIDENCE GATE HOLD ({len(priced)} priced) → "
                            + "; ".join(hold_reasons) + " → dashboard for human")
            else:
                actions.append("quote_ready_awaiting_approval")

        session.add(AuditLog(
            action="autonomous_quote_finalized",
            entity_type="quotation",
            entity_id=quotation.id,
            details={"quote_number": quotation.quote_number, "priced": len(priced),
                     "unpriced": len(unpriced), "all_confident": all_confident,
                     "hold_reasons": hold_reasons, "status": quotation.status.value},
            performed_by="agent",
        ))
        return actions

    @staticmethod
    def _entity_for_currency(currency: str) -> str:
        """v1.1 — International: the customer is ALWAYS quoted under Company B International
        Pte Ltd (Singapore). (The India/Company A entity was removed in this build.)"""
        return "Company B Pte Ltd"

    def _build_quotation_html(self, quotation: Quotation, line_items: list, alternatives: list,
                              currency: str, validity_days: int, is_update: bool) -> str:
        """Deterministically build the customer quotation email body (no LLM).
        Renders priced rows with unit price + line total, pending rows marked
        'Quotation to follow shortly', and newly-priced rows highlighted green + 'New'."""
        # Which legal entity the customer sees is driven by the QUOTE CURRENCY, not the inbox account:
        # INR is billed by Company A (Pune), USD by Company B International (Singapore).
        entity = self._entity_for_currency(currency)
        cust = html.escape(quotation.customer_name or "Customer")
        from core.consolidation import is_internal_remark

        # A revised quote (post-negotiation) shows the customer the progression Last Quoted → Your
        # Target → Revised Price. Detected by any line carrying the previous price we captured during
        # negotiation.
        is_revision = any(it.get("prev_unit_price") is not None for it in line_items)
        tag_label = "Revised" if is_revision else "New"

        def money(v):
            try:
                return f"{currency} {float(v):,.2f}"
            except (TypeError, ValueError):
                return f"{currency} {html.escape(str(v))}"

        cell = 'padding:6px;border:1px solid #dddddd;'
        rows = ""
        grand = 0.0
        has_pending = False
        has_new = False
        has_no_bid = False
        has_eud = False
        for it in line_items:
            if not (it.get("mpn") or it.get("description")):
                continue
            mpn = html.escape(str(it.get("mpn") or "-"))
            desc = html.escape(str(it.get("description") or "-"))
            make = html.escape(str(it.get("manufacturer") or "-"))
            qty = it.get("quantity")
            qty_disp = html.escape(str(qty)) if qty not in (None, "") else "-"
            spq_disp = html.escape(str(it.get("spq"))) if it.get("spq") not in (None, "") else "-"
            moq_disp = html.escape(str(it.get("moq"))) if it.get("moq") not in (None, "") else "-"
            dc_disp = html.escape(str(it.get("date_code"))) if it.get("date_code") not in (None, "") else "-"
            lead = html.escape(str(it.get("lead_time") or "-"))
            # Prefer the consolidation Remark (exact / cross-brand / split / availability), fall back
            # to any team pricing note — but NEVER surface an internal negotiation/cost note to the
            # customer (defence-in-depth safety net on top of the source fixes above).
            remark_txt = it.get("remark") or it.get("pricing_notes")
            if is_internal_remark(remark_txt):
                remark_txt = ""
            note = html.escape(str(remark_txt)) if remark_txt else ""
            # Comparison cells (revised quotes only): what we quoted before + the customer's target.
            if is_revision:
                pv = it.get("prev_unit_price")
                tpc = it.get("customer_target_price")
                extra_cells = (
                    f'<td style="{cell}text-align:right;color:#777;">{money(pv) if pv is not None else "-"}</td>'
                    f'<td style="{cell}text-align:right;color:#777;">{money(tpc) if tpc is not None else "-"}</td>'
                )
            else:
                extra_cells = ""
            up = it.get("unit_price")
            status = (it.get("pricing_status") or "").lower()
            newly = bool(it.get("newly_priced"))
            if up is not None:
                try:
                    grand += float(up) * float(qty)
                except (TypeError, ValueError):
                    pass
                price_cell = money(up)
                if newly:
                    has_new = True
                    row_style = ' style="background:#e8f7e9;"'
                    remark_cell = f'<span style="background:#2e7d32;color:#ffffff;font-size:11px;padding:2px 6px;border-radius:3px;">{tag_label}</span>'
                    remark_cell += (' ' + note) if note else ''
                else:
                    row_style = ''
                    remark_cell = note or '-'
            elif status == "no_bid":
                has_no_bid = True
                row_style = ' style="background:#fdecea;"'
                price_cell = '<em style="color:#c62828;">No Bid</em>'
                spq_disp = moq_disp = lead = '-'
                remark_cell = note or '<em style="color:#c62828;">No bid — please verify the part number</em>'
            else:
                has_pending = True
                row_style = ' style="background:#fff8e1;"'
                price_cell = '<em style="color:#b26a00;">Pending</em>'
                spq_disp = moq_disp = lead = '-'
                remark_cell = note or '<em style="color:#b26a00;">Quotation to follow shortly</em>'
            if it.get("eud_required"):
                has_eud = True
                base = '' if remark_cell in ('-', '') else (remark_cell + ' ')
                remark_cell = base + ('<span style="background:#8e24aa;color:#ffffff;font-size:10px;'
                                      'padding:1px 5px;border-radius:3px;">EUD</span>')
            rows += (
                f'<tr{row_style}>'
                f'<td style="{cell}"><strong>{mpn}</strong></td>'
                f'<td style="{cell}">{desc}</td>'
                f'<td style="{cell}">{make}</td>'
                f'<td style="{cell}text-align:right;">{qty_disp}</td>'
                f'{extra_cells}'
                f'<td style="{cell}text-align:right;">{price_cell}</td>'
                f'<td style="{cell}text-align:right;">{spq_disp}</td>'
                f'<td style="{cell}text-align:right;">{moq_disp}</td>'
                f'<td style="{cell}">{lead}</td>'
                f'<td style="{cell}">{dc_disp}</td>'
                f'<td style="{cell}">{remark_cell}</td>'
                f'</tr>'
            )

        th = 'padding:6px;border:1px solid #dddddd;text-align:left;'
        # Revised quotes gain Last Quoted + Your Target columns and rename Price → Revised Price so the
        # customer sees the full progression (Last Quoted → Your Target → Revised Price).
        extra_headers = (
            f'<th style="{th}">Last Quoted ({currency})</th><th style="{th}">Your Target ({currency})</th>'
            if is_revision else ''
        )
        price_header = f'Revised Price ({currency})' if is_revision else f'Price ({currency})'
        header = (
            f'<tr style="background:#0d47a1;color:#ffffff;">'
            f'<th style="{th}">MPN</th><th style="{th}">Description</th><th style="{th}">Make</th>'
            f'<th style="{th}">Quantity</th>{extra_headers}<th style="{th}">{price_header}</th><th style="{th}">SPQ</th>'
            f'<th style="{th}">MOQ</th><th style="{th}">Lead Time</th><th style="{th}">D/C</th>'
            f'<th style="{th}">Remark</th></tr>'
        )

        if is_revision:
            update_note = (
                '<p style="padding:8px 12px;background:#e8f7e9;border-left:4px solid #2e7d32;">'
                'This is a <strong>revised quotation</strong> against the target prices you shared. '
                'For each line you can see your <strong>Last Quoted</strong> price, <strong>Your Target</strong>, '
                'and our <strong>Revised Price</strong>. Revised lines are highlighted in green.</p>'
            )
        elif is_update and has_new:
            update_note = (
                '<p style="padding:8px 12px;background:#e8f7e9;border-left:4px solid #2e7d32;">'
                'This is an <strong>updated quotation</strong>. Items newly quoted since the previous '
                'version are highlighted in green and tagged <strong>New</strong>.</p>'
            )
        else:
            update_note = ''
        total_note = (
            f'<p style="margin-top:12px;"><strong>Estimated total'
            f'{" (quoted items only)" if has_pending else ""}: {money(grand)}</strong>'
            f'{" — excludes items still pending" if has_pending else ""}</p>'
        )
        pending_note = (
            '<p style="padding:8px 12px;background:#fff8e1;border-left:4px solid #f57c00;">'
            'Pricing for the item(s) marked <em style="color:#b26a00;">Pending</em> is being '
            'finalised and will be shared shortly in a follow-up quotation.</p>'
            if has_pending else ''
        )
        no_bid_note = (
            '<p style="padding:8px 12px;background:#fdecea;border-left:4px solid #c62828;">'
            'The item(s) marked <em style="color:#c62828;">No Bid</em> could not be sourced against the '
            'part number supplied — please re-confirm the exact MPN/manufacturer so we can quote them.</p>'
            if has_no_bid else ''
        )
        eud_note = (
            '<p style="padding:8px 12px;background:#f3e5f5;border-left:4px solid #8e24aa;">'
            'Item(s) tagged <span style="background:#8e24aa;color:#ffffff;font-size:10px;padding:1px 5px;'
            'border-radius:3px;">EUD</span> may be controlled (dual-use) and will require a signed '
            '<strong>End-User Declaration</strong> before dispatch. Our team will share the format on order confirmation.</p>'
            if has_eud else ''
        )

        alt_html = ''
        if alternatives:
            alt_rows = ''
            for alt in alternatives:
                alt_rows += (
                    f'<tr><td style="padding:6px;border:1px solid #dddddd;">{html.escape(str(alt.get("original_mpn") or "-"))}</td>'
                    f'<td style="padding:6px;border:1px solid #dddddd;">{html.escape(str(alt.get("brand") or "-"))}</td>'
                    f'<td style="padding:6px;border:1px solid #dddddd;">{html.escape(str(alt.get("suggested_mpn") or "-"))}</td>'
                    f'<td style="padding:6px;border:1px solid #dddddd;">{html.escape(str(alt.get("comparison_notes") or ""))}</td></tr>'
                )
            alt_html = (
                '<h4 style="margin-top:16px;">Suggested alternatives from our authorised line</h4>'
                '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px;">'
                f'<tr style="background:#f0f0f0;"><th style="{th}">Original MPN</th><th style="{th}">Our Brand</th>'
                f'<th style="{th}">Alt MPN</th><th style="{th}">Notes</th></tr>{alt_rows}</table>'
            )

        return (
            f'<div style="font-family:Arial,sans-serif;font-size:14px;color:#222;">'
            f'<p>Dear {cust},</p>'
            f'<p>Thank you for your enquiry. Please find our quotation below from <strong>{entity}</strong> '
            f'(Ref: {html.escape(quotation.quote_number or "")}).</p>'
            f'{update_note}'
            f'<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px;">'
            f'{header}{rows}</table>'
            f'{total_note}{pending_note}{no_bid_note}{eud_note}{alt_html}'
            f'<p style="margin-top:12px;padding:10px 12px;background:#f5f7fa;border-left:4px solid #0d47a1;font-size:13px;color:#333;">'
            f'<strong>Terms:</strong> Prices in {currency}, per piece, ex-tax &middot; <strong>Ex-Works Singapore</strong> '
            f'&middot; payment <strong>T/T</strong> &middot; goods <strong>100% new &amp; original</strong> (RoHS / Pb-free) '
            f'&middot; lead time is EXW Singapore (includes inbound transit) '
            f'&middot; quotation valid for <strong>{validity_days} days</strong>. '
            f'Datasheets and stock-label photos (date code + packing) available on request.</p>'
            f'<p>We look forward to your confirmation.</p>'
            f'<p>Best regards,<br>{entity} Sales Team</p>'
            f'</div>'
        )

    def _build_alternatives_html(self, alternatives: list) -> str:
        """Customer-facing 'Suggested alternatives from our authorised line' table.

        No Match % is shown — an approximate percentage misleads (the customer decides fit from the
        specs in Notes + the datasheet), so we present the comparison and let their engineer judge.

        Three shapes of row, and the difference is deliberate:
          - a human-confirmed cross prints the exact part number;
          - a catalogue-verified match prints the SERIES and says the exact part number follows on
            confirmation;
          - any other authorised line is offered at BRAND level ("equivalent from our line, exact
            part on confirmation") with NO part number.
        That escalation is not hedging for its own sake — a catalogue table proves a series exists,
        and stating a specific MPN we have not verified is how a fabricated part number reached a
        customer before. The brand itself is always a real authorised brand (gated by the caller).
        """
        if not alternatives:
            return ""
        cell = 'padding:6px;border:1px solid #dddddd;'
        th = 'padding:6px;border:1px solid #dddddd;text-align:left;'
        rows = ""
        any_pending = False
        for alt in alternatives:
            mpn = alt.get("suggested_mpn")
            series = alt.get("series")
            if mpn:
                offer = f'<strong>{html.escape(str(mpn))}</strong>'
            elif series:
                any_pending = True
                offer = (f'<strong>{html.escape(str(series))}</strong> series'
                         '<br><span style="font-size:11px;color:#666;">exact part no. on confirmation</span>')
            elif alt.get("brand"):
                # Brand-level match from one of our authorised lines — no verified part number yet.
                any_pending = True
                offer = ('Equivalent from our line'
                         '<br><span style="font-size:11px;color:#666;">exact part no. on confirmation</span>')
            else:
                continue  # no real brand to offer — never print a blank suggestion
            note = html.escape(str(alt.get("comparison_notes") or ""))
            if alt.get("fit") == "partial":
                # Say it plainly rather than burying it in the notes — a partial cross that reads
                # like a drop-in is exactly how a customer ends up with the wrong part.
                note = "<strong>Close match, not a drop-in.</strong> " + note
            caveats = str(alt.get("caveats") or "")
            if caveats:
                note += f'<br><em style="color:#666;">To verify: {html.escape(caveats)}</em>'
            rows += (
                f'<tr>'
                f'<td style="{cell}">{html.escape(str(alt.get("original_mpn") or "-"))}</td>'
                f'<td style="{cell}">{html.escape(str(alt.get("original_manufacturer") or "-"))}</td>'
                f'<td style="{cell}"><strong>{html.escape(str(alt.get("brand") or "-"))}</strong></td>'
                f'<td style="{cell}">{offer}</td>'
                f'<td style="{cell}">{note}</td>'
                f'</tr>'
            )
        if not rows:
            return ""
        pending_note = (
            '<p style="font-size:12px;color:#666;">Where a series is shown, our engineer will '
            'confirm the exact part number against the datasheet before we quote.</p>'
        ) if any_pending else ''
        return (
            '<div style="font-family:Arial,sans-serif;font-size:13px;color:#222;margin-top:18px;">'
            '<h4 style="margin-bottom:4px;">Suggested alternatives from our authorised line</h4>'
            '<p style="margin-top:0;">Some of the requested parts are outside our authorised range. '
            'We can offer the following equivalents from our own line for your engineer to review '
            '(see Notes for the matching specifications):</p>'
            '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;">'
            f'<tr style="background:#0d47a1;color:#ffffff;">'
            f'<th style="{th}">Requested MPN</th><th style="{th}">Requested Make</th>'
            f'<th style="{th}">Our Brand</th><th style="{th}">Our Equivalent</th>'
            f'<th style="{th}">Notes</th></tr>'
            f'{rows}</table>'
            f'{pending_note}'
            '<p style="font-size:12px;color:#666;">Please confirm if you would like us to quote any of these alternatives. Datasheets available on request.</p>'
            '</div>'
        )

    def _build_target_ack_html(self, customer_name: str, currency: str, target_items: list) -> str:
        """Acknowledge a customer's target price on the quotation thread while we re-source it.
        Reassures them we're working to match it and a revised quote is coming — so they're never
        left silent during the re-sourcing window."""
        cust = html.escape(str(customer_name or "there").split()[0] if customer_name else "there")
        rows = ""
        for t in (target_items or []):
            tp = t.get("target_price")
            if not (t.get("mpn") and tp not in (None, "")):
                continue
            cell = 'padding:6px;border:1px solid #dddddd;'
            rows += (f'<tr><td style="{cell}"><strong>{html.escape(str(t.get("mpn")))}</strong></td>'
                     f'<td style="{cell}text-align:right;">{html.escape(str(currency))} '
                     f'{html.escape(str(tp))}</td></tr>')
        table = ""
        if rows:
            th = 'padding:6px;border:1px solid #dddddd;text-align:left;'
            table = ('<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:10px 0;">'
                     f'<tr style="background:#0d47a1;color:#ffffff;"><th style="{th}">MPN</th>'
                     f'<th style="{th}">Your target</th></tr>' + rows + '</table>')
        return (
            '<div style="font-family:Arial,sans-serif;font-size:14px;color:#222;line-height:1.5;">'
            f'<p>Dear {cust},</p>'
            '<p>Thank you for sharing your target price. We have noted the following and are '
            '<strong>checking with our sources to match it</strong>:</p>'
            f'{table}'
            '<p>We will revert with a <strong>revised quotation</strong> shortly. We appreciate your '
            'business and will do our best to meet your target.</p>'
            '<p>Best regards,<br>Sales Department</p>'
            '</div>'
        )

    def _build_missing_info_html(self, bom_items: list, currency_assumed: bool) -> str:
        """Ask the customer for the details they didn't provide (quantity, make, currency, lead time)
        so we can quote accurately. Only the genuinely-missing fields are asked; returns "" when the
        inquiry already carried everything. Make is only asked when part-lookup couldn't identify it,
        so we never re-ask for something we already resolved from Mouser."""
        items = bom_items or []
        ask = []
        # Quantity — asked if ANY line has no usable quantity.
        def _has_qty(it):
            q = it.get("quantity")
            try:
                return q is not None and float(q) > 0
            except (TypeError, ValueError):
                return bool(str(q).strip()) and str(q).strip() != "?"
        if any(not _has_qty(it) for it in items):
            ask.append("the <strong>quantity</strong> required for each part")
        # Make / manufacturer — only if we STILL don't know it after part-lookup.
        if any(not (it.get("manufacturer") or "").strip() for it in items):
            ask.append("the <strong>manufacturer / make</strong> (if you have a preference)")
        # v1.1 — Company B International quotes in USD by default, so we don't ask the customer to pick a
        # currency (a customer needing SGD/EUR states it, and that quote is held for review).
        if not ask:
            return ""
        # Lead time is always useful to confirm once we're already asking something.
        ask.append("your <strong>required delivery date / lead time</strong>")
        lis = "".join(f'<li style="margin-bottom:4px;">{a}</li>' for a in ask)
        return (
            '<div style="font-family:Arial,sans-serif;font-size:13.5px;color:#222;margin-top:16px;'
            'padding:12px 15px;background:#fff8e1;border-left:4px solid #f57c00;">'
            '<p style="margin:0 0 6px;"><strong>To prepare an accurate quotation, could you please confirm:</strong></p>'
            f'<ul style="margin:0;padding-left:20px;">{lis}</ul>'
            '<p style="margin:8px 0 0;font-size:12px;color:#666;">If a quantity isn&rsquo;t specified, we&rsquo;ll '
            'quote at the standard pack quantity (SPQ) / minimum order quantity (MOQ).</p>'
            '</div>'
        )

    def _build_authorized_html(self, items: list) -> str:
        """Customer-facing 'we can supply these directly' block for the parts the customer inquired
        about that fall on OUR authorised lines — the up-front genuineness/traceability signal."""
        if not items:
            return ""
        cell = 'padding:6px;border:1px solid #dddddd;'
        th = 'padding:6px;border:1px solid #dddddd;text-align:left;'
        rows = ""
        for it in items:
            mpn = it.get("mpn") or it.get("description") or "N/A"
            rows += (
                f'<tr>'
                f'<td style="{cell}"><strong>{html.escape(str(mpn))}</strong></td>'
                f'<td style="{cell}">{html.escape(str(it.get("manufacturer") or "-"))}</td>'
                f'<td style="{cell}text-align:right;">{html.escape(str(it.get("quantity") or "?"))}</td>'
                f'</tr>'
            )
        return (
            '<div style="font-family:Arial,sans-serif;font-size:13px;color:#222;margin-top:18px;">'
            '<h4 style="margin-bottom:4px;color:#2e7d32;">Parts from our authorised lines &mdash; we can supply these directly</h4>'
            '<p style="margin-top:0;">You are covered on the following parts by our <strong>authorised distribution</strong>: '
            'genuine product, fully traceable, with the latest date codes. We will quote these directly from the principal:</p>'
            '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;">'
            f'<tr style="background:#2e7d32;color:#ffffff;">'
            f'<th style="{th}">MPN</th><th style="{th}">Make</th>'
            f'<th style="{th}" >Qty</th></tr>'
            f'{rows}</table>'
            '</div>'
        )

    def _infer_quote_currency(self, bom_items: list, email_data: dict, customer_info: dict):
        """v1.1 — Company B International: we AUTO-price in USD. Detect a currency the customer explicitly
        states (USD/SGD/EUR/INR); a non-USD currency is returned as-is so the confidence gate holds the
        quote for a human (we don't auto-convert SGD/EUR). Default is USD. Returns (currency, assumed,
        source); `assumed=True` flags a one-click confirm on the dashboard."""
        # 1) Explicit currency captured per line.
        for it in bom_items:
            cur = (it.get("currency") or "").strip().upper()
            if cur in ("USD", "SGD", "EUR", "INR"):
                return cur, False, f"stated:{cur.lower()}"

        # 2) A currency token / symbol in the customer's text (check S$/SGD and € before the bare $).
        blob = " ".join(f"{it.get('target_price') or ''} {it.get('special_requirements') or ''}"
                        for it in bom_items)
        blob += " " + (email_data.get("body_text") or "")[:2000]
        low = blob.lower()
        if ("s$" in low) or ("sgd" in low) or ("singapore dollar" in low):
            return "SGD", False, "symbol:sgd"
        if ("€" in blob) or ("eur" in low) or ("euro" in low):
            return "EUR", False, "symbol:eur"
        if ("₹" in blob) or ("inr" in low) or ("rs." in low) or (" rs " in low) or ("rupee" in low):
            return "INR", False, "symbol:inr"
        if ("$" in blob) or ("usd" in low) or ("us$" in low) or ("dollar" in low):
            return "USD", False, "symbol:usd"

        # 3) Default to USD (International).
        return "USD", True, "default:usd"

    def _expired_quote_lines(self, quotation) -> set:
        """Line keys (mpn or description) whose winning vendor quote validity has lapsed as of now.
        Used at PO time so we never commit a purchase at a cost the vendor no longer honours."""
        now = datetime.now(timezone.utc)
        expired = set()
        for it in (quotation.line_items or []):
            raw = it.get("quote_valid_until")
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(str(raw))
            except (ValueError, TypeError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)  # stored ISO may be naive
            if dt < now:
                key = it.get("mpn") or it.get("description")
                if key:
                    expired.add(key)
        return expired

    def _handle_vendor_quote_reply(self, quotation, email_data: dict, session) -> dict:
        """A vendor replied to our RFQ (matched by the AE-Q-… number in the subject), often OFF-THREAD or
        from a different colleague. Parse it like a human: a price → VendorQuote row(s); an explicit
        decline → mark that vendor No-Bid. Label the mail 'Quotation' and mark read. This keeps a real
        quote (or a No-Bid) from being lost / mis-read as a 'vendor stock offer' just because it arrived
        on a fresh thread (real cases: Kimter's quote, WT's 'no bid')."""
        from core.database import VendorRFQ, VendorQuote, Vendor
        from core.vendor_sourcing import _to_float, _to_int
        actions = []
        frm = (email_data.get("from_email") or "").lower()

        def _dom(e):
            return (e or "").lower().split("@")[-1].replace(">", "").strip()
        sender_dom = _dom(frm)

        # Match the sender to a vendor we RFQ'd on this quote — by the RFQ email's domain, else the vendor
        # record's domain (a colleague from the same company counts).
        rfqs = session.query(VendorRFQ).filter_by(quotation_id=quotation.id).all()
        vrfq = None
        for r in rfqs:
            if r.vendor_email and _dom(r.vendor_email) and _dom(r.vendor_email) == sender_dom:
                vrfq = r
                break
        if vrfq is None:
            for r in rfqs:
                v = session.get(Vendor, r.vendor_id) if r.vendor_id else None
                if v and v.email and _dom(v.email) == sender_dom:
                    vrfq = r
                    break

        # Read the reply (prefer cleaned HTML when the plain body is thin).
        body = email_data.get("body_text") or ""
        if (len(body.strip()) < 20 or "<table" in (email_data.get("body_html") or "").lower()) \
                and email_data.get("body_html"):
            from core.bom_extractor import html_to_clean_text
            cleaned = html_to_clean_text(email_data["body_html"])
            if len(cleaned) > len(body):
                body = cleaned

        known = [it.get("mpn") for it in (quotation.line_items or []) if it.get("mpn")]
        pricing = None
        try:
            pricing = self.claude.extract_pricing_from_reply(
                reply_body=body, known_mpns=known, currency=(quotation.currency or "USD"))
        except Exception as e:
            logger.warning(f"[{quotation.quote_number}] off-thread vendor-quote parse failed: {e}")

        if pricing and pricing.get("has_pricing") and vrfq:
            det_cur = (pricing.get("currency") or quotation.currency or "USD").upper()
            n = 0
            for it in pricing.get("items", []):
                cost = _to_float(it.get("unit_price"))
                if cost is None:
                    continue
                session.add(VendorQuote(
                    quotation_id=quotation.id, vendor_rfq_id=vrfq.id, vendor_id=vrfq.vendor_id,
                    vendor_name=vrfq.vendor_name, mpn=it.get("mpn"), cost_price=cost, currency=det_cur,
                    moq=_to_int(it.get("moq")), spq=_to_int(it.get("spq")),
                    offered_qty=_to_int(it.get("available_qty")), lead_time=it.get("lead_time"),
                    packaging=(it.get("packaging") or None), date_code=(it.get("date_code") or None),
                    notes=it.get("notes")))
                n += 1
            if n:
                vrfq.status = "replied"
                vrfq.replied_at = datetime.now(timezone.utc)
                actions.append(f"vendor_quote_captured_offthread_{n}")
                logger.info(f"[{quotation.quote_number}] OFF-THREAD quote from {frm} → {n} cost quote(s) "
                            f"captured for {vrfq.vendor_name}")
            else:
                actions.append("vendor_reply_no_priced_lines")
        elif vrfq:
            low = body.lower()
            if any(w in low for w in ("no bid", "no-bid", "not authorised", "not authorized", "no stock",
                                      "cannot quote", "can't quote", "unable to quote", "no offer",
                                      "we decline", "not able to")):
                vrfq.status = "no_bid"
                vrfq.replied_at = datetime.now(timezone.utc)
                actions.append("vendor_no_bid_recorded")
                logger.info(f"[{quotation.quote_number}] {vrfq.vendor_name} declined (No-Bid) off-thread")
            else:
                actions.append("vendor_reply_no_pricing")
        else:
            actions.append("vendor_quote_unmatched_vendor")

        try:
            self.gmail.add_label(email_data["gmail_id"], "SemiSales/Quotation")
            self.gmail.mark_as_read(email_data["gmail_id"])
        except Exception as e:
            logger.warning(f"Could not label/read vendor quote reply: {e}")

        session.commit()
        return {"email_id": None, "type": "vendor_quote",
                "actions_taken": actions or ["vendor_quote_reply"]}

    def _handle_vendor_po_reply(self, vpo, email_data: dict, session) -> dict:
        """A VENDOR replied to one of OUR purchase orders (AE-PO-…). WE placed the order, so read their
        reply the way a human would and update the vendor-PO status — accepted → confirmed, dispatched
        → shipped, delivered → received, or CANCELLED if they decline (e.g. 'no stock, can't process').
        On a decline (or an unclear reply) we flag the order for a human to re-source. We NEVER send a
        customer-style 'thank you for your PO' back to a supplier."""
        # Prefer cleaned HTML when the plain body is thin (same approach as the negotiation handler).
        body = email_data.get("body_text") or ""
        html_body = email_data.get("body_html") or ""
        if (len(body.strip()) < 40 or "<table" in html_body.lower()) and html_body:
            from core.bom_extractor import html_to_clean_text
            cleaned = html_to_clean_text(html_body)
            if len(cleaned) > len(body):
                body = cleaned

        try:
            verdict = self.claude.classify_po_response(
                subject=email_data.get("subject", ""), body=body[:3000], po_number=vpo.po_number or "")
        except Exception as e:
            logger.error(f"[{vpo.po_number}] Could not classify vendor PO reply: {e}")
            verdict = {"status": "unclear", "reason": ""}

        raw = (verdict.get("status") or "unclear").strip().lower()
        reason = (verdict.get("reason") or "").strip()
        STATUS_MAP = {
            "confirmed": "confirmed", "accepted": "confirmed", "acknowledged": "confirmed",
            "dispatched": "shipped", "shipped": "shipped",
            "delivered": "received", "received": "received",
            "declined": "cancelled", "rejected": "cancelled", "no_stock": "cancelled", "cancelled": "cancelled",
        }
        new_status = STATUS_MAP.get(raw)
        actions = []

        if new_status:
            vpo.status = new_status
            actions.append(f"vendor_po_{new_status}")
            logger.info(f"[{vpo.po_number}] Vendor {vpo.vendor_name} replied → PO '{new_status}'"
                        + (f" — {reason}" if reason else ""))
        else:
            actions.append("vendor_po_reply_unclear")
            logger.info(f"[{vpo.po_number}] Vendor {vpo.vendor_name} reply unclear — PO left 'placed' for review")

        # A supplier who can't fulfil (or an ambiguous reply) needs a human to re-source — flag the
        # parent order on the dashboard. We do NOT auto-place a replacement PO (a PO is a real commitment).
        if new_status == "cancelled" or not new_status:
            quote = session.query(Quotation).filter_by(id=vpo.quotation_id).first()
            if quote:
                quote.status = QuoteStatus.AWAITING_APPROVAL
            session.add(AuditLog(
                action="vendor_po_declined" if new_status == "cancelled" else "vendor_po_reply_unclear",
                entity_type="vendor_po", entity_id=vpo.id,
                details={"po_number": vpo.po_number, "vendor": vpo.vendor_name,
                         "reason": reason, "raw_status": raw},
                performed_by="agent"))
            actions.append("flagged_for_human_resource")

        try:
            self.gmail.mark_as_read(email_data["gmail_id"])
            self.gmail.add_label(email_data["gmail_id"], "SemiSales/Internal")
        except Exception as e:
            logger.warning(f"Could not label vendor PO reply: {e}")
        session.commit()
        logger.info(f"Vendor PO reply on {vpo.po_number} from {email_data.get('from_email')} — "
                    f"handled as supplier response (NOT a customer PO)")
        return {"email_id": None, "type": "vendor_po_reply", "actions_taken": actions}

    def _handle_po(self, session, db_email: Email, email_data: dict) -> list[str]:
        """Handle PO emails: acknowledge, find linked quotation, forward to same team threads for processing."""
        actions = []

        # Reuse the memoized customer info (avoids a second identical LLM call)
        customer_info = self._get_customer_info(email_data)
        customer_name = customer_info.get("name") or self._extract_name(email_data["from_email"])

        # Send auto-acknowledgement for PO
        entity = "Company B International"
        ack_html = f"""<p>Dear {customer_name},</p>
<p>Thank you for your Purchase Order. We have received it and our team is reviewing it now.</p>
<p>We will confirm the order details, delivery schedule, and any discrepancies (if found) within the next few hours.</p>
<p>Best regards,<br>{entity} Sales Team</p>"""

        try:
            if self._send_customer_reply(
                thread_id=email_data["thread_id"],
                to_email=email_data["from_email"],
                subject=email_data["subject"],
                body_html=ack_html,
                kind="PO acknowledgement",
                cc_emails=self._customer_team_addrs(email_data),
            ):
                actions.append("sent_po_acknowledgement")
            else:
                actions.append("po_ack_suppressed_testing_mode")
        except Exception as e:
            logger.error(f"Failed to send PO ack: {e}")

        # Find the linked quotation (same logic as negotiation)
        quotation = None

        if email_data.get("thread_id"):
            orig_email = session.query(Email).filter(
                Email.thread_id == email_data["thread_id"],
                Email.email_type == EmailType.RFQ,
            ).first()
            if orig_email:
                quotation = session.query(Quotation).filter_by(email_id=orig_email.id).first()

        if not quotation:
            import re
            match = re.search(r'(AE|Company A)-Q-\d{8}-\d{4}', email_data.get("subject", ""))
            if match:
                quotation = session.query(Quotation).filter_by(quote_number=match.group(0)).first()

        if not quotation:
            from_email = email_data["from_email"]
            if "<" in from_email:
                from_email = from_email.split("<")[-1].replace(">", "").strip().lower()
            quotation = session.query(Quotation).filter(
                Quotation.customer_email.ilike(f"%{from_email}%"),
                Quotation.status.in_([
                    QuoteStatus.SENT, QuoteStatus.FOLLOW_UP_1, QuoteStatus.FOLLOW_UP_2,
                    QuoteStatus.REVISED_SENT,
                ]),
            ).order_by(Quotation.created_at.desc()).first()

        if not quotation:
            # No linked quotation — still forward PO to team but as standalone
            logger.warning("PO email but no matching quotation found - escalating to human")
            db_email.status = EmailStatus.ESCALATED
            actions.append("po_no_quote_found_escalated")
            return actions

        logger.info(f"PO linked to quotation {quotation.quote_number}")

        # ---- AUTONOMOUS: place POs to the winning vendors (instead of the internal CSR forward) ----
        if self.autonomous_enabled:
            # Quote-validity guard: if any line's winning vendor quote has expired since we quoted,
            # do NOT commit a PO at a stale cost — re-source those lines first.
            expired_lines = self._expired_quote_lines(quotation)
            if expired_lines:
                items = [dict(it) for it in (quotation.line_items or [])]
                for it in items:
                    if (it.get("mpn") or it.get("description")) in expired_lines:
                        it.update(unit_price=None, pricing_status="needs_review",
                                  pricing_note="Vendor quote expired before PO — re-sourcing",
                                  remark="Vendor quote expired — re-sourcing")
                quotation.line_items = items
                quotation.status = QuoteStatus.SOURCING_VENDORS
                db_email.status = EmailStatus.PROCESSING
                actions.append(f"po_held_{len(expired_lines)}_expired_quotes_resourcing")
                logger.warning(f"[{quotation.quote_number}] PO held — {len(expired_lines)} vendor quote(s) "
                               f"expired; re-sourcing before committing")
                session.add(AuditLog(action="po_held_expired_quotes", entity_type="quotation",
                                     entity_id=quotation.id,
                                     details={"quote_number": quotation.quote_number,
                                              "expired_lines": list(expired_lines)},
                                     performed_by="agent"))
                return actions
            try:
                from core.order_processing import OrderProcessor
                op = OrderProcessor(self.gmail, self.account, automatic=self.automatic_mode,
                                    vendor_test_email=self.vendor_test_email,
                                    oversight_cc=self.vendor_rfq_cc)
                placed, err = op.place_vendor_pos(quotation, session, customer_po_ref=email_data.get("subject"))
                if placed:
                    actions.append(f"placed_{len(placed)}_vendor_pos")
                elif err:
                    actions.append(f"vendor_po_{err}")
                    logger.warning(f"[{quotation.quote_number}] No vendor POs placed: {err}")
            except Exception as e:
                logger.error(f"Vendor PO placement failed: {e}")
                actions.append("vendor_po_failed")
            quotation.status = QuoteStatus.PO_FORWARDED
            db_email.status = EmailStatus.PROCESSING
            from_email = email_data["from_email"]
            if "<" in from_email:
                from_email = from_email.split("<")[-1].replace(">", "").strip().lower()
            lead = session.query(Lead).filter_by(email=from_email).first()
            if lead:
                lead.total_orders = (lead.total_orders or 0) + 1
                lead.status = "customer"
            session.add(AuditLog(action="vendor_pos_placed", entity_type="quotation",
                                 entity_id=quotation.id, details={"quote_number": quotation.quote_number},
                                 performed_by="agent"))
            return actions

        # Build PO forwarding email
        customer_company = quotation.customer_company or ""
        po_body = email_data["body_text"] or ""
        if len(po_body.strip()) < 30:
            from core.bom_extractor import html_to_clean_text
            po_body = html_to_clean_text(email_data.get("body_html") or "")

        attachment_list = ""
        if email_data.get("attachment_names"):
            attachment_list = "<br>".join(f"📎 {name}" for name in email_data["attachment_names"])

        forward_body = f"""
        <p><strong>🎉 PURCHASE ORDER RECEIVED — Please Create Sales Order</strong></p>
        <p><strong>Quote Reference:</strong> {quotation.quote_number}</p>
        <p><strong>Customer:</strong> {customer_name} ({quotation.customer_email})</p>
        <p><strong>Company:</strong> {customer_company or 'Not specified'}</p>
        <p><strong>Entity:</strong> {entity}</p>
        <p><strong>Currency:</strong> {quotation.currency}</p>
        {f'<p><strong>Attachments:</strong><br>{attachment_list}</p>' if attachment_list else ''}

        <h3>PO Details from Customer</h3>
        <div style="padding: 12px; background: #f5f5f5; border: 1px solid #ddd; margin: 10px 0;">
            {po_body[:2000]}
        </div>

        <h3>Original Quoted Items</h3>
        <table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse; width: 100%;">
            <tr style="background: #e8f5e9;">
                <th>#</th>
                <th>MPN</th>
                <th>Manufacturer</th>
                <th>Qty</th>
                <th>Unit Price ({quotation.currency})</th>
            </tr>
            {''.join(f"""<tr>
                <td>{i}</td>
                <td><strong>{item.get('mpn', 'N/A')}</strong></td>
                <td>{item.get('manufacturer', 'N/A')}</td>
                <td>{item.get('quantity', '-')}</td>
                <td>{item.get('unit_price', '-')}</td>
            </tr>""" for i, item in enumerate(quotation.line_items or [], 1))}
        </table>

        <p style="margin-top: 20px; padding: 12px; background: #e8f5e9; border-left: 4px solid #4caf50;">
        <strong>ACTION REQUIRED:</strong> Please create a Sales Order and process this PO.<br>
        Verify quantities and prices against our quotation {quotation.quote_number}.<br>
        Confirm delivery schedule with the customer.
        </p>
        """

        # Forward PO to CSR team (new email — CSR creates Sales Order)
        if self.csr_team_emails:
            try:
                all_csr = ", ".join(self.csr_team_emails)
                self.gmail.send_new_email(
                    to_email=all_csr,
                    subject=f"[PO RECEIVED] {quotation.quote_number} - {customer_company or customer_name} - Please Create Sales Order",
                    body_html=forward_body,
                )
                actions.append("po_forwarded_to_csr_team")
                logger.info(f"PO forwarded to CSR team: {all_csr}")
            except Exception as e:
                logger.error(f"Failed to forward PO to CSR team: {e}")
                actions.append("csr_forward_failed")
        else:
            # Fallback: forward to product/purchase team if no CSR configured
            if quotation.product_thread_id:
                try:
                    self.gmail.send_reply(
                        thread_id=quotation.product_thread_id,
                        to_email=", ".join(self.product_team_emails),
                        subject=f"[PO RECEIVED] {quotation.quote_number} - {customer_company or customer_name}",
                        body_html=forward_body,
                    )
                    actions.append("po_forwarded_to_product_team_no_csr")
                except Exception as e:
                    logger.error(f"Failed to forward PO to product team: {e}")
            if quotation.purchase_thread_id:
                try:
                    self.gmail.send_reply(
                        thread_id=quotation.purchase_thread_id,
                        to_email=", ".join(self.purchase_team_emails),
                        subject=f"[PO RECEIVED] {quotation.quote_number} - {customer_company or customer_name}",
                        body_html=forward_body,
                    )
                    actions.append("po_forwarded_to_purchase_team_no_csr")
                except Exception as e:
                    logger.error(f"Failed to forward PO to purchase team: {e}")

        # Update quotation status
        quotation.status = QuoteStatus.PO_RECEIVED
        db_email.status = EmailStatus.PROCESSING

        # Update lead stats
        from_email = email_data["from_email"]
        if "<" in from_email:
            from_email = from_email.split("<")[-1].replace(">", "").strip().lower()
        lead = session.query(Lead).filter_by(email=from_email).first()
        if lead:
            lead.total_orders = (lead.total_orders or 0) + 1
            lead.status = "customer"

        session.add(AuditLog(
            action="po_forwarded_to_team",
            entity_type="quotation",
            entity_id=quotation.id,
            details={
                "quote_number": quotation.quote_number,
                "customer": quotation.customer_email,
                "has_attachments": email_data.get("has_attachments", False),
            },
            performed_by="agent",
        ))

        actions.append(f"po_linked_to_{quotation.quote_number}")
        return actions

    def _handle_vendor_offer(self, session, db_email: Email, email_data: dict) -> list[str]:
        """A VENDOR pushed an unsolicited stock offer at us. Capture the offered lines as standing
        market intel (MarketOffer rows) — do NOT reply. Surfaced later when a customer asks for a
        matching part (grounded market-awareness — real vendor prices over time, not LLM-invented)."""
        from core.database import MarketOffer, Vendor

        def _f(v):
            try:
                return float(str(v).replace(",", "").strip())
            except (TypeError, ValueError):
                return None

        def _i(v):
            try:
                return int(float(str(v).replace(",", "").strip()))
            except (TypeError, ValueError):
                return None

        # Prefer cleaned HTML when the plain body is thin (offer lists are usually HTML tables).
        body = email_data.get("body_text") or ""
        html_body = email_data.get("body_html") or ""
        if (len(body.strip()) < 60 or "<table" in html_body.lower()) and html_body:
            from core.bom_extractor import html_to_clean_text
            cleaned = html_to_clean_text(html_body)
            if len(cleaned) > len(body):
                body = cleaned

        try:
            offer = self.claude.extract_vendor_offer(subject=email_data.get("subject", ""), body=body)
        except Exception as e:
            logger.error(f"Vendor-offer extraction failed: {e}")
            offer = {"items": []}

        # Vendor identity from the sender; link to a known Vendor row if we have one.
        raw_from = email_data.get("from_email", "") or ""
        vemail = (raw_from.split("<")[-1].replace(">", "").strip().lower() if "<" in raw_from
                  else raw_from.strip().lower())
        vname = self._extract_name(raw_from)
        vrow = session.query(Vendor).filter(Vendor.email.ilike(f"%{vemail}%")).first() if vemail else None

        stored = 0
        for it in (offer.get("items") or []):
            mpn = (it.get("mpn") or "").strip()
            price = _f(it.get("unit_price"))
            if not mpn or price is None:
                continue
            session.add(MarketOffer(
                mpn=mpn.upper(), manufacturer=(it.get("manufacturer") or None),
                vendor_id=(vrow.id if vrow else None),
                vendor_name=(vrow.name if vrow else vname), vendor_email=vemail or None,
                unit_price=price, currency=(it.get("currency") or "USD").upper(),
                date_code=(it.get("date_code") or None), lead_time=(it.get("lead_time") or None),
                moq=_i(it.get("moq")), offered_qty=_i(it.get("quantity")),
                packaging=(it.get("packaging") or None),
                source_gmail_id=email_data.get("gmail_id"), source_thread_id=email_data.get("thread_id"),
            ))
            stored += 1

        db_email.status = EmailStatus.PROCESSING
        try:
            self.gmail.mark_as_read(email_data["gmail_id"])
            self.gmail.add_label(email_data["gmail_id"], "SemiSales/Internal")
        except Exception as e:
            logger.warning(f"Could not label vendor offer: {e}")
        logger.info(f"Vendor stock-offer from {vemail or vname}: captured {stored} market-intel line(s) — no reply sent")
        return [f"captured_{stored}_market_offers"]

    def _handle_complaint(self, session, db_email: Email, email_data: dict) -> list[str]:
        """Handle complaints: send holding reply and escalate immediately."""
        customer_name = self._extract_name(email_data["from_email"])
        entity = "Company B International"

        holding_html = f"""<p>Dear {customer_name},</p>
<p>Thank you for reaching out. We take your concern very seriously.</p>
<p>Your message has been escalated to our senior team and you will receive a detailed response shortly.</p>
<p>We sincerely apologize for any inconvenience.</p>
<p>Best regards,<br>{entity} Sales Team</p>"""

        sent = False
        try:
            sent = self._send_customer_reply(
                thread_id=email_data["thread_id"],
                to_email=email_data["from_email"],
                subject=email_data["subject"],
                body_html=holding_html,
                kind="complaint holding reply",
                cc_emails=self._customer_team_addrs(email_data),
            )
        except Exception as e:
            logger.error(f"Failed to send complaint holding reply: {e}")

        db_email.status = EmailStatus.ESCALATED
        return ["sent_holding_reply" if sent else "holding_reply_suppressed_testing_mode",
                "escalated_complaint_to_human"]

    def _negotiate_autonomous(self, session, db_email: Email, quotation: Quotation, neg_round,
                              target_items: list, currency: str) -> list[str]:
        """Autonomous negotiation against a customer's target prices — reasons like a human buyer.

        Per line with a target:
          - If we can meet it by trimming margin down to (not below) the negotiation floor, quote AT
            the target (we absorb it ourselves; no need to trouble the vendor).
          - Otherwise the vendor cost is too high: gauge how hard the customer is pushing (their % off
            our last quote), then ask the vendor to sharpen THEIR OWN cost by a sensible, CAPPED amount
            (never demand a blind lowball verbatim). When the vendor's improved cost comes back,
            consolidation flexes our margin within [floor, default] to give our best price.
        The customer's target price / our margin are NEVER shared with any vendor or team — the vendor
        only ever sees a percentage ask on their own last cost."""
        actions = []
        floor = self.negotiation_min_margin

        def _f(v):
            try:
                return float(str(v).replace(",", "").replace(currency, "").replace("$", "").strip())
            except (TypeError, ValueError, AttributeError):
                return None

        tp_map = {}
        for t in (target_items or []):
            k = (t.get("mpn") or "").strip().upper()
            tp = _f(t.get("target_price"))
            if k and tp:
                tp_map[k] = tp

        items = [dict(it) for it in (quotation.line_items or [])]
        met = 0
        resource_parts = []
        for it in items:
            tp = tp_map.get((it.get("mpn") or "").strip().upper())
            if tp is None:
                continue
            landed = it.get("landed_cost") or it.get("cost_price")
            if not landed or landed <= 0:
                continue
            implied_margin = (tp / landed - 1.0) * 100.0
            prev_price = it.get("unit_price")   # what we quoted BEFORE this revision (customer-facing)
            if implied_margin >= floor:
                # Meet the target by trimming margin (never below the floor). Quote AT the target.
                # NOTE: `remark` is CUSTOMER-FACING — never put our margin/buy-cost in it. The internal
                # detail goes to `internal_note`; prev price + their target drive the comparison columns.
                it.update(margin_percent=round(implied_margin, 2), unit_price=round(tp, 4),
                          newly_priced=True, negotiated=True, pricing_status="priced",
                          prev_unit_price=prev_price, customer_target_price=round(tp, 4),
                          remark="Revised price",
                          internal_note=f"met target by margin trim (margin now {implied_margin:.0f}%)")
                met += 1
            else:
                # Target needs a margin below our floor at the CURRENT vendor cost -> we must get the
                # vendor cheaper. Think like a human buyer instead of back-solving a rigid figure:
                #  1. See how hard the customer is pushing (their % off our last quote).
                #  2. Ask the vendor for the cost that would let us keep our NORMAL (default) margin at
                #     the customer's target — but CAP the ask (max_vendor_ask): a blind lowball (e.g.
                #     35% below our quote) is never demanded of a vendor verbatim; we ask for at most
                #     ~20% off their own last cost. What the vendor can't give, our margin flex absorbs.
                #  3. When the vendor's improved cost returns, consolidation flexes margin floor..default
                #     to give our best price (see _apply_negotiation_margin).
                base_cost = it.get("cost_price") or landed
                default_m = self.default_margin
                cust_discount_pct = ((prev_price - tp) / prev_price * 100.0) if prev_price else None
                # Cost that preserves our default margin at the customer's target (landed ∝ vendor cost).
                ideal_cost_default = (tp / (1.0 + default_m / 100.0)) * (base_cost / landed)
                needed_reduction_pct = (base_cost - ideal_cost_default) / base_cost * 100.0 if base_cost else 0.0
                ask_pct = round(max(1.0, min(needed_reduction_pct, self.max_vendor_ask)), 1)
                target_cost = round(base_cost * (1.0 - ask_pct / 100.0), 4)
                resource_parts.append({
                    "mpn": it.get("mpn"), "description": it.get("description"),
                    "manufacturer": it.get("manufacturer"), "quantity": it.get("quantity"),
                    "_target_cost": target_cost, "_ask_pct": ask_pct,
                    "_prev_vendor_cost": round(base_cost, 4),
                })
                # remark stays blank (customer never sees the re-sourcing note); prev/target retained
                # so that when the vendor's cheaper cost comes back and this line is re-priced, the
                # customer quote can still show Last Quoted / Your Target / Revised Price.
                disc = f"{cust_discount_pct:.0f}% below our quote" if cust_discount_pct is not None else "a sharper price"
                it.update(unit_price=None, newly_priced=False, negotiated=False,
                          pricing_status="needs_review",
                          prev_unit_price=prev_price, customer_target_price=round(tp, 4),
                          remark="",
                          internal_note=(f"customer pushing {disc}; asked vendor for ~{ask_pct:.0f}% off "
                                         f"(target cost {it.get('cost_currency') or 'USD'} {target_cost})"))

        quotation.line_items = items
        neg_round.team_revised_prices = [{"mpn": it.get("mpn"), "unit_price": it.get("unit_price"),
                                          "margin_percent": it.get("margin_percent")}
                                         for it in items if it.get("negotiated")]
        neg_round.revised_pricing_at = datetime.now(timezone.utc)

        # Re-source the unmet lines at OUR target cost (customer's target is never included).
        if resource_parts:
            try:
                from core.vendor_sourcing import VendorSourcer
                sourcer = VendorSourcer(self.gmail, self.account, automatic=self.automatic_mode,
                                        vendor_test_email=self.vendor_test_email,
                                        max_vendors=self.vendor_max_vendors,
                                        rfq_cc=self.vendor_rfq_cc)
                # Reply on each vendor's EXISTING quote thread (one mail trail per vendor), not a
                # fresh RFQ each round.
                sent, _unsourced = sourcer.resend_target_cost(quotation, resource_parts, session)
                quotation.status = QuoteStatus.SOURCING_VENDORS  # wait for cheaper vendor costs
                db_email.status = EmailStatus.PROCESSING
                actions.append(f"renegotiation_resourced_{len(sent)}_lines_at_our_target_cost")
                logger.info(f"[{quotation.quote_number}] Renegotiation: {met} met by margin trim, "
                            f"{len(resource_parts)} re-sourced at our target cost (customer target NOT shared)")
                # Acknowledge the customer's target price on the SAME quotation thread, so they know
                # we're working on it and a revised quote is coming (we don't leave them silent while
                # we re-source vendors). Gated by AGENT_MODE like every other customer send.
                ack_thread = getattr(db_email, "thread_id", None)
                if ack_thread and quotation.customer_email:
                    try:
                        ack = self._build_target_ack_html(quotation.customer_name, currency, target_items)
                        if self._send_customer_reply(
                                thread_id=ack_thread, to_email=quotation.customer_email,
                                subject=getattr(db_email, "subject", None) or "Quotation",
                                body_html=ack, kind="target_price_ack",
                                cc_emails=_addrs(quotation.customer_cc) or _addrs(getattr(db_email, "cc", None))):
                            actions.append("sent_target_price_ack")
                    except Exception as e:
                        logger.error(f"Target-price acknowledgement failed: {e}")
            except Exception as e:
                logger.error(f"Renegotiation re-source failed: {e}")
                actions.append("renegotiation_resource_failed")
            return actions

        # Pure margin-trim case — build + send/hold the revised quote via the confidence gate.
        if met:
            quotation.pricing_received_at = datetime.now(timezone.utc)
            actions.extend(self.finalize_autonomous_quote(session, quotation))
            actions.append(f"renegotiation_met_{met}_lines_by_margin_trim")
            logger.info(f"[{quotation.quote_number}] Renegotiation: met {met} line(s) by trimming margin to target")
        else:
            quotation.status = QuoteStatus.AWAITING_APPROVAL
            db_email.status = EmailStatus.AWAITING_APPROVAL
            actions.append("negotiation_no_targets_matched_escalated")
        return actions

    def _handle_negotiation(self, session, db_email: Email, email_data: dict) -> list[str]:
        """Handle price negotiations: extract target prices, forward to same team threads."""
        actions = []

        # Find the original quotation by matching thread_id or quote number in subject
        quotation = None

        # Try 1: Match by thread — customer replies on same thread as our quotation
        if email_data.get("thread_id"):
            orig_email = session.query(Email).filter(
                Email.thread_id == email_data["thread_id"],
                Email.email_type == EmailType.RFQ,
            ).first()
            if orig_email:
                quotation = session.query(Quotation).filter_by(email_id=orig_email.id).first()

        # Try 2: Match by quote number in subject (AE-Q-XXXXXXXX-XXXX or CA-Q-...)
        if not quotation:
            import re
            match = re.search(r'(AE|Company A)-Q-\d{8}-\d{4}', email_data.get("subject", ""))
            if match:
                quotation = session.query(Quotation).filter_by(quote_number=match.group(0)).first()

        # Try 3: Match by customer email + most recent sent quotation
        if not quotation:
            from_email = email_data["from_email"]
            if "<" in from_email:
                from_email = from_email.split("<")[-1].replace(">", "").strip().lower()
            quotation = session.query(Quotation).filter(
                Quotation.customer_email.ilike(f"%{from_email}%"),
                Quotation.status.in_([
                    QuoteStatus.SENT, QuoteStatus.FOLLOW_UP_1, QuoteStatus.FOLLOW_UP_2,
                    QuoteStatus.REVISED_SENT,
                ]),
            ).order_by(Quotation.created_at.desc()).first()

        if not quotation:
            logger.warning("Negotiation email but no matching quotation found - escalating to human")
            db_email.status = EmailStatus.ESCALATED
            actions.append("negotiation_no_quote_found_escalated")
            return actions

        logger.info(f"Negotiation linked to quotation {quotation.quote_number}")

        # Extract target prices from customer's email
        known_mpns = [item.get("mpn", "") for item in (quotation.line_items or []) if item.get("mpn")]

        # Use HTML body if plain text is short (customer tables often in HTML)
        body_for_extraction = email_data["body_text"] or ""
        html_body = email_data.get("body_html") or ""
        if (len(body_for_extraction.strip()) < 50 or "<table" in html_body.lower()) and html_body:
            from core.bom_extractor import html_to_clean_text
            cleaned = html_to_clean_text(html_body)
            if len(cleaned) > len(body_for_extraction):
                body_for_extraction = cleaned

        target_data = self.claude.extract_target_prices(
            subject=email_data["subject"],
            body=body_for_extraction,
            known_mpns=known_mpns,
            currency=quotation.currency or "USD",
        )

        # Update negotiation round
        new_round = (quotation.negotiation_round or 0) + 1
        quotation.negotiation_round = new_round

        # Store negotiation round record
        neg_round = NegotiationRound(
            quotation_id=quotation.id,
            round_number=new_round,
            customer_email_id=db_email.id,
            customer_target_prices=target_data.get("items", []),
        )
        session.add(neg_round)
        session.flush()

        actions.append(f"negotiation_round_{new_round}_for_{quotation.quote_number}")

        # AUTONOMOUS negotiation: handle it ourselves — cut margin toward the target, and only
        # re-source (at OUR target cost) if the target needs less than our floor margin. The
        # customer's target price is NEVER shared with a vendor or team.
        if self.autonomous_enabled:
            return actions + self._negotiate_autonomous(
                session, db_email, quotation, neg_round,
                target_data.get("items", []), target_data.get("currency") or quotation.currency or "USD")

        # ---- INTERNAL-TEAM path (non-autonomous): forward the customer's target prices to the team ----
        # Build the forwarding email with customer's target prices
        customer_name = quotation.customer_name or self._extract_name(email_data["from_email"])
        customer_company = quotation.customer_company or ""
        entity = "Company B International"

        target_items = target_data.get("items", [])
        currency = target_data.get("currency") or quotation.currency or "USD"
        customer_message = target_data.get("customer_message", "Customer requesting revised pricing")

        # Build HTML table of customer's target prices
        items_html = ""
        for i, item in enumerate(target_items, 1):
            items_html += f"""
            <tr>
                <td>{i}</td>
                <td><strong>{item.get('mpn', 'N/A')}</strong></td>
                <td>{item.get('quantity', '-')}</td>
                <td>{currency} {item.get('current_price', '-')}</td>
                <td style="color: #d32f2f; font-weight: bold;">{currency} {item.get('target_price', '-')}</td>
                <td>{item.get('notes', '-')}</td>
            </tr>"""

        # If no specific target items extracted, include the raw customer message
        if not target_items:
            items_html = f"""
            <tr><td colspan="6" style="padding: 12px;">
                <em>Customer message:</em><br>{body_for_extraction[:500]}
            </td></tr>"""

        forward_body = f"""
        <p><strong>⚠️ NEGOTIATION ROUND {new_round} — Customer Target Prices</strong></p>
        <p><strong>Quote Reference:</strong> {quotation.quote_number}</p>
        <p><strong>Customer:</strong> {customer_name} ({quotation.customer_email})</p>
        <p><strong>Company:</strong> {customer_company or 'Not specified'}</p>
        <p><strong>Entity:</strong> {entity}</p>
        <p><strong>Customer says:</strong> {customer_message}</p>

        <h3>Customer's Target Prices</h3>
        <table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse; width: 100%;">
            <tr style="background: #fff3e0;">
                <th>#</th>
                <th>MPN</th>
                <th>Qty</th>
                <th>Our Quoted Price</th>
                <th>Customer Target</th>
                <th>Notes</th>
            </tr>
            {items_html}
        </table>

        <p style="margin-top: 20px; padding: 12px; background: #ffebee; border-left: 4px solid #d32f2f;">
        <strong>ACTION REQUIRED:</strong> Please review the customer's target prices and reply on this thread with your revised selling prices.<br>
        If you can match or get closer to the target, provide the updated price per MPN.<br>
        The AI agent will automatically pick up the revised pricing and draft a revised quotation.
        </p>
        """

        # Reply on the SAME product team thread
        if quotation.product_thread_id:
            try:
                self.gmail.send_reply(
                    thread_id=quotation.product_thread_id,
                    to_email=", ".join(self.product_team_emails),
                    subject=f"[NEGOTIATION R{new_round}] {quotation.quote_number} - Customer Target Prices",
                    body_html=forward_body,
                )
                actions.append("target_prices_forwarded_to_product_team")
                logger.info(f"Target prices forwarded to product team on existing thread {quotation.product_thread_id}")
            except Exception as e:
                logger.error(f"Failed to forward negotiation to product team: {e}")
                actions.append("product_team_forward_failed")

        # Reply on the SAME purchase team thread
        if quotation.purchase_thread_id:
            try:
                self.gmail.send_reply(
                    thread_id=quotation.purchase_thread_id,
                    to_email=", ".join(self.purchase_team_emails),
                    subject=f"[NEGOTIATION R{new_round}] {quotation.quote_number} - Customer Target Prices",
                    body_html=forward_body,
                )
                actions.append("target_prices_forwarded_to_purchase_team")
                logger.info(f"Target prices forwarded to purchase team on existing thread {quotation.purchase_thread_id}")
            except Exception as e:
                logger.error(f"Failed to forward negotiation to purchase team: {e}")
                actions.append("purchase_team_forward_failed")

        # Update quotation status — now waiting for team's revised prices
        quotation.status = QuoteStatus.AWAITING_REVISED_PRICING
        neg_round.forwarded_at = datetime.now(timezone.utc)

        db_email.status = EmailStatus.PROCESSING

        session.add(AuditLog(
            action="negotiation_forwarded_to_team",
            entity_type="quotation",
            entity_id=quotation.id,
            details={
                "quote_number": quotation.quote_number,
                "round": new_round,
                "target_items": len(target_items),
                "customer_message": customer_message,
            },
            performed_by="agent",
        ))

        return actions

    def _extract_name(self, from_email: str) -> str:
        """Extract a display name from email 'From' field."""
        if "<" in from_email:
            name = from_email.split("<")[0].strip().strip('"')
            if name:
                return name
        # Fallback: use part before @
        email_part = from_email.split("<")[-1].replace(">", "").strip()
        return email_part.split("@")[0].replace(".", " ").title()
