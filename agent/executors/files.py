"""File-system actions, confined to the session workspace."""

from __future__ import annotations

import difflib
import shutil
from pathlib import Path

from .. import config
from .base import ActionError, ExecutionResult, SandboxError, rel, resolve_in_workspace

PROTECTED_NAMES = {"store.db", "store.db-journal", "store.db-wal"}


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        raise ActionError(f"'{path.name}' is a binary file and can't be read as text.")
    return data.decode("utf-8", errors="replace")


class FileExecutor:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, p: str) -> Path:
        return resolve_in_workspace(self.root, p)

    def check_writable(self, p: str) -> Path:
        path = self._path(p)
        if path == self.root.resolve():
            raise SandboxError("The workspace root itself can't be modified.")
        if path.name in PROTECTED_NAMES:
            raise SandboxError("The database file can't be changed with file tools - use run_sql instead.")
        return path

    # -- read-only -----------------------------------------------------------
    def list_files(self, path: str, recursive: bool, **_) -> ExecutionResult:
        target = self._path(path)
        if not target.exists():
            raise ActionError(f"'{path}' does not exist.")
        if not target.is_dir():
            raise ActionError(f"'{path}' is a file, not a folder.")
        entries = sorted(target.rglob("*") if recursive else target.iterdir(), key=lambda p: p.as_posix().lower())
        rows = []
        for p in entries[:500]:
            is_dir = p.is_dir()
            rows.append([rel(self.root, p) + ("/" if is_dir else ""), "folder" if is_dir else "file",
                         "" if is_dir else _human_size(p.stat().st_size)])
        text = "\n".join(f"{r[0]}\t{r[2]}" for r in rows) or "(empty folder)"
        if len(entries) > 500:
            text += f"\n... {len(entries) - 500} more entries"
        return ExecutionResult(
            content=text,
            summary=f"{len(rows)} entries in {rel(self.root, target)}",
            display={"kind": "table", "columns": ["path", "type", "size"], "rows": rows},
        )

    def read_file(self, path: str, **_) -> ExecutionResult:
        target = self._path(path)
        if not target.is_file():
            raise ActionError(f"'{path}' is not an existing file.")
        text = _read_text(target)
        shown = text[: config.MAX_FILE_READ_CHARS]
        note = f"\n\n[truncated: showing {len(shown):,} of {len(text):,} characters]" if len(text) > len(shown) else ""
        return ExecutionResult(
            content=shown + note,
            summary=f"Read {rel(self.root, target)} ({len(text):,} chars)",
            display={"kind": "text", "text": shown, "language": target.suffix.lstrip(".") or "text"},
        )

    # -- previews used by the safety policy --------------------------------------
    def preview_write(self, path: str, content: str, mode: str) -> tuple[bool, str | None]:
        """Returns (file_exists, diff_or_none)."""
        target = self.check_writable(path)
        if not target.exists():
            return False, None
        if target.is_dir():
            raise ActionError(f"'{path}' is a folder.")
        if mode != "overwrite":
            return True, None
        old = _read_text(target)
        diff = difflib.unified_diff(
            old.splitlines(), content.splitlines(), fromfile=f"{path} (current)", tofile=f"{path} (proposed)", lineterm="", n=2
        )
        return True, "\n".join(list(diff)[:200]) or "(no changes - content is identical)"

    def describe_target(self, path: str) -> str:
        target = self.check_writable(path)
        if not target.exists():
            raise ActionError(f"'{path}' does not exist.")
        if target.is_dir():
            files = [p for p in target.rglob("*") if p.is_file()]
            listing = "\n".join("  " + rel(self.root, p) for p in files[:25])
            more = f"\n  ... and {len(files) - 25} more" if len(files) > 25 else ""
            return f"Folder with {len(files)} file(s):\n{listing}{more}"
        size = target.stat().st_size
        try:
            head = "\n".join(_read_text(target).splitlines()[:8])
        except ActionError:
            head = "(binary file)"
        return f"File, {_human_size(size)}. First lines:\n{head}"

    # -- mutating ------------------------------------------------------------------
    def write_file(self, path: str, content: str, mode: str, **_) -> ExecutionResult:
        if len(content) > config.MAX_FILE_WRITE_CHARS:
            raise ActionError(f"Content is too large ({len(content):,} chars, max {config.MAX_FILE_WRITE_CHARS:,}).")
        target = self.check_writable(path)
        if target.is_dir():
            raise ActionError(f"'{path}' is a folder.")
        if mode == "create" and target.exists():
            raise ActionError(f"'{path}' already exists. Use mode 'overwrite' or 'append', or choose another name.")
        if mode == "append" and not target.exists():
            mode = "create"
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with target.open("a", encoding="utf-8") as f:
                f.write(content)
        else:
            target.write_text(content, encoding="utf-8")
        verb = {"create": "Created", "overwrite": "Overwrote", "append": "Appended to"}[mode]
        return ExecutionResult(
            content=f"{verb} {rel(self.root, target)} ({len(content):,} chars written).",
            summary=f"{verb} {rel(self.root, target)}",
            display={"kind": "text", "text": content[:5000], "language": target.suffix.lstrip(".") or "text"},
        )

    def move_file(self, source: str, destination: str, **_) -> ExecutionResult:
        src = self.check_writable(source)
        dst = self.check_writable(destination)
        if not src.exists():
            raise ActionError(f"'{source}' does not exist.")
        if dst.is_relative_to(src):
            raise ActionError("Can't move a folder into itself.")
        if dst.exists() and dst.is_dir():
            raise ActionError(f"'{destination}' is an existing folder; give the full destination path including the name.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.unlink()  # approved replacement (policy marks this destructive)
        shutil.move(str(src), str(dst))
        return ExecutionResult(
            content=f"Moved {source} -> {rel(self.root, dst)}.",
            summary=f"Moved {source} → {rel(self.root, dst)}",
        )

    def delete_file(self, path: str, **_) -> ExecutionResult:
        target = self.check_writable(path)
        if not target.exists():
            raise ActionError(f"'{path}' does not exist.")
        if target.is_dir():
            count = sum(1 for p in target.rglob("*") if p.is_file())
            shutil.rmtree(target)
            msg = f"Deleted folder {path} ({count} file(s))."
        else:
            target.unlink()
            msg = f"Deleted {path}."
        return ExecutionResult(content=msg, summary=msg)
