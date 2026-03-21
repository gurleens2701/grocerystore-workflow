"""
Daily report file saving/loading for gas station stores.

Reports are stored as plain text files at:
  reports/{store_id}/{year}/{month_name}/{DD-mon-YYYY}.txt
"""

from datetime import date, datetime
from pathlib import Path

from config.settings import settings

# Root reports directory relative to project root
_REPORTS_ROOT = Path("reports")


def _parse_date(report_date) -> date:
    """Accept date object or ISO string (YYYY-MM-DD)."""
    if isinstance(report_date, date):
        return report_date
    return date.fromisoformat(str(report_date))


def get_report_path(store_id: str, report_date) -> Path:
    """
    Returns Path for the report file, creating directories if needed.

    Structure: reports/{store_id}/{year}/{month_name}/{DD-mon-YYYY}.txt
    Example:   reports/moraine/2026/march/15-mar-2026.txt
    """
    d = _parse_date(report_date)
    month_name = d.strftime("%B").lower()          # "march"
    filename = d.strftime("%-d-%b-%Y").lower() + ".txt"  # "15-mar-2026.txt"

    path = _REPORTS_ROOT / store_id / str(d.year) / month_name / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def save_daily_report(store_id: str, sales: dict, right: dict) -> Path:
    """
    Build a plain-text daily sheet report and save it to disk.

    sales keys: date, day_of_week, product_sales, departments, lotto_in,
                lotto_online, sales_tax, gpi, grand_total, cash_drops, card,
                check, atm, pull_tab, coupon, loyalty, vendor, refunds,
                total_transactions
    right keys: lotto_po, lotto_cr, food_stamp

    Returns the Path where the file was saved.
    """
    report_date = _parse_date(sales["date"])
    path = get_report_path(store_id, report_date)

    # ── Pull values ──────────────────────────────────────────────────────────
    product_sales  = sales.get("product_sales", 0.0)
    departments    = sales.get("departments", [])
    lotto_in       = sales.get("lotto_in", 0.0)
    lotto_online   = sales.get("lotto_online", 0.0)
    sales_tax      = sales.get("sales_tax", 0.0)
    gpi            = sales.get("gpi", 0.0)
    grand_total    = sales.get("grand_total", 0.0)
    refunds        = sales.get("refunds", 0.0)
    total_txns     = sales.get("total_transactions", 0)

    cash           = sales.get("cash_drops", 0.0)
    card           = sales.get("card", 0.0)
    check          = sales.get("check", 0.0)
    atm            = sales.get("atm", 0.0)
    pull_tab       = sales.get("pull_tab", 0.0)
    coupon         = sales.get("coupon", 0.0)
    loyalty        = sales.get("loyalty", 0.0)
    vendor         = sales.get("vendor", 0.0)

    lotto_po       = right.get("lotto_po", 0.0)
    lotto_cr       = right.get("lotto_cr", 0.0)
    food_stamp     = right.get("food_stamp", 0.0)

    # ── Over/short (mirrors bot.py logic) ────────────────────────────────────
    total_right = round(
        cash + card + check + lotto_po + lotto_cr + atm
        + coupon + pull_tab + food_stamp + loyalty + vendor, 2
    )
    diff = round(total_right - grand_total, 2)
    if diff > 0:
        over_short_label = f"OVER           +${diff:.2f}"
    elif diff < 0:
        over_short_label = f"SHORT          -${abs(diff):.2f}"
    else:
        over_short_label = f"EVEN            $0.00"

    # ── Formatting helpers ────────────────────────────────────────────────────
    W = 38  # total line width
    SEP = "-" * W

    def row(label: str, val: float, dash_if_zero: bool = False) -> str:
        if dash_if_zero and val == 0:
            return f"  {label:<22} {'--':>10}"
        return f"  {label:<22} ${val:>9.2f}"

    # ── Build report lines ────────────────────────────────────────────────────
    lines = [
        settings.store_name.upper(),
        f"DATE:  {sales.get('day_of_week', '')} {sales.get('date', '')}",
        SEP,
        "",
        "PRODUCT SALES",
    ]

    for dept in departments:
        name = dept.get("name", "")
        amt  = dept.get("sales", 0.0)
        lines.append(f"  {name:<22} ${amt:>9.2f}")

    lines += [
        SEP,
        row("TOTAL", product_sales),
        "",
        "OTHER",
        row("IN. LOTTO", lotto_in),
        row("ON. LINE", lotto_online),
        row("SALES TAX", sales_tax),
        row("GPI", gpi),
        SEP,
        row("GRAND TOTAL", grand_total),
        "",
    ]

    if refunds:
        lines.append(f"  Refunds on record:       ${refunds:>9.2f}")
        lines.append("")

    lines += [
        "PAYMENTS",
        row("LOTTO P.O", lotto_po, dash_if_zero=True),
        row("LOTTO CR.", lotto_cr, dash_if_zero=True),
        row("ATM", atm, dash_if_zero=True),
        row("CASH DROP", cash),
        row("CHECK", check),
        row("C.CARD", card),
        row("COUPON", coupon, dash_if_zero=True),
        row("PULL TAB", pull_tab, dash_if_zero=True),
        row("FOOD STAMP", food_stamp, dash_if_zero=True),
        row("LOYALTY/ALTRIA", loyalty, dash_if_zero=True),
        row("VENDOR PAYOUT", vendor, dash_if_zero=True),
        SEP,
        row("TOTAL PAYMENTS", total_right),
        SEP,
        f"  {over_short_label}",
        "",
        f"  Baskets: {total_txns}",
        "",
        SEP,
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def load_daily_report(store_id: str, report_date) -> str | None:
    """
    Load a saved daily report. Returns file contents or None if not found.
    """
    path = get_report_path(store_id, report_date)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def list_monthly_reports(store_id: str, year: int, month: int) -> list[Path]:
    """
    List all daily report files for a given month, sorted ascending.
    """
    # Derive month name from a date object
    month_name = date(year, month, 1).strftime("%B").lower()
    directory = _REPORTS_ROOT / store_id / str(year) / month_name
    if not directory.exists():
        return []
    return sorted(directory.glob("*.txt"))
