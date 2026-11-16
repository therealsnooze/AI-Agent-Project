"""Tool (action) definitions sent to the model.

Each schema is `strict`, so the API guarantees schema-valid arguments; the
executor still re-validates everything (see validation.py) before running.
Every side-effecting tool asks for a `rationale`, which is shown to the user
in approval prompts and recorded in the audit log.
"""

from __future__ import annotations

from . import config

_RATIONALE = {
    "type": "string",
    "description": "One short, user-facing sentence explaining why this action is needed. Shown in approval prompts and the audit log.",
}


def _tool(name: str, description: str, properties: dict, domain: str) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
        "_domain": domain,  # stripped before sending
    }


_ALLOWED_HOSTS_DOC = "\n".join(f"- {host}: {doc}" for host, doc in config.ALLOWED_HTTP_HOSTS.items())

_TOOL_SPECS: list[dict] = [
    _tool(
        "update_plan",
        "Record or update your step-by-step plan for the current request. Call it before starting any request that needs 2+ actions, "
        "and again as steps complete. The plan is displayed to the user as a live checklist. Has no side effects.",
        {
            "steps": {
                "type": "array",
                "description": "The full plan, in order (send every step each time, not just changes).",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short imperative step, e.g. 'Count last month's orders'."},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "done", "skipped"]},
                    },
                    "required": ["title", "status"],
                    "additionalProperties": False,
                },
            }
        },
        "planning",
    ),
    # --- Files ---------------------------------------------------------------
    _tool(
        "list_files",
        "List files and folders in the sandboxed workspace with their sizes. Use '.' for the workspace root.",
        {
            "path": {"type": "string", "description": "Folder path relative to the workspace root."},
            "recursive": {"type": "boolean", "description": "Include sub-folders."},
            "rationale": _RATIONALE,
        },
        "files",
    ),
    _tool(
        "read_file",
        f"Read a UTF-8 text file from the workspace (first {config.MAX_FILE_READ_CHARS:,} characters).",
        {"path": {"type": "string", "description": "File path relative to the workspace root."}, "rationale": _RATIONALE},
        "files",
    ),
    _tool(
        "write_file",
        "Write a UTF-8 text file in the workspace. Parent folders are created automatically. "
        "mode='create' fails if the file exists; 'overwrite' replaces it (requires user approval); 'append' adds to the end.",
        {
            "path": {"type": "string", "description": "File path relative to the workspace root."},
            "content": {"type": "string", "description": "The full text to write."},
            "mode": {"type": "string", "enum": ["create", "overwrite", "append"]},
            "rationale": _RATIONALE,
        },
        "files",
    ),
    _tool(
        "move_file",
        "Move or rename a file or folder inside the workspace. Replacing an existing destination requires user approval.",
        {
            "source": {"type": "string", "description": "Existing path relative to the workspace root."},
            "destination": {"type": "string", "description": "New path relative to the workspace root."},
            "rationale": _RATIONALE,
        },
        "files",
    ),
    _tool(
        "delete_file",
        "Delete a file, or a folder and everything in it. Always requires user approval.",
        {"path": {"type": "string", "description": "Path relative to the workspace root."}, "rationale": _RATIONALE},
        "files",
    ),
    # --- Database ------------------------------------------------------------
    _tool(
        "describe_database",
        "Show every table in the store's SQLite database with its columns, types and row count. Call this before writing SQL "
        "if you have not seen the schema in this conversation.",
        {"rationale": _RATIONALE},
        "database",
    ),
    _tool(
        "run_sql",
        "Run exactly ONE SQLite statement against the store database. SELECT runs immediately in read-only mode. "
        "INSERT/CREATE may run directly; UPDATE/DELETE/DROP/ALTER require user approval and are previewed (affected row count) first. "
        f"Results are capped at {config.MAX_SQL_ROWS} rows - use LIMIT/aggregates for large tables. "
        "Dates are stored as ISO text 'YYYY-MM-DD'; use SQLite date functions.",
        {"query": {"type": "string", "description": "A single SQLite statement, no trailing commentary."}, "rationale": _RATIONALE},
        "database",
    ),
    _tool(
        "export_query_to_csv",
        "Run a read-only SELECT and save the full result as a CSV file in the workspace. Use this instead of run_sql + write_file "
        "when the user wants data exported. Replacing an existing file requires approval.",
        {
            "query": {"type": "string", "description": "A single SELECT statement."},
            "path": {"type": "string", "description": "Destination .csv path relative to the workspace root."},
            "rationale": _RATIONALE,
        },
        "database",
    ),
    # --- Web APIs -------------------------------------------------------------
    _tool(
        "http_get",
        "HTTPS GET request to an allowlisted public JSON API. No API keys are needed. Allowed hosts and example paths:\n"
        + _ALLOWED_HOSTS_DOC
        + "\nOther hosts are blocked. To get weather for a city, first geocode it, then call the forecast endpoint.",
        {"url": {"type": "string", "description": "Full https:// URL including query string."}, "rationale": _RATIONALE},
        "web",
    ),
    # --- System ----------------------------------------------------------------
    _tool(
        "system_info",
        "Report host details: OS, Python version, CPU count, current server time, and workspace disk usage.",
        {"rationale": _RATIONALE},
        "system",
    ),
    _tool(
        "run_command",
        "Run one allowlisted, read-only command in the workspace folder (no shell: pipes, redirects, globs and chaining are rejected). "
        "Allowed: date, whoami, hostname, uname, echo, ls, cat, head, tail, wc, du, df, python --version, git --version. "
        "File arguments must be inside the workspace. Prefer the file tools for reading files; use this for system diagnostics.",
        {"command": {"type": "string", "description": "e.g. 'wc -l notes/meeting-notes.md' or 'df -h'"}, "rationale": _RATIONALE},
        "system",
    ),
]

TOOL_DOMAINS: dict[str, str] = {spec["name"]: spec["_domain"] for spec in _TOOL_SPECS}
TOOL_SCHEMAS: dict[str, dict] = {spec["name"]: spec["input_schema"] for spec in _TOOL_SPECS}
# What the API receives: deterministic order, internal keys removed (stable for prompt caching).
API_TOOLS: list[dict] = [{k: v for k, v in spec.items() if not k.startswith("_")} for spec in _TOOL_SPECS]

DOMAIN_ICONS = {"planning": "🧭", "files": "📁", "database": "🗄️", "web": "🌐", "system": "💻"}
