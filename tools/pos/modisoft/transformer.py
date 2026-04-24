"""
tools/pos/modisoft/transformer.py

Modisoft mobile API → canonical daily sales dict.

Input shape (from fetch_raw_closing):
  {
    "Grocery": [{DeptName, DeptType, Sales, NetSales, ...}, ...],
    "Fuel":    [{FuelType, Volume, Amount, Retail}, ...],
    "FinancialData": {
      "Tenders":   [{Cashier: "CASH|CREDIT|DEBIT|FOODSTAMP|CHECK", Amount}],
      "SalesTax":  156.35,
      "SafeDrops": [{Amount}, ...],
      "PaidOuts":  [{Amount, Payee}, ...],   # vendor payouts
      "PaidIns":   [{Amount}, ...],
      "Voids":     [...],
      "LineVoids": [...],
      "Refunds":   [...],
      "NoSales":   [...],
    }
  }

All amounts from Modisoft are in DOLLARS (not cents like NRS).
Dept types: Grocery | Lottery | Services | Misc
Lottery depts can be positive (sales) or negative (payouts/cashouts).
"""

from datetime import date


def _sum_amt(items, field: str = "Amount") -> float:
    """Sum a field across a list, coercing to float, returning a rounded dollar amount."""
    return round(sum(float((i or {}).get(field, 0) or 0) for i in (items or [])), 2)


def transform_daily_sales(raw: dict, target_date: date) -> dict:
    grocery = raw.get("Grocery", []) or []
    fuel = raw.get("Fuel", []) or []
    financial = raw.get("FinancialData", {}) or {}

    # -----------------------------------------------------------------------
    # LEFT SIDE — Product + lottery + tax
    # -----------------------------------------------------------------------

    # Only true Grocery departments count as product sales
    grocery_depts = [d for d in grocery if d.get("DeptType") == "Grocery"]
    product_sales = round(sum(float(d.get("NetSales", 0) or 0) for d in grocery_depts), 2)
    departments = [
        {
            "name": (d.get("DeptName") or "").strip(),
            "items": 0,
            "sales": round(float(d.get("NetSales", 0) or 0), 2),
        }
        for d in grocery_depts
    ]

    # Lottery breakdown — sum sales, payouts, pull tabs separately
    lotto_in = 0.0
    lotto_online = 0.0
    lotto_payout = 0.0
    pull_tab = 0.0
    for d in grocery:
        if d.get("DeptType") != "Lottery":
            continue
        name = (d.get("DeptName") or "").strip().upper()
        net = float(d.get("NetSales", 0) or 0)

        if "PULL TAB" in name:
            # Pull tab payout is tracked separately
            pull_tab += abs(net)
        elif "PAID OUT" in name or "PAYOUT" in name or "CASHOUT" in name:
            lotto_payout += abs(net)
        elif "ONLINE" in name:
            lotto_online += net
        else:
            # "LOTTO", "LOTTERY", "INSTANT LOTTO" all roll into instant
            lotto_in += net

    lotto_in = round(lotto_in, 2)
    lotto_online = round(lotto_online, 2)
    lotto_payout = round(lotto_payout, 2)
    pull_tab = round(pull_tab, 2)

    # ATM + coupon from Services/Misc dept types
    atm = 0.0
    coupon = 0.0
    for d in grocery:
        name = (d.get("DeptName") or "").strip().upper()
        net = float(d.get("NetSales", 0) or 0)
        if "ATM" in name:
            atm += abs(net)
        elif "COUPON" in name:
            coupon += abs(net)
    atm = round(atm, 2)
    coupon = round(coupon, 2)

    # Sales tax straight from FinancialData
    sales_tax = round(float(financial.get("SalesTax", 0) or 0), 2)

    # GPI — no Modisoft equivalent
    gpi = 0.0

    # -----------------------------------------------------------------------
    # FUEL (extra fields — Modisoft stores typically sell fuel)
    # -----------------------------------------------------------------------

    gas_dollars = round(sum(float(f.get("Amount", 0) or 0) for f in fuel), 2)
    gas_gallons = round(sum(float(f.get("Volume", 0) or 0) for f in fuel), 3)

    # Per-grade breakdown for reporting
    fuel_grades = [
        {
            "grade": f.get("FuelType"),
            "gallons": round(float(f.get("Volume", 0) or 0), 3),
            "amount": round(float(f.get("Amount", 0) or 0), 2),
            "price_per_gallon": round(float(f.get("Retail", 0) or 0), 3),
        }
        for f in fuel
    ]

    # -----------------------------------------------------------------------
    # RIGHT SIDE — Tenders (payments)
    # -----------------------------------------------------------------------

    cash = credit = debit = check = food_stamp = 0.0
    for t in financial.get("Tenders", []) or []:
        name = (t.get("Cashier") or "").upper()
        amt = float(t.get("Amount", 0) or 0)
        # Order matters — check substrings carefully
        if "FOODSTAMP" in name or "EBT" in name:
            food_stamp += amt
        elif "CREDIT" in name:
            credit += amt
        elif "DEBIT" in name:
            debit += amt
        elif "CHECK" in name:
            check += amt
        elif "CASH" in name:
            cash += amt

    cash = round(cash, 2)
    credit = round(credit, 2)
    debit = round(debit, 2)
    check = round(check, 2)
    food_stamp = round(food_stamp, 2)
    card = round(credit + debit, 2)

    # Cash drop = safe drops by the cashier
    cash_drop = _sum_amt(financial.get("SafeDrops", []))

    # Refunds, vendor payouts, paid ins
    refunds = _sum_amt(financial.get("Refunds", []))
    vendor = _sum_amt(financial.get("PaidOuts", []))
    paid_in = _sum_amt(financial.get("PaidIns", []))

    # -----------------------------------------------------------------------
    # TOTALS
    # -----------------------------------------------------------------------

    other_subtotal = round(lotto_in + lotto_online + sales_tax + gpi, 2)
    grand_total = round(product_sales + other_subtotal + gas_dollars, 2)
    total_payments = round(cash + credit + debit + check + food_stamp, 2)

    return {
        "date": str(target_date),
        "day_of_week": target_date.strftime("%A").upper(),
        # --- Left side ---
        "departments": departments,
        "product_sales": product_sales,
        "lotto_in": lotto_in,
        "lotto_online": lotto_online,
        "sales_tax": sales_tax,
        "gpi": gpi,
        "refunds": refunds,
        "other_subtotal": other_subtotal,
        "grand_total": grand_total,
        # --- Fuel (Modisoft-specific extras) ---
        "gas_dollars": gas_dollars,
        "gas_gallons": gas_gallons,
        "fuel_grades": fuel_grades,
        # --- Right side: payments ---
        "cash": cash,
        "card": card,
        "credit": credit,
        "debit": debit,
        "check": check,
        "lotto_payout": lotto_payout,
        "atm": atm,
        "pull_tab": pull_tab,
        "coupon": coupon,
        "food_stamp": food_stamp,
        "vendor": vendor,
        "paid_in": paid_in,
        "total_payments": total_payments,
        # --- Misc ---
        "cash_drop": cash_drop,
        "cash_drops": cash_drop,  # legacy alias
        # --- Legacy aliases (match NRS shape) ---
        "total_sales": product_sales,
        "net_sales": grand_total,
        "cash_sales": cash,
        "card_sales": card,
    }
