# How to Run — Autonomous Sales Agent

A short, practical guide. For the architecture, see `AUTONOMOUS_AGENT.md`.

---

## 1. Prerequisites (once)

- **Python 3.11+**
- **Claude Code CLI** installed and signed in to your **$200 Max plan** (`claude` must run from a terminal). The agent calls it as a subprocess — no API key needed.
- **Gmail** access to the shared inbox (`pm@company-b.example`) via `config/credentials.json` (Google OAuth desktop credentials).

Install dependencies:
```bash
cd "D:\IT Dept\Developements\AI Agent\Autonomous Agent\autonomous-agent"
pip install -r requirements.txt
```

---

## 2. One-time setup

**a) Authenticate Gmail** (opens a browser once, stores tokens):
```bash
python main.py --setup
```

**b) Load your vendors** — fill the **Emails** column in `Vendors.xlsx` (routing works without it, but sending needs emails), then:
```bash
python scripts/import_company_b_vendors.py "C:\Users\<USER>\Downloads\Vendors.xlsx"
```
(Or add/edit vendors later on the `/vendors` dashboard page.)

---

## 3. Configure `.env`

The important keys (safe defaults are already set):

```ini
AGENT_MODE=testing            # testing = drafts only, nothing reaches customers/vendors
AUTONOMOUS_ENABLED=false      # false = internal-team flow; true = vendor sourcing + auto-pricing
VENDOR_TEST_EMAIL=            # in testing, ALL vendor RFQs/POs go here (set to YOUR inbox)
DASHBOARD_PORT=8001

# India import pricing (auto; verify duty as parts appear)
FX_AUTO_FETCH=true            # daily USD->INR + FX_BUFFER_PERCENT (2%)
DEFAULT_MARGIN_PERCENT=15
DEFAULT_FREIGHT_PERCENT=6
```

---

## 4. Run

```bash
python main.py              # agent (email monitor) + dashboard together
python main.py --dashboard  # dashboard only
python main.py --agent      # agent only
```

Dashboard: **http://127.0.0.1:8001**

---

## 5. Go live — the safe, staged path

**Stage 1 — watch it work (nothing goes out):**
```ini
AGENT_MODE=testing
AUTONOMOUS_ENABLED=true
VENDOR_TEST_EMAIL=you@youremail.com
```
Send a test RFQ to `pm@company-b.example`. The agent classifies it, extracts the BOM, and (in testing) **drafts** the customer quote to the dashboard and **redirects every vendor RFQ to your inbox** with a `[TEST]` banner — no real customer or vendor is touched. Watch it on `/quotations`, `/sourcing/<id>`, `/orders`.

**Stage 2 — go hands-off:**
```ini
AGENT_MODE=automatic
```
Now confident quotes auto-send to customers, and vendor RFQs/POs go to the real vendors. Anything uncertain (missing price, unverified HSN duty) still lands on the dashboard for you.

Restart `python main.py` after any `.env` change.

---

## 6. Daily use — what to check on the dashboard

| Page | What you do there |
|---|---|
| `/quotations` | quotes in progress; **Review** any waiting for approval |
| `/sourcing/<id>` | internal view — vendor quotes, cost/margin, what's `needs_review` |
| `/hsn` | **confirm the BCD%** for any new HSN (verify once → reused forever); FX rate + "Refresh now" |
| `/vendors` | vendor list, learned **Score**; set manual priority or **Recompute scores** |
| `/orders` | customer POs → vendor POs; advance status (placed → shipped → received) |

---

## 7. Quick troubleshooting

- **Nothing sends to customers** → `AGENT_MODE=testing` (by design). Switch to `automatic`.
- **INR import lines stuck at `needs_review`** → confirm their HSN's BCD% on `/hsn`.
- **Vendor RFQ says "no email on file"** → add the vendor's email (Excel + re-import, or `/vendors`).
- **`Claude CLI timeout`** → heavy extraction is slow; `CLAUDE_TIMEOUT_SECONDS=300` is already set. Ensure `claude` is signed in and you have quota.
- **Dashboard won't start on a network IP** → set `DASHBOARD_USER`/`DASHBOARD_PASSWORD` (it refuses to expose without auth).

---

**Safety recap:** in `testing` mode the agent never emails a real customer or vendor; every money-or-law number is either live (FX) or your verified reference (HSN duty) — it never invents one.
