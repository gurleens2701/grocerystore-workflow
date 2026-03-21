"""
Mem0 memory integration for the gas station agent.
Stores and retrieves contextual memory across agent runs.
"""

from typing import Any

from config.settings import settings

_client = None


def _get_client():
    global _client
    if _client is None:
        from mem0 import MemoryClient
        _client = MemoryClient(api_key=settings.mem0_api_key)
    return _client


USER_ID = "gas-station-agent"


def add_memory(content: str, metadata: dict[str, Any] | None = None) -> str:
    """Store a new memory."""
    if not settings.mem0_api_key:
        return "Mem0 not configured (MEM0_API_KEY missing)"

    try:
        client = _get_client()
        result = client.add(
            content,
            user_id=USER_ID,
            metadata=metadata or {},
        )
        return f"Memory stored: {result}"
    except Exception as e:
        return f"Memory storage failed: {e}"


def search_memory(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search memories by semantic query."""
    if not settings.mem0_api_key:
        return []

    try:
        client = _get_client()
        results = client.search(query, user_id=USER_ID, limit=limit)
        return results
    except Exception as e:
        return [{"error": str(e)}]


def get_all_memories() -> list[dict[str, Any]]:
    """Retrieve all stored memories."""
    if not settings.mem0_api_key:
        return []

    try:
        client = _get_client()
        return client.get_all(user_id=USER_ID)
    except Exception as e:
        return [{"error": str(e)}]


def remember_anomaly(description: str, date_str: str) -> str:
    """Store an anomaly/alert observation."""
    return add_memory(
        f"Anomaly on {date_str}: {description}",
        metadata={"type": "anomaly", "date": date_str},
    )


def remember_daily_summary(summary: str, date_str: str) -> str:
    """Store a daily summary observation."""
    return add_memory(
        f"Daily summary for {date_str}: {summary}",
        metadata={"type": "daily_summary", "date": date_str},
    )


def get_context_for_report() -> str:
    """Retrieve recent memory context to inform the daily report."""
    memories = search_memory("recent sales anomaly trend alert", limit=5)
    if not memories:
        return "No prior context available."

    lines = []
    for m in memories:
        if "memory" in m:
            lines.append(f"- {m['memory']}")
        elif "content" in m:
            lines.append(f"- {m['content']}")

    return "\n".join(lines) if lines else "No relevant memories found."
