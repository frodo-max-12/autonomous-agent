"""
SemiSales AI Agent - Auto-Learning Module
Stores human corrections and customer history to improve agent accuracy over time.

How it works:
1. Every time a human corrects an agent's classification, we store it
2. Before classifying a new email, we look for similar past corrections
3. We inject those as few-shot examples into the Claude prompt
4. Over time, the agent learns the specific patterns of your business
"""

from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from core.database import (
    ClassificationFeedback, CustomerHistory, Email, Lead,
    get_session,
)


class LearningManager:
    """Manages agent auto-learning via feedback and customer history."""

    def __init__(self, database_url: str):
        self.database_url = database_url

    def record_correction(
        self,
        email_id: int,
        agent_classification: str,
        human_correction: str,
        reason: str = "",
    ) -> bool:
        """Record a human correction to an agent's email classification."""
        session = get_session(self.database_url)
        try:
            email = session.query(Email).filter_by(id=email_id).first()
            if not email:
                return False

            feedback = ClassificationFeedback(
                email_id=email_id,
                subject=email.subject,
                body_snippet=(email.body_text or "")[:500],
                from_email=email.from_email,
                agent_classification=agent_classification,
                human_correction=human_correction,
                reason=reason,
            )
            session.add(feedback)

            # Update the email itself with the correction
            from core.database import EmailType
            try:
                email.email_type = EmailType(human_correction)
            except ValueError:
                pass

            session.commit()
            logger.info(f"Recorded correction: {agent_classification} -> {human_correction}")
            return True
        finally:
            session.close()

    def get_relevant_corrections(self, subject: str, body: str, max_examples: int = 5) -> list[dict]:
        """Find past corrections that might be relevant to the current email.

        Simple matching: look for corrections with similar subject keywords or from same domain.
        Returns a list of correction examples to use as few-shot prompts.
        """
        session = get_session(self.database_url)
        try:
            all_corrections = session.query(ClassificationFeedback).order_by(
                ClassificationFeedback.created_at.desc()
            ).limit(200).all()

            if not all_corrections:
                return []

            subject_lower = (subject or "").lower()
            body_lower = (body or "")[:500].lower()
            keywords = set(subject_lower.split())

            # Score each correction by keyword overlap
            scored = []
            for correction in all_corrections:
                score = 0
                corr_subj = (correction.subject or "").lower()
                corr_body = (correction.body_snippet or "").lower()

                # Keyword overlap in subject
                corr_keywords = set(corr_subj.split())
                overlap = len(keywords & corr_keywords)
                score += overlap * 2

                # Body snippet overlap (simple)
                for kw in keywords:
                    if len(kw) > 3 and kw in corr_body:
                        score += 1

                if score > 0:
                    scored.append((score, correction))

            scored.sort(key=lambda x: -x[0])
            top = scored[:max_examples]

            return [{
                "subject": c.subject,
                "body_snippet": c.body_snippet[:200],
                "correct_classification": c.human_correction,
                "agent_got_wrong": c.agent_classification,
                "reason": c.reason or "",
            } for _, c in top]
        finally:
            session.close()

    def get_customer_context(self, customer_email: str) -> dict:
        """Get historical context about a customer for personalization."""
        session = get_session(self.database_url)
        try:
            email_lower = customer_email.lower().strip()
            if "<" in email_lower:
                email_lower = email_lower.split("<")[-1].replace(">", "").strip()

            lead = session.query(Lead).filter_by(email=email_lower).first()
            history = session.query(CustomerHistory).filter_by(
                customer_email=email_lower
            ).order_by(CustomerHistory.created_at.desc()).limit(10).all()

            if not lead and not history:
                return {"is_known": False}

            context = {
                "is_known": True,
                "name": lead.name if lead else None,
                "company": lead.company if lead else None,
                "country": lead.country if lead else None,
                "total_inquiries": lead.total_inquiries if lead else 0,
                "total_orders": lead.total_orders if lead else 0,
                "industry": lead.industry if lead else None,
                "application": lead.application if lead else None,
                "typical_brands": lead.typical_brands_used if lead else [],
                "past_interactions": [
                    {
                        "type": h.interaction_type,
                        "summary": h.summary,
                        "date": h.created_at.strftime("%Y-%m-%d") if h.created_at else "",
                    }
                    for h in history
                ],
            }
            return context
        finally:
            session.close()

    def build_classification_context(self, subject: str, body: str, from_email: str) -> str:
        """Build additional context to inject into the classification prompt.

        Includes:
        - Relevant past corrections as few-shot examples
        - Customer history (if known)
        """
        context_parts = []

        # Relevant corrections
        corrections = self.get_relevant_corrections(subject, body, max_examples=3)
        if corrections:
            context_parts.append("LEARNED FROM PAST CORRECTIONS:")
            for i, c in enumerate(corrections, 1):
                context_parts.append(
                    f"Example {i}: Email with subject '{c['subject'][:80]}' "
                    f"was initially classified as '{c['agent_got_wrong']}' "
                    f"but correctly should be '{c['correct_classification']}'. "
                    f"{c['reason']}"
                )
            context_parts.append("")

        # Customer history
        customer_ctx = self.get_customer_context(from_email)
        if customer_ctx.get("is_known"):
            context_parts.append(
                f"KNOWN CUSTOMER: {customer_ctx.get('name', 'Unknown')} "
                f"from {customer_ctx.get('company', 'Unknown')}, "
                f"{customer_ctx.get('total_inquiries', 0)} past inquiries, "
                f"{customer_ctx.get('total_orders', 0)} past orders."
            )
            if customer_ctx.get("industry"):
                context_parts.append(f"Industry: {customer_ctx['industry']}")
            if customer_ctx.get("typical_brands"):
                context_parts.append(f"Typical brands: {', '.join(customer_ctx['typical_brands'])}")
            context_parts.append("")

        return "\n".join(context_parts) if context_parts else ""

    def get_stats(self) -> dict:
        """Get learning/feedback stats."""
        session = get_session(self.database_url)
        try:
            total_corrections = session.query(ClassificationFeedback).count()
            total_interactions = session.query(CustomerHistory).count()
            return {
                "total_corrections": total_corrections,
                "total_interactions": total_interactions,
            }
        finally:
            session.close()
