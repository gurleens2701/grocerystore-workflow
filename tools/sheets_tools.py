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

import asyncio
import calendar
from datetime import date, datetime, timedelta
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from config.settings import settings
from config.store_context import get_store_sheet_id

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

def get_daily_sheet_column(store_id: str, field_name: str) -> int | None:
    """
    Look up the 1-based column index for a field in the 'daily_sales' section
    from platform.store_sheet_mappings. Returns None if not found.
    Fix point: if a value writes to the wrong column, the mapping row in the DB is the bug.
    """
    try:
        import asyncio as _asyncio
        from sqlalchemy import select
        from db.database import get_async_session
        from db.models import StoreSheetMapping

        async def _query():
            async with get_async_session() as session:
                row = (await session.execute(
                    select(StoreSheetMapping).where(
                        StoreSheetMapping.store_id == store_id,
                        StoreSheetMapping.section == "daily_sales",
                        StoreSheetMapping.field_name == field_name,
                    )
                )).scalars().first()
                return row.column_index if row else None

        loop = _asyncio.get_event_loop()
        if loop.is_running():
            # Called from async context — run in executor to avoid nesting
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_asyncio.run, _query())
                return future.result(timeout=5)
        else:
            return loop.run_until_complete(_query())
    except Exception as _e:
        import logging
        logging.getLogger(__name__).debug("get_daily_sheet_column failed for %s: %s", field_name, _e)
        return None


def log_daily_sales(sales_data: dict[str, Any]) -> str:
    """Write daily sales to Google Sheet using store_sheet_mappings from DB.

    Column layout comes from platform.store_sheet_mappings for the active store.

    For dept.X field_names, X is matched against department names two ways:
      1. Direct lowercase match (e.g. "BEER" → dept.beer for Modisoft)
      2. Normalized short key via _DEPT_COL_MAP (e.g. "Cigarettes" → dept.cigs for NRS)
    Both layers coexist so existing NRS (Moraine) mappings need no DB changes.

    Un-mapped fields produce a WARNING log line and are left blank — no crash, no guess.
    Single batched worksheet.update() call; no per-cell writes.
    """
    import logging as _logging
    from sqlalchemy import select as _select
    from db.database import get_sync_session as _get_sync_session
    from db.models import StoreSheetMapping as _Mapping
    from config.store_context import get_active_store as _get_active_store

    _log = _logging.getLogger(__name__)
    store_id = _get_active_store()
    target_date = date.fromisoformat(sales_data.get("date", str(date.today())))

    from db.models import StoreDailyReportRule as _Rule

    with _get_sync_session() as _sess:
        mappings = _sess.execute(
            _select(_Mapping).where(
                _Mapping.store_id == store_id,
                _Mapping.section == "daily_sales",
            )
        ).scalars().all()

        # Pre-load right-side rules if computed total/over-short columns exist
        _needs_computed = any(
            m.field_name in ("sheet_total_payments", "sheet_over_short")
            for m in mappings
        )
        right_rules = (
            _sess.execute(
                _select(_Rule).where(
                    _Rule.store_id == store_id,
                    _Rule.section == "right",
                )
            ).scalars().all()
            if _needs_computed else []
        )

    if not mappings:
        raise ValueError(
            f"No sheet mappings for store '{store_id}' section 'daily_sales'. "
            "Populate platform.store_sheet_mappings."
        )

    max_col = max(m.column_index for m in mappings)
    row_data: list[Any] = [""] * max_col

    # Build dept lookup: both direct lowercase name AND _DEPT_COL_MAP short key.
    # Direct: Modisoft returns "BEER" → "beer" matches dept.beer.
    # Short key: NRS returns "Cigarettes" → _DEPT_COL_MAP maps it to "cigs" → matches dept.cigs.
    dept_vals: dict[str, float] = {}
    for d in sales_data.get("departments", []):
        raw_lower = d["name"].lower()
        dept_vals[raw_lower] = d.get("sales", 0)
        short_key = _DEPT_COL_MAP.get(raw_lower)
        if short_key:
            dept_vals[short_key] = d.get("sales", 0)

    for m in mappings:
        col_idx = m.column_index - 1  # 0-based
        field = m.field_name

        if field == "date":
            row_data[col_idx] = sales_data.get("date", "")
        elif field == "sheet_total_payments":
            total_right = round(sum(
                float(sales_data.get(r.field_name, 0) or 0)
                for r in right_rules
            ), 2)
            row_data[col_idx] = total_right
        elif field == "sheet_over_short":
            total_right = round(sum(
                float(sales_data.get(r.field_name, 0) or 0)
                for r in right_rules
            ), 2)
            gt = float(sales_data.get("grand_total", 0) or 0)
            row_data[col_idx] = round(total_right - gt, 2)
        elif field.startswith("dept."):
            dept_key = field[5:]  # strip "dept."
            val = dept_vals.get(dept_key)
            if val is not None:
                row_data[col_idx] = val
            else:
                _log.warning("store=%s: dept '%s' not in today's data; leaving blank", store_id, dept_key)
        else:
            val = sales_data.get(field)
            if val is None:
                _log.warning("store=%s: field '%s' not in sales_data; leaving blank", store_id, field)
            else:
                row_data[col_idx] = val

    client = _get_client()
    spreadsheet = client.open_by_key(get_store_sheet_id())
    sheet = _get_or_create_monthly_tab(spreadsheet, target_date)

    target_row = _DAILY_DATA_START + target_date.day - 1
    col_end = gspread.utils.rowcol_to_a1(target_row, len(row_data))
    sheet.update(f"A{target_row}:{col_end}", [row_data])
    return f"Daily sales logged for {sales_data.get('date')} → {_month_tab_name(target_date)}"


# ---------------------------------------------------------------------------
# Smart column lookup — reads the actual sheet headers, no hardcoded lists
# ---------------------------------------------------------------------------

def _normalize_label(s: str) -> str:
    """Lowercase + drop non-alphanumeric — for fuzzy matching column headers."""
    return "".join(c for c in s.lower() if c.isalnum())


def find_column_in_section(
    sheet,
    header_row: int,
    value_name: str,
    col_start: int = 1,
    col_end: int | None = None,
) -> tuple[int, str] | None:
    """Read the sheet's header row, fuzzy-find the column for value_name.

    Returns (1-based column index, actual label as it appears in the sheet)
    or None if no reasonable match.

    col_start/col_end constrain the search to a sub-range of the row — used
    when multiple sections share a header row (e.g. PAYROLL starts at col 20).

    Match priority:
      1. Exact (case-insensitive, alphanumerics only)
      2. Substring either direction ("pepsi" → "PEPSI CO", "PEPSI" → "pepsi cola")
      3. Difflib close-match (handles "bonbright" ↔ "BONERIGHT", "ligget" ↔ "LIGGETT")
    """
    header = sheet.row_values(header_row)
    if col_end is None:
        col_end = len(header)

    candidates: list[tuple[int, str]] = []
    for i in range(col_start - 1, min(col_end, len(header))):
        label = header[i].strip()
        if not label or label.upper() in ("DATE", "TOTAL"):
            continue
        candidates.append((i + 1, label))

    if not candidates:
        return None

    target = _normalize_label(value_name)
    if not target:
        return None

    # 1. Exact normalized match
    for idx, label in candidates:
        if _normalize_label(label) == target:
            return (idx, label)

    # 2. Substring either direction
    for idx, label in candidates:
        norm = _normalize_label(label)
        if target in norm or norm in target:
            return (idx, label)

    # 3. Difflib fuzzy match
    import difflib
    norm_to_pair = {_normalize_label(label): (idx, label) for idx, label in candidates}
    close = difflib.get_close_matches(target, list(norm_to_pair.keys()), n=1, cutoff=0.7)
    if close:
        return norm_to_pair[close[0]]

    return None


# ---------------------------------------------------------------------------
# Unified log_entry — discover sections from the sheet, fuzzy-match, write
# ---------------------------------------------------------------------------
#
# A "section" is detected by scanning for any column labeled "DATE" in the sheet.
# Each DATE column starts a new section. The section spans from that DATE column
# up to the next DATE column (or end of row). The section title is found by
# searching above the header row for a non-empty caps label aligned with or to
# the left of the section's start column.
#
# Why dynamic: owners add sections all the time (e.g. "GAS INVOICE $ / INVOICE
# CH#" appended to COGS). Hardcoding a config table would force a code change
# every time. Reading the sheet itself means: add a column → next call picks it up.

import time as _time

# Per-(sheet_id, tab_name) section cache. 5-min TTL.
_SECTION_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_SECTION_TTL = 300


def _discover_sections_uncached(sheet) -> list[dict]:
    """Scan the sheet, return a list of detected sections.

    Each entry: {
      "title": str,            # human-readable label for the section
      "header_row": int,       # 1-based row containing column labels
      "data_start_row": int,   # = header_row + 1
      "col_start": int,        # 1-based column where the section's DATE col sits
      "col_end": int,          # last column index belonging to this section
    }
    """
    all_values = sheet.get_all_values()
    sections: list[dict] = []
    for row_idx, row in enumerate(all_values, start=1):
        date_cols = [c + 1 for c, v in enumerate(row) if v.strip().upper() == "DATE"]
        if not date_cols:
            continue
        for i, date_col in enumerate(date_cols):
            if i + 1 < len(date_cols):
                col_end = date_cols[i + 1] - 1
            else:
                last_non_empty = max((c + 1 for c, v in enumerate(row) if v.strip()), default=date_col)
                col_end = last_non_empty
            all_titles = _find_titles_above(all_values, row_idx, date_col, col_end)
            primary_title = all_titles[0] if all_titles else None
            if not primary_title:
                # No title above the section's column range — fall back to the
                # section's first non-DATE column label (e.g. "GAS INVOICE $").
                for c in range(date_col, col_end + 1):
                    if c <= len(row):
                        candidate = row[c - 1].strip()
                        if candidate and candidate.upper() not in ("DATE", "TOTAL"):
                            primary_title = candidate
                            all_titles = [candidate]
                            break
            sections.append({
                "title": primary_title or f"section_r{row_idx}_c{date_col}",
                "all_titles": all_titles,
                "header_row": row_idx,
                "data_start_row": row_idx + 1,
                "col_start": date_col,
                "col_end": col_end,
            })
    return sections


def _find_titles_above(all_values: list, header_row_idx: int, col_start: int, col_end: int) -> list[str]:
    """Return EVERY non-DATE/TOTAL label found in rows above the header inside
    the section's column range. Used to disambiguate sub-sections (e.g.
    Hamilton's COGS has both 'INVENTORY (COGS' on row 36 and 'SPECIFIC ITEMS
    GROCERY VENDORS' on row 37 — we need both for alias matching to work)."""
    found: list[str] = []
    for offset in (1, 2, 3, 4):
        title_row_idx = header_row_idx - offset
        if title_row_idx < 1:
            break
        title_row = all_values[title_row_idx - 1]
        for c in range(col_start - 1, min(col_end, len(title_row))):
            label = title_row[c].strip()
            if not label or label.upper() in ("DATE", "TOTAL"):
                continue
            if len(label) > 1 and label not in found:
                found.append(label)
    return found


def _discover_sections(sheet) -> list[dict]:
    """5-min cached wrapper around _discover_sections_uncached."""
    key = (sheet.spreadsheet.id, sheet.title)
    now = _time.time()
    cached = _SECTION_CACHE.get(key)
    if cached and now - cached[0] < _SECTION_TTL:
        return cached[1]
    result = _discover_sections_uncached(sheet)
    _SECTION_CACHE[key] = (now, result)
    return result


def _section_data_columns(sheet, sec: dict) -> list[tuple[int, str]]:
    """Return [(col_idx, label), ...] for a section, excluding DATE/TOTAL/empty."""
    header = sheet.row_values(sec["header_row"])
    out: list[tuple[int, str]] = []
    for i in range(sec["col_start"] - 1, min(sec["col_end"], len(header))):
        label = header[i].strip()
        if not label or label.upper() in ("DATE", "TOTAL"):
            continue
        out.append((i + 1, label))
    return out


def _find_in_section_dynamic(sheet, sec: dict, item: str) -> tuple[int, str] | None:
    """Fuzzy-find item in a discovered section. Returns (col_idx, label) or None."""
    candidates = _section_data_columns(sheet, sec)
    if not candidates:
        return None
    target_norm = _normalize_label(item)
    if not target_norm:
        return None
    target_lower = item.lower().strip()

    # 1. Exact normalized match
    for idx, label in candidates:
        if _normalize_label(label) == target_norm:
            return (idx, label)

    # 2a. User typed less than the column name — target is a substring of label
    #     "rent" → "STORE RENT", "pepsi" → "PEPSI CO"
    for idx, label in candidates:
        if target_norm in _normalize_label(label):
            return (idx, label)

    # 2b. User typed more than the column name — label appears as a WORD in target
    #     "altria rebate" → "ALTRIA". Word boundary required to prevent
    #     "ICE" from matching "gas in[voice]" (substring without word boundary).
    import re
    target_for_words = re.sub(r"[^a-z0-9\s]", " ", target_lower)
    for idx, label in candidates:
        label_norm = _normalize_label(label)
        if not label_norm:
            continue
        pattern = r"\b" + re.escape(label_norm) + r"\b"
        if re.search(pattern, target_for_words):
            return (idx, label)

    # 3. Difflib fuzzy fallback (handles "bonbright" ↔ "BONERIGHT", etc.)
    import difflib
    norm_to_pair = {_normalize_label(label): (idx, label) for idx, label in candidates}
    close = difflib.get_close_matches(target_norm, list(norm_to_pair.keys()), n=1, cutoff=0.7)
    if close:
        return norm_to_pair[close[0]]
    return None


def _find_check_column(sheet, sec: dict) -> int | None:
    """Find a 'check #' or 'CH#' column inside a section. Returns col_idx or None."""
    header = sheet.row_values(sec["header_row"])
    for i in range(sec["col_start"] - 1, min(sec["col_end"], len(header))):
        norm = _normalize_label(header[i])
        if "check" in norm or norm.endswith("ch") or "ch#" in header[i].lower().replace(" ", ""):
            return i + 1
    return None


# Friendly hint aliases — accepted in the `section` arg to bias the search.
_SECTION_ALIASES = {
    "invoice":  ["inventory", "cogs", "invoice"],
    "expense":  ["expense", "expenses"],
    "payroll":  ["payroll"],
    "rebate":   ["rebate", "rebates"],
    "revenue":  ["profit", "revenue", "took home"],
    "gas":      ["gas invoice", "gas"],
    "daily":    ["daily sales", "daily"],
}


def _filter_sections_by_hint(sections: list[dict], hint: str) -> list[dict]:
    """Filter discovered sections to those whose title (or any parent title
    above) matches a hint alias. A section is kept if any of its all_titles
    contains an alias as a substring (or vice versa)."""
    aliases = _SECTION_ALIASES.get(hint.lower().strip(), [hint.lower().strip()])
    alias_norms = [_normalize_label(a) for a in aliases]
    out = []
    for s in sections:
        all_titles = s.get("all_titles") or [s["title"]]
        title_norms = [_normalize_label(t) for t in all_titles]
        matched = False
        for tn in title_norms:
            for an in alias_norms:
                if an and tn and (an in tn or tn in an):
                    matched = True
                    break
            if matched:
                break
        if matched:
            out.append(s)
    return out


def log_entry(
    item: str,
    amount: float,
    entry_date: date,
    section: str | None = None,
    check_number: str | None = None,
) -> dict:
    """Log a monetary entry to the right cell in the monthly tab.

    Discovers sections from the sheet itself (every "DATE" header marks a section),
    fuzzy-matches `item` against the section's column labels, writes the amount,
    stamps the section's DATE column, and (if check_number given) writes that to
    the matching CHECK# / CH# column in the same section.
    """
    client = _get_client()
    spreadsheet = client.open_by_key(get_store_sheet_id())
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)

    sections = _discover_sections(sheet)
    if not sections:
        return {"ok": False, "reason": "no_sections", "search": item}

    if section:
        sections = _filter_sections_by_hint(sections, section)
        if not sections:
            return {"ok": False, "reason": "bad_section", "search": item}

    matches: list[dict] = []
    for sec in sections:
        m = _find_in_section_dynamic(sheet, sec, item)
        if m:
            col_idx, label = m
            matches.append({"sec": sec, "col_idx": col_idx, "label": label})

    if not matches:
        # Invalidate cache once in case the user just added a column.
        _SECTION_CACHE.pop((sheet.spreadsheet.id, sheet.title), None)
        sections = _discover_sections(sheet)
        if section:
            sections = _filter_sections_by_hint(sections, section)
        for sec in sections:
            m = _find_in_section_dynamic(sheet, sec, item)
            if m:
                col_idx, label = m
                matches.append({"sec": sec, "col_idx": col_idx, "label": label})

    if not matches:
        available = {s["title"]: [lbl for _, lbl in _section_data_columns(sheet, s)] for s in sections}
        return {"ok": False, "reason": "not_found", "available": available, "search": item}

    if len(matches) > 1:
        return {
            "ok": False,
            "reason": "ambiguous",
            "matches": [{"section": m["sec"]["title"], "label": m["label"]} for m in matches],
        }

    chosen = matches[0]
    sec = chosen["sec"]
    target_row = sec["data_start_row"] + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, chosen["col_idx"])
    date_cell = gspread.utils.rowcol_to_a1(target_row, sec["col_start"])

    sheet.update(cell, [[amount]])

    # Stamp the section's DATE column if empty.
    try:
        date_existing = sheet.get(date_cell)
        date_is_empty = not (date_existing and date_existing[0] and date_existing[0][0].strip())
    except Exception:
        date_is_empty = True
    if date_is_empty:
        sheet.update(date_cell, [[entry_date.strftime("%-m/%-d")]], value_input_option="USER_ENTERED")

    # If a check number was provided, write it to the section's CH# column.
    check_cell = None
    if check_number:
        check_col = _find_check_column(sheet, sec)
        if check_col:
            check_cell = gspread.utils.rowcol_to_a1(target_row, check_col)
            sheet.update(check_cell, [[check_number]], value_input_option="USER_ENTERED")

    return {
        "ok": True,
        "section": sec["title"],
        "label": chosen["label"],
        "cell": cell,
        "check_cell": check_cell,
        "amount": amount,
        "date": str(entry_date),
    }


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

    client = _get_client()
    spreadsheet = client.open_by_key(get_store_sheet_id())
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)

    match = find_column_in_section(sheet, _COGS_HEADER_ROW, vendor)
    if not match:
        return f"⚠️ Vendor '{vendor}' has no matching column in the COGS section. Add a column to the sheet, or check the spelling."
    col_idx, label = match

    target_row = _COGS_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)
    sheet.update(cell, [[amount]])

    return f"COGS logged: {label} ${amount:.2f} on {entry_date} → {_month_tab_name(entry_date)}"


def mark_invoice_paid(vendor: str, entry_date: date) -> str:
    """
    Turn the COGS cell for this vendor+date green to indicate the bank confirmed payment.
    If the vendor isn't found in the sheet, silently returns.
    """
    client = _get_client()
    spreadsheet = client.open_by_key(get_store_sheet_id())
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)

    match = find_column_in_section(sheet, _COGS_HEADER_ROW, vendor)
    if not match:
        return f"Vendor {vendor!r} not in COGS columns — skipped paid mark"
    col_idx, label = match

    target_row = _COGS_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)

    sheet.format(cell, {
        "backgroundColor": {"red": 0.71, "green": 0.84, "blue": 0.66}  # soft green
    })

    return f"Marked PAID: {label} on {entry_date} (cell {cell} → green)"


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

    client = _get_client()
    spreadsheet = client.open_by_key(get_store_sheet_id())
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)

    # EXPENSES section is cols 1 through (PAYROLL start - 1) in the same header row.
    match = find_column_in_section(
        sheet, _EXP_HEADER_ROW, category,
        col_start=1, col_end=_PAYROLL_COL_START - 1,
    )
    if not match:
        return f"⚠️ Expense '{category}' has no matching column. Add a column to the sheet or check spelling."
    col_idx, label = match

    target_row = _EXP_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)
    sheet.update(cell, [[amount]])

    return f"Expense logged: {label} ${amount:.2f} on {entry_date}"


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

    client = _get_client()
    spreadsheet = client.open_by_key(get_store_sheet_id())
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)

    # PAYROLL shares the EXPENSES header row but starts at _PAYROLL_COL_START.
    match = find_column_in_section(
        sheet, _EXP_HEADER_ROW, employee,
        col_start=_PAYROLL_COL_START,
    )
    if not match:
        return f"⚠️ Employee '{employee}' has no matching payroll column in the sheet."
    col_idx, label = match

    target_row = _EXP_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)
    sheet.update(cell, [[amount]])

    return f"Payroll logged: {label} ${amount:.2f} on {entry_date}"


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

    client = _get_client()
    spreadsheet = client.open_by_key(get_store_sheet_id())
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)

    # REBATES occupies cols 1 through (PROFIT start - 1) on the REVENUES header row.
    match = find_column_in_section(
        sheet, _REV_HEADER_ROW, vendor,
        col_start=1, col_end=_PROFIT_COL_START - 1,
    )
    if not match:
        return f"⚠️ Rebate vendor '{vendor}' has no matching column in the REBATES section."
    col_idx, label = match

    target_row = _REV_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)
    sheet.update(cell, [[amount]])

    return f"Rebate logged: {label} ${amount:.2f} on {entry_date}"


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

    client = _get_client()
    spreadsheet = client.open_by_key(get_store_sheet_id())
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)

    # PROFIT TOOK HOME starts at _PROFIT_COL_START on the REVENUES header row.
    match = find_column_in_section(
        sheet, _REV_HEADER_ROW, category,
        col_start=_PROFIT_COL_START,
    )
    if not match:
        return f"⚠️ Revenue category '{category}' has no matching column in PROFIT TOOK HOME."
    col_idx, label = match

    target_row = _REV_DATA_START + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)
    sheet.update(cell, [[amount]])

    return f"Revenue logged: {label} ${amount:.2f} on {entry_date}"


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


def _find_in_section_by_amount(section_hint: str, amount: float, days_back: int = 14) -> list[tuple[date, str, float]]:
    """Search the active store's sheet for any cell in `section_hint` with
    matching amount (±$1, within days_back days). Returns list of
    (entry_date, label, cell_amount), most recent first, capped at 3.

    Uses dynamic section discovery — works for every store's sheet layout.
    """
    client = _get_client()
    spreadsheet = client.open_by_key(get_store_sheet_id())
    today = date.today()
    matches: list[tuple[date, str, float]] = []

    for month_start in _months_to_check(days_back):
        try:
            sheet = _get_or_create_monthly_tab(spreadsheet, month_start)
            sections = _filter_sections_by_hint(_discover_sections(sheet), section_hint)
            for sec in sections:
                cols = _section_data_columns(sheet, sec)
                if not cols:
                    continue
                col_to_label = {c: lbl for c, lbl in cols}
                col_min = min(col_to_label)
                col_max = max(col_to_label)
                days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
                row_start = sec["data_start_row"]
                row_end = row_start + days_in_month - 1
                start_cell = gspread.utils.rowcol_to_a1(row_start, col_min)
                end_cell = gspread.utils.rowcol_to_a1(row_end, col_max)
                rows = sheet.get(f"{start_cell}:{end_cell}")
                for day_offset, row in enumerate(rows):
                    for col_offset, cell in enumerate(row):
                        actual_col = col_min + col_offset
                        if actual_col not in col_to_label:
                            continue
                        val = _parse_cell_amount(cell)
                        if val is not None and abs(val - amount) <= 1.0:
                            entry_date = month_start.replace(day=day_offset + 1)
                            if (today - entry_date).days <= days_back:
                                matches.append((entry_date, col_to_label[actual_col], val))
        except Exception:
            continue

    matches.sort(key=lambda x: x[0], reverse=True)
    return matches[:3]


def _find_in_section_by_label_amount(section_hint: str, label_query: str, amount: float, days_back: int = 14) -> tuple[date, str] | None:
    """Find a cell in `section_hint` whose column matches `label_query` (fuzzy)
    AND has the given amount (±$1) within days_back days. Returns
    (entry_date, label) or None."""
    client = _get_client()
    spreadsheet = client.open_by_key(get_store_sheet_id())
    today = date.today()

    for month_start in _months_to_check(days_back):
        try:
            sheet = _get_or_create_monthly_tab(spreadsheet, month_start)
            sections = _filter_sections_by_hint(_discover_sections(sheet), section_hint)
            for sec in sections:
                m = _find_in_section_dynamic(sheet, sec, label_query)
                if not m:
                    continue
                col_idx, label = m
                days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
                row_start = sec["data_start_row"]
                row_end = row_start + days_in_month - 1
                start_cell = gspread.utils.rowcol_to_a1(row_start, col_idx)
                end_cell = gspread.utils.rowcol_to_a1(row_end, col_idx)
                col_values = sheet.get(f"{start_cell}:{end_cell}")
                for day_offset, r in enumerate(col_values):
                    val = _parse_cell_amount(r[0] if r else None)
                    if val is not None and abs(val - amount) <= 1.0:
                        entry_date = month_start.replace(day=day_offset + 1)
                        if (today - entry_date).days <= days_back:
                            return (entry_date, label)
        except Exception:
            continue
    return None


def find_cogs_by_vendor(vendor: str, amount: float, days_back: int = 14) -> tuple[date, str] | None:
    return _find_in_section_by_label_amount("invoice", vendor, amount, days_back)


def find_cogs_by_amount(amount: float, days_back: int = 14) -> list[tuple[date, str, float]]:
    return _find_in_section_by_amount("invoice", amount, days_back)


def find_expense_by_category(category: str, amount: float, days_back: int = 14) -> tuple[date, str] | None:
    return _find_in_section_by_label_amount("expense", category, amount, days_back)


def find_expense_by_amount(amount: float, days_back: int = 14) -> list[tuple[date, str, float]]:
    return _find_in_section_by_amount("expense", amount, days_back)


def find_rebate_by_vendor(vendor: str, amount: float, days_back: int = 14) -> tuple[date, str] | None:
    return _find_in_section_by_label_amount("rebate", vendor, amount, days_back)


def _legacy_find_rebate_by_vendor_unused(vendor: str, amount: float, days_back: int = 14) -> tuple[date, str] | None:
    """Kept only so the next chunk of original code below still compiles —
    it's now dead and will be removed in the next cleanup pass."""
    col_name = resolve_rebate_vendor(vendor)
    if not col_name:
        return None
    col_idx = REBATES_HEADERS.index(col_name) + 1
    client = _get_client()
    spreadsheet = client.open_by_key(get_store_sheet_id())
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
    return _mark_section_cell_paid("expense", category, entry_date, "expense")


def mark_cc_settled(sale_date: date, bank_deposit: float, bank_date: date) -> str:
    """Highlight the credit-card column cell green for the matched sale day.

    Each store labels their card column differently — Moraine uses 'CREDIT',
    Hamilton uses 'C.C.'. We discover the DAILY SALES section dynamically and
    fuzzy-match the column by trying common card-payment aliases.
    """
    client = _get_client()
    spreadsheet = client.open_by_key(get_store_sheet_id())
    sheet = _get_or_create_monthly_tab(spreadsheet, sale_date)

    # Find the DAILY SALES section (or whatever the leftmost section is on row 2-ish).
    sections = _discover_sections(sheet)
    daily_sections = _filter_sections_by_hint(sections, "daily")
    if not daily_sections:
        # Fallback: take the first discovered section (usually DAILY SALES).
        daily_sections = sections[:1]

    cc_col = None
    matched_label = None
    for sec in daily_sections:
        # Try each common card-payment label, prefer the most specific match.
        for alias in ("c.c.", "cc", "credit", "card", "ccard"):
            m = _find_in_section_dynamic(sheet, sec, alias)
            if m:
                cc_col, matched_label = m
                break
        if cc_col:
            break

    if not cc_col:
        return f"⚠️ No card column found in DAILY SALES — skipped CC settlement highlight"

    # Day-N row inside the DAILY SALES section.
    sec = next((s for s in daily_sections if any(c == cc_col for c, _ in _section_data_columns(sheet, s))), daily_sections[0])
    target_row = sec["data_start_row"] + sale_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, cc_col)

    sheet.format(cell, {"backgroundColor": _PAID_GREEN})
    try:
        sheet.update_note(cell, f"CC settled ${bank_deposit:,.2f} on {bank_date}")
    except Exception:
        pass
    return f"Marked CC settled: {sale_date} {matched_label} cell → green (bank ${bank_deposit:,.2f} on {bank_date})"


def mark_rebate_paid(vendor: str, entry_date: date) -> str:
    """Turn the rebate cell for this vendor+date green (bank confirmed receipt)."""
    return _mark_section_cell_paid("rebate", vendor, entry_date, "rebate")


def _mark_section_cell_paid(kind: str, item: str, entry_date: date, section_hint: str) -> str:
    """Highlight a section cell green for the given item+date — dynamic discovery."""
    client = _get_client()
    spreadsheet = client.open_by_key(get_store_sheet_id())
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)
    sections = _filter_sections_by_hint(_discover_sections(sheet), section_hint)
    for sec in sections:
        m = _find_in_section_dynamic(sheet, sec, item)
        if m:
            col_idx, label = m
            target_row = sec["data_start_row"] + entry_date.day - 1
            cell = gspread.utils.rowcol_to_a1(target_row, col_idx)
            sheet.format(cell, {"backgroundColor": _PAID_GREEN})
            return f"Marked PAID: {label} {kind} on {entry_date}"
    return f"{kind.capitalize()} {item!r} not found in sheet — skipped paid mark"


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


def _log_and_highlight_dynamic(item: str, amount: float, entry_date: date, section_hint: str) -> str:
    """Find the right cell via dynamic section discovery, log if empty, highlight green.

    Used by the bank reconciler to mark cells when the bank confirms a payment.
    If the cell has an existing value (owner typed it manually before the bank
    cleared), we PRESERVE it and just highlight — never overwrite manual data.
    """
    client = _get_client()
    spreadsheet = client.open_by_key(get_store_sheet_id())
    sheet = _get_or_create_monthly_tab(spreadsheet, entry_date)

    sections = _discover_sections(sheet)
    sections = _filter_sections_by_hint(sections, section_hint)

    matches: list[tuple[dict, tuple[int, str]]] = []
    for sec in sections:
        m = _find_in_section_dynamic(sheet, sec, item)
        if m:
            matches.append((sec, m))

    if not matches:
        return f"'{item}' not in {section_hint} columns — skipped"
    if len(matches) > 1:
        # Reconciler path is automated — pick the first deterministically rather
        # than asking the user. Log the ambiguity so we can investigate later.
        log.warning("ambiguous section match for item=%r section_hint=%r — picked first", item, section_hint)

    sec, (col_idx, label) = matches[0]
    target_row = sec["data_start_row"] + entry_date.day - 1
    cell = gspread.utils.rowcol_to_a1(target_row, col_idx)

    existing = _read_cell_value(sheet, cell)
    if existing is None:
        sheet.update(cell, [[amount]])
        # Stamp section's own DATE column if empty
        date_cell = gspread.utils.rowcol_to_a1(target_row, sec["col_start"])
        try:
            date_existing = sheet.get(date_cell)
            date_is_empty = not (date_existing and date_existing[0] and date_existing[0][0].strip())
        except Exception:
            date_is_empty = True
        if date_is_empty:
            sheet.update(date_cell, [[entry_date.strftime("%-m/%-d")]], value_input_option="USER_ENTERED")
        action = f"logged ${amount:.2f}"
    else:
        action = f"kept existing ${existing:.2f}"

    sheet.format(cell, {"backgroundColor": _PAID_GREEN})
    return f"{sec['title']} {label} on {entry_date}: {action}, highlighted"


def log_expense_and_highlight(category: str, amount: float, entry_date: date) -> str:
    return _log_and_highlight_dynamic(category, amount, entry_date, "expense")


def log_invoice_and_highlight(vendor: str, amount: float, entry_date: date) -> str:
    return _log_and_highlight_dynamic(vendor, amount, entry_date, "invoice")


def log_rebate_and_highlight(vendor: str, amount: float, entry_date: date) -> str:
    return _log_and_highlight_dynamic(vendor, amount, entry_date, "rebate")


def log_payroll_and_highlight(employee: str, amount: float, entry_date: date) -> str:
    return _log_and_highlight_dynamic(employee, amount, entry_date, "payroll")


def log_revenue_and_highlight(category: str, amount: float, entry_date: date) -> str:
    return _log_and_highlight_dynamic(category, amount, entry_date, "revenue")


def _match_description_to_section(description: str, section_hint: str) -> str | None:
    """Find a column in `section_hint` whose label appears as a word in
    `description`. Returns the matched column LABEL (as it appears in the
    sheet) or None. Reads the active store's actual sheet — no hardcoded
    alias maps."""
    import re
    try:
        client = _get_client()
        spreadsheet = client.open_by_key(get_store_sheet_id())
        sheet = _get_or_create_monthly_tab(spreadsheet, date.today())
        sections = _filter_sections_by_hint(_discover_sections(sheet), section_hint)
    except Exception:
        return None

    desc_norm = re.sub(r"[^a-z0-9\s]", " ", description.lower())
    candidates: list[tuple[str, int]] = []  # (label, length-for-tiebreak)
    for sec in sections:
        for _, label in _section_data_columns(sheet, sec):
            label_norm = _normalize_label(label)
            if len(label_norm) < 3:
                continue
            pattern = r"\b" + re.escape(label_norm) + r"\b"
            if re.search(pattern, desc_norm):
                candidates.append((label, len(label_norm)))
    if not candidates:
        return None
    # Prefer longest match (most specific)
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]


def match_description_to_cogs_vendor(description: str) -> str | None:
    return _match_description_to_section(description, "invoice")


def match_description_to_expense(description: str) -> str | None:
    return _match_description_to_section(description, "expense")


def match_description_to_rebate(description: str) -> str | None:
    return _match_description_to_section(description, "rebate")
