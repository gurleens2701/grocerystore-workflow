# Moraine Foodmart — Gas Station Agent

## Project Overview
Daily automated agent for **Moraine Foodmart** (gas station convenience store).
- Runs at 7AM ET: fetches NRS sales/inventory, logs to Google Sheets, sends Telegram report
- Stack: Python 3.13, LangGraph + LangChain + Claude, Playwright, httpx, gspread, python-telegram-bot, APScheduler
- Deployed on Hetzner VPS via Docker Compose, domain: `clerkai.live`
- Dashboard: Next.js app in `dashboard/`

## Key Files
- `bot.py` — Telegram bot, all command handlers, daily report state machine
- `api/main.py` — FastAPI backend for dashboard
- `tools/nrs_tools.py` — NRS Plus portal login (Playwright) + API calls
- `tools/bank_reconciler.py` — Plaid bank sync, auto-categorization, CC matching
- `tools/main_agent.py` — Claude-powered chat agent for Telegram freeform messages
- `tools/intent_router.py` — Haiku-based intent classifier
- `dashboard/app/bank/page.tsx` — Bank reconciliation UI
- `config/settings.py` — All env vars via pydantic settings
- `db/models.py` — SQLAlchemy models
- `.env` — secrets (never commit)

## Development Rules
- Python venv is at `.venv` — always use `.venv/bin/python` or `. .venv/bin/activate`
- Deploy: `git push` on local, then `git pull && docker compose up -d --build` on VPS
- After nginx config changes: `docker compose restart nginx` (flushes DNS cache)
- Never commit `.env` or `config/google_credentials.json`

## NRS API
- Login: Playwright headless → capture token from `pos-papi.nrsplus.com` URL path
- Token format: `u56967-{hex}` — changes each session
- Store ID: 69653 (main), elmer_id=69201 (terminal 1), elmer_id=77790 (terminal 2)
- All money values from API are in **cents** — divide by 100

## Telegram Bot
- Webhook mode (not polling) — listens on internal port 8080
- Webhook URL: `https://clerkai.live/telegram-webhook`
- CHAT_ID: 8525501774
- Daily report flow uses `_STATE_SALES` state in DB — user can say "cancel" to exit it

## Database State Keys
- `sales` — pending daily report data
- `chat_history` — rolling conversation history (last 20 messages)
- `bk_confirm_{txn_id}` — pending bank transaction subcategory input

## Coding Style
- Keep responses and messages plain text (no markdown) in Telegram except where parse_mode=MARKDOWN is intentional
- Async throughout — use `run_in_executor` for sync calls (Claude, NRS)
- Errors go to Telegram as user-friendly messages, not raw exceptions
