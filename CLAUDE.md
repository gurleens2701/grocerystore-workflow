# Gas Station Agent — Multi-Store Platform

## Project Overview
Config-driven platform for gas station convenience stores. One backend, one database, many stores.
- Each store's behavior (daily sheet fields, POS type, tools, schedules) comes from DB config — no code changes to onboard
- Stack: Python 3.13, LangChain (ChatAnthropic + @tool) + Claude Sonnet, Playwright, httpx, gspread, python-telegram-bot, APScheduler
- Deployed on Hetzner VPS (178.104.61.18) via Docker Compose, domain: `clerkai.live`
- Dashboard: Next.js app in `dashboard/`

## Architecture

### Database schemas
- `platform` — store config: stores, workflows, scheduler_policies, daily_report_rules, sheet_mappings, tool_policies, bank_rules, users, user_store_memberships
- `canonical` — business data: daily_sales, invoices, expenses, bank_transactions, etc. All tables have `store_id` column
- `raw_nrs` — raw POS API payloads saved before transformation (safety net for re-processing)
- `ops` — monitoring: job_history, audit_logs
- `public` — pending_state (transient bot state)

### POS connector pattern
- `tools/pos/{pos_type}/client.py` — Playwright login + raw httpx API calls
- `tools/pos/{pos_type}/transformer.py` — raw API response → canonical dict (cents→dollars for NRS, field mapping)
- `tools/pos/dispatcher.py` — routes by `store.pos_type`, saves raw payload, calls transformer
- Callers use `dispatcher.fetch_daily_sales(store, date)` — don't know which POS

### Config-driven components
- `config/store_registry.py` — `StoreProfile` dataclass loaded from platform tables. Single object carries all per-store config
- `bot.py` — routes Telegram messages by `chat_id` → `platform.stores`. Guard at group=-1 rejects unknown chats
- `main.py` — scheduler reads `platform.store_scheduler_policies`, registers one job per store per policy
- `tools/main_agent.py` — `_build_tool_list(store_id)` reads `platform.store_tool_policies`
- `bot.py` `_prompt_for_right_side()` / `_parse_right_side()` — read `platform.store_daily_report_rules`

## Key Files
- `bot.py` — Telegram bot, command handlers, daily report state machine, per-store scheduler entry points
- `api/main.py` — FastAPI dashboard backend, per-user auth, store-scoped queries
- `api/auth.py` — bcrypt auth against `platform.users`, JWT with `store_ids`
- `config/store_registry.py` — StoreProfile loader from platform tables
- `tools/pos/dispatcher.py` — POS-agnostic daily sales fetch
- `tools/pos/nrs/client.py` — NRS Playwright login + raw API calls
- `tools/pos/nrs/transformer.py` — NRS cents→dollars field mapping
- `tools/pos/modisoft/client.py` — Modisoft connector (stub — not yet implemented)
- `tools/main_agent.py` — Claude Sonnet chat agent with per-store tool list
- `tools/bank_reconciler.py` — Plaid bank sync, auto-categorization from `platform.store_bank_rules`
- `tools/intent_router.py` — keyword-based intent classifier (no API call)
- `db/models.py` — SQLAlchemy models across all schemas
- `db/database.py` — shared async engine, `get_session_for_store()` uses shared DB
- `config/settings.py` — env vars via pydantic settings
- `.env` — secrets (never commit)

## Admin CLIs
- `scripts/onboard_store.py` — interactive: add store to DB, copy template rules, test connectivity
- `scripts/create_user.py` — interactive: create dashboard login with store access
- `scripts/manage_store.py` — interactive: edit workflows, daily sheet fields, scheduler jobs, tools, tool experiments

## Development Rules
- Python venv is at `.venv` — always use `.venv/bin/python` or `. .venv/bin/activate`
- Deploy: `git push` on local, then `git pull && docker compose up -d --build` on VPS
- After rebuilding app/api containers: `docker compose restart nginx` (flushes DNS cache for container IPs)
- Never commit `.env` or `config/google_credentials.json`
- Scheduler job changes require `docker compose restart app`. All other config changes are live immediately.

## NRS API
- Login: Playwright headless → capture token from `pos-papi.nrsplus.com` URL path
- Token format: `u56967-{hex}` — changes each session
- Moraine Store ID: 69653, elmer_id=69201 (terminal 1), elmer_id=77790 (terminal 2)
- All money values from API are in **cents** — divide by 100 (done in transformer.py)

## Telegram Bot
- Webhook mode (not polling) — listens on internal port 8080
- Webhook URL: `https://clerkai.live/telegram-webhook`
- Per-store routing: `platform.stores.chat_id` — guard rejects unknown chats
- Daily report flow uses `_STATE_SALES` state in DB — user can say "cancel" to exit

## Coding Style
- Keep Telegram messages plain text (no markdown) except where parse_mode=MARKDOWN is intentional
- Async throughout — use `run_in_executor` for sync calls
- Errors go to Telegram as user-friendly messages, not raw exceptions
- Per-store: always filter by `store_id`. Never hardcode store-specific values

# Behavioral Guidelines

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
