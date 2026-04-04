"""
Intent router — deterministic keyword matching for daily_fetch trigger.

All other intents are handled by the unified Claude agent, so we only need
to detect explicit daily report trigger phrases here. This eliminates an
unnecessary Haiku API call per message and prevents misclassification.

Returns:
  "daily_fetch"  — user explicitly wants to trigger the daily NRS fetch
  "other"        — everything else (handled by the agent)
"""

import re

# Phrases that explicitly trigger the daily report fetch.
# Must be specific commands, NOT questions about sales data.
_DAILY_PHRASES = [
    "run daily",
    "do daily",
    "daily report",
    "daily fetch",
    "fetch daily",
    "pull daily",
    "do the daily",
    "daily sheet",
    "start daily",
    "get daily",
    "daily numbers",
    "run report",
    "do report",
]

# Compile a regex that matches any of the trigger phrases as whole words
_DAILY_RE = re.compile(
    "|".join(re.escape(p) for p in _DAILY_PHRASES),
    re.IGNORECASE,
)


def classify_message(text: str) -> str:
    """
    Classify a message. Returns "daily_fetch" or "other".

    Only explicit daily report trigger commands match. Questions about sales
    ("what was my sale yesterday") go to "other" so the agent handles them.
    """
    clean = text.strip().lower()

    # /daily command
    if clean in ("/daily", "daily"):
        return "daily_fetch"

    # Check for trigger phrases, but NOT if it looks like a question
    if "?" in clean:
        return "other"

    if _DAILY_RE.search(clean):
        return "daily_fetch"

    return "other"
