"""
SemiSales AI Agent - Configuration Settings
"""

from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from pathlib import Path
from typing import Optional


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # --- Claude Code SDK (uses your Max plan - no separate API key needed) ---
    anthropic_api_key: str = Field("not-needed", description="Not needed - uses Claude Code CLI")
    claude_model: str = Field("claude-sonnet-5", description="Light model (classification, drafting) via Claude Code CLI")
    claude_heavy_model: str = Field("claude-sonnet-5", description="Heavy model (BOM/pricing/alternatives extraction) for accuracy")
    claude_effort: str = Field("high", description="Reasoning effort for the CLI (--effort): low|medium|high|xhigh|max. Higher = more thinking = more accurate but slower")
    claude_max_tokens: int = Field(4096, description="Max tokens for LLM responses")
    claude_timeout_seconds: int = Field(600, description="Per-call Claude CLI timeout (higher effort / Sonnet 5 can be slow — raise this if calls get 'stuck'/time out)")

    # --- Google Workspace ---
    google_credentials_file: str = Field("config/credentials.json")
    google_token_file: str = Field("config/token.json")

    # --- Gmail Account (v1.1 — Company B International only) ---
    main_email: str = Field("admin@company-b.example")

    # --- Internal Domains (DO NOT reply to these - they are our own team) ---
    internal_domains: str = Field(
        "company-b.example",
        description="Comma-separated list of internal domains to skip"
    )

    # --- Purchase Team Emails (Sourcing/Trading - for UNAUTHORIZED brands) ---
    purchase_team_company_b: str = Field("", description="Company B purchase team email(s), comma-separated")
    purchase_team_company_a: str = Field("", description="Company A purchase team email(s), comma-separated")

    # --- Product Team Emails (for AUTHORIZED brands - 50+ line card vendors) ---
    product_team_company_b: str = Field("", description="Company B product team email(s), comma-separated")
    product_team_company_a: str = Field("", description="Company A product team email(s), comma-separated")

    # --- CSR Team Emails (creates Sales Orders from customer POs) ---
    csr_team_company_b: str = Field("", description="Company B CSR team email(s), comma-separated")
    csr_team_company_a: str = Field("", description="Company A CSR team email(s), comma-separated")

    def get_internal_domains(self) -> list[str]:
        return [d.strip().lower() for d in self.internal_domains.split(",") if d.strip()]

    def get_purchase_emails(self, account: str) -> list[str]:
        """Purchase team handles UNAUTHORIZED brands (sourcing/trading)."""
        raw = self.purchase_team_company_b if account == "main" else self.purchase_team_company_a
        return [e.strip() for e in raw.split(",") if e.strip()]

    def get_product_emails(self, account: str) -> list[str]:
        """Product team handles AUTHORIZED brands (50+ line card)."""
        raw = self.product_team_company_b if account == "main" else self.product_team_company_a
        return [e.strip() for e in raw.split(",") if e.strip()]

    def get_csr_emails(self, account: str) -> list[str]:
        """CSR team creates Sales Orders from customer POs."""
        raw = self.csr_team_company_b if account == "main" else self.csr_team_company_a
        return [e.strip() for e in raw.split(",") if e.strip()]

    def get_vendor_rfq_cc(self) -> list[str]:
        """Internal addresses CC'd on every real vendor RFQ (sourcing oversight)."""
        return [e.strip() for e in (self.vendor_rfq_cc or "").split(",") if e.strip()]

    # --- Agent Mode ---
    # "testing"   = drafts for approval, NO automatic customer emails (ack / PO-ack / complaint / follow-ups
    #               are suppressed). Internal team forwarding still happens; the dashboard "Send" still works.
    # "automatic" = full auto mode (customer ack + follow-ups sent automatically).
    agent_mode: str = Field("testing", description="testing (safe, no auto customer sends) or automatic")

    def is_automatic(self) -> bool:
        """True only when the agent is allowed to send customer-facing email automatically."""
        return (self.agent_mode or "").strip().lower() == "automatic"

    # --- Agent Settings ---
    agent_check_interval_seconds: int = Field(60, description="How often to check for new emails (seconds)")
    auto_acknowledge_enabled: bool = Field(True)
    auto_acknowledge_delay_seconds: int = Field(120, description="Delay before sending auto-ack")
    followup_day3_enabled: bool = Field(True)
    followup_day7_enabled: bool = Field(True)

    # --- Quotation ---  (v1.1 — Company B International: quotes are in USD)
    default_currency: str = Field("USD", description="Standing quote currency for Company B International")
    quote_validity_days: int = Field(30)

    # --- Autonomous pipeline: vendor sourcing + margin engine (Quote Analyst) ---
    autonomous_enabled: bool = Field(False, description="Master switch: True routes RFQs to vendors + auto-prices; False keeps the human team flow")
    default_margin_percent: float = Field(15.0, description="Default resale margin % applied over the vendor cost")
    default_expenses_percent: float = Field(3.0, description="Freight/handling % added on top of vendor cost before margin")
    margin_overrides: str = Field("", description="Per-category/brand margin % overrides, e.g. 'MCU:20,Passives:12,STMicroelectronics:18'")
    vendor_reply_wait_hours: int = Field(24, description="How long to wait for vendor cost replies before quoting with whatever arrived")
    auto_send_confidence_enabled: bool = Field(True, description="Confidence-gate: auto-send a customer quote only when every line is confidently priced; else escalate to dashboard")
    consolidation_llm_planning: bool = Field(True, description="Let the LLM decide the per-line sourcing PLAN (which vendor lots + qty, single-vs-split, shortage, hold-for-review) from grounded vendor facts. Code still computes every price/qty and VALIDATES the plan, falling back to the deterministic rules if the plan is unsafe, over-allocated, or the model is unreachable. Off = pure deterministic rules.")

    # --- Part identification (resolve a bare MPN -> real make + description) ---
    part_lookup_provider: str = Field("none", description="MPN lookup provider: none | mouser (Mouser Search API — free key, simple; digikey/octopart can be added)")
    part_lookup_api_key: str = Field("", description="API key for the part-lookup provider (e.g. your Mouser Search API key)")

    # --- Negotiation (autonomous) ---
    negotiation_min_margin_percent: float = Field(10.0, description="When a customer shares a target price, the agent may cut margin down to (not below) this floor to meet it; if the target needs less, the line is re-sourced to the vendor at OUR target cost (customer's target is NEVER shared with vendors/team)")
    max_vendor_ask_percent: float = Field(20.0, description="Human cap on how much cheaper we ask a vendor to go in ONE negotiation round. A blind customer lowball (e.g. 35% below our quote) is NOT passed to the vendor verbatim — we ask for at most this % off their own last cost, then flex our margin (floor..default) to give our best price.")

    # --- Confidence-gate guardrails (hard limits before an autonomous SEND) ---
    min_margin_percent: float = Field(8.0, description="Auto-send blocked if any priced line's margin % is below this floor (thin-margin protection)")
    auto_send_max_value: float = Field(0.0, description="Auto-send blocked if the quote total exceeds this (in the quote currency). 0 = no ceiling")
    eud_watchlist_keywords: str = Field("", description="Extra dual-use keywords to flag 'Requires End-User Declaration', e.g. 'radar:RF,seeker:Missile' or plain 'gyroscope,accelerometer'")

    # --- Vendor-send SAFETY (precaution while real vendor emails aren't loaded) ---
    vendor_test_email: str = Field("", description="In AGENT_MODE=testing, redirect ALL vendor RFQs here (your own inbox) so no real vendor is emailed. Blank = suppress vendor sends entirely in testing.")
    vendor_max_vendors: int = Field(8, description="Cap on how many vendors are emailed per inquiry (prevents blasting the whole list)")
    vendor_rfq_cc: str = Field("admin@company-b.example,owner@company-b.example", description="Comma-separated internal addresses CC'd on every REAL vendor RFQ (sourcing oversight). Applied only on real sends, not test redirects.")
    transit_weeks_to_sg: float = Field(2.0, description="Weeks to move a NON-Singapore vendor's goods into Singapore. Added to the vendor's lead time so the customer quote is stated EXW Singapore (a Singapore vendor adds 0).")

    # --- FX (USD<->INR) — v1.1 keeps this ONLY to convert an Indian vendor's INR cost to USD.
    # (The India import/customs-duty engine — BCD/SWS/HSN — was removed; v1.1 sells in USD, no INR sales.)
    usd_inr_fx_rate: float = Field(83.0, description="USD->INR manual fallback (used only if no auto-fetched rate is stored)")
    fx_auto_fetch: bool = Field(True, description="Auto-fetch USD->INR daily from a free currency API")
    fx_buffer_percent: float = Field(2.0, description="Safety buffer added to the fetched FX rate (covers volatility so FX moves don't erode margin)")
    fx_refresh_hours: int = Field(20, description="Fetch a new rate when the stored one is older than this")
    fx_max_age_hours: int = Field(48, description="Beyond this age the stored rate is flagged STALE (still used, but warned)")

    def get_margin_overrides(self) -> dict:
        """Parse 'MCU:20,Passives:12' into {'mcu':20.0,'passives':12.0} (keys lowercased)."""
        out = {}
        for pair in (self.margin_overrides or "").split(","):
            if ":" in pair:
                k, v = pair.split(":", 1)
                try:
                    out[k.strip().lower()] = float(v.strip())
                except ValueError:
                    continue
        return out

    # --- Dashboard ---
    dashboard_host: str = Field("127.0.0.1")  # localhost by default; only expose via a proxy/auth
    dashboard_port: int = Field(8000)
    dashboard_secret_key: str = Field("change-this-to-a-random-secret")
    # Optional HTTP Basic auth for the dashboard. REQUIRED if dashboard_host is not localhost.
    dashboard_user: str = Field("")
    dashboard_password: str = Field("")

    def dashboard_auth_enabled(self) -> bool:
        return bool(self.dashboard_user and self.dashboard_password)

    # --- Database ---
    database_url: str = Field(f"sqlite:///{BASE_DIR / 'data' / 'semisales.db'}")

    # --- Logging ---
    log_level: str = Field("INFO")
    log_file: str = Field(str(BASE_DIR / "logs" / "semisales.log"))

    # --- Gmail API Scopes ---
    gmail_scopes: list[str] = Field(
        default=[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.modify",
        ]
    )

    model_config = {
        "env_file": str(BASE_DIR / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


def get_settings() -> Settings:
    return Settings()
