"""
Invoice extraction engine using Claude Sonnet vision.

Accepts vendor invoice photos (bytes) or text descriptions and returns
structured line-item data. Handles varied invoice formats from McLane,
Heidelburg, Core-Mark, Roma Wholesale, Pepsi, and other convenience store vendors.
"""

import base64
import imghdr
import json
import re

import anthropic

from config.settings import settings

_SYSTEM_PROMPT = """\
You are an invoice parser for a gas station convenience store. \
Extract products and prices from vendor invoices. \
Vendors include McLane, Core-Mark, Roma Wholesale, Heidelburg, Pepsi, and others.

STEP 1 — PRICING RULES:

CIGARETTES AND TOBACCO (most important):
Wholesale distributors sell cigarettes by the CARTON (10 packs).
The price shown on the invoice is ALWAYS the carton price.
- unit_price = carton_price / 10  (price per single pack)
- case_price = the carton price as printed
- case_qty = 10
Example: Marlboro $99.00 on invoice → unit_price=9.90, case_price=99.00, case_qty=10
Example: L&D $48.76 on invoice → unit_price=4.876, case_price=48.76, case_qty=10
NEVER store the carton price as unit_price for cigarettes.

ALL OTHER PRODUCTS:
- If invoice shows case price + case quantity: unit_price = case_price / case_qty
- If invoice shows unit price directly: use it as unit_price
- If invoice shows line total + quantity ordered: unit_price = line_total / quantity
- If invoice shows original price + discount: unit_price = original_price - discount_per_unit

Skip: subtotal rows, grand total, tax, bottle deposits, freight/delivery fees.

STEP 2 — STANDARDIZED NAME RULES:

Create a standardized_name that normalizes product names so the same product \
from different vendors can be matched in a database.

CIGARETTES (Marlboro, Camel, Newport, Basic, 24/7, Crowns, LD, L&D, Winston, etc.):
- BX or BOX → "Box"
- SP or SOFT → "Soft Pack"
- KS → "King Size"
- 100 or 100S → "100s"
- REMOVE: FSC, CT, CRT, 1CT (compliance/case codes)
- Format: Brand + Variant + Size + Pack Type
- Examples:
  "24/7 RED BX KS FSC 1 CT" → "24/7 Red King Size Box"
  "MARLBORO GOLD BX 100 FSC" → "Marlboro Gold 100s Box"
  "MARLBORO BLACK BOX KING SIZE FSC" → "Marlboro Black King Size Box"
  "L&D MENTHOL BOX KING SIZE FSC" → "LD Menthol King Size Box"
  "BASIC GOLD BOX 100 FSC" → "Basic Gold 100s Box"

CIGARS/LITTLE CIGARS (White Owl, Swisher, Lil Leaf, Black & Mild, etc.):
- PK → "Pack"
- REMOVE: price info (2/1.19, 3/$2.99), CT counts
- Format: Brand + Variant + Pack Size
- Examples:
  "WHITE OWL RED WHITE & BERRY 2/1.19 2PK/30CT" → "White Owl Red White Berry 2 Pack"
  "LIL LEAF RUSSIAN CREAM 3/$2.99 10 3PK" → "Lil Leaf Russian Cream 3 Pack"

CANDY/SNACKS:
- Keep size if it identifies the product (1.86oz, 2oz)
- REMOVE: CT when it is a case quantity
- Format: Brand + Flavor + Size
- Examples:
  "SNICKERS 1.86OZ 48CT" → "Snickers 1.86oz"
  "LAFFY TAFFY ROPE MYSTERY SWIRL 24CT" → "Laffy Taffy Rope Mystery Swirl"

BEVERAGES:
- Keep size and pack count
- Format: Brand + Flavor + Size + Pack
- Examples:
  "COCA COLA 12OZ 24PK" → "Coca Cola 12oz 24 Pack"
  "RED BULL 8.4OZ 12CT" → "Red Bull 8.4oz 12 Pack"

ALWAYS REMOVE from standardized_name: price info ($, /), case quantities at end, \
compliance codes (FSC, CRT), trailing CT unless it is pack count for cigars.

STEP 3 — CONFIDENCE:
- confidence per item: 0-100, how certain you are the price and name are correct
- overall confidence: average of all item confidences

For category use one of: TOBACCO, BEVERAGE, BEER, GROCERY, CANDY, SNACK, \
DAIRY, FROZEN, HEALTH, OTC, GENERAL

Return ONLY valid JSON — no explanation, no markdown, no extra text:
{
  "vendor": "<vendor name, uppercase>",
  "invoice_date": "<YYYY-MM-DD or empty string>",
  "invoice_number": "<invoice/order number or empty string>",
  "confidence": <overall confidence 0-100>,
  "items": [
    {
      "item_name_raw": "<product name exactly as printed>",
      "item_name": "<cleaned readable product name>",
      "standardized_name": "<normalized name for cross-vendor matching>",
      "upc": "<UPC or item code, or empty string>",
      "unit_price": <float, price per single pack/unit after discount>,
      "case_price": <float or null>,
      "case_qty": <integer or null>,
      "category": "<category or empty string>",
      "confidence": <0-100>
    }
  ]
}
"""

_EMPTY_RESULT = {
    "vendor": "",
    "invoice_date": "",
    "invoice_number": "",
    "confidence": 0,
    "items": [],
}


def _make_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _detect_media_type(photo_bytes: bytes) -> str:
    """Detect image media type from magic bytes."""
    kind = imghdr.what(None, h=photo_bytes)
    mapping = {
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
    }
    return mapping.get(kind, "image/jpeg")


def _parse_claude_json(raw: str) -> dict:
    """Strip markdown fences and parse JSON from Claude's response."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    return json.loads(text)


def _normalise_result(data: dict) -> dict:
    """Ensure all expected top-level keys are present."""
    return {
        "vendor": data.get("vendor", ""),
        "invoice_date": data.get("invoice_date", ""),
        "invoice_number": data.get("invoice_number", ""),
        "confidence": data.get("confidence", 0),
        "items": data.get("items", []),
    }


def extract_invoice_from_photo(photo_bytes: bytes) -> dict:
    """
    Extract line items from a vendor invoice photo.

    Returns dict with keys: vendor, invoice_date, invoice_number, confidence, items.
    On failure, also includes an 'error' key.
    """
    try:
        client = _make_client()
        media_type = _detect_media_type(photo_bytes)
        b64_data = base64.standard_b64encode(photo_bytes).decode("utf-8")

        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            temperature=0.3,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Extract all product line items from this vendor invoice. "
                                "Return only the JSON as specified — no extra text."
                            ),
                        },
                    ],
                }
            ],
        )

        raw_text = message.content[0].text
        data = _parse_claude_json(raw_text)
        return _normalise_result(data)

    except Exception as exc:
        return {**_EMPTY_RESULT, "error": str(exc)}


def extract_invoice_from_text(text: str) -> dict:
    """
    Extract line items from a text description or OCR output of a vendor invoice.

    Returns dict with keys: vendor, invoice_date, invoice_number, confidence, items.
    On failure, also includes an 'error' key.
    """
    try:
        client = _make_client()

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            temperature=0.3,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Here is the text content of a vendor invoice. "
                        "Extract all product line items and return only the JSON as specified — "
                        "no extra text.\n\n"
                        f"{text}"
                    ),
                }
            ],
        )

        raw_text = message.content[0].text
        data = _parse_claude_json(raw_text)
        return _normalise_result(data)

    except Exception as exc:
        return {**_EMPTY_RESULT, "error": str(exc)}
