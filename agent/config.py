"""Central configuration. Every value can be overridden with an environment variable."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = PROJECT_ROOT / "seed"
WORKSPACES_DIR = Path(os.getenv("OPSPILOT_WORKSPACES", PROJECT_ROOT / "workspaces"))
LOG_DIR = Path(os.getenv("OPSPILOT_LOG_DIR", PROJECT_ROOT / "logs"))
AUDIT_LOG_PATH = LOG_DIR / "audit.jsonl"

# --- LLM -------------------------------------------------------------------
# Prices are USD per million tokens (input, output). Used only for the
# cost estimate shown in the UI.
MODELS: dict[str, dict] = {
    "claude-opus-5-5": {"label": "Claude Opus 5.5", "input": 4.00, "output": 20.00, "server_fallbacks": True},
    "claude-sonnet-5-5": {"label": "Claude Sonnet 5.5", "input": 2.00, "output": 10.00, "server_fallbacks": True},
    "claude-haiku-5-5": {"label": "Claude Haiku 5.5", "input": 0.10, "output": 0.50, "server_fallbacks": False},
}
DEFAULT_MODEL = os.getenv("OPSPILOT_MODEL", "claude-opus-5-5")
EFFORT_LEVELS = ["low", "medium", "high", "xhigh"]
DEFAULT_EFFORT = os.getenv("OPSPILOT_EFFORT", "medium")
MAX_TOKENS = 16000
REQUEST_TIMEOUT_SECONDS = 180.0
SDK_MAX_RETRIES = 3  # SDK retries 408/409/429/5xx with exponential backoff
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Client-side rate limiting (protects the API key when the app is public).
MIN_SECONDS_BETWEEN_REQUESTS = float(os.getenv("OPSPILOT_MIN_INTERVAL", "0.5"))
MAX_REQUESTS_PER_SESSION = int(os.getenv("OPSPILOT_MAX_REQUESTS", "80"))

# --- Agent loop ------------------------------------------------------------
MAX_AGENT_STEPS = 15  # model calls per user request
MAX_TOOL_RESULT_CHARS = 12000

# --- Executors -------------------------------------------------------------
MAX_FILE_READ_CHARS = 20000
MAX_FILE_WRITE_CHARS = 200000
MAX_SQL_ROWS = 200
HTTP_TIMEOUT_SECONDS = 10
HTTP_MAX_BYTES = 300_000
COMMAND_TIMEOUT_SECONDS = 10

# Public, key-less APIs the agent may call. Anything else is blocked.
ALLOWED_HTTP_HOSTS: dict[str, str] = {
    "api.open-meteo.com": "Weather forecasts. e.g. /v1/forecast?latitude=34.05&longitude=-118.24&current=temperature_2m,wind_speed_10m&daily=temperature_2m_max,temperature_2m_min&timezone=auto",
    "geocoding-api.open-meteo.com": "City name -> latitude/longitude. e.g. /v1/search?name=Los%20Angeles&count=1",
    "api.frankfurter.dev": "Currency exchange rates. e.g. /v1/latest?base=USD&symbols=EUR,MXN",
    "en.wikipedia.org": "Article summaries. e.g. /api/rest_v1/page/summary/Coffee_roasting",
    "api.github.com": "Public GitHub repo/user info. e.g. /repos/python/cpython",
}
