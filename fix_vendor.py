"""Check Feb 8 payouts field for vendor payout."""
import sys, asyncio, json
sys.path.insert(0, "/Users/gurleensingh/gas-station-agent")

from tools.nrs_tools import _authenticate, _PAPI_BASE, _STORE_ID
import httpx

async def main():
    token = await _authenticate()
    for d in ["2026-02-08", "2026-03-08"]:
        url = f"{_PAPI_BASE}/{token}/pcrhist/{_STORE_ID}/stats/yesterday/{d}/{d}?elmer_id=0"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url)
            data = r.json().get("data", {})
        print(f"\n=== {d} payouts ===")
        print(json.dumps(data.get("payouts", {}), indent=2))
        print(f"=== {d} cashback ===")
        print(json.dumps(data.get("cashback", []), indent=2))

asyncio.run(main())
