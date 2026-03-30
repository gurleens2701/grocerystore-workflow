"""
tools/report_ocr.py

OCR a POS-printed daily sales report using Claude vision.

A POS printout typically contains:
  - Department sales breakdown (each dept with its total)
  - Overall product/sales total
  - Sales tax
  - Payment breakdown: cash, credit/debit card, check, EBT
  - Possibly: GPI, online lotto, cash drop, ATM

Things usually NOT on a POS printout (must be asked):
  - Lotto payout  (manual cash given to scratch winners)
  - Lotto credit  (net lottery figure for the period)
  - Instant/scratch lotto sales (sometimes tracked by POS, sometimes not)
"""

import base64
import json
import logging
from datetime import date

import anthropic

from config.settings import settings

log = logging.getLogger(__name__)

# Fields that the POS printer should always have — if missing it's a bad photo
_POS_HAS = ["product_sales", "sales_tax", "card", "cash_drop"]

# Fields that the POS printer usually has but not always
_POS_USUALLY = ["lotto_in", "lotto_online", "gpi"]

# Fields almost never on a POS printout — always need to ask
_ALWAYS_ASK = ["lotto_po", "lotto_cr"]

# All payment-side fields we track
_PAYMENT_FIELDS = [
    "cash_drop", "card", "check", "lotto_po", "lotto_cr",
    "food_stamp", "atm", "pull_tab", "coupon", "loyalty",
]

_ALL_NUMERIC = ["product_sales", "lotto_in", "lotto_online", "sales_tax", "gpi"] + _PAYMENT_FIELDS

_PROMPT = """\
This is a daily sales report printed from a POS (point-of-sale) system at a gas station \
convenience store. It may be a thermal printer receipt or printed sheet.

Extract every value you can find. Use null for anything not present or illegible.

SALES (left side of the sheet):
- product_sales: total product/department sales (the final TOTAL or NET SALES line, NOT including lottery or tax)
- departments: array of department breakdown lines — each item: {"name": "...", "sales": 0.00}
  (look for lines like "CIGARETTES 1234.56", "BEER & WINE 345.00", "GROCERY 210.00", etc.)
- lotto_in: instant / scratch-off lottery ticket sales (may be labeled INSTANT LOTTO, SCRATCH)
- lotto_online: online / terminal lottery sales (may be labeled ONLINE LOTTO, LOTTERY TERMINAL, KENO)
- sales_tax: total sales tax collected
- gpi: GPI or fee-buster / surcharge amount

PAYMENTS (right side / tender breakdown):
- cash_drop: cash dropped to safe (DROP TO SAFE, CASH DROP, or NET CASH)
- card: credit/debit card total (CREDIT, DEBIT, or combined C.CARD total)
- check: check payments
- lotto_po: lottery payout — cash paid OUT to scratch lottery winners (LOTTERY PAYOUT, LOTTO OUT)
  Note: this is usually NOT on POS reports — leave null if not seen.
- lotto_cr: net lottery credit (LOTTO CR, NET LOTTERY) — usually NOT on POS report.
- food_stamp: EBT / SNAP / food stamp amount
- atm: ATM payouts
- pull_tab: pull tab
- coupon: coupon redemptions
- loyalty: loyalty program / Altria payments

ALSO:
- report_date: date on the report (YYYY-MM-DD)

Return ONLY a valid JSON object. Example:
{
  "product_sales": 1872.45,
  "departments": [
    {"name": "CIGARETTES", "sales": 987.50},
    {"name": "BEER & WINE", "sales": 345.00},
    {"name": "GROCERY", "sales": 420.45},
    {"name": "CANDY & SNACKS", "sales": 119.50}
  ],
  "lotto_in": 234.00,
  "lotto_online": 156.00,
  "sales_tax": 45.20,
  "gpi": 12.00,
  "cash_drop": 800.00,
  "card": 543.00,
  "check": 0,
  "lotto_po": null,
  "lotto_cr": null,
  "food_stamp": 27.50,
  "atm": 0,
  "pull_tab": 0,
  "coupon": 0,
  "loyalty": 0,
  "report_date": "2026-03-28"
}"""


def extract_daily_report_from_photo(image_bytes: bytes) -> dict:
    """
    OCR a POS-printed daily report photo using Claude vision.

    Returns:
      {
        "extracted": {field: float | None, ...},
        "departments": [{"name": str, "sales": float}, ...],
        "must_ask": [str, ...],     # fields ALWAYS needed but not found (lotto_po, lotto_cr + any POS fields missing)
        "report_date": date | None,
      }
    Raises on API / JSON error.
    """
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    },
                },
                {"type": "text", "text": _PROMPT},
            ],
        }],
    )

    text = response.content[0].text.strip()
    if "```" in text:
        for part in text.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("{"):
                text = part
                break

    data: dict = json.loads(text)

    # Parse numeric fields
    extracted: dict = {}
    for f in _ALL_NUMERIC:
        raw = data.get(f)
        if raw is None:
            extracted[f] = None
        else:
            try:
                extracted[f] = round(float(str(raw).replace(",", "").replace("$", "")), 2)
            except (ValueError, TypeError):
                extracted[f] = None

    # Parse departments list
    departments = []
    raw_depts = data.get("departments") or []
    if isinstance(raw_depts, list):
        for d in raw_depts:
            if isinstance(d, dict) and d.get("name") and d.get("sales") is not None:
                try:
                    departments.append({
                        "name": str(d["name"]).upper(),
                        "sales": round(float(str(d["sales"]).replace(",", "")), 2),
                    })
                except (ValueError, TypeError):
                    pass

    # Parse report date
    report_date: date | None = None
    raw_date = data.get("report_date")
    if raw_date:
        try:
            report_date = date.fromisoformat(str(raw_date)[:10])
        except (ValueError, TypeError):
            pass

    # Determine what we MUST ask the user for:
    # 1. Fields that should always be on the POS report but are missing (bad photo / unusual POS)
    # 2. Fields that are never on the POS report and are always needed
    must_ask = []

    # POS should always have these — flag if missing
    for f in _POS_HAS:
        if extracted.get(f) is None:
            must_ask.append(f)

    # Lotto_in and food_stamp: ask if missing
    for f in ["lotto_in", "food_stamp"]:
        if extracted.get(f) is None:
            must_ask.append(f)

    # These are NEVER on POS — always ask (if not somehow already present)
    for f in _ALWAYS_ASK:
        if extracted.get(f) is None and f not in must_ask:
            must_ask.append(f)

    return {
        "extracted": extracted,
        "departments": departments,
        "must_ask": must_ask,
        "report_date": report_date,
    }
