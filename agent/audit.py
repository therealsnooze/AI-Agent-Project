"""Append-only JSONL audit log. Every request, model call, approval decision
and executed action is recorded so a session can be reconstructed later."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_MAX_FIELD_CHARS = 2000


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return value[:_MAX_FIELD_CHARS] + f"... [truncated {len(value) - _MAX_FIELD_CHARS} chars]"
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v) for v in value]
    return value


class AuditLogger:
    def __init__(self, path: Path, session_id: str):
        self.path = path
        self.session_id = session_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **fields: Any) -> dict:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session": self.session_id,
            "event": event,
            **_truncate(fields),
        }
        line = json.dumps(record, default=str, ensure_ascii=False)
        with _LOCK, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return record

    def read(self, only_this_session: bool = True, limit: int = 1000) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        with _LOCK, self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if only_this_session and rec.get("session") != self.session_id:
                    continue
                records.append(rec)
        return records[-limit:]
