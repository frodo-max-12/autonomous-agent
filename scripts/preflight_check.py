"""
Pre-flight for the 24/7 service: confirm the two external dependencies authenticate — the Claude Code
CLI and Gmail (intl.sales@ via token_main.json) — WITHOUT starting the agent loop or touching the
inbox. Safe to run while the config is live.
"""
import os
import sys

os.chdir(r"D:\IT Dept\Developements\AI Agent\Autonomous Agent\autonomous-agent v1.1")
sys.path.insert(0, os.getcwd())

from config.settings import get_settings          # noqa: E402
from llm.claude_client import ClaudeClient         # noqa: E402
from gmail.client import GmailClient               # noqa: E402

s = get_settings()

print("=== 1. Claude Code CLI ===")
ClaudeClient(api_key=s.anthropic_api_key, model=s.claude_model, heavy_model=s.claude_heavy_model,
             timeout=s.claude_timeout_seconds, effort=getattr(s, "claude_effort", "high"))

print("\n=== 2. Gmail auth (config/token_main.json) ===")
g = GmailClient(account_name="main", credentials_file=s.google_credentials_file,
                token_file="config/token_main.json", scopes=s.gmail_scopes)
g.authenticate()

print("\nPREFLIGHT OK — Claude CLI + Gmail both authenticate. No emails were processed.")
