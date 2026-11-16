"""System actions: host info and an allowlist of read-only commands.

Commands never go through a shell. Each executable has its own argument
rule, file arguments are confined to the workspace, and shell syntax is
rejected outright (an allowlist, not a blocklist)."""

from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .. import config
from .base import ActionError, ExecutionResult, SandboxError, resolve_in_workspace

SHELL_SYNTAX = re.compile(r"[;&|`$<>(){}*?\[\]!\\\n]")
FLAG = re.compile(r"^-{1,2}[A-Za-z][A-Za-z0-9-]*$")

# executable -> rule: "none" (no args), "flags", "flags+paths", "any-text", or a set of exact arg lists
COMMANDS: dict[str, object] = {
    "date": "none",
    "whoami": "none",
    "hostname": "none",
    "uname": "flags",
    "df": "flags",
    "echo": "any-text",
    "ls": "flags+paths",
    "cat": "flags+paths",
    "head": "flags+paths",
    "tail": "flags+paths",
    "wc": "flags+paths",
    "du": "flags+paths",
    "python": {("--version",), ("-V",)},
    "git": {("--version",)},
}


def parse_command(command: str, root: Path) -> list[str]:
    """Validate a command string and return argv. Raises SandboxError if not allowed."""
    if SHELL_SYNTAX.search(command):
        raise SandboxError("Shell syntax (pipes, redirects, chaining, globs, variables) is not allowed.")
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise SandboxError(f"Could not parse command: {e}") from e
    if not argv:
        raise SandboxError("Empty command.")
    prog, args = argv[0], argv[1:]
    if prog not in COMMANDS:
        raise SandboxError(f"'{prog}' is not an allowed command. Allowed: {', '.join(COMMANDS)}.")
    rule = COMMANDS[prog]
    if isinstance(rule, set):
        if tuple(args) not in rule:
            raise SandboxError(f"'{prog}' may only be run as: " + " | ".join(f"{prog} {' '.join(a)}" for a in rule))
    elif rule == "none" and args:
        raise SandboxError(f"'{prog}' takes no arguments here.")
    elif rule == "flags":
        bad = [a for a in args if not FLAG.match(a)]
        if bad:
            raise SandboxError(f"Only simple flags are allowed for '{prog}' (rejected: {bad}).")
    elif rule == "flags+paths":
        for i, a in enumerate(args):
            if a.startswith("-"):
                if not FLAG.match(a):
                    raise SandboxError(f"Flag '{a}' is not allowed.")
            elif a.isdigit() and i > 0 and args[i - 1] in ("-n", "-c"):
                continue  # value for head/tail -n N
            else:
                resolve_in_workspace(root, a)
    return argv


class SystemExecutor:
    def __init__(self, root: Path):
        self.root = root

    def system_info(self, **_) -> ExecutionResult:
        usage = shutil.disk_usage(self.root)
        ws_bytes = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        info = {
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "cpu_count": os.cpu_count(),
            "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "disk_total_gb": round(usage.total / 1e9, 1),
            "disk_free_gb": round(usage.free / 1e9, 1),
            "workspace_files": sum(1 for p in self.root.rglob("*") if p.is_file()),
            "workspace_kb": round(ws_bytes / 1024, 1),
        }
        text = "\n".join(f"{k}: {v}" for k, v in info.items())
        return ExecutionResult(content=text, summary=f"{info['os']}, Python {info['python']}",
                               display={"kind": "json", "data": info})

    def run_command(self, command: str, **_) -> ExecutionResult:
        argv = parse_command(command, self.root)
        exe = shutil.which(argv[0])
        if exe is None:
            raise ActionError(f"'{argv[0]}' is allowed but not installed on this host ({platform.system()}).")
        try:
            proc = subprocess.run(
                [exe, *argv[1:]],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=config.COMMAND_TIMEOUT_SECONDS,
                shell=False,
                env={k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "LANG", "TZ")},
            )
        except subprocess.TimeoutExpired as e:
            raise ActionError(f"Command timed out after {config.COMMAND_TIMEOUT_SECONDS}s.") from e
        out = (proc.stdout + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")).strip()
        out = out[: config.MAX_TOOL_RESULT_CHARS] or "(no output)"
        return ExecutionResult(
            content=f"exit code {proc.returncode}\n{out}",
            summary=f"`{command}` exited with {proc.returncode}",
            display={"kind": "text", "text": out, "language": "text"},
            ok=proc.returncode == 0,
        )
