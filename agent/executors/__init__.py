"""Domain executors and the registry that maps tool names to operations."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .base import ActionError, ExecutionResult, SandboxError, resolve_in_workspace
from .database import DatabaseExecutor
from .files import FileExecutor
from .system import SystemExecutor
from .web import WebExecutor

__all__ = ["ActionError", "ExecutionResult", "Executors", "SandboxError", "resolve_in_workspace"]


class Executors:
    def __init__(self, root: Path):
        self.root = root
        self.files = FileExecutor(root)
        self.db = DatabaseExecutor(root / "store.db", self.files)
        self.web = WebExecutor()
        self.system = SystemExecutor(root)
        self.handlers: dict[str, Callable[..., ExecutionResult]] = {
            "list_files": self.files.list_files,
            "read_file": self.files.read_file,
            "write_file": self.files.write_file,
            "move_file": self.files.move_file,
            "delete_file": self.files.delete_file,
            "describe_database": self.db.describe_database,
            "run_sql": self.db.run_sql,
            "export_query_to_csv": self.db.export_query_to_csv,
            "http_get": self.web.http_get,
            "system_info": self.system.system_info,
            "run_command": self.system.run_command,
        }

    def execute(self, tool: str, args: dict) -> ExecutionResult:
        handler = self.handlers.get(tool)
        if handler is None:
            raise ActionError(f"No executor for tool '{tool}'.")
        return handler(**args)
