"""
Main LangGraph agent for the gas station.

Daily workflow:
  1. Fetch yesterday's sales from NRS Pay
  2. Fetch transactions from NRS Pay
  3. Fetch inventory levels from NRS Pay
  4. Fetch bank balances from Plaid (if configured)
  5. Log everything to Google Sheets
  6. Send daily report + any alerts via Telegram
  7. Store observations in Mem0 memory
"""

import json
from datetime import date
from typing import Annotated, Any, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from config.settings import settings
from memory.mem0_memory import (
    get_context_for_report,
    remember_anomaly,
    remember_daily_summary,
)
from tools.nrs_tools import fetch_daily_sales, fetch_inventory, fetch_transactions
from tools.plaid_tools import get_bank_balances
from tools.sheets_tools import (
    log_bank_balance,
    log_daily_sales,
    log_inventory,
    log_transactions,
)
from tools.telegram_tools import (
    send_bank_alert,
    send_daily_report,
    send_error_alert,
    send_low_stock_alert,
    send_message,
)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    sales_data: dict[str, Any]
    transactions: list[dict[str, Any]]
    inventory_data: dict[str, Any]
    bank_data: dict[str, Any]
    errors: list[str]


# ---------------------------------------------------------------------------
# LangChain tools (wrapped for the agent)
# ---------------------------------------------------------------------------

@tool
def tool_fetch_daily_sales(date_str: str = "") -> str:
    """Fetch daily sales summary from NRS Pay. date_str: YYYY-MM-DD or empty for yesterday."""
    result = fetch_daily_sales(date_str)
    return json.dumps(result)


@tool
def tool_fetch_transactions(date_str: str = "") -> str:
    """Fetch transaction list from NRS Pay. date_str: YYYY-MM-DD or empty for yesterday."""
    result = fetch_transactions(date_str)
    return json.dumps(result)


@tool
def tool_fetch_inventory() -> str:
    """Fetch current inventory levels from NRS Pay."""
    result = fetch_inventory()
    return json.dumps(result)


@tool
def tool_get_bank_balances(access_token: str = "") -> str:
    """Fetch bank account balances via Plaid. access_token: Plaid access token."""
    result = get_bank_balances(access_token)
    return json.dumps(result)


@tool
def tool_log_daily_sales(sales_json: str) -> str:
    """Log daily sales data to Google Sheets. sales_json: JSON string of sales data."""
    data = json.loads(sales_json)
    return log_daily_sales(data)


@tool
def tool_log_transactions(transactions_json: str, target_date: str = "") -> str:
    """Log transactions to Google Sheets. transactions_json: JSON array string."""
    data = json.loads(transactions_json)
    return log_transactions(data, target_date)


@tool
def tool_log_inventory(inventory_json: str) -> str:
    """Log inventory snapshot to Google Sheets. inventory_json: JSON string."""
    data = json.loads(inventory_json)
    return log_inventory(data)


@tool
def tool_log_bank_balance(balance_json: str) -> str:
    """Log bank balances to Google Sheets. balance_json: JSON string."""
    data = json.loads(balance_json)
    return log_bank_balance(data)


@tool
def tool_send_daily_report(sales_json: str) -> str:
    """Send daily sales report via Telegram. sales_json: JSON string of sales data."""
    data = json.loads(sales_json)
    return send_daily_report(data)


@tool
def tool_send_low_stock_alert(inventory_json: str) -> str:
    """Send low stock alert via Telegram if any items are low. inventory_json: JSON string."""
    data = json.loads(inventory_json)
    return send_low_stock_alert(data)


@tool
def tool_send_bank_alert(balance_json: str, threshold: float = 5000.0) -> str:
    """Send bank low balance alert via Telegram. balance_json: JSON string."""
    data = json.loads(balance_json)
    return send_bank_alert(data, threshold)


@tool
def tool_send_message(text: str) -> str:
    """Send a plain text message via Telegram."""
    return send_message(text)


@tool
def tool_remember_summary(summary: str, date_str: str = "") -> str:
    """Store a daily summary in Mem0 memory."""
    d = date_str or str(date.today())
    return remember_daily_summary(summary, d)


@tool
def tool_remember_anomaly(description: str, date_str: str = "") -> str:
    """Store an anomaly observation in Mem0 memory."""
    d = date_str or str(date.today())
    return remember_anomaly(description, d)


@tool
def tool_get_memory_context() -> str:
    """Retrieve recent memory context to inform analysis."""
    return get_context_for_report()


ALL_TOOLS = [
    tool_fetch_daily_sales,
    tool_fetch_transactions,
    tool_fetch_inventory,
    tool_get_bank_balances,
    tool_log_daily_sales,
    tool_log_transactions,
    tool_log_inventory,
    tool_log_bank_balance,
    tool_send_daily_report,
    tool_send_low_stock_alert,
    tool_send_bank_alert,
    tool_send_message,
    tool_remember_summary,
    tool_remember_anomaly,
    tool_get_memory_context,
]

TOOLS_BY_NAME = {t.name: t for t in ALL_TOOLS}

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

llm = ChatAnthropic(
    model="claude-opus-4-6",
    api_key=settings.anthropic_api_key,
).bind_tools(ALL_TOOLS)

SYSTEM_PROMPT = """You are an intelligent gas station management agent.

Your daily job is to:
1. Fetch yesterday's sales data from NRS Pay
2. Fetch the transaction list from NRS Pay
3. Fetch current inventory levels from NRS Pay
4. Optionally fetch bank balances if an access token is available
5. Log all data to Google Sheets
6. Send a formatted daily report via Telegram
7. Send low stock alerts if any items are below threshold
8. Send bank balance alerts if any account is below $5,000
9. Check memory for recent anomalies and trends
10. Store a daily summary and any anomalies in memory

Be thorough. Complete ALL steps. If a step fails, log the error and continue with the remaining steps.
Always send the daily report even if some data is missing.
"""


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

def agent_node(state: AgentState) -> AgentState:
    """Main agent reasoning node."""
    response = llm.invoke(state["messages"])
    return {"messages": [response]}


def tool_node(state: AgentState) -> AgentState:
    """Execute tool calls from the last AI message."""
    last_message = state["messages"][-1]
    tool_messages = []

    for tool_call in last_message.tool_calls:
        tool_name = tool_call["name"]
        tool_args = tool_call["args"]

        try:
            result = TOOLS_BY_NAME[tool_name].invoke(tool_args)
        except Exception as e:
            result = f"ERROR: {e}"

        tool_messages.append(
            ToolMessage(
                content=str(result),
                tool_call_id=tool_call["id"],
            )
        )

    return {"messages": tool_messages}


def should_continue(state: AgentState) -> str:
    """Route: continue to tools or end."""
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"
    return END


# ---------------------------------------------------------------------------
# Build graph
# ---------------------------------------------------------------------------

def build_graph() -> StateGraph:
    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_daily_workflow(plaid_access_token: str = "") -> dict[str, Any]:
    """
    Run the full daily gas station workflow.
    Returns the final agent state messages.
    """
    graph = build_graph()

    today = str(date.today())
    task_description = (
        f"Today is {today}. Run the complete daily gas station workflow:\n"
        "1. Get memory context from previous days\n"
        "2. Fetch yesterday's daily sales from NRS Pay\n"
        "3. Fetch yesterday's transactions from NRS Pay\n"
        "4. Fetch current inventory levels from NRS Pay\n"
    )
    if plaid_access_token:
        task_description += f"5. Fetch bank balances using access token: {plaid_access_token}\n"
    task_description += (
        "6. Log all fetched data to Google Sheets\n"
        "7. Send the daily sales report via Telegram\n"
        "8. Send low stock alerts if needed\n"
        "9. Send bank alerts if any balance is below $5,000\n"
        "10. Store a daily summary in memory\n"
        "11. Store any anomalies or unusual patterns in memory\n"
        "Complete all steps and report what was done."
    )

    initial_state: AgentState = {
        "messages": [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=task_description),
        ],
        "sales_data": {},
        "transactions": [],
        "inventory_data": {},
        "bank_data": {},
        "errors": [],
    }

    final_state = graph.invoke(initial_state)

    # Return summary of last AI message
    for msg in reversed(final_state["messages"]):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            return {"status": "completed", "summary": msg.content}

    return {"status": "completed", "summary": "Workflow finished"}
