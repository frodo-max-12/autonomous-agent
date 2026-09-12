# Deploy the Autonomous Sales Agent 24/7 on a Linux VPS (Max-plan CLI)

Goal: run the autonomous agent always-on on a cheap Linux VPS instead of the laptop —
no sleep, no shutdown, and **none of the Windows "session-0" crash** that stopped it on 30 Jul.

Backend chosen: **keep the Claude Max plan via the Claude Code CLI** (headless token).
See the ToS note at the bottom — this was chosen with the risk understood.

---

## 0. What you're buying
- **VPS:** Ubuntu 24.04, **2 GB RAM** minimum (Python + Node + Claude CLI). Hetzner (~€4.5/mo),
  DigitalOcean / Vultr (~$6/mo). That's the only real recurring cost.
- **Domain:** OPTIONAL, ~$10/yr — only if you want a public dashboard URL. Not needed to run.

---

## 1. Create the VPS and a service user
```bash
# as root on the fresh VPS:
adduser agent           # non-root service user
usermod -aG sudo agent
# then log back in as agent
```

## 2. Copy the app to the VPS (code + secrets in one shot)
The GitHub repo excludes secrets, so the simplest path is to ship your working folder.
**On the laptop (Git Bash / PowerShell):** zip the folder WITHOUT the junk, then upload.
```bash
# exclude venv / caches / logs so the zip stays small
cd "D:/IT Dept/Developements/AI Agent/Autonomous Agent"
powershell -c "Compress-Archive -Path 'autonomous-agent v1.1\*' -DestinationPath agent.zip -Force"
scp agent.zip company_b@YOUR_VPS_IP:/home/agent/
```
**On the VPS:**
```bash
mkdir -p ~/ai-agent && cd ~/ai-agent && unzip ~/agent.zip
rm -rf .venv __pycache__ */__pycache__ .pytest_cache logs/*   # drop any Windows venv/caches
```
This brings the code AND `.env`, `config/token_main.json`, `config/credentials.json`,
`data/autonomous.db` (so your 563 vendors + learning carry over).

> ⚠️ The Gmail token file is **`config/token_main.json`** (main.py hardcodes it) — make sure
> it made it into the zip. The `.env` line `GOOGLE_TOKEN_FILE=config/token.json` is misleading
> and unused; don't rely on it. Gmail auth refreshes non-interactively, so it works headless.

## 3. Provision (Python, Node, Claude CLI, deps)
```bash
cd ~/ai-agent
bash deploy/setup_vps.sh
```
Note the two paths it prints at the end (`which claude`, `which node`) — you'll need them in step 5.

## 4. Authenticate the Claude CLI (the one tricky step)
The VPS has no browser, so mint a token on a machine that DOES have one (your laptop):
```bash
claude setup-token          # opens browser, prints a ~1-YEAR OAuth token
```
Copy that token onto the VPS into the secrets file:
```bash
cd ~/ai-agent
cp deploy/ai-agent.env.example deploy/ai-agent.env
nano deploy/ai-agent.env    # paste the token as CLAUDE_CODE_OAUTH_TOKEN=...
chmod 600 deploy/ai-agent.env
```
Verify the CLI actually answers under this user:
```bash
CLAUDE_CODE_OAUTH_TOKEN="$(grep -oP '(?<=CLAUDE_CODE_OAUTH_TOKEN=).*' deploy/ai-agent.env)" \
  claude -p "reply with OK" --output-format json
```
You should get a JSON reply. If it says *login expired / not found*, the token is wrong.

## 5. Install the systemd service
```bash
# Edit the unit if your username/paths differ, and set PATH= to the dirs from step 3:
nano deploy/ai-agent.service
sudo cp deploy/ai-agent.service /etc/systemd/system/ai-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now ai-agent
```
Watch it come up:
```bash
sudo systemctl status ai-agent
journalctl -u ai-agent -f            # live logs
tail -f ~/ai-agent/logs/autonomous.log
```
You want to see: `Authenticated as intl.sales@…`, the poll loop, and **no** "Claude CLI error".

## 6. Reach the dashboard (no domain needed)
From your laptop, tunnel the local-only dashboard over SSH:
```bash
ssh -L 8002:127.0.0.1:8002 company_b@YOUR_VPS_IP
# then open http://localhost:8002 in your browser
```
Keep `DASHBOARD_HOST=127.0.0.1` (already set) so it's never exposed to the internet.
Only expose it via a domain + reverse proxy later if you truly need to — and keep the
`DASHBOARD_USER`/`DASHBOARD_PASSWORD` on, because the dashboard can send customer emails.

---

## ⚠️ Operational must-knows

1. **Only ONE agent may run against the shared Gmail inbox.** If the laptop service and the VPS
   both run, they will each process the same unread mail and fire **duplicate customer acks +
   duplicate vendor RFQs**. Before the VPS goes live, on the laptop (elevated PowerShell):
   ```powershell
   Stop-Service AI-Agent ; Set-Service AI-Agent -StartupType Disabled
   # (or fully remove it:  C:\nssm\nssm.exe remove AI-Agent confirm )
   ```

2. **First start will "catch up" on old mail.** The copied DB's last-processed date is 30 Jul, so
   on first boot the backfill sweep reads inbox mail from 30 Jul → today. If you do NOT want ~2
   weeks of backlog processed at once, tell me and I'll advance the watermark before you ship.

3. **Vendor blast is now uncapped.** With the cap bug fixed, one inquiry emails ~250–300 vendors
   and the send loop has **no throttle** → Gmail will rate-limit partway and silently skip vendors.
   Add the send-throttle before go-live (ask me) or you won't actually reach the whole list.

4. **Token rotation.** The `claude setup-token` credential lasts ~1 year and does NOT auto-refresh.
   Set a reminder ~11 months out: run `claude setup-token` again, update `deploy/ai-agent.env`,
   then `sudo systemctl restart ai-agent`. If it ever expires, the logs show `Login expired` and
   the agent stops processing until you rotate.

## Manage the service
```bash
sudo systemctl restart ai-agent      # after any code/.env/token change
sudo systemctl stop ai-agent
journalctl -u ai-agent --since "1 hour ago"
```

---

## ToS note (recorded)
Anthropic restricts personal Max/Pro subscriptions to individual, interactive use; automated /
unattended workloads are directed to a metered **Anthropic API key** (policy tightened ~Apr 2026).
Running the Max plan on this server 24/7 is against that guidance and could get the account
flagged, which would silently stop the agent. This deployment keeps the Max plan by explicit
choice. The compliant alternative (swap `claude_client.py` to the Messages API, ~$5–15/mo at
current volume with a hard spend cap) can be adopted later with no change to steps 1–6 except the
auth line.
