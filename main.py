"""
SemiSales AI Agent - Main Entry Point
Starts the agent (email monitoring) and dashboard (web UI) together.

Usage:
    python main.py              # Run both agent + dashboard
    python main.py --dashboard  # Dashboard only (no email monitoring)
    python main.py --agent      # Agent only (no dashboard)
    python main.py --setup      # First-time Google OAuth setup
"""

import sys
import threading
import argparse
from pathlib import Path

import uvicorn
from loguru import logger

# Add project root to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import get_settings, Settings
from core.agent import SemiSalesAgent
from core.database import init_database
from dashboard.app import app as dashboard_app, set_config


def setup_logging(settings: Settings):
    """Configure loguru logging."""
    logger.remove()  # Remove default handler
    logger.add(sys.stderr, level=settings.log_level, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - {message}")
    logger.add(settings.log_file, level=settings.log_level, rotation="10 MB", retention="30 days")


def run_setup(settings: Settings):
    """First-time setup: authenticate Gmail accounts."""
    from gmail.client import GmailClient

    print("=" * 60)
    print("SemiSales AI Agent - First Time Setup")
    print("=" * 60)
    print()
    print("This will open your browser to authenticate the Gmail account.")
    print()

    # Company B International account (v1.1 — the only account)
    print(f"Authenticate Company B International ({settings.main_email})")
    print("Press Enter to continue...")
    input()

    main_client = GmailClient(
        account_name="main",
        credentials_file=settings.google_credentials_file,
        token_file="config/token_main.json",
        scopes=settings.gmail_scopes,
    )
    main_client.authenticate(interactive=True)
    print(f"  Authenticated as: {main_client.user_email}")

    # Initialize database
    init_database(settings.database_url)

    print()
    print("=" * 60)
    print("Setup complete! You can now run: python main.py")
    print("=" * 60)


def run_agent(settings: Settings) -> SemiSalesAgent:
    """Initialize and start the email monitoring agent."""
    agent = SemiSalesAgent(settings)

    # Set up Gmail account — v1.1 is Company B International only (single inbox; no Company A).
    agent.setup_gmail_account("main", "config/token_main.json")

    return agent


def _validate_dashboard_security(settings: Settings):
    """Refuse to expose the dashboard on the network without authentication.
    The dashboard can send real customer email and export the full lead DB, so a network
    bind (0.0.0.0 / LAN IP) MUST have DASHBOARD_USER + DASHBOARD_PASSWORD configured."""
    host = (settings.dashboard_host or "").strip()
    local_hosts = {"127.0.0.1", "localhost", "::1", ""}
    if host not in local_hosts and not settings.dashboard_auth_enabled():
        logger.critical(
            f"Dashboard host is '{host}' (network-exposed) but no DASHBOARD_USER/DASHBOARD_PASSWORD is set. "
            f"Refusing to start without authentication. Set DASHBOARD_HOST=127.0.0.1, or configure "
            f"DASHBOARD_USER and DASHBOARD_PASSWORD in .env."
        )
        raise SystemExit(1)
    if settings.dashboard_secret_key == "change-this-to-a-random-secret":
        logger.warning("DASHBOARD_SECRET_KEY is still the default placeholder — set a random value in .env.")


def run_dashboard(settings: Settings, gmail_clients: dict = None):
    """Start the FastAPI dashboard."""
    _validate_dashboard_security(settings)
    set_config(
        database_url=settings.database_url,
        gmail_clients=gmail_clients,
        auth_user=settings.dashboard_user,
        auth_password=settings.dashboard_password,
    )
    if settings.dashboard_auth_enabled():
        logger.info("Dashboard HTTP Basic auth: ENABLED")
    else:
        logger.info(f"Dashboard auth disabled — bound to {settings.dashboard_host} (localhost only).")
    uvicorn.run(
        dashboard_app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="info",
    )


def main():
    parser = argparse.ArgumentParser(description="SemiSales AI Agent")
    parser.add_argument("--setup", action="store_true", help="First-time Google OAuth setup")
    parser.add_argument("--dashboard", action="store_true", help="Run dashboard only")
    parser.add_argument("--agent", action="store_true", help="Run agent only (no dashboard)")
    args = parser.parse_args()

    # Load settings
    settings = get_settings()
    setup_logging(settings)

    if args.setup:
        run_setup(settings)
        return

    # Initialize database
    init_database(settings.database_url)

    if args.dashboard:
        # Dashboard only mode
        logger.info("Starting dashboard only (no email monitoring)")
        run_dashboard(settings)

    elif args.agent:
        # Agent only mode
        logger.info("Starting agent only (no dashboard)")
        agent = run_agent(settings)
        agent.run()

    else:
        # Full mode: agent + dashboard
        logger.info("Starting SemiSales AI Agent (Agent + Dashboard)")

        # Validate dashboard security BEFORE authenticating/starting the agent, so a misconfig
        # fails fast instead of after the agent is already live.
        _validate_dashboard_security(settings)

        # Start agent
        agent = run_agent(settings)

        # Start agent in background thread
        agent_thread = threading.Thread(target=agent.run, daemon=True)
        agent_thread.start()
        logger.info("Agent started in background thread")

        # Start dashboard in main thread (blocking)
        run_dashboard(settings, agent.gmail_clients)


if __name__ == "__main__":
    main()
