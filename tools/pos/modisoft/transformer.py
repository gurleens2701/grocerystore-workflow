"""
tools/pos/modisoft/transformer.py

Modisoft raw API response → canonical daily sales dict.

TODO (Phase 3 Modisoft onboarding):
  - Map Modisoft field names to canonical field names
  - Convert dollar amounts (Modisoft uses dollars, not cents)
  - Handle gas sales (gas_dollars, gas_gallons) in extra_fields
"""

from datetime import date


def transform_daily_sales(raw: dict, target_date: date) -> dict:
    """Convert raw Modisoft daily stats to canonical dict. Not yet implemented."""
    raise NotImplementedError(
        "Modisoft transformer not yet implemented. "
        "See tools/pos/modisoft/transformer.py to build it."
    )
