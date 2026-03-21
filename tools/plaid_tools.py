"""
Plaid tools for fetching bank account balances and transactions.
Falls back gracefully if Plaid credentials are not configured.
"""

from datetime import date, timedelta
from typing import Any

from config.settings import settings


def _client():
    from plaid.api import plaid_api
    from plaid.model.products import Products
    from plaid.model.country_code import CountryCode
    from plaid import ApiClient, Configuration, Environment

    env_map = {
        "sandbox": Environment.Sandbox,
        "development": Environment.Development,
        "production": Environment.Production,
    }
    configuration = Configuration(
        host=env_map.get(settings.plaid_env, Environment.Sandbox),
        api_key={
            "clientId": settings.plaid_client_id,
            "secret": settings.plaid_secret,
        },
    )
    api_client = ApiClient(configuration)
    return plaid_api.PlaidApi(api_client)


def _is_configured() -> bool:
    return bool(settings.plaid_client_id and settings.plaid_secret)


def get_bank_balances(access_token: str = "") -> dict[str, Any]:
    """
    Fetch current bank account balances via Plaid.
    access_token: Plaid access token for the linked account.
    If not provided, returns a placeholder with instructions.
    """
    if not _is_configured():
        return {
            "error": "Plaid not configured",
            "message": "Set PLAID_CLIENT_ID and PLAID_SECRET in .env",
            "accounts": [],
        }

    if not access_token:
        return {
            "error": "No access token",
            "message": "Provide a Plaid access_token to fetch balances",
            "accounts": [],
        }

    try:
        from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest

        client = _client()
        request = AccountsBalanceGetRequest(access_token=access_token)
        response = client.accounts_balance_get(request)

        accounts = [
            {
                "name": acct["name"],
                "type": str(acct["type"]),
                "subtype": str(acct.get("subtype", "")),
                "available": acct["balances"].get("available"),
                "current": acct["balances"].get("current"),
                "currency": acct["balances"].get("iso_currency_code", "USD"),
            }
            for acct in response["accounts"]
        ]
        return {"accounts": accounts}

    except Exception as e:
        return {"error": str(e), "accounts": []}


def get_recent_transactions(access_token: str = "", days: int = 7) -> dict[str, Any]:
    """
    Fetch recent bank transactions via Plaid.
    """
    if not _is_configured():
        return {
            "error": "Plaid not configured",
            "transactions": [],
        }

    if not access_token:
        return {
            "error": "No access token",
            "transactions": [],
        }

    try:
        from plaid.model.transactions_get_request import TransactionsGetRequest
        from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions

        end_date = date.today()
        start_date = end_date - timedelta(days=days)

        client = _client()
        request = TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
        )
        response = client.transactions_get(request)

        transactions = [
            {
                "date": str(t["date"]),
                "name": t["name"],
                "amount": t["amount"],
                "category": t.get("category", []),
                "transaction_id": t["transaction_id"],
            }
            for t in response["transactions"]
        ]
        return {"transactions": transactions, "total": len(transactions)}

    except Exception as e:
        return {"error": str(e), "transactions": []}


def create_link_token() -> dict[str, Any]:
    """
    Create a Plaid Link token to initiate account linking.
    Returns the link_token needed to open Plaid Link in a frontend.
    """
    if not _is_configured():
        return {"error": "Plaid not configured"}

    try:
        from plaid.model.link_token_create_request import LinkTokenCreateRequest
        from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
        from plaid.model.products import Products
        from plaid.model.country_code import CountryCode

        client = _client()
        request = LinkTokenCreateRequest(
            products=[Products("transactions"), Products("auth")],
            client_name="Gas Station Agent",
            country_codes=[CountryCode("US")],
            language="en",
            user=LinkTokenCreateRequestUser(client_user_id="gas-station-owner"),
        )
        response = client.link_token_create(request)
        return {"link_token": response["link_token"]}

    except Exception as e:
        return {"error": str(e)}
