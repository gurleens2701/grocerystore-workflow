"""
Intent router — classifies incoming Telegram messages using Claude Haiku.

Returns one of:
  "daily_numbers"  — owner replying with lotto PO/CR/food stamp numbers
  "expense"        — logging a bill/cost (electricity $340 march 10)
  "rebate"         — logging a vendor rebate received (pmhelix rebate $820)
  "revenue"        — logging revenue/profit took home (car payment $300)
  "invoice"        — vendor delivery invoice (mclane $2100 3/14)
  "query"          — natural language question about store data
  "unknown"        — unrecognised, ignore silently
"""

import anthropic
from config.settings import settings

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


_SYSTEM = """You are an intent classifier for a gas station / convenience store Telegram bot.
Classify the user's message into exactly one of these intents:

- daily_numbers: owner providing 3 numbers after daily sheet (lotto PO, lotto CR, food stamp)
- expense: owner logging a store expense/bill (electricity, rent, garbage, insurance, etc.)
- rebate: owner logging a tobacco or vendor rebate received (pmhelix, altria, ussmoke, etc.)
- revenue: owner logging profit took home or personal revenue category (car payment, food, committee, house, etc.)
- invoice: owner logging a vendor delivery/purchase invoice (mclane, heidelburg, pepsi, etc.)
- query: owner asking a question about their store data
- unknown: anything else

Reply with ONLY the intent word, nothing else."""


def classify_message(text: str) -> str:
    """Classify a Telegram message. Returns intent string. Fast — uses Haiku."""
    try:
        msg = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16,
            system=_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        intent = msg.content[0].text.strip().lower()
        valid = {"daily_numbers", "expense", "rebate", "revenue", "invoice", "query", "unknown"}
        return intent if intent in valid else "unknown"
    except Exception:
        return "unknown"
