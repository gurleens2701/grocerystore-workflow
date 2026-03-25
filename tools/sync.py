"""
Nightly sync — Google Sheets → PostgreSQL.

Runs at midnight every day via APScheduler.

Logic:
  For each cell in expenses, rebates, revenues sections:
    - Read current value from Google Sheets
    - Compare to PostgreSQL record for same (store_id, date, category)
    - If Sheet value differs from PostgreSQL → Sheet wins (owner edited it)
    - Update PostgreSQL, set last_updated_by = 'owner'
    - Bot never overwrites owner edits going forward

This ensures manual sheet edits are captured even if owner bypasses Telegram.
"""

import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from sqlalchemy import select, update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.settings import settings
from db.database import get_async_session
from db.models import DailySales, Expense, Rebate, Revenue
from tools.sheets_tools import (
    DAILY_HEADERS,
    EXPENSES_HEADERS,
    PROFIT_HEADERS,
    REBATES_HEADERS,
    _DAILY_DATA_START,
    _EXP_DATA_START,
    _PROFIT_COL_START,
    _REV_DATA_START,
    _get_client,
    _get_or_create_monthly_tab,
    _month_tab_name,
)

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _to_decimal(val: Any) -> Decimal | None:
    """Convert a cell value to Decimal. Returns None if empty or unparseable."""
    if val is None or val == "" or val == "—":
        return None
    try:
        cleaned = str(val).replace("$", "").replace(",", "").strip()
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def _read_sheet_section(
    sheet: gspread.Worksheet,
    start_row: int,
    num_days: int,
    headers: list[str],
    col_offset: int = 1,
) -> dict[int, dict[str, Decimal]]:
    """
    Read a rectangular section from the sheet.
    Returns {day: {header: value}} for all non-empty cells.
    col_offset: 1-based starting column of this section.
    """
    end_row = start_row + num_days - 1
    end_col = col_offset + len(headers) - 1

    start_a1 = gspread.utils.rowcol_to_a1(start_row, col_offset)
    end_a1 = gspread.utils.rowcol_to_a1(end_row, end_col)
    values = sheet.get(f"{start_a1}:{end_a1}")

    result: dict[int, dict[str, Decimal]] = {}
    for row_idx, row in enumerate(values):
        day = row_idx + 1
        row_data: dict[str, Decimal] = {}
        for col_idx, header in enumerate(headers):
            if header in ("DATE", "TOTAL"):
                continue
            if col_idx < len(row):
                val = _to_decimal(row[col_idx])
                if val is not None:
                    row_data[header] = val
        if row_data:
            result[day] = row_data
    return result


async def _sync_expenses(
    store_id: str,
    sheet: gspread.Worksheet,
    target_date: date,
    num_days: int,
) -> int:
    """Sync EXPENSES section from Sheets → PostgreSQL. Returns count of updates."""
    sheet_data = _read_sheet_section(sheet, _EXP_DATA_START, num_days, EXPENSES_HEADERS)
    updates = 0

    async with get_async_session() as session:
        for day, cols in sheet_data.items():
            row_date = date(target_date.year, target_date.month, day)
            for category, sheet_amount in cols.items():
                # Check PostgreSQL
                result = await session.execute(
                    select(Expense).where(
                        Expense.store_id == store_id,
                        Expense.expense_date == row_date,
                        Expense.category == category,
                    )
                )
                existing = result.scalar_one_or_none()

                if existing is None:
                    # New entry from sheet — insert
                    session.add(Expense(
                        store_id=store_id,
                        expense_date=row_date,
                        category=category,
                        amount=sheet_amount,
                        last_updated_by="owner",
                    ))
                    updates += 1
                elif existing.last_updated_by == "bot" and existing.amount != sheet_amount:
                    # Bot wrote it, owner changed it in sheet — sheet wins
                    existing.amount = sheet_amount
                    existing.last_updated_by = "owner"
                    updates += 1
                # If last_updated_by == "owner", owner already owns it — no change

    return updates


async def _sync_rebates(
    store_id: str,
    sheet: gspread.Worksheet,
    target_date: date,
    num_days: int,
) -> int:
    """Sync REBATES section from Sheets → PostgreSQL. Returns count of updates."""
    sheet_data = _read_sheet_section(sheet, _REV_DATA_START, num_days, REBATES_HEADERS)
    updates = 0

    async with get_async_session() as session:
        for day, cols in sheet_data.items():
            row_date = date(target_date.year, target_date.month, day)
            for vendor, sheet_amount in cols.items():
                result = await session.execute(
                    select(Rebate).where(
                        Rebate.store_id == store_id,
                        Rebate.rebate_date == row_date,
                        Rebate.vendor == vendor,
                    )
                )
                existing = result.scalar_one_or_none()

                if existing is None:
                    session.add(Rebate(
                        store_id=store_id,
                        rebate_date=row_date,
                        vendor=vendor,
                        amount=sheet_amount,
                        last_updated_by="owner",
                    ))
                    updates += 1
                elif existing.last_updated_by == "bot" and existing.amount != sheet_amount:
                    existing.amount = sheet_amount
                    existing.last_updated_by = "owner"
                    updates += 1

    return updates


async def _sync_revenues(
    store_id: str,
    sheet: gspread.Worksheet,
    target_date: date,
    num_days: int,
) -> int:
    """Sync PROFIT TOOK HOME section from Sheets → PostgreSQL. Returns count of updates."""
    sheet_data = _read_sheet_section(
        sheet, _REV_DATA_START, num_days, PROFIT_HEADERS, col_offset=_PROFIT_COL_START
    )
    updates = 0

    async with get_async_session() as session:
        for day, cols in sheet_data.items():
            row_date = date(target_date.year, target_date.month, day)
            for category, sheet_amount in cols.items():
                result = await session.execute(
                    select(Revenue).where(
                        Revenue.store_id == store_id,
                        Revenue.revenue_date == row_date,
                        Revenue.category == category,
                    )
                )
                existing = result.scalar_one_or_none()

                if existing is None:
                    session.add(Revenue(
                        store_id=store_id,
                        revenue_date=row_date,
                        category=category,
                        amount=sheet_amount,
                        last_updated_by="owner",
                    ))
                    updates += 1
                elif existing.last_updated_by == "bot" and existing.amount != sheet_amount:
                    existing.amount = sheet_amount
                    existing.last_updated_by = "owner"
                    updates += 1

    return updates


async def _sync_daily_sales(
    store_id: str,
    sheet: gspread.Worksheet,
    target_date: date,
    num_days: int,
) -> int:
    """Sync DAILY SALES section from Sheets → PostgreSQL. Returns count of upserts."""
    # Read the entire daily sales block in one API call
    end_row = _DAILY_DATA_START + num_days - 1
    end_col = len(DAILY_HEADERS)
    start_a1 = gspread.utils.rowcol_to_a1(_DAILY_DATA_START, 1)
    end_a1 = gspread.utils.rowcol_to_a1(end_row, end_col)
    rows = sheet.get(f"{start_a1}:{end_a1}")

    updates = 0
    dept_names = [
        "BEER", "CIGS", "DAIRY", "N.TAX", "TAX", "ICE", "LBAIT",
        "PIZZA", "POP", "PREROLL", "TOBBACO", "VAPE", "WINE", "PROPANE",
    ]
    # Column indices (0-based) in DAILY_HEADERS
    h = DAILY_HEADERS
    idx = {name: h.index(name) for name in h}

    async with get_async_session() as session:
        for row_i, row in enumerate(rows):
            day = row_i + 1
            if day > num_days or not row:
                continue

            def cell(col_name: str) -> Decimal:
                i = idx.get(col_name, -1)
                if i < 0 or i >= len(row):
                    return Decimal(0)
                return _to_decimal(row[i]) or Decimal(0)

            sale_date = date(target_date.year, target_date.month, day)
            product_sales = cell("SALE")
            grand_total = cell("G.TOT")
            if product_sales == 0 and grand_total == 0:
                continue  # empty row, skip

            departments = [
                {"name": d, "sales": float(cell(d)), "items": 0} for d in dept_names
            ]

            # Check if record already exists
            result = await session.execute(
                select(DailySales).where(
                    DailySales.store_id == store_id,
                    DailySales.sale_date == sale_date,
                )
            )
            existing = result.scalar_one_or_none()

            kwargs = dict(
                product_sales=product_sales,
                lotto_online=cell("ONLINE"),
                lotto_in=cell("INSTANT"),
                lotto_po=cell("LOTTO"),
                lotto_cr=cell("L.CREDIT"),
                atm=cell("ATM"),
                cash_drop=cell("CASH"),
                check_amount=cell("CHECK"),
                card=cell("CREDIT"),
                coupon=cell("COUPON"),
                pull_tab=cell("P.TAB"),
                sales_tax=cell("S.TAX"),
                food_stamp=cell("FOODS"),
                vendor_payout=cell("PAYOUT"),
                loyalty=cell("2 ALTRI"),
                grand_total=grand_total,
                departments=departments,
            )

            if existing is None:
                session.add(DailySales(store_id=store_id, sale_date=sale_date, **kwargs))
                updates += 1
            else:
                for k, v in kwargs.items():
                    setattr(existing, k, v)
                updates += 1

    return updates


async def run_nightly_sync(store_id: str) -> None:
    """
    Main nightly sync entry point. Called by APScheduler at midnight.
    Syncs current month's expenses, rebates, and revenues from Sheets → PostgreSQL.
    """
    today = date.today()
    log.info("[%s] Nightly sync started for %s", store_id, _month_tab_name(today))

    try:
        client = _get_client()
        spreadsheet = client.open_by_key(settings.google_sheet_id)
        sheet = _get_or_create_monthly_tab(spreadsheet, today)

        import calendar
        num_days = calendar.monthrange(today.year, today.month)[1]

        sales_count = await _sync_daily_sales(store_id, sheet, today, num_days)
        exp_count = await _sync_expenses(store_id, sheet, today, num_days)
        reb_count = await _sync_rebates(store_id, sheet, today, num_days)
        rev_count = await _sync_revenues(store_id, sheet, today, num_days)

        total = sales_count + exp_count + reb_count + rev_count
        log.info(
            "[%s] Nightly sync complete — %d sales, %d expenses, %d rebates, %d revenues (%d total)",
            store_id, sales_count, exp_count, reb_count, rev_count, total,
        )

        # Run anomaly checks after sync so we have latest data
        from tools.alerts import run_anomaly_checks
        from db.state import save_state
        alerts = await run_anomaly_checks(store_id, today)
        if alerts:
            log.info("[%s] %d anomalies found — stored for Telegram delivery.", store_id, len(alerts))
            await save_state(store_id, "pending_alerts", {"alerts": alerts, "date": str(today)})

    except Exception as e:
        log.error("[%s] Nightly sync failed: %s", store_id, e, exc_info=True)
