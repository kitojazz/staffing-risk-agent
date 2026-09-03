"""Agent configuration. Everything tunable lives here."""

import os


def _int(name: str, default: int) -> int:
    """Read an environment variable as an integer, with a default."""
    return int(os.environ.get(name, default))


# --- Analysis window ---
# 14 days: >= staffing lead time (1-3 weeks to cover a role).
WINDOW_DAYS = _int("WINDOW_DAYS", 14)

# --- Retry policy ---
MAX_ATTEMPTS = _int("MAX_ATTEMPTS", 4)      # ~99% success at 30% failure rate
RUN_BUDGET_SECONDS = _int("RUN_BUDGET_SECONDS", 120)
BACKOFF_BASE_SECONDS = 1.0                   # 1s, 2s, 4s

# --- Confidence thresholds ---
CONFIDENCE_ASSERT = 0.9    # >= assert
CONFIDENCE_ASK = 0.5       # between ASK and ASSERT: ask
# < CONFIDENCE_ASK: report uncertainty, do not assert

# --- Reminders ---
REMINDER_DAYS = _int("REMINDER_DAYS", 7)
RESOLUTION_TTL_DAYS = _int("RESOLUTION_TTL_DAYS", 90)

# --- Endpoints and credentials ---
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
CANDIDATE_TOKEN = os.environ.get("CANDIDATE_TOKEN", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Dead man's switch: the agent pings this URL on a successful finish.
# If the ping doesn't arrive on time, the external service (e.g. Healthchecks.io) alerts.
HEARTBEAT_URL = os.environ.get("HEARTBEAT_URL", "")

# --- Critical vs enriching sources ---
# If a critical source fails, the agent stays silent.
CRITICAL_SOURCES = ("projects", "allocations", "time_off", "users")
ENRICHING_SOURCES = ("tasks", "members", "opportunities", "accounts", "time_entries")
