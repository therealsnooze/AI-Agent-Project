"""Safety policy: classify every action's risk *in code*, independent of
what the model claims, and decide whether a human must approve it.

    SAFE         read-only; runs automatically
    WRITE        creates/changes data non-destructively; approval only in strict mode
    DESTRUCTIVE  overwrites or deletes data; always needs approval, with a preview
    BLOCKED      violates the sandbox/allowlists; never runs
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .executors import Executors, SandboxError, resolve_in_workspace
from .executors.database import KINDS, classify_sql
from .executors.system import parse_command
from .executors.web import check_url


class Risk(str, Enum):
    SAFE = "safe"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    BLOCKED = "blocked"


@dataclass
class Assessment:
    risk: Risk
    reasons: list[str] = field(default_factory=list)
    preview: str | None = None
    preview_language: str = "text"


APPROVAL_MODES = {
    "standard": "Approve destructive actions only",
    "strict": "Approve every change",
}


def needs_approval(assessment: Assessment, mode: str) -> bool:
    if assessment.risk is Risk.DESTRUCTIVE:
        return True
    return assessment.risk is Risk.WRITE and mode == "strict"


def assess(tool: str, args: dict, ex: Executors) -> Assessment:
    """Raises ActionError (not SandboxError) when the action is merely invalid,
    e.g. deleting a file that doesn't exist - that is reported to the model as
    an ordinary error rather than a policy block."""
    try:
        return _assess(tool, args, ex)
    except SandboxError as e:
        return Assessment(Risk.BLOCKED, [str(e)])


def _assess(tool: str, args: dict, ex: Executors) -> Assessment:
    if tool in ("update_plan", "describe_database", "system_info"):
        return Assessment(Risk.SAFE)

    if tool in ("list_files", "read_file"):
        resolve_in_workspace(ex.root, args["path"])
        return Assessment(Risk.SAFE)

    if tool == "http_get":
        check_url(args["url"])
        return Assessment(Risk.SAFE, ["GET request to an allowlisted public API"])

    if tool == "run_command":
        parse_command(args["command"], ex.root)
        return Assessment(Risk.SAFE, ["Allowlisted read-only command, no shell"])

    if tool == "write_file":
        exists, diff = ex.files.preview_write(args["path"], args["content"], args["mode"])
        if args["mode"] == "overwrite" and exists:
            return Assessment(Risk.DESTRUCTIVE, ["Replaces the current contents of an existing file"], diff, "diff")
        verb = "Appends to" if args["mode"] == "append" and exists else "Creates"
        return Assessment(Risk.WRITE, [f"{verb} a file in the workspace"], args["content"][:3000])

    if tool == "move_file":
        ex.files.check_writable(args["source"])
        dst = ex.files.check_writable(args["destination"])
        if dst.exists():
            return Assessment(Risk.DESTRUCTIVE, ["The destination already exists and will be replaced"],
                              ex.files.describe_target(args["destination"]))
        return Assessment(Risk.WRITE, ["Moves/renames a file inside the workspace"])

    if tool == "delete_file":
        preview = ex.files.describe_target(args["path"])
        return Assessment(Risk.DESTRUCTIVE, ["Permanently deletes data (there is no recycle bin)"], preview)

    if tool == "run_sql":
        stmt, kind = classify_sql(args["query"])
        read_only, destructive = KINDS[kind]
        if read_only:
            return Assessment(Risk.SAFE, ["Read-only query (enforced by a query_only connection)"])
        if destructive:
            reasons = {
                "update": "Modifies existing database rows",
                "delete": "Permanently deletes database rows",
                "replace": "May overwrite existing database rows",
                "drop": "Permanently removes a database object and its data",
                "alter": "Changes the database schema",
            }[kind]
            return Assessment(Risk.DESTRUCTIVE, [reasons], ex.db.dry_run(stmt, kind) + "\n\n" + stmt, "sql")
        return Assessment(Risk.WRITE, [f"Adds data/structure to the database ({kind.upper()})"], stmt, "sql")

    if tool == "export_query_to_csv":
        stmt, kind = classify_sql(args["query"])
        if kind != "read":
            raise SandboxError("export_query_to_csv only accepts read-only SELECT queries.")
        dst = ex.files.check_writable(args["path"])
        if dst.exists():
            return Assessment(Risk.DESTRUCTIVE, ["Replaces an existing file"], ex.files.describe_target(args["path"]))
        return Assessment(Risk.WRITE, ["Creates a CSV file in the workspace"], stmt, "sql")

    raise SandboxError(f"Unknown tool '{tool}'.")
