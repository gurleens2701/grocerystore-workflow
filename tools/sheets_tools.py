"""
Google Sheets tools for logging gas station data.

Tab structure: one tab per month, named "MARCH 2026" etc.
Each tab layout (rows are 1-indexed):

  Row  1       : DAILY SALES header (dark green)
  Row  2       : Column headers (light green, bold)
  Rows 3-33    : One row per day (pre-filled dates)
  Row 34       : TOTAL row
  Row 35       : spacer

  Row 36       : INVENTORY (COGS) header (dark green)
  Row 37       : Sub-category headers (GROCERY VENDORS | SODA | BEER)
  Row 38       : Vendor column headers  (DATE, ACE UNLIMITED, ..., TOTAL)
  Rows 39-69   : One row per day (pre-filled dates)
  Row 70       : TOTAL row
  Row 71       : spacer

  Row 72       : EXPENSES header (dark green)
  Row 73       : Expense column headers
  Rows 74-104  : One row per day
  Row 105      : spacer

  Row 106      : REVENUES header (dark green)
  Row 107      : Revenue column headers
  Rows 108+    : Revenue entries
"""

import calendar
from datetime import date, datetime, timedelta
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from config.settings import settings

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ---------------------------------------------------------------------------
# Row layout
# ---------------------------------------------------------------------------
_DAILY_TITLE_ROW  = 1
_DAILY_HEADER_ROW = 2
_DAILY_DATA_START = 3      # day 1 = row 3, day N = row N+2
_DAILY_TOTAL_ROW  = 34

_COGS_TITLE_ROW   = 36
_COGS_SUBCAT_ROW  = 37     # sub-category row: GROCERY VENDORS | SODA | BEER
_COGS_HEADER_ROW  = 38     # vendor name row
_COGS_DATA_START  = 39     # day 1 = row 39, day N = row N+38
_COGS_TOTAL_ROW   = 70

_EXP_TITLE_ROW    = 72
_EXP_HEADER_ROW   = 73
_EXP_DATA_START   = 74

_REV_TITLE_ROW    = 106
_REV_SUBHDR_ROW   = 107   # "REBATES" | "PROFIT TOOK HOME" sub-labels
_REV_HEADER_ROW   = 108   # actual column names for both tables
_REV_DATA_START   = 109

# Column start positions (1-based) for side-by-side sections
_PAYROLL_COL_START = 20   # payroll starts at column T
_ORDERS_COL_START  = 30   # orders starts at column AD
_PROFIT_COL_START  = 18   # PROFIT TOOK HOME starts at column R

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

# Maps NRS department names (lowercase) → column key for DAILY SALES
_DEPT_COL_MAP = {
    "beer":                "beer",
    "cigarettes":          "cigs",
    "dairy":               "dairy",
    "grocery non-taxable": "n_tax",
    "grocery taxable":     "tax",
    "ice":                 "ice",
    "live bait":           "lbait",
    "pizzza":              "pizza",
    "pizza":               "pizza",
    "pop":                 "pop",
    "pre roll":            "preroll",
    "tobacco":             "tobbaco",
    "vape & delta":        "vape",
    "wine and beer":       "wine",
    "propain tank":        "propane",
    "pay in":              "payin",
}

DAILY_HEADERS = [
    "DATE",
    "BEER", "CIGS", "DAIRY", "N.TAX", "TAX", "ICE", "LBAIT",
    "PIZZA", "POP", "PREROLL", "TOBBACO", "VAPE", "WINE", "PROPANE",
    "SALE",
    "ONLINE", "INSTANT",
    "LOTTO", "L.CREDIT",
    "ATM", "CASH", "CHECK", "CREDIT", "COUPON", "P.TAB", "S.TAX",
    "PAYIN", "FOODS", "PAYOUT", "REASON", "2 ALTRI",
    "G.TOT",
]

# INVENTORY (COGS) — vendor columns
# Sub-categories and their vendor lists
_GROCERY_VENDORS = [
    "ACE UNLIMITED", "HD DISTRIBUTION", "PIZZAHUNT", "HERSHEY", "GRIPPO",
    "SAMS", "SUNSET", "MR WHOLESALE", "BAIT", "ICE", "FRITOLAY", "WALMART",
    "CM OIL", "LEAVES", "REGAL", "ROMA WHOLESALE", "ANGEL WHOLESALE",
    "GREIS", "SVV", "COREMARK", "EMPORIUM WHOLESALE",
]
_SODA_VENDORS = ["PEPSI", "REDBULL", "COCA COLA", "7UP"]
_BEER_VENDORS  = [
    "OHIO EAGLE", "OHIO VANGUARD", "BONERIGHT", "GLAZER",
    "HEIDELBURG", "RHINESE", "SOUTHERN G", "PULSTAR",
]

COGS_VENDOR_COLS = ["DATE"] + _GROCERY_VENDORS + _SODA_VENDORS + _BEER_VENDORS + ["TOTAL"]

# Column index (1-based) for each vendor in the COGS sheet
_VENDOR_COL_INDEX: dict[str, int] = {
    v: i + 1 for i, v in enumerate(COGS_VENDOR_COLS)
}

# Vendor alias map: lowercase alias → exact column name
VENDOR_ALIAS_MAP: dict[str, str] = {}
for _v in COGS_VENDOR_COLS:
    VENDOR_ALIAS_MAP[_v.lower()] = _v
# Extra aliases
_EXTRA_ALIASES = {
    "ace":               "ACE UNLIMITED",
    "hd":                "HD DISTRIBUTION",
    "hd distrib":        "HD DISTRIBUTION",
    "hersheys":          "HERSHEY",
    "frito":             "FRITOLAY",
    "frito lay":         "FRITOLAY",
    "frito-lay":         "FRITOLAY",
    "sams club":         "SAMS",
    "sam's club":        "SAMS",
    "mr whole":          "MR WHOLESALE",
    "roma":              "ROMA WHOLESALE",
    "angel":             "ANGEL WHOLESALE",
    "emporio":           "EMPORIUM WHOLESALE",
    "emporium":          "EMPORIUM WHOLESALE",
    "red bull":          "REDBULL",
    "coke":              "COCA COLA",
    "cocacola":          "COCA COLA",
    "coca-cola":         "COCA COLA",
    "ohio eagle":        "OHIO EAGLE",
    "ohio vanguard":     "OHIO VANGUARD",
    "vanguard":          "OHIO VANGUARD",
    "boneright":         "BONERIGHT",
    "glazer":            "GLAZER",
    "heidelburg":        "HEIDELBURG",
    "heidelberg":        "HEIDELBURG",
    "rhinese":           "RHINESE",
    "southern":          "SOUTHERN G",
    "southern g":        "SOUTHERN G",
    "pulstar":           "PULSTAR",
}
VENDOR_ALIAS_MAP.update({k.lower(): v for k, v in _EXTRA_ALIASES.items()})

# EXPENSES left columns
EXPENSES_HEADERS = [
    "DATE", "NRS", "ELECTRICITY", "GARBAGE", "LAWYER", "ABT",
    "MAINTENANCE", "TAXES", "GRASSCUT", "UTILITIES", "INSURANCE",
    "RENT", "R.PATEL", "VECTOR", "PEST CONTROL", "SPECTRUM",
    "INVENTORY", "TOTAL",
]

# PAYROLL sub-section (starts at _PAYROLL_COL_START)
PAYROLL_HEADERS = [
    "DATE", "SIMMT", "ARMAAN", "KARAN", "YOGESH",
    "UGAIN", "ANUSHA", "KRISHALA", "TOTAL",
]

# ORDERS sub-section (starts at _ORDERS_COL_START)
ORDERS_HEADERS = ["DATE", "CORDER", "AMOUNT", "INVOICE"]

# REVENUES — REBATES table (left, starts col A)
REBATES_HEADERS = [
    "DATE", "USSMOKE", "PMHELIX", "ALG", "LIGGET", "ITG", "NDA",
    "ATMUAIR", "COREM", "JC&CO", "REYNOLD", "INMAR", "BITCOIN",
    "MISCELLANEOUS", "LOTTO", "TOTAL",
]

# REVENUES — PROFIT TOOK HOME table (right, starts at _PROFIT_COL_START)
PROFIT_HEADERS = [
    "DATE", "COMMITTE", "CAR PAYMENT", "FOOD",
    "FOR HOUSE", "TAXABLE", "EXTRA", "TOTAL",
]

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
_GREEN_DARK  = {"red": 0.118, "green": 0.490, "blue": 0.204}
_GREEN_LIGHT = {"red": 0.851, "green": 0.918, "blue": 0.827}
_WHITE       = {"red": 1.0,   "green": 1.0,   "blue": 1.0}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_client() -> gspread.Client:
    creds = Credentials.from_service_account_file(
        settings.google_credentials_file, scopes=SCOPES
    )
    return gspread.authorize(creds)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_request(sheet_id: int, start_row: int, end_row: int,
                 start_col: int, end_col: int, fmt: dict) -> dict:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row - 1,
                "endRowIndex": end_row,
                "startColumnIndex": start_col - 1,
                "endColumnIndex": end_col,
            },
            "cell": {"userEnteredFormat": fmt},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
        }
    }


def _section_title_fmt(sid: int, row: int, ncols: int) -> dict:
    return _fmt_request(sid, row, row, 1, ncols, {
        "backgroundColor": _GREEN_DARK,
        "textFormat": {"bold": True, "foregroundColor": _WHITE, "fontSize": 11},
        "horizontalAlignment": "CENTER",
    })


def _col_header_fmt(sid: int, row: int, ncols: int) -> dict:
    return _fmt_request(sid, row, row, 1, ncols, {
        "backgroundColor": _GREEN_LIGHT,
        "textFormat": {"bold": True},
        "horizontalAlignment": "CENTER",
    })


def _total_row_fmt(sid: int, row: int, ncols: int) -> dict:
    return _fmt_request(sid, row, row, 1, ncols, {
        "backgroundColor": _GREEN_LIGHT,
        "textFormat": {"bold": True},
    })


def _apply_formatting(spreadsheet: gspread.Spreadsheet, sheet: gspread.Worksheet) -> None:
    sid = sheet.id
    nd  = len(DAILY_HEADERS)
    nc  = len(COGS_VENDOR_COLS)
    # Full width needed for EXPENSES row (payroll + orders on right)
    ne_full = _ORDERS_COL_START + len(ORDERS_HEADERS) - 1
    # Full width needed for REVENUES row (rebates + profit side-by-side)
    nr_full = _PROFIT_COL_START + len(PROFIT_HEADERS) - 1

    def _partial_header_fmt(row: int, start_col: int, end_col: int) -> dict:
        return _fmt_request(sid, row, row, start_col, end_col, {
            "backgroundColor": _GREEN_LIGHT,
            "textFormat": {"bold": True},
            "horizontalAlignment": "CENTER",
        })

    requests = [
        # DAILY SALES
        _section_title_fmt(sid, _DAILY_TITLE_ROW, nd),
        _col_header_fmt(sid, _DAILY_HEADER_ROW, nd),
        _total_row_fmt(sid, _DAILY_TOTAL_ROW, nd),
        # COGS
        _section_title_fmt(sid, _COGS_TITLE_ROW, nc),
        _col_header_fmt(sid, _COGS_SUBCAT_ROW, nc),
        _col_header_fmt(sid, _COGS_HEADER_ROW, nc),
        _total_row_fmt(sid, _COGS_TOTAL_ROW, nc),
        # EXPENSES title spans full width
        _section_title_fmt(sid, _EXP_TITLE_ROW, ne_full),
        # EXPENSES left header
        _col_header_fmt(sid, _EXP_HEADER_ROW, len(EXPENSES_HEADERS)),
        # PAYROLL sub-label on title row (reuse light green for sub-label)
        _partial_header_fmt(_EXP_TITLE_ROW, _PAYROLL_COL_START,
                            _PAYROLL_COL_START + len(PAYROLL_HEADERS) - 1),
        # PAYROLL column headers
        _partial_header_fmt(_EXP_HEADER_ROW, _PAYROLL_COL_START,
                            _PAYROLL_COL_START + len(PAYROLL_HEADERS) - 1),
        # ORDERS sub-label + headers
        _partial_header_fmt(_EXP_TITLE_ROW, _ORDERS_COL_START,
                            _ORDERS_COL_START + len(ORDERS_HEADERS) - 1),
        _partial_header_fmt(_EXP_HEADER_ROW, _ORDERS_COL_START,
                            _ORDERS_COL_START + len(ORDERS_HEADERS) - 1),
        # REVENUES title spans full width
        _section_title_fmt(sid, _REV_TITLE_ROW, nr_full),
        # REBATES sub-header
        _partial_header_fmt(_REV_SUBHDR_ROW, 1, len(REBATES_HEADERS)),
        # PROFIT TOOK HOME sub-header
        _partial_header_fmt(_REV_SUBHDR_ROW, _PROFIT_COL_START,
                            _PROFIT_COL_START + len(PROFIT_HEADERS) - 1),
        # REBATES column headers
        _col_header_fmt(sid, _REV_HEADER_ROW, len(REBATES_HEADERS)),
        # PROFIT column headers
        _partial_header_fmt(_REV_HEADER_ROW, _PROFIT_COL_START,
                            _PROFIT_COL_START + len(PROFIT_HEADERS) - 1),
        # Freeze daily sales header
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sid,
                    "gridProperties": {"frozenRowCount": _DAILY_HEADER_ROW},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
    ]
    spreadsheet.batch_update({"requests": requests})


# ---------------------------------------------------------------------------
# Monthly tab management
# ---------------------------------------------------------------------------

def _month_tab_name(d: date) -> str:
    return d.strftime("%B %Y").upper()


def _get_or_create_monthly_tab(
    spreadsheet: gspread.Spreadsheet, target_date: date
) -> gspread.Worksheet:
    title = _month_tab_name(target_date)
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return _create_monthly_tab(spreadsheet, target_date, title)


def _create_monthly_tab(
    spreadsheet: gspread.Spreadsheet, target_date: date, title: str
) -> gspread.Worksheet:
    days = calendar.monthrange(target_date.year, target_date.month)[1]
    ncols = max(
        len(DAILY_HEADERS),
        len(COGS_VENDOR_COLS),
        _ORDERS_COL_START + len(ORDERS_HEADERS),
        _PROFIT_COL_START + len(PROFIT_HEADERS),
    ) + 2
    nrows = _REV_DATA_START + days + 5
    sheet = spreadsheet.add_worksheet(title=title, rows=nrows, cols=ncols)

    updates: list[dict] = []

    def row_update(row: int, values: list) -> dict:
        end_col = gspread.utils.rowcol_to_a1(row, len(values))
        return {"range": f"A{row}:{end_col}", "values": [values]}

    # ── DAILY SALES ──────────────────────────────────────────────────────
    updates.append(row_update(_DAILY_TITLE_ROW, ["DAILY SALES"]))
    updates.append(row_update(_DAILY_HEADER_ROW, DAILY_HEADERS))
    for day in range(1, days + 1):
        d = date(target_date.year, target_date.month, day)
        updates.append(row_update(_DAILY_DATA_START + day - 1, [d.strftime("%Y-%m-%d")]))
    updates.append(row_update(_DAILY_TOTAL_ROW, ["TOTAL"]))

    # ── INVENTORY (COGS) ─────────────────────────────────────────────────
    updates.append(row_update(_COGS_TITLE_ROW, ["INVENTORY (COGS)"]))

    # Sub-category header row: label each group
    subcat_row = ["DATE"]
    subcat_row += ["SPECIFIC ITEMS GROCERY VENDORS/STORE PICKUP"] + [""] * (len(_GROCERY_VENDORS) - 1)
    subcat_row += ["SODA"] + [""] * (len(_SODA_VENDORS) - 1)
    subcat_row += ["BEER"] + [""] * (len(_BEER_VENDORS) - 1)
    subcat_row += [""]  # TOTAL
    updates.append(row_update(_COGS_SUBCAT_ROW, subcat_row))
    updates.append(row_update(_COGS_HEADER_ROW, COGS_VENDOR_COLS))

    for day in range(1, days + 1):
        d = date(target_date.year, target_date.month, day)
        updates.append(row_update(_COGS_DATA_START + day - 1, [d.strftime("%Y-%m-%d")]))
    updates.append(row_update(_COGS_TOTAL_ROW, ["TOTAL"]))

    # ── EXPENSES ─────────────────────────────────────────────────────────
    updates.append(row_update(_EXP_TITLE_ROW, ["EXPENSES"]))
    updates.append(row_update(_EXP_HEADER_ROW, EXPENSES_HEADERS))

    # PAYROLL sub-section (same rows, different columns)
    def row_update_at(row: int, start_col: int, values: list) -> dict:
        start_a1 = gspread.utils.rowcol_to_a1(row, start_col)
        end_a1   = gspread.utils.rowcol_to_a1(row, start_col + len(values) - 1)
        return {"range": f"{start_a1}:{end_a1}", "values": [values]}

    updates.append(row_update_at(_EXP_TITLE_ROW, _PAYROLL_COL_START, ["PAYROLL"]))
    updates.append(row_update_at(_EXP_HEADER_ROW, _PAYROLL_COL_START, PAYROLL_HEADERS))
    updates.append(row_update_at(_EXP_TITLE_ROW, _ORDERS_COL_START, ["ORDERS"]))
    updates.append(row_update_at(_EXP_HEADER_ROW, _ORDERS_COL_START, ORDERS_HEADERS))

    for day in range(1, days + 1):
        d = date(target_date.year, target_date.month, day)
        row = _EXP_DATA_START + day - 1
        updates.append(row_update(row, [d.strftime("%Y-%m-%d")]))
        updates.append(row_update_at(row, _PAYROLL_COL_START, [d.strftime("%Y-%m-%d")]))
        updates.append(row_update_at(row, _ORDERS_COL_START,  [d.strftime("%Y-%m-%d")]))

    # ── REVENUES ─────────────────────────────────────────────────────────
    updates.append(row_update(_REV_TITLE_ROW, ["REVENUES"]))
    # Sub-headers
    updates.append(row_update(_REV_SUBHDR_ROW, ["REBATES"]))
    updates.append(row_update_at(_REV_SUBHDR_ROW, _PROFIT_COL_START, ["PROFIT TOOK HOME"]))
    # Column headers
    updates.append(row_update(_REV_HEADER_ROW, REBATES_HEADERS))
    updates.append(row_update_at(_REV_HEADER_ROW, _PROFIT_COL_START, PROFIT_HEADERS))
    # Pre-fill dates for both tables
    for day in range(1, days + 1):
        d = date(target_date.year, target_date.month, day)
        row = _REV_DATA_START + day - 1
        updates.append(row_update(row, [d.strftime("%Y-%m-%d")]))
        updates.append(row_update_at(row, _PROFIT_COL_START, [d.strftime("%Y-%m-%d")]))

    sheet.batch_update(updates)
    _apply_formatting(spreadsheet, sheet)
    return sheet


def cleanup_old_tabs(spreadsheet: gspread.Spreadsheet) -> None:
    old = {"Daily Sales", "Transactions", "Inventory", "Bank", "Sheet1"}
    for ws in spreadsheet.worksheets():
        if ws.title in old:
            try:
                spreadsheet.del_worksheet(ws)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Daily sales logging
# ---------------------------------------------------------------------------

def log_daily_sales(sales_data: dict[str, Any]) -> str:
    target_date = date.fromisoformat(sales_data.get("date", str(date.today())))
    client = _get_client()
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    sheet = _get_or_create_monthly_tab(spreadsheet, target_date)

    dept_vals: dict[str, float] = {}
    for d in sales_data.get("departments", []):
        col = _DEPT_COL_MAP.get(d["name"].lower())
        if col:
            dept_vals[col] = d.get("sales", 0)

    row_data = [
        sales_data.get("date", ""),
        dept_vals.get("beer", 0),   dept_vals.get("cigs", 0),
        dept_vals.get("dairy", 0),  dept_vals.get("n_tax", 0),
        dept_vals.get("tax", 0),    dept_vals.get("ice", 0),
        dept_vals.get("lbait", 0),  dept_vals.get("pizza", 0),
        dept_vals.get("pop", 0),    dept_vals.get("preroll", 0),
        dept_vals.get("tobbaco", 0),dept_vals.get("vape", 0),
        dept_vals.get("wine", 0),   dept_vals.get("propane", 0),
        sales_data.get("product_sales", 0),
        sales_data.get("lotto_online", 0),
        sales_data.get("lotto_in", 0),
        sales_data.get("lotto_po", 0),
        sales_data.get("lotto_cr", 0),
        sales_data.get("atm", 0),
        sales_data.get("cash_drops", 0),
        sales_data.get("check", 0),
        sales_data.get("card", 0),
        sales_data.get("coupon", 0),
        sales_data.get("pull_tab", 0),
        sales_data.get("sales_tax", 0),
        dept_vals.get("payin", 0),
        sales_data.get("food_stamp", 0),
        sales_data.get("vendor", 0),
        "",
        sales_data.get("loyalty", 0),
        sales_data.get("grand_total", 0),
    ]

    target_row = _DAILY_DATA_START + target_date.day - 1
    col_end = gspread.utils.rowcol_to_a1(target_row, len(row_data))
    sheet.update(f"A{target_row}:{col_end}", [row_data])
    return f"Daily sales logged for {sales_data.get('date')} → {_month_tab_name(target_date)}"


# ---------------------------------------------------------------------------
# COGS / Invoice logging
# ---------------------------------------------------------------------------

def resolve_vendor(name: str) -> str:
    """Resolve a vendor alias/name to the exact column name. Returns uppercased raw name if not found."""
    return VENDOR_ALIAS_MAP.get(name.lower().strip(), name.upper().strip())


def log_cogs_entry(
    vendor: str,
    amount: float,
    entry_date: date | None = None,
    invoice_num: str = "",
    notes: str = "",
) -> str:
    if entry_date is None:
        entry_date = date.today()

    exact_vendor = resolve_vendor(vendor)
    if exact_vendor not in _VENDOR_COL_INDEX or exact_vendor in ("DATE", "TOTAL"):
        # Unknown vendor — log to a notes column at the end
        col_idx = len(COGS_VENDOR_COLS) + 1
        label = vendor
    else:
        col_idx = _VENDOR_COL_INDEX[exact_vendor]
        label = exact_vendor

    client = _get_client()
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)

    # Row = COGS_DATA_START + (day - 1)
    target_row = _COGS_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)
    sheet.update(cell, [[amount]])

    return f"COGS logged: {label} ${amount:.2f} on {entry_date} → {_month_tab_name(entry_date)}"


def mark_invoice_paid(vendor: str, entry_date: date) -> str:
    """
    Turn the COGS cell for this vendor+date green to indicate the bank confirmed payment.
    If the vendor isn't found in the sheet, silently returns.
    """
    exact_vendor = resolve_vendor(vendor)
    if exact_vendor not in _VENDOR_COL_INDEX or exact_vendor in ("DATE", "TOTAL"):
        return f"Vendor {vendor!r} not in COGS columns — skipped paid mark"

    col_idx = _VENDOR_COL_INDEX[exact_vendor]
    target_row = _COGS_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)

    client = _get_client()
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)

    # Green background = bank confirmed payment
    sheet.format(cell, {
        "backgroundColor": {"red": 0.71, "green": 0.84, "blue": 0.66}  # soft green
    })

    return f"Marked PAID: {exact_vendor} on {entry_date} (cell {cell} → green)"


# ---------------------------------------------------------------------------
# Expense logging
# ---------------------------------------------------------------------------

# Maps lowercase alias → exact EXPENSES_HEADERS column name
_EXPENSE_COL_MAP: dict[str, str] = {h.lower(): h for h in EXPENSES_HEADERS if h not in ("DATE", "TOTAL")}
_EXPENSE_ALIASES: dict[str, str] = {
    "electric":       "ELECTRICITY",
    "hydro":          "ELECTRICITY",
    "power":          "ELECTRICITY",
    "trash":          "GARBAGE",
    "garbage":        "GARBAGE",
    "legal":          "LAWYER",
    "attorney":       "LAWYER",
    "abt":            "ABT",
    "repair":         "MAINTENANCE",
    "fix":            "MAINTENANCE",
    "property tax":   "TAXES",
    "tax":            "TAXES",
    "lawn":           "GRASSCUT",
    "grass":          "GRASSCUT",
    "water":          "UTILITIES",
    "gas":            "UTILITIES",
    "pest":           "PEST CONTROL",
    "exterminator":   "PEST CONTROL",
    "internet":       "SPECTRUM",
    "cable":          "SPECTRUM",
    "spectrum":       "SPECTRUM",
    "nrs":            "NRS",
    "r patel":        "R.PATEL",
    "rpatel":         "R.PATEL",
    "vector":         "VECTOR",
    "insurance":      "INSURANCE",
    "rent":           "RENT",
    "inventory":      "INVENTORY",
}
_EXPENSE_COL_MAP.update({k.lower(): v for k, v in _EXPENSE_ALIASES.items()})


def resolve_expense_category(name: str) -> str | None:
    """Resolve a natural-language expense name to a column header. Returns None if not found."""
    return _EXPENSE_COL_MAP.get(name.lower().strip())


def log_expense(category: str, amount: float, entry_date: date | None = None) -> str:
    """Log an expense to the EXPENSES section of the monthly sheet."""
    if entry_date is None:
        entry_date = date.today()

    col_name = resolve_expense_category(category)
    if not col_name:
        col_name = "INVENTORY"  # fallback column for unrecognised categories

    col_idx = EXPENSES_HEADERS.index(col_name) + 1  # 1-based

    client = _get_client()
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)

    target_row = _EXP_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)
    sheet.update(cell, [[amount]])

    return f"Expense logged: {col_name} ${amount:.2f} on {entry_date}"


# ---------------------------------------------------------------------------
# Payroll logging
# ---------------------------------------------------------------------------

# Map lowercase name/alias → exact PAYROLL_HEADERS column name
_PAYROLL_COL_MAP: dict[str, str] = {h.lower(): h for h in PAYROLL_HEADERS if h not in ("DATE", "TOTAL")}


def resolve_payroll_name(name: str) -> str | None:
    return _PAYROLL_COL_MAP.get(name.lower().strip())


def log_payroll(employee: str, amount: float, entry_date: date | None = None) -> str:
    """Log a payroll payment to the PAYROLL section of the monthly sheet."""
    if entry_date is None:
        entry_date = date.today()

    col_name = resolve_payroll_name(employee)
    if not col_name:
        return f"⚠️ Employee '{employee}' not found in payroll. Known names: {', '.join(h for h in PAYROLL_HEADERS if h not in ('DATE', 'TOTAL'))}"

    # col index within PAYROLL_HEADERS (0-based) → absolute sheet column (1-based)
    local_idx = PAYROLL_HEADERS.index(col_name)
    col_idx = _PAYROLL_COL_START + local_idx

    client = _get_client()
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)

    target_row = _EXP_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)
    sheet.update(cell, [[amount]])

    return f"Payroll logged: {col_name} ${amount:.2f} on {entry_date}"


# ---------------------------------------------------------------------------
# Rebate logging
# ---------------------------------------------------------------------------

_REBATE_COL_MAP: dict[str, str] = {h.lower(): h for h in REBATES_HEADERS if h not in ("DATE", "TOTAL")}
_REBATE_ALIASES: dict[str, str] = {
    "us smoke":   "USSMOKE",
    "ussmoke":    "USSMOKE",
    "pm helix":   "PMHELIX",
    "pmhelix":    "PMHELIX",
    "altria":     "PMHELIX",
    "alg":        "ALG",
    "liggett":    "LIGGET",
    "ligget":     "LIGGET",
    "itg":        "ITG",
    "nda":        "NDA",
    "atm uair":   "ATMUAIR",
    "atmuair":    "ATMUAIR",
    "coremark":   "COREM",
    "corem":      "COREM",
    "jc":         "JC&CO",
    "jcco":       "JC&CO",
    "reynolds":   "REYNOLD",
    "reynold":    "REYNOLD",
    "rj reynolds":"REYNOLD",
    "inmar":      "INMAR",
    "bitcoin":    "BITCOIN",
    "misc":       "MISCELLANEOUS",
    "lotto":      "LOTTO",
    "lottery":    "LOTTO",
}
_REBATE_COL_MAP.update({k.lower(): v for k, v in _REBATE_ALIASES.items()})


def resolve_rebate_vendor(name: str) -> str | None:
    return _REBATE_COL_MAP.get(name.lower().strip())


def log_rebate(vendor: str, amount: float, entry_date: date | None = None) -> str:
    """Log a rebate to the REVENUES → REBATES section of the monthly sheet."""
    if entry_date is None:
        entry_date = date.today()

    col_name = resolve_rebate_vendor(vendor)
    if not col_name:
        col_name = "MISCELLANEOUS"

    col_idx = REBATES_HEADERS.index(col_name) + 1  # 1-based (starts at col A = 1)

    client = _get_client()
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)

    target_row = _REV_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)
    sheet.update(cell, [[amount]])

    return f"Rebate logged: {col_name} ${amount:.2f} on {entry_date}"


# ---------------------------------------------------------------------------
# Revenue / profit took home logging
# ---------------------------------------------------------------------------

_PROFIT_COL_MAP: dict[str, str] = {h.lower(): h for h in PROFIT_HEADERS if h not in ("DATE", "TOTAL")}
_PROFIT_ALIASES: dict[str, str] = {
    "committee":    "COMMITTE",
    "committe":     "COMMITTE",
    "car":          "CAR PAYMENT",
    "car payment":  "CAR PAYMENT",
    "food":         "FOOD",
    "house":        "FOR HOUSE",
    "for house":    "FOR HOUSE",
    "home":         "FOR HOUSE",
    "taxable":      "TAXABLE",
    "extra":        "EXTRA",
}
_PROFIT_COL_MAP.update({k.lower(): v for k, v in _PROFIT_ALIASES.items()})


def resolve_revenue_category(name: str) -> str | None:
    return _PROFIT_COL_MAP.get(name.lower().strip())


def log_revenue(category: str, amount: float, entry_date: date | None = None) -> str:
    """Log a revenue/profit-took-home entry to the REVENUES → PROFIT TOOK HOME section."""
    if entry_date is None:
        entry_date = date.today()

    col_name = resolve_revenue_category(category)
    if not col_name:
        col_name = "EXTRA"

    # PROFIT TOOK HOME columns start at _PROFIT_COL_START
    col_idx = _PROFIT_COL_START + PROFIT_HEADERS.index(col_name)

    client = _get_client()
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)

    target_row = _REV_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)
    sheet.update(cell, [[amount]])

    return f"Revenue logged: {col_name} ${amount:.2f} on {entry_date}"


# ---------------------------------------------------------------------------
# Legacy stubs
# ---------------------------------------------------------------------------

def log_transactions(transactions: list[dict[str, Any]], target_date: str = "") -> str:
    return f"Transactions skipped ({len(transactions)} dept rows)"


def log_inventory(inventory_data: dict[str, Any]) -> str:
    return "Inventory snapshot skipped"


def read_recent_sales(days: int = 7) -> list[dict[str, Any]]:
    return []


# ---------------------------------------------------------------------------
# Bank reconciliation — sheet lookup & highlight helpers
# ---------------------------------------------------------------------------

def _parse_cell_amount(val: Any) -> float | None:
    """Parse a cell value to float; return None if empty or non-numeric."""
    if not val:
        return None
    try:
        return float(str(val).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def _months_to_check(days_back: int) -> list[date]:
    """Return month-start dates covering today back to days_back, most recent first."""
    today = date.today()
    earliest = today - timedelta(days=days_back)
    months: list[date] = []
    d = earliest.replace(day=1)
    while d <= today.replace(day=1):
        months.append(d)
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return list(reversed(months))


def find_cogs_by_vendor(vendor: str, amount: float, days_back: int = 14) -> tuple[date, str] | None:
    """
    Search COGS section for a vendor + amount match (±$1, within days_back days).
    Returns (entry_date, exact_vendor_col) or None.
    """
    exact_vendor = resolve_vendor(vendor)
    if exact_vendor not in _VENDOR_COL_INDEX or exact_vendor in ("DATE", "TOTAL"):
        return None
    col_idx = _VENDOR_COL_INDEX[exact_vendor]
    client = _get_client()
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    today = date.today()

    for month_start in _months_to_check(days_back):
        try:
            sheet = _get_or_create_monthly_tab(spreadsheet, month_start)
            days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
            start_cell = gspread.utils.rowcol_to_a1(_COGS_DATA_START, col_idx)
            end_cell   = gspread.utils.rowcol_to_a1(_COGS_DATA_START + days_in_month - 1, col_idx)
            col_values = sheet.get(f"{start_cell}:{end_cell}")
            for day_offset, row in enumerate(col_values):
                val = _parse_cell_amount(row[0] if row else None)
                if val is not None and abs(val - amount) <= 1.0:
                    entry_date = month_start.replace(day=day_offset + 1)
                    if (today - entry_date).days <= days_back:
                        return (entry_date, exact_vendor)
        except Exception:
            continue
    return None


def find_cogs_by_amount(amount: float, days_back: int = 14) -> list[tuple[date, str, float]]:
    """
    Search COGS section for any vendor with matching amount (±$1, within days_back days).
    Returns list of (entry_date, vendor_col, cell_amount), most recent first, capped at 3.
    """
    client = _get_client()
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    today = date.today()
    matches: list[tuple[date, str, float]] = []

    for month_start in _months_to_check(days_back):
        try:
            sheet = _get_or_create_monthly_tab(spreadsheet, month_start)
            days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
            start_cell = gspread.utils.rowcol_to_a1(_COGS_DATA_START, 1)
            end_cell   = gspread.utils.rowcol_to_a1(_COGS_DATA_START + days_in_month - 1, len(COGS_VENDOR_COLS))
            all_rows   = sheet.get(f"{start_cell}:{end_cell}")
            for day_offset, row in enumerate(all_rows):
                for col_offset, cell in enumerate(row):
                    val = _parse_cell_amount(cell)
                    if val is not None and abs(val - amount) <= 1.0:
                        vendor_col = COGS_VENDOR_COLS[col_offset]
                        if vendor_col in ("DATE", "TOTAL"):
                            continue
                        entry_date = month_start.replace(day=day_offset + 1)
                        if (today - entry_date).days <= days_back:
                            matches.append((entry_date, vendor_col, val))
        except Exception:
            continue

    matches.sort(key=lambda x: x[0], reverse=True)
    return matches[:3]


def find_expense_by_category(category: str, amount: float, days_back: int = 14) -> tuple[date, str] | None:
    """
    Search EXPENSES section for a category + amount match (±$1, within days_back days).
    Returns (entry_date, col_name) or None.
    """
    col_name = resolve_expense_category(category)
    if not col_name:
        return None
    col_idx = EXPENSES_HEADERS.index(col_name) + 1
    client = _get_client()
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    today = date.today()

    for month_start in _months_to_check(days_back):
        try:
            sheet = _get_or_create_monthly_tab(spreadsheet, month_start)
            days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
            start_cell = gspread.utils.rowcol_to_a1(_EXP_DATA_START, col_idx)
            end_cell   = gspread.utils.rowcol_to_a1(_EXP_DATA_START + days_in_month - 1, col_idx)
            col_values = sheet.get(f"{start_cell}:{end_cell}")
            for day_offset, row in enumerate(col_values):
                val = _parse_cell_amount(row[0] if row else None)
                if val is not None and abs(val - amount) <= 1.0:
                    entry_date = month_start.replace(day=day_offset + 1)
                    if (today - entry_date).days <= days_back:
                        return (entry_date, col_name)
        except Exception:
            continue
    return None


def find_expense_by_amount(amount: float, days_back: int = 14) -> list[tuple[date, str, float]]:
    """
    Search EXPENSES section for any category with matching amount (±$1, within days_back days).
    Returns list of (entry_date, col_name, cell_amount), most recent first, capped at 3.
    """
    client = _get_client()
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    today = date.today()
    matches: list[tuple[date, str, float]] = []

    for month_start in _months_to_check(days_back):
        try:
            sheet = _get_or_create_monthly_tab(spreadsheet, month_start)
            days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
            n_cols = len(EXPENSES_HEADERS) - 1  # skip TOTAL
            start_cell = gspread.utils.rowcol_to_a1(_EXP_DATA_START, 2)  # skip DATE
            end_cell   = gspread.utils.rowcol_to_a1(_EXP_DATA_START + days_in_month - 1, n_cols)
            all_rows   = sheet.get(f"{start_cell}:{end_cell}")
            for day_offset, row in enumerate(all_rows):
                for col_offset, cell in enumerate(row):
                    val = _parse_cell_amount(cell)
                    if val is not None and abs(val - amount) <= 1.0:
                        col_name = EXPENSES_HEADERS[col_offset + 1]  # +1 for DATE skip
                        if col_name == "TOTAL":
                            continue
                        entry_date = month_start.replace(day=day_offset + 1)
                        if (today - entry_date).days <= days_back:
                            matches.append((entry_date, col_name, val))
        except Exception:
            continue

    matches.sort(key=lambda x: x[0], reverse=True)
    return matches[:3]


def find_rebate_by_vendor(vendor: str, amount: float, days_back: int = 14) -> tuple[date, str] | None:
    """
    Search REBATES section for a vendor + amount match (±$1, within days_back days).
    Returns (entry_date, col_name) or None.
    """
    col_name = resolve_rebate_vendor(vendor)
    if not col_name:
        return None
    col_idx = REBATES_HEADERS.index(col_name) + 1
    client = _get_client()
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    today = date.today()

    for month_start in _months_to_check(days_back):
        try:
            sheet = _get_or_create_monthly_tab(spreadsheet, month_start)
            days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
            start_cell = gspread.utils.rowcol_to_a1(_REV_DATA_START, col_idx)
            end_cell   = gspread.utils.rowcol_to_a1(_REV_DATA_START + days_in_month - 1, col_idx)
            col_values = sheet.get(f"{start_cell}:{end_cell}")
            for day_offset, row in enumerate(col_values):
                val = _parse_cell_amount(row[0] if row else None)
                if val is not None and abs(val - amount) <= 1.0:
                    entry_date = month_start.replace(day=day_offset + 1)
                    if (today - entry_date).days <= days_back:
                        return (entry_date, col_name)
        except Exception:
            continue
    return None


def mark_expense_paid(category: str, entry_date: date) -> str:
    """Turn the expense cell for this category+date green (bank confirmed payment)."""
    col_name = resolve_expense_category(category)
    if not col_name:
        return f"Expense category '{category}' not found"
    col_idx    = EXPENSES_HEADERS.index(col_name) + 1
    target_row = _EXP_DATA_START + entry_date.day - 1
    cell       = gspread.utils.rowcol_to_a1(target_row, col_idx)
    client     = _get_client()
    sheet      = _get_or_create_monthly_tab(client.open_by_key(settings.google_sheet_id), entry_date)
    sheet.format(cell, {"backgroundColor": {"red": 0.71, "green": 0.84, "blue": 0.66}})
    return f"Marked PAID: {col_name} expense on {entry_date}"


def mark_cc_settled(sale_date: date, bank_deposit: float, bank_date: date) -> str:
    """
    Highlight the CREDIT column cell green for the matched sale day,
    indicating the CC settlement cleared in the bank.
    Adds a note with the bank deposit date and amount.
    """
    # CREDIT is in DAILY_HEADERS
    credit_idx = DAILY_HEADERS.index("CREDIT") + 1  # 1-based column
    target_row = _DAILY_DATA_START + sale_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, credit_idx)

    client = _get_client()
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    sheet = _get_or_create_monthly_tab(spreadsheet, sale_date)

    # Green background
    sheet.format(cell, {
        "backgroundColor": {"red": 0.71, "green": 0.84, "blue": 0.66}
    })
    # Add note with bank info (gspread >=5.x)
    try:
        sheet.update_note(cell, f"CC settled ${bank_deposit:,.2f} on {bank_date}")
    except Exception:
        pass  # note is optional
    return f"Marked CC settled: {sale_date} CREDIT cell → green (bank ${bank_deposit:,.2f} on {bank_date})"


def mark_rebate_paid(vendor: str, entry_date: date) -> str:
    """Turn the rebate cell for this vendor+date green (bank confirmed receipt)."""
    col_name = resolve_rebate_vendor(vendor)
    if not col_name:
        return f"Rebate vendor '{vendor}' not found"
    col_idx    = REBATES_HEADERS.index(col_name) + 1
    target_row = _REV_DATA_START + entry_date.day - 1
    cell       = gspread.utils.rowcol_to_a1(target_row, col_idx)
    client     = _get_client()
    sheet      = _get_or_create_monthly_tab(client.open_by_key(settings.google_sheet_id), entry_date)
    sheet.format(cell, {"backgroundColor": {"red": 0.71, "green": 0.84, "blue": 0.66}})
    return f"Marked PAID: {col_name} rebate on {entry_date}"


# ---------------------------------------------------------------------------
# Combined log-if-empty + highlight helpers (used by bank reconciler)
# ---------------------------------------------------------------------------

_PAID_GREEN = {"red": 0.71, "green": 0.84, "blue": 0.66}


def _read_cell_value(sheet, cell: str) -> float | None:
    """Read a single cell and return its parsed numeric value, or None if empty."""
    try:
        vals = sheet.get(cell)
        if vals and vals[0]:
            return _parse_cell_amount(vals[0][0])
    except Exception:
        pass
    return None


def log_expense_and_highlight(category: str, amount: float, entry_date: date) -> str:
    """Log expense to sheet if cell is empty, always highlight green.
    Preserves any existing manually-entered value in the cell.
    """
    col_name = resolve_expense_category(category)
    if not col_name:
        col_name = "INVENTORY"  # fallback
    col_idx = EXPENSES_HEADERS.index(col_name) + 1
    target_row = _EXP_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)

    client = _get_client()
    sheet = _get_or_create_monthly_tab(client.open_by_key(settings.google_sheet_id), entry_date)

    existing = _read_cell_value(sheet, cell)
    if existing is None:
        sheet.update(cell, [[amount]])
        action = f"logged ${amount:.2f}"
    else:
        action = f"kept existing ${existing:.2f}"

    sheet.format(cell, {"backgroundColor": _PAID_GREEN})
    return f"Expense {col_name} on {entry_date}: {action}, highlighted"


def log_invoice_and_highlight(vendor: str, amount: float, entry_date: date) -> str:
    """Log COGS invoice to sheet if cell is empty, always highlight green.
    Preserves any existing manually-entered value in the cell.
    """
    exact_vendor = resolve_vendor(vendor)
    if exact_vendor not in _VENDOR_COL_INDEX or exact_vendor in ("DATE", "TOTAL"):
        return f"Vendor {vendor!r} not in COGS columns — skipped"

    col_idx = _VENDOR_COL_INDEX[exact_vendor]
    target_row = _COGS_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)

    client = _get_client()
    sheet = _get_or_create_monthly_tab(client.open_by_key(settings.google_sheet_id), entry_date)

    existing = _read_cell_value(sheet, cell)
    if existing is None:
        sheet.update(cell, [[amount]])
        action = f"logged ${amount:.2f}"
    else:
        action = f"kept existing ${existing:.2f}"

    sheet.format(cell, {"backgroundColor": _PAID_GREEN})
    return f"COGS {exact_vendor} on {entry_date}: {action}, highlighted"


def log_rebate_and_highlight(vendor: str, amount: float, entry_date: date) -> str:
    """Log rebate to sheet if cell is empty, always highlight green.
    Preserves any existing manually-entered value in the cell.
    """
    col_name = resolve_rebate_vendor(vendor)
    if not col_name:
        col_name = "MISCELLANEOUS"
    col_idx = REBATES_HEADERS.index(col_name) + 1
    target_row = _REV_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)

    client = _get_client()
    sheet = _get_or_create_monthly_tab(client.open_by_key(settings.google_sheet_id), entry_date)

    existing = _read_cell_value(sheet, cell)
    if existing is None:
        sheet.update(cell, [[amount]])
        action = f"logged ${amount:.2f}"
    else:
        action = f"kept existing ${existing:.2f}"

    sheet.format(cell, {"backgroundColor": _PAID_GREEN})
    return f"Rebate {col_name} on {entry_date}: {action}, highlighted"


def log_payroll_and_highlight(employee: str, amount: float, entry_date: date) -> str:
    """Log payroll to sheet if cell is empty, always highlight green.
    Preserves any existing manually-entered value in the cell.
    If employee name can't be resolved to a known column, skips cleanly.
    """
    col_name = resolve_payroll_name(employee)
    if not col_name:
        return f"Payroll employee '{employee}' not in PAYROLL columns — skipped"

    local_idx = PAYROLL_HEADERS.index(col_name)
    col_idx = _PAYROLL_COL_START + local_idx
    target_row = _EXP_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)

    client = _get_client()
    sheet = _get_or_create_monthly_tab(client.open_by_key(settings.google_sheet_id), entry_date)

    existing = _read_cell_value(sheet, cell)
    if existing is None:
        sheet.update(cell, [[amount]])
        action = f"logged ${amount:.2f}"
    else:
        action = f"kept existing ${existing:.2f}"

    sheet.format(cell, {"backgroundColor": _PAID_GREEN})
    return f"Payroll {col_name} on {entry_date}: {action}, highlighted"


def match_description_to_cogs_vendor(description: str) -> str | None:
    """Return the COGS vendor column name if any alias appears in the description."""
    desc_lower = description.lower()
    for alias in sorted(VENDOR_ALIAS_MAP.keys(), key=len, reverse=True):
        if alias in ("date", "total"):
            continue
        if alias in desc_lower:
            return VENDOR_ALIAS_MAP[alias]
    return None


def match_description_to_expense(description: str) -> str | None:
    """Return the expense column name if any alias appears in the description."""
    desc_lower = description.lower()
    for alias in sorted(_EXPENSE_COL_MAP.keys(), key=len, reverse=True):
        if alias in ("date", "total", "inventory"):
            continue
        if alias in desc_lower:
            return _EXPENSE_COL_MAP[alias]
    return None


def match_description_to_rebate(description: str) -> str | None:
    """Return the rebate column name if any alias appears in the description."""
    desc_lower = description.lower()
    for alias in sorted(_REBATE_COL_MAP.keys(), key=len, reverse=True):
        if alias in ("date", "total"):
            continue
        if alias in desc_lower:
            return _REBATE_COL_MAP[alias]
    return None
