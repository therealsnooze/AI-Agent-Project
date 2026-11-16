from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ActionError(Exception):
    """Expected, user-explainable failure (bad path, SQL error, HTTP 404...).
    The message is returned to the model so it can correct itself."""


class SandboxError(ActionError):
    """The action tried to reach outside the allowed boundary."""


@dataclass
class ExecutionResult:
    content: str                      # what the model sees
    summary: str                      # one line for the UI and audit log
    display: dict[str, Any] | None = None  # rich rendering hint for the UI
    ok: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


def resolve_in_workspace(root: Path, rel_path: str) -> Path:
    """Resolve a user/model supplied path and refuse anything outside root
    (absolute paths, '..' traversal, symlinks pointing out)."""
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise SandboxError("Path must be a non-empty string.")
    raw = rel_path.strip().replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":") or raw.startswith("~"):
        raise SandboxError(f"'{rel_path}' is absolute. Use a path relative to the workspace root.")
    root = root.resolve()
    target = (root / raw).resolve()
    if target != root and not target.is_relative_to(root):
        raise SandboxError(f"'{rel_path}' points outside the workspace.")
    return target


def rel(root: Path, path: Path) -> str:
    r = path.resolve().relative_to(root.resolve()).as_posix()
    return r or "."
