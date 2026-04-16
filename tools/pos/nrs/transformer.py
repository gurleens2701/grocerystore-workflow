"""
tools/pos/nrs/transformer.py

NRS raw API response → canonical daily sales dict.

Takes the raw dict returned by client.fetch_raw_stats() and converts it to
the shape the rest of the app (bot.py, sheets_tools, daily_report workflow) expects.

All money values from NRS are in cents — divide by 100 happens here.
"""

from datetime import date


def _cents(v) -> float:
    """Convert NRS cents integer to dollars float."""
    try:
        return round(int(v) / 100, 2)
    except (TypeError, ValueError):
        return 0.0


def transform_daily_sales(raw: dict, target_date: date) -> dict:
    """
    Convert raw NRS pcrhist stats payload to canonical daily sales dict.

    Args:
        raw: The 'data' dict from the NRS pcrhist API response.
        target_date: The date the sales are for.

    Returns:
        Canonical daily sales dict consumed by bot.py and sheets_tools.
    """
    payamts = raw.get("payamts", {}) or {}

    # --- Product departments (LEFT TOP) ---
    bydept = raw.get("bydept", []) or []
    departments = [
        {"name": d["dept"], "items": d.get("items", 0), "sales": _cents(d.get("sales", 0))}
        for d in bydept if d.get("dept")
    ]
    product_sales = round(sum(d["sales"] for d in departments), 2)

    # --- Other sales (LEFT BOTTOM) ---
    byother = raw.get("byotherdept", []) or []
    lotto_in = 0.0
    lotto_online = 0.0
    for d in byother:
        name = (d.get("dept") or "").lower()
        if "instant" in name:
            lotto_in = _cents(d.get("sales", 0))
        elif "online" in name:
            lotto_online = _cents(d.get("sales", 0))

    # Sales tax from collections
    collections = raw.get("collections", {}) or {}
    sales_tax = 0.0
    for v in collections.values():
        if isinstance(v, dict) and v.get("type") == "Tax":
            sales_tax = round(sales_tax + _cents(v.get("explicit", 0)), 2)

    # GPI = feebuster
    gpi = _cents(raw.get("feebuster", 0))

    # Refunds
    refunds_raw = raw.get("refunds", {})
    refunds = _cents(refunds_raw.get("amt", 0)) if isinstance(refunds_raw, dict) else 0.0

    other_subtotal = round(lotto_in + lotto_online + sales_tax + gpi, 2)
    # Grand total matches manual sheet: product + lotto + tax + GPI (refunds NOT deducted)
    grand_total = round(product_sales + other_subtotal, 2)

    # --- Payments (RIGHT) ---
    cash = _cents(payamts.get("cash", 0))
    card = _cents(payamts.get("credit_debit", 0))
    check = _cents(payamts.get("check", 0))
    ebt = _cents((payamts.get("ebt_snap", 0) or 0) + (payamts.get("ebt_cash", 0) or 0))
    altri = _cents(payamts.get("altri", 0))
    loyal = _cents(payamts.get("loyal", 0))

    # Lottery payout, ATM, pull tab from cashback list
    cashback_list = raw.get("cashback", []) or []
    lotto_payout = 0.0
    atm = 0.0
    pull_tab = 0.0
    for cb in cashback_list:
        ptype = (cb.get("paytype") or "").lower()
        if "lottery" in ptype or "lotto" in ptype:
            lotto_payout = round(lotto_payout + _cents(cb.get("amt", 0)), 2)
        elif "atm" in ptype:
            atm = round(atm + _cents(cb.get("amt", 0)), 2)
        elif "pull tab" in ptype or "pulltab" in ptype:
            pull_tab = round(pull_tab + _cents(cb.get("amt", 0)), 2)

    # Vendor payout from payouts (cash paid out at register)
    payouts_data = raw.get("payouts", {}) or {}
    vendor = _cents(payouts_data.get("amt", 0))

    # Coupon and loyalty/altria from payamts
    coupon = _cents(payamts.get("coupon", 0))
    altria = _cents(payamts.get("altri", 0))
    loyalty_combined = round(altria + loyal, 2)

    total_payments = round(cash + card + check + ebt + altri + loyal, 2)

    # Cash drops to safe
    drops = raw.get("drops", {}) or {}
    cash_drops = _cents(drops.get("amt", 0))

    return {
        "date": str(target_date),
        "day_of_week": target_date.strftime("%A").upper(),
        # Product sales
        "departments": departments,
        "product_sales": product_sales,
        # Other items
        "lotto_in": lotto_in,
        "lotto_online": lotto_online,
        "sales_tax": sales_tax,
        "gpi": gpi,
        "refunds": refunds,
        "other_subtotal": other_subtotal,
        "grand_total": grand_total,
        # Payments
        "cash": cash,
        "card": card,
        "check": check,
        "lotto_payout": lotto_payout,
        "atm": atm,
        "pull_tab": pull_tab,
        "coupon": coupon,
        "loyalty": loyalty_combined,
        "vendor": vendor,
        "ebt": ebt,
        "altri": altri,
        "total_payments": total_payments,
        # Misc
        "total_transactions": payamts.get("num_sales", 0),
        "cash_drops": cash_drops,
        # Legacy aliases
        "total_sales": product_sales,
        "net_sales": grand_total,
        "cash_sales": cash,
        "card_sales": card,
    }
