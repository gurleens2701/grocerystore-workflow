"""
scripts/manage_store.py

Interactive CLI for managing per-store configuration.

Usage (on VPS):
    docker compose exec app python scripts/manage_store.py

Covers:
  - Workflows: feature flags per store (daily report, bank recon, sheet sync, etc.)
  - Daily sheet fields: add / edit / remove / reorder fields per store
  - Scheduler jobs: schedules, enable/disable per store
  - Tools: enable/disable per store
  - Tool experiments: enable on one store first, then promote to others

Note: scheduler changes require  docker compose restart app  to take effect.
      All other changes are live immediately.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Workflow flags definition ────────────────────────────────────────────────
# (attr_name, display_name, type)
# type is "bool" or "choice:opt1,opt2,opt3"

WORKFLOW_FLAGS = [
    ("daily_report_enabled",  "Daily report",           "bool"),
    ("daily_report_mode",     "Daily report mode",      "choice:nrs_pull,modisoft_pull,manual_entry"),
    ("manual_entry_enabled",  "Owner enters manual numbers", "bool"),
    ("nightly_sheet_sync",    "Nightly Google Sheet sync",   "bool"),
    ("bank_recon_enabled",    "Bank reconciliation",    "bool"),
    ("month_end_summary",     "Month-end cash flow summary", "bool"),
    ("weekly_bank_summary",   "Weekly bank summary",    "bool"),
    ("invoice_ocr_enabled",   "Invoice OCR",            "bool"),
    ("unified_agent_enabled", "AI agent (Telegram chat)", "bool"),
]

# Known jobs (shown as defaults when adding a job to a new store)
KNOWN_JOBS = [
    ("daily_fetch",    "0 7 * * *",   "Daily NRS/POS fetch at 7am"),
    ("bank_sync",      "every_4h",    "Bank transaction sync every 4h"),
    ("nightly_sync",   "every_15m",   "Google Sheet sync every 15min"),
    ("weekly_summary", "0 18 * * 0",  "Weekly bank summary Sunday 6pm"),
    ("cashflow",       "0 8 L * *",   "Month-end cashflow summary"),
]


# ── UI helpers ───────────────────────────────────────────────────────────────

def clear():
    print("\n" + "─" * 55)


def header(title: str):
    print(f"\n{'═' * 55}")
    print(f"  {title}")
    print(f"{'═' * 55}")


def ask(prompt: str, default: str = "") -> str:
    display = f"  {prompt} [{default}]: " if default else f"  {prompt}: "
    while True:
        val = input(display).strip()
        if val:
            return val
        if default != "":
            return default
        print("  (required)")


def pick(prompt: str, options: list[str], default: str = "") -> str:
    """Pick one value from a list. Shows options inline."""
    opts = "/".join(options)
    return ask(f"{prompt} ({opts})", default=default or options[0])


def confirm(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"  {prompt} [{hint}]: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def menu(title: str, options: list[str], back_label: str = "Back") -> int:
    """
    Show a numbered menu. Returns 1-based index of chosen option,
    or 0 for back/quit.
    """
    print(f"\n  {title}")
    for i, opt in enumerate(options, 1):
        print(f"    {i}. {opt}")
    print(f"    0. {back_label}")
    while True:
        raw = input("  → ").strip()
        if raw == "0":
            return 0
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw)
        print(f"  Enter 0–{len(options)}")


# ── DB helpers ───────────────────────────────────────────────────────────────

async def get_stores() -> list:
    from db.database import get_async_session
    from db.models import Store
    from sqlalchemy import select
    async with get_async_session() as s:
        return (await s.execute(
            select(Store).where(Store.is_active == True).order_by(Store.store_name)
        )).scalars().all()


async def get_workflows(store_id: str):
    from db.database import get_async_session
    from db.models import StoreWorkflow
    from sqlalchemy import select
    async with get_async_session() as s:
        return (await s.execute(
            select(StoreWorkflow).where(StoreWorkflow.store_id == store_id)
        )).scalars().first()


async def get_report_rules(store_id: str) -> list:
    from db.database import get_async_session
    from db.models import StoreDailyReportRule
    from sqlalchemy import select
    async with get_async_session() as s:
        return (await s.execute(
            select(StoreDailyReportRule)
            .where(StoreDailyReportRule.store_id == store_id)
            .order_by(StoreDailyReportRule.section, StoreDailyReportRule.display_order)
        )).scalars().all()


async def get_jobs(store_id: str) -> list:
    from db.database import get_async_session
    from db.models import StoreSchedulerPolicy
    from sqlalchemy import select
    async with get_async_session() as s:
        return (await s.execute(
            select(StoreSchedulerPolicy)
            .where(StoreSchedulerPolicy.store_id == store_id)
            .order_by(StoreSchedulerPolicy.job_name)
        )).scalars().all()


async def get_tool_policies(store_id: str) -> list:
    from db.database import get_async_session
    from db.models import StoreToolPolicy
    from sqlalchemy import select
    async with get_async_session() as s:
        return (await s.execute(
            select(StoreToolPolicy)
            .where(StoreToolPolicy.store_id == store_id)
            .order_by(StoreToolPolicy.tool_name)
        )).scalars().all()


async def get_all_tool_policies() -> list:
    """All tool policy rows across every store."""
    from db.database import get_async_session
    from db.models import StoreToolPolicy
    from sqlalchemy import select
    async with get_async_session() as s:
        return (await s.execute(
            select(StoreToolPolicy).order_by(StoreToolPolicy.tool_name, StoreToolPolicy.store_id)
        )).scalars().all()


# ── Store picker ─────────────────────────────────────────────────────────────

async def pick_store(prompt: str = "Select store") -> object | None:
    stores = await get_stores()
    if not stores:
        print("  No active stores found.")
        return None
    print()
    for i, s in enumerate(stores, 1):
        print(f"    {i}. {s.store_name}  ({s.store_id})")
    print(f"    0. Back")
    while True:
        raw = input(f"  {prompt} → ").strip()
        if raw == "0":
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(stores):
            return stores[int(raw) - 1]
        print(f"  Enter 0–{len(stores)}")


# ── Summary view ─────────────────────────────────────────────────────────────

async def show_summary(store):
    header(f"Config summary — {store.store_name}")
    print(f"  store_id  : {store.store_id}")
    print(f"  pos_type  : {store.pos_type}")
    print(f"  chat_id   : {store.chat_id}")
    print(f"  timezone  : {store.timezone}")

    wf = await get_workflows(store.store_id)
    if wf:
        print("\n  Workflows:")
        for attr, label, _ in WORKFLOW_FLAGS:
            val = getattr(wf, attr)
            icon = "✓" if val is True else ("✗" if val is False else "─")
            print(f"    {icon}  {label:<35} {val}")

    rules = await get_report_rules(store.store_id)
    print(f"\n  Daily sheet fields ({len(rules)}):")
    for r in rules:
        tag = "[manual]" if r.source == "manual" else "[auto]  "
        print(f"    {r.section:<5} #{r.display_order:<2}  {tag}  {r.label:<22} ({r.field_name})")

    jobs = await get_jobs(store.store_id)
    print(f"\n  Scheduler jobs ({len(jobs)}):")
    for j in jobs:
        status = "ON " if j.enabled else "OFF"
        print(f"    [{status}]  {j.job_name:<20} {j.schedule}")

    tools = await get_tool_policies(store.store_id)
    on  = [t.tool_name for t in tools if t.enabled]
    off = [t.tool_name for t in tools if not t.enabled]
    print(f"\n  Tools — {len(on)} enabled, {len(off)} disabled:")
    for name in on:
        print(f"    [ON ]  {name}")
    for name in off:
        print(f"    [OFF]  {name}")

    input("\n  Press Enter to continue...")


# ── Workflows ────────────────────────────────────────────────────────────────

async def manage_workflows(store):
    from db.database import get_async_session
    from db.models import StoreWorkflow
    from sqlalchemy import select

    while True:
        wf = await get_workflows(store.store_id)
        clear()
        print(f"\n  Workflows — {store.store_name}\n")

        rows = []
        for attr, label, kind in WORKFLOW_FLAGS:
            val = getattr(wf, attr)
            rows.append((attr, label, kind, val))
            if kind == "bool":
                icon = "ON " if val else "OFF"
                print(f"    {len(rows):>2}. [{icon}]  {label}")
            else:
                print(f"    {len(rows):>2}. [   ]  {label:<35} = {val}")
        print(f"     0. Back")

        raw = input("\n  Edit which? → ").strip()
        if raw == "0":
            break
        if not raw.isdigit() or not (1 <= int(raw) <= len(rows)):
            continue

        idx = int(raw) - 1
        attr, label, kind, current = rows[idx]

        if kind == "bool":
            new_val = not current
            print(f"\n  {label}: {current} → {new_val}")
            if confirm("Apply?"):
                async with get_async_session() as s:
                    row = (await s.execute(
                        select(StoreWorkflow).where(StoreWorkflow.store_id == store.store_id)
                    )).scalars().first()
                    setattr(row, attr, new_val)
                    await s.commit()
                print(f"  ✓ {label} → {'ON' if new_val else 'OFF'}")
        else:
            # choice field
            choices = kind.split(":")[1].split(",")
            new_val = pick(f"  {label}", choices, default=str(current))
            if new_val != str(current) and confirm(f"Set to {new_val!r}?"):
                async with get_async_session() as s:
                    row = (await s.execute(
                        select(StoreWorkflow).where(StoreWorkflow.store_id == store.store_id)
                    )).scalars().first()
                    setattr(row, attr, new_val)
                    await s.commit()
                print(f"  ✓ {label} → {new_val}")

        await asyncio.sleep(0.5)


# ── Daily sheet fields ────────────────────────────────────────────────────────

def _print_rules(rules):
    print(f"\n  {'#':<4} {'SEC':<6} {'SOURCE':<8} {'ORDER':<6} {'LABEL':<22} FIELD")
    print(f"  {'─'*70}")
    for i, r in enumerate(rules, 1):
        src = "[manual]" if r.source == "manual" else "[auto]  "
        print(f"  {i:<4} {r.section:<6} {src}  {r.display_order:<6} {r.label:<22} {r.field_name}")


async def manage_sheet_fields(store):
    from db.database import get_async_session
    from db.models import StoreDailyReportRule
    from sqlalchemy import select, delete

    while True:
        rules = await get_report_rules(store.store_id)
        clear()
        print(f"\n  Daily sheet fields — {store.store_name}")
        _print_rules(rules)

        choice = menu("Action", ["Edit a field", "Add new field", "Remove a field"])
        if choice == 0:
            break

        elif choice == 1:
            # Edit
            raw = ask("Field number to edit")
            if not raw.isdigit() or not (1 <= int(raw) <= len(rules)):
                continue
            r = rules[int(raw) - 1]
            print(f"\n  Editing: {r.label}  ({r.field_name})")
            new_label   = ask("Label", default=r.label)
            new_source  = pick("Source", ["api", "manual"], default=r.source)
            new_section = pick("Section", ["left", "right"], default=r.section)
            new_order   = int(ask("Display order", default=str(r.display_order)))
            if confirm("Save changes?"):
                async with get_async_session() as s:
                    row = (await s.execute(
                        select(StoreDailyReportRule).where(StoreDailyReportRule.id == r.id)
                    )).scalars().first()
                    row.label = new_label
                    row.source = new_source
                    row.section = new_section
                    row.display_order = new_order
                    await s.commit()
                print(f"  ✓ Updated {r.field_name}")

        elif choice == 2:
            # Add
            print("\n  Add new field:")
            field_name  = ask("Field name (internal key, e.g. propane)")
            label       = ask("Label (shown on sheet, e.g. PROPANE)")
            source      = pick("Source", ["manual", "api"], default="manual")
            section     = pick("Section", ["left", "right"], default="right")
            existing_orders = [r.display_order for r in rules]
            next_order  = (max(existing_orders) + 1) if existing_orders else 1
            display_order = int(ask("Display order", default=str(next_order)))
            print(f"\n  Adding: [{source}] {section} #{display_order}  {label}  ({field_name})")
            if confirm("Add?"):
                async with get_async_session() as s:
                    s.add(StoreDailyReportRule(
                        store_id=store.store_id, section=section, field_name=field_name,
                        label=label, source=source, display_order=display_order,
                    ))
                    await s.commit()
                print(f"  ✓ Added {field_name}")

        elif choice == 3:
            # Remove
            raw = ask("Field number to remove")
            if not raw.isdigit() or not (1 <= int(raw) <= len(rules)):
                continue
            r = rules[int(raw) - 1]
            print(f"\n  Remove: {r.label}  ({r.field_name})")
            if confirm("Are you sure?", default=False):
                async with get_async_session() as s:
                    await s.execute(
                        delete(StoreDailyReportRule).where(StoreDailyReportRule.id == r.id)
                    )
                    await s.commit()
                print(f"  ✓ Removed {r.field_name}")


# ── Scheduler jobs ────────────────────────────────────────────────────────────

async def manage_scheduler(store):
    from db.database import get_async_session
    from db.models import StoreSchedulerPolicy
    from sqlalchemy import select, delete

    while True:
        jobs = await get_jobs(store.store_id)
        clear()
        print(f"\n  Scheduler jobs — {store.store_name}\n")
        for i, j in enumerate(jobs, 1):
            status = "ON " if j.enabled else "OFF"
            print(f"    {i:>2}. [{status}]  {j.job_name:<22} {j.schedule}")

        choice = menu("Action", [
            "Toggle a job on/off",
            "Change a job's schedule",
            "Add a job",
            "Remove a job",
        ])
        if choice == 0:
            break

        elif choice == 1:
            raw = ask("Job number")
            if not raw.isdigit() or not (1 <= int(raw) <= len(jobs)):
                continue
            j = jobs[int(raw) - 1]
            new_val = not j.enabled
            print(f"\n  {j.job_name}: {'ON' if j.enabled else 'OFF'} → {'ON' if new_val else 'OFF'}")
            if confirm("Apply?"):
                async with get_async_session() as s:
                    row = (await s.execute(
                        select(StoreSchedulerPolicy).where(StoreSchedulerPolicy.id == j.id)
                    )).scalars().first()
                    row.enabled = new_val
                    await s.commit()
                print(f"  ✓ {j.job_name} → {'ON' if new_val else 'OFF'}")
                print("  ⚠  Restart required:  docker compose restart app")

        elif choice == 2:
            raw = ask("Job number")
            if not raw.isdigit() or not (1 <= int(raw) <= len(jobs)):
                continue
            j = jobs[int(raw) - 1]
            print(f"\n  Current schedule: {j.schedule}")
            print("  Formats: '0 7 * * *' (cron)  |  'every_4h'  |  'every_15m'  |  '0 8 L * *' (last day)")
            new_sched = ask("New schedule", default=j.schedule)
            if new_sched != j.schedule and confirm(f"Change to {new_sched!r}?"):
                async with get_async_session() as s:
                    row = (await s.execute(
                        select(StoreSchedulerPolicy).where(StoreSchedulerPolicy.id == j.id)
                    )).scalars().first()
                    row.schedule = new_sched
                    await s.commit()
                print(f"  ✓ {j.job_name} schedule → {new_sched}")
                print("  ⚠  Restart required:  docker compose restart app")

        elif choice == 3:
            print("\n  Known jobs (press Enter to use preset, or type your own):")
            existing_names = {j.job_name for j in jobs}
            available = [(n, s, d) for n, s, d in KNOWN_JOBS if n not in existing_names]
            for i, (n, s, d) in enumerate(available, 1):
                print(f"    {i}. {n:<22} {s:<16} {d}")
            raw = input("  Preset number or Enter to define manually → ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(available):
                job_name, schedule, _ = available[int(raw) - 1]
            else:
                job_name = ask("Job name")
                print("  Formats: '0 7 * * *' (cron)  |  'every_4h'  |  'every_15m'")
                schedule = ask("Schedule")
            if confirm(f"Add job {job_name!r} with schedule {schedule!r}?"):
                async with get_async_session() as s:
                    s.add(StoreSchedulerPolicy(
                        store_id=store.store_id, job_name=job_name,
                        schedule=schedule, enabled=True,
                    ))
                    await s.commit()
                print(f"  ✓ Added {job_name}")
                print("  ⚠  Restart required:  docker compose restart app")

        elif choice == 4:
            raw = ask("Job number to remove")
            if not raw.isdigit() or not (1 <= int(raw) <= len(jobs)):
                continue
            j = jobs[int(raw) - 1]
            if confirm(f"Remove job {j.job_name!r}?", default=False):
                async with get_async_session() as s:
                    await s.execute(
                        delete(StoreSchedulerPolicy).where(StoreSchedulerPolicy.id == j.id)
                    )
                    await s.commit()
                print(f"  ✓ Removed {j.job_name}")
                print("  ⚠  Restart required:  docker compose restart app")


# ── Tools (single store) ──────────────────────────────────────────────────────

async def manage_tools_for_store(store):
    from db.database import get_async_session
    from db.models import StoreToolPolicy
    from sqlalchemy import select, delete

    # Get canonical tool list from the agent
    try:
        from tools.main_agent import _ALL_TOOLS
        all_known = [t.name for t in _ALL_TOOLS]
    except Exception:
        all_known = []

    while True:
        policies = await get_tool_policies(store.store_id)
        policy_map = {p.tool_name: p for p in policies}

        # Merge: known tools + any custom ones already in DB
        all_tools = list(dict.fromkeys(all_known + [p.tool_name for p in policies]))

        clear()
        print(f"\n  Tools — {store.store_name}\n")
        rows = []
        for name in all_tools:
            p = policy_map.get(name)
            if p is None:
                status = "─ (not in DB)"
            elif p.enabled:
                status = "ON "
            else:
                status = "OFF"
            rows.append((name, p))
            print(f"    {len(rows):>2}. [{status}]  {name}")

        choice = menu("Action", [
            "Toggle tool on/off",
            "Add custom tool",
            "Remove tool from this store",
        ])
        if choice == 0:
            break

        elif choice == 1:
            raw = ask("Tool number")
            if not raw.isdigit() or not (1 <= int(raw) <= len(rows)):
                continue
            name, p = rows[int(raw) - 1]
            if p is None:
                # Not in DB yet — add it as enabled
                if confirm(f"Add {name!r} to this store as ON?"):
                    async with get_async_session() as s:
                        s.add(StoreToolPolicy(store_id=store.store_id, tool_name=name, enabled=True))
                        await s.commit()
                    print(f"  ✓ {name} → ON")
            else:
                new_val = not p.enabled
                if confirm(f"{name}: {'ON' if p.enabled else 'OFF'} → {'ON' if new_val else 'OFF'}?"):
                    async with get_async_session() as s:
                        row = (await s.execute(
                            select(StoreToolPolicy).where(StoreToolPolicy.id == p.id)
                        )).scalars().first()
                        row.enabled = new_val
                        await s.commit()
                    print(f"  ✓ {name} → {'ON' if new_val else 'OFF'}")

        elif choice == 2:
            tool_name = ask("Tool name (exact, e.g. query_inventory)")
            enabled = confirm("Enable it?", default=True)
            if confirm(f"Add {tool_name!r} ({'ON' if enabled else 'OFF'})?"):
                async with get_async_session() as s:
                    s.add(StoreToolPolicy(store_id=store.store_id, tool_name=tool_name, enabled=enabled))
                    await s.commit()
                print(f"  ✓ Added {tool_name}")

        elif choice == 3:
            raw = ask("Tool number to remove")
            if not raw.isdigit() or not (1 <= int(raw) <= len(rows)):
                continue
            name, p = rows[int(raw) - 1]
            if p is None:
                print("  Not in DB for this store — nothing to remove.")
            elif confirm(f"Remove {name!r} from this store?", default=False):
                async with get_async_session() as s:
                    await s.execute(
                        delete(StoreToolPolicy).where(StoreToolPolicy.id == p.id)
                    )
                    await s.commit()
                print(f"  ✓ Removed {name}")


# ── Tool experiments & deployment ────────────────────────────────────────────

async def tool_experiments():
    from db.database import get_async_session
    from db.models import StoreToolPolicy
    from sqlalchemy import select

    while True:
        stores = await get_stores()
        all_policies = await get_all_tool_policies()

        try:
            from tools.main_agent import _ALL_TOOLS
            known_tools = [t.name for t in _ALL_TOOLS]
        except Exception:
            known_tools = []

        # Build matrix: tool → {store_id: enabled | None}
        tool_store_map: dict[str, dict[str, bool | None]] = {}
        for p in all_policies:
            tool_store_map.setdefault(p.tool_name, {})
            tool_store_map[p.tool_name][p.store_id] = p.enabled
        for name in known_tools:
            tool_store_map.setdefault(name, {})

        # Sort tools: known first, then custom
        sorted_tools = known_tools + [t for t in tool_store_map if t not in known_tools]

        header("Tool Experiments & Deployment")
        store_ids = [s.store_id for s in stores]
        store_names = [s.store_name[:10] for s in stores]

        # Print matrix header
        col_w = 12
        header_row = f"  {'Tool':<30}" + "".join(f"{n:<{col_w}}" for n in store_names)
        print(header_row)
        print("  " + "─" * (30 + col_w * len(stores)))

        tool_rows = []
        for name in sorted_tools:
            row_str = f"  {name:<30}"
            for sid in store_ids:
                val = tool_store_map[name].get(sid)
                if val is True:
                    cell = "ON"
                elif val is False:
                    cell = "OFF"
                else:
                    cell = "─"
                row_str += f"{cell:<{col_w}}"
            tool_rows.append(name)
            print(f"  {len(tool_rows):>2}. {row_str[4:]}")

        choice = menu("Action", [
            "Experiment — enable on ONE store",
            "Promote — push to all stores",
            "Promote — push to chosen stores",
            "Roll back — disable on all stores",
            "Enable on all stores",
        ])
        if choice == 0:
            break

        # Pick tool
        raw = ask("Tool number")
        if not raw.isdigit() or not (1 <= int(raw) <= len(tool_rows)):
            continue
        tool_name = tool_rows[int(raw) - 1]

        if choice == 1:
            # Experiment on one store
            store = await pick_store(f"Enable {tool_name!r} on which store?")
            if store and confirm(f"Enable {tool_name!r} on {store.store_name} only?"):
                await _set_tool(tool_name, [store.store_id], True, all_policies)
                print(f"  ✓ {tool_name} enabled on {store.store_name}")
                print(f"  Tip: test it, then choose 'Promote' to roll out to other stores.")

        elif choice == 2:
            # Promote to all
            if confirm(f"Enable {tool_name!r} on ALL {len(stores)} stores?"):
                await _set_tool(tool_name, store_ids, True, all_policies)
                print(f"  ✓ {tool_name} → ON on all stores")

        elif choice == 3:
            # Promote to chosen stores
            print("\n  Stores:")
            for i, s in enumerate(stores, 1):
                print(f"    {i}. {s.store_name}")
            raw_list = ask("Store numbers (comma-separated, e.g. 1,3)")
            chosen = []
            for part in raw_list.split(","):
                part = part.strip()
                if part.isdigit() and 1 <= int(part) <= len(stores):
                    chosen.append(stores[int(part) - 1].store_id)
            if chosen and confirm(f"Enable {tool_name!r} on {len(chosen)} store(s)?"):
                await _set_tool(tool_name, chosen, True, all_policies)
                print(f"  ✓ {tool_name} → ON on chosen stores")

        elif choice == 4:
            # Roll back — disable on all
            if confirm(f"Disable {tool_name!r} on ALL stores?", default=False):
                await _set_tool(tool_name, store_ids, False, all_policies)
                print(f"  ✓ {tool_name} → OFF on all stores")

        elif choice == 5:
            # Enable on all
            if confirm(f"Enable {tool_name!r} on ALL stores?"):
                await _set_tool(tool_name, store_ids, True, all_policies)
                print(f"  ✓ {tool_name} → ON on all stores")

        await asyncio.sleep(0.5)


async def _set_tool(tool_name: str, store_ids: list[str], enabled: bool, existing_policies: list):
    """Upsert tool policy rows for the given store_ids."""
    from db.database import get_async_session
    from db.models import StoreToolPolicy
    from sqlalchemy import select

    existing_map = {(p.store_id, p.tool_name): p for p in existing_policies}
    async with get_async_session() as s:
        for sid in store_ids:
            p = existing_map.get((sid, tool_name))
            if p:
                # Update existing row
                row = (await s.execute(
                    select(StoreToolPolicy).where(StoreToolPolicy.id == p.id)
                )).scalars().first()
                if row:
                    row.enabled = enabled
            else:
                # Insert new row
                s.add(StoreToolPolicy(store_id=sid, tool_name=tool_name, enabled=enabled))
        await s.commit()


# ── Per-store menu ────────────────────────────────────────────────────────────

async def store_menu(store):
    while True:
        clear()
        print(f"\n  Managing: {store.store_name}  ({store.pos_type})")
        choice = menu("What to manage", [
            "View full config summary",
            "Workflows (feature flags)",
            "Daily sheet fields",
            "Scheduler jobs",
            "Tools",
        ])
        if choice == 0:
            break
        elif choice == 1:
            await show_summary(store)
        elif choice == 2:
            await manage_workflows(store)
        elif choice == 3:
            await manage_sheet_fields(store)
        elif choice == 4:
            await manage_scheduler(store)
        elif choice == 5:
            await manage_tools_for_store(store)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    while True:
        header("Gas Station Agent — Store Manager")
        choice = menu("Main menu", [
            "Manage a store",
            "Tool experiments & deployment",
        ], back_label="Exit")

        if choice == 0:
            print("\n  Bye.\n")
            break
        elif choice == 1:
            store = await pick_store()
            if store:
                await store_menu(store)
        elif choice == 2:
            await tool_experiments()


if __name__ == "__main__":
    asyncio.run(main())
