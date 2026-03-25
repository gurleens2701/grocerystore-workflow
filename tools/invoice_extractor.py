"""
Invoice extraction engine using Claude Sonnet vision.

Accepts vendor invoice photos (bytes) or text descriptions and returns
structured line-item data. Handles varied invoice formats from McLane,
Heidelburg, Coremark, Pepsi, and other convenience store vendors.
"""

import base64
import imghdr
import json
import re

import anthropic

from config.settings import settings

_SYSTEM_PROMPT = """\
You are extracting line items from a convenience store vendor invoice. \
The store is a gas station convenience store that buys from wholesale distributors \
like McLane, Heidelburg, Coremark, Pepsi, Core-Mark, and others. \
Each vendor has a completely different invoice layout.

Your job is to identify every product line and extract the correct unit price — \
the price the store actually pays per single sellable unit after all discounts.

Rules for determining unit_price:
- If the invoice shows: original price, a discount, a quantity, and a line total:
    unit_price = (line_total) / quantity   OR   original_price - discount_per_unit
    (use whichever is explicitly shown; confirm they are consistent)
- If the invoice shows only a case price (e.g. "case of 24 = $28.80"):
    unit_price = case_price / case_qty  (e.g. 1.20), also return case_price and case_qty
- If the invoice shows a net unit price directly: use that value as unit_price
- Do NOT use the line total as unit_price

Skip: subtotal rows, grand total rows, tax rows, bottle/can deposit rows, \
freight/delivery fee rows, and any rows that are not individual product line items.

For category, use one of: TOBACCO, BEVERAGE, BEER, GROCERY, CANDY, SNACK, \
DAIRY, FROZEN, HEALTH, OTC, GENERAL — only if reasonably obvious from the name.

Return ONLY valid JSON matching this exact schema — no explanation, no markdown, \
no extra text:
{
  "vendor": "<vendor name, uppercase>",
  "invoice_date": "<YYYY-MM-DD or empty string if not found>",
  "invoice_number": "<invoice/order number or empty string>",
  "items": [
    {
      "item_name_raw": "<product name exactly as printed on invoice>",
      "item_name": "<cleaned readable product name>",
      "upc": "<UPC or item code, or empty string>",
      "unit_price": <float, price per single unit after discount>,
      "case_price": <float or null>,
      "case_qty": <integer or null>,
      "category": "<category string or empty string>"
    }
  ]
}
"""

_EMPTY_RESULT = {
    "vendor": "",
    "invoice_date": "",
    "invoice_number": "",
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
    # Remove ```json ... ``` or ``` ... ``` fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    return json.loads(text)


def _normalise_result(data: dict) -> dict:
    """Ensure all expected top-level keys are present."""
    result = {
        "vendor": data.get("vendor", ""),
        "invoice_date": data.get("invoice_date", ""),
        "invoice_number": data.get("invoice_number", ""),
        "items": data.get("items", []),
    }
    return result


def extract_invoice_from_photo(photo_bytes: bytes) -> dict:
    """
    Extract line items from a vendor invoice photo.

    Args:
        photo_bytes: Raw bytes of a JPEG, PNG, or WebP invoice image.

    Returns:
        dict with keys: vendor, invoice_date, invoice_number, items (list).
        On failure, also includes an 'error' key.
    """
    try:
        client = _make_client()
        media_type = _detect_media_type(photo_bytes)
        b64_data = base64.standard_b64encode(photo_bytes).decode("utf-8")

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
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
                                "Please extract all product line items from this vendor invoice. "
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

    except Exception as exc:  # pylint: disable=broad-except
        return {**_EMPTY_RESULT, "error": str(exc)}


def extract_invoice_from_text(text: str) -> dict:
    """
    Extract line items from a text description or OCR output of a vendor invoice.

    Args:
        text: Plain-text representation of the invoice (copied text, OCR output, etc.).

    Returns:
        dict with keys: vendor, invoice_date, invoice_number, items (list).
        On failure, also includes an 'error' key.
    """
    try:
        client = _make_client()

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
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

    except Exception as exc:  # pylint: disable=broad-except
        return {**_EMPTY_RESULT, "error": str(exc)}
