#!/usr/bin/env bash
# setup_vps.sh — one-time provisioning for the Autonomous Sales Agent on a fresh Ubuntu 24.04 VPS.
# Run as the NON-root service user (e.g. `company_b`), from anywhere:
#     bash setup_vps.sh
# It installs system deps, Node + the Claude CLI, Python venv, and the app deps.
# It does NOT copy secrets or auth Claude — those are manual steps (see DEPLOY-VPS.md).
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/ai-agent}"     # where you unzipped/cloned the agent
echo ">> App dir: $APP_DIR"

echo ">> [1/5] System packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip curl unzip ca-certificates

echo ">> [2/5] Node.js (for the Claude CLI)"
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi
node --version

echo ">> [3/5] Claude Code CLI"
if ! command -v claude >/dev/null 2>&1; then
  npm install -g @anthropic-ai/claude-code
fi
echo "   claude at: $(command -v claude || echo 'NOT FOUND — fix PATH')"

echo ">> [4/5] Python venv + app deps"
cd "$APP_DIR"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt
mkdir -p logs

echo ">> [5/5] Sanity: which claude / node (put THESE dirs in ai-agent.service PATH=)"
echo "   claude -> $(command -v claude)"
echo "   node   -> $(command -v node)"
echo
echo ">> Done. Next (manual): run 'claude setup-token', fill deploy/ai-agent.env,"
echo "   copy the .env + config/token_main.json + config/credentials.json + data/autonomous.db,"
echo "   then install the systemd service (see deploy/DEPLOY-VPS.md)."
