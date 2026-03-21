"""
tools/normalizer.py

Normalizes invoice item names into canonical keys using Claude.

Ensures "Marl Red Shrt" (McLane) and "Marlboro Red Short" (Heidelburg)
both map to the same canonical key: MARLBORO-RED-SHORT.

Strategy: attribute extraction — Brand + Flavor/SubBrand + Format + Size.
This prevents merging distinct products (Kings ≠ Short ≠ 100s).
"""

import asyncio
import json
import re

import anthropic
from sqlalchemy import select

from config.settings import settings
from db.database import get_async_session
from db.models import InvoiceItem

_CONFIDENCE_THRESHOLD = 0.85  # below this → flagged for user review

_SYSTEM_PROMPT = """\
You are a convenience store inventory specialist who normalizes product names \
from vendor invoices into canonical keys. Different vendors write the same product \
differently — your job is to make them match.

CANONICAL KEY FORMAT: BRAND-FLAVOR_SUBBRAND-FORMAT-SIZE  (all caps, hyphens between components)

BRAND ALIASES (always expand):
- Marl / Mrlb / MRBL → MARLBORO
- PM / Philip Morris → MARLBORO (for cigarettes)
- Newp / Newpt → NEWPORT
- Caml / Cml → CAMEL
- Wns / Wnst → WINSTON
- Ck / Cke → COKE / COCA_COLA
- Pep / Pps → PEPSI
- Dor / Dors → DORITOS
- Lay / Lays → LAYS
- Redbll / RB → RED_BULL
- BM / Blk Mld → BLACK_MILD
- NV → NON_FILTERED (or expand from context)

FORMAT ALIASES (always expand):
- Shrt / Shts / Sh → SHORT
- KG / Kng / Kngs → KING
- 100 / 100s → 100S
- FT / Ft → FULL_TASTE
- SW / Sw → SWEET
- Menth / Mnt / Mnth → MENTHOL
- Lts / Lt → LIGHT
- Ultr / Ult → ULTRA_LIGHT

SIZE: Keep as-is but normalize units (20z → 20OZ, 2L → 2L, 1.5L → 1.5L)

RULES:
1. Different formats/sizes are ALWAYS different products. Never merge Kings and Shorts.
2. If unit_price is $50–$90 for a tobacco product → likely a carton, add -CARTON suffix
3. If unit_price is $8–$14 for cigarettes → single pack
4. Match against existing_names if similarity >= 80%. Use the existing name if it matches.
5. If genuinely new (no existing match >= 80%), generate a new canonical key.
6. Confidence reflects certainty: 0.95+ = certain, 0.85–0.94 = likely, <0.85 = unsure

EXAMPLES:
- "Marl Red Shrt" $9.20 → MARLBORO-RED-SHORT (0.97)
- "Marlboro Red Short" $9.20 → MARLBORO-RED-SHORT (0.99)
- "PM Marl Red KG" $9.45 → MARLBORO-RED-KING (0.95)
- "Marl Red 100s" $9.50 → MARLBORO-RED-100S (0.96)
- "Newpt Menth KG" $10.10 → NEWPORT-MENTHOL-KING (0.95)
- "Coke 20oz" $1.85 → COCA_COLA-ORIGINAL-20OZ (0.98)
- "Diet Ck 2L" $2.10 → COCA_COLA-DIET-2L (0.95)
- "BM FT SW" $2.50 → BLACK_MILD-FULL_TASTE-SWEET (0.90)
- "Dor Nacho 2.75oz" $1.20 → DORITOS-NACHO-2.75OZ (0.93)

Return ONLY valid JSON array — no explanation, no markdown:
[
  {
    "item_name_raw": "<exactly as provided>",
    "canonical_name": "<BRAND-FLAVOR-FORMAT>",
    "match_existing": "<matched existing key, or null if new>",
    "confidence": 0.95
  }
]
"""


def _make_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _parse_json(raw: str) -> list:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


async def _fetch_existing_canonical_names(store_id: str) -> list[str]:
    """Fetch all distinct canonical names already in the DB for this store."""
    async with get_async_session() as session:
        q = select(InvoiceItem.canonical_name).where(
            InvoiceItem.store_id == store_id,
            InvoiceItem.canonical_name.isnot(None),
        ).distinct()
        result = await session.execute(q)
        return [row[0] for row in result.fetchall() if row[0]]


async def _normalize_async(items: list[dict], store_id: str) -> list[dict]:
    """
    For each item, generate a canonical_name and confidence score.
    Matches against existing canonical names in the DB.
    Returns items with canonical_name and confidence added.
    """
    if not items:
        return items

    existing_names = await _fetch_existing_canonical_names(store_id)

    # Build input for Claude — just raw name + unit_price per item
    items_input = [
        {"item_name_raw": item.get("item_name_raw") or item.get("item_name", ""), "unit_price": item.get("unit_price", 0)}
        for item in items
    ]

    existing_block = "\n".join(existing_names) if existing_names else "(none yet — this is the first invoice)"

    user_msg = (
        f"Existing canonical names in database:\n{existing_block}\n\n"
        f"Items to normalize:\n{json.dumps(items_input, indent=2)}\n\n"
        "Return the JSON array as specified."
    )

    client = _make_client()
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = message.content[0].text
    normalized = _parse_json(raw)

    # Merge back into original items list by position
    result = []
    for i, item in enumerate(items):
        enriched = dict(item)
        if i < len(normalized):
            n = normalized[i]
            enriched["canonical_name"] = n.get("canonical_name", "").upper().strip()
            enriched["confidence"] = float(n.get("confidence", 0.5))
            enriched["match_existing"] = n.get("match_existing")
            # If it matched an existing name, use that as item_name for consistency
            if n.get("match_existing"):
                enriched["item_name"] = n["match_existing"]
            else:
                enriched["item_name"] = enriched["canonical_name"]
        else:
            enriched["canonical_name"] = None
            enriched["confidence"] = 0.5
            enriched["match_existing"] = None
        result.append(enriched)

    return result


def normalize_items(items: list[dict], store_id: str) -> list[dict]:
    """
    Sync wrapper. Takes extracted invoice items, returns them with
    canonical_name and confidence added to each item dict.
    """
    return asyncio.run(_normalize_async(items, store_id))
