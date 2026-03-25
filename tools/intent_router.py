"""
Intent router — classifies incoming messages using Claude Haiku.

Returns one of:
  "daily_fetch"    — wants to trigger/see daily NRS fetch & report
  "price_lookup"   — asking for price/cost of a specific product
  "order"          — wants to build/compile an order list
  "health"         — asking about weekly health score or store performance
  "daily_numbers"  — owner replying with lotto PO/CR/food stamp numbers
  "expense"        — logging a bill/cost (electricity $340 march 10)
  "rebate"         — logging a vendor rebate received (pmhelix rebate $820)
  "revenue"        — logging revenue/profit took home (car payment $300)
  "invoice"        — vendor delivery invoice (mclane $2100 3/14)
  "sync"           — wants to sync/refresh data from Google Sheets right now
  "query"          — natural language question about store data (sales, expenses, etc.)
  "unknown"        — unrecognised
"""

import anthropic
from config.settings import settings

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


_SYSTEM = """You are an intent classifier for a gas station / convenience store bot.
Classify the user's message into exactly one of these intents:

- daily_fetch: user wants to trigger the daily NRS fetch, see yesterday's sales report, or do the daily sheet. Examples: "do daily report", "fetch yesterday sales", "what were yesterday's sales", "pull daily numbers", "run daily"
- price_lookup: user asking for the price or cost of a specific product. Examples: "how much is marlboro red", "what does coke cost", "price of newport", "cost of monster energy"
- order: user wants to build an order list or compile an order. Examples: "order marlboro x2 coke x5", "make order for pepsi and chips", "I need to order newport and monster"
- health: user asking about store health score, weekly performance, how the store is doing. Examples: "health score", "how is the store doing", "weekly report", "store performance"
- daily_numbers: owner providing exactly 3 numbers after a daily sheet prompt (lotto PO, lotto CR, food stamp). Only when these 3 specific numbers are being submitted.
- expense: owner logging a store expense/bill (electricity, rent, garbage, insurance, payroll, etc.)
- rebate: owner logging a tobacco or vendor rebate received (pmhelix, altria, ussmoke, etc.)
- revenue: owner logging profit took home or personal revenue (car payment, food, committee, house, etc.)
- invoice: owner logging a vendor delivery/purchase invoice total (mclane $2100 3/14, heidelburg 500, etc.)
- sync: user wants to sync or refresh data from Google Sheets into the system right now. Examples: "sync it", "can you sync", "sync now", "refresh data", "pull from sheets", "update from google sheet"
- query: natural language question about existing store data — sales history, expenses, invoices, revenue, comparisons, trends. Examples: "how much did I make last week", "what were my expenses in March", "compare this week vs last"
- unknown: greetings, unrecognised, or anything not listed above

Reply with ONLY the intent word, nothing else."""


def classify_message(text: str) -> str:
    """Classify a message. Returns intent string. Fast — uses Haiku."""
    try:
        msg = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16,
            system=_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        intent = msg.content[0].text.strip().lower()
        valid = {
            "daily_fetch", "price_lookup", "order", "health",
            "daily_numbers", "expense", "rebate", "revenue", "invoice", "sync", "query", "unknown",
        }
        return intent if intent in valid else "unknown"
    except Exception:
        return "unknown"
