"""SQLite actions with statement classification, dry-run previews and a
read-only connection for queries (defence in depth: even a misclassified
statement cannot write through a query_only connection)."""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from pathlib import Path

from .. import config
from .base import ActionError, ExecutionResult, SandboxError, rel
from .files import FileExecutor

READ_PRAGMAS = {"table_info", "table_xinfo", "table_list", "index_list", "index_info", "foreign_key_list"}
BLOCKED_KEYWORDS = {"ATTACH", "DETACH", "VACUUM", "REINDEX", "ANALYZE", "BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}

# kind -> (is_read_only, is_destructive)
KINDS = {
    "read": (True, False),
    "insert": (False, False),
    "create": (False, False),
    "update": (False, True),
    "delete": (False, True),
    "replace": (False, True),
    "drop": (False, True),
    "alter": (False, True),
}


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def single_statement(sql: str) -> str:
    """Return the statement without a trailing ';', or raise if there are several."""
    text = _strip_comments(sql).strip()
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"', "`"):
            quote = ch
        elif ch == ";":
            if text[i + 1:].strip():
                raise SandboxError("Only one SQL statement per call is allowed. Split it into separate run_sql calls.")
            return text[:i].strip()
    if not text:
        raise ActionError("The query is empty.")
    return text


def classify_sql(sql: str) -> tuple[str, str]:
    """Returns (normalized_statement, kind). Raises ActionError for blocked statements."""
    stmt = single_statement(sql)
    first = re.match(r"\s*([A-Za-z]+)", stmt)
    keyword = first.group(1).upper() if first else ""
    if keyword in ("SELECT", "WITH", "VALUES", "EXPLAIN"):
        return stmt, "read"
    if keyword == "PRAGMA":
        name = re.match(r"\s*PRAGMA\s+(?:\w+\.)?(\w+)", stmt, re.I)
        if name and name.group(1).lower() in READ_PRAGMAS and "=" not in stmt:
            return stmt, "read"
        raise SandboxError("Only schema-inspection PRAGMAs (table_info, index_list, ...) are allowed.")
    if keyword == "INSERT":
        return stmt, "replace" if re.match(r"\s*INSERT\s+OR\s+REPLACE", stmt, re.I) else "insert"
    if keyword == "CREATE":
        if re.match(r"\s*CREATE\s+(TEMP\w*\s+)?TRIGGER", stmt, re.I):
            raise SandboxError("Creating triggers is not allowed.")
        return stmt, "create"
    if keyword in ("UPDATE", "DELETE", "REPLACE", "DROP", "ALTER"):
        return stmt, keyword.lower()
    if keyword in BLOCKED_KEYWORDS:
        raise SandboxError(f"{keyword} statements are not allowed.")
    raise SandboxError(f"Unsupported or unrecognized SQL statement starting with '{keyword or stmt[:20]}'.")


def _authorizer(action, arg1, arg2, db_name, trigger):
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() not in READ_PRAGMAS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class DatabaseExecutor:
    def __init__(self, db_path: Path, files: FileExecutor):
        self.db_path = db_path
        self.files = files

    def _connect(self, read_only: bool) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=5)
        conn.execute("PRAGMA foreign_keys = ON")
        if read_only:
            conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_authorizer)
        return conn

    # -- read-only -----------------------------------------------------------
    def describe_database(self, **_) -> ExecutionResult:
        conn = self._connect(read_only=True)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            lines, rows = [], []
            for t in tables:
                cols = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
                count = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                col_desc = ", ".join(f"{c[1]} {c[2]}{' PK' if c[5] else ''}" for c in cols)
                lines.append(f"{t} ({count} rows): {col_desc}")
                rows.append([t, count, col_desc])
            fks = []
            for t in tables:
                for fk in conn.execute(f'PRAGMA foreign_key_list("{t}")'):
                    fks.append(f"{t}.{fk[3]} -> {fk[2]}.{fk[4]}")
            if fks:
                lines.append("Foreign keys: " + "; ".join(fks))
        finally:
            conn.close()
        return ExecutionResult(
            content="\n".join(lines) or "(no tables)",
            summary=f"{len(tables)} tables",
            display={"kind": "table", "columns": ["table", "rows", "columns"], "rows": rows},
        )

    def _select(self, stmt: str, limit: int | None) -> tuple[list[str], list[list], bool]:
        conn = self._connect(read_only=True)
        try:
            cur = conn.execute(stmt)
            columns = [d[0] for d in cur.description or []]
            if limit is None:
                data = cur.fetchall()
                truncated = False
            else:
                data = cur.fetchmany(limit + 1)
                truncated = len(data) > limit
                data = data[:limit]
            return columns, [list(r) for r in data], truncated
        except sqlite3.Error as e:
            raise ActionError(f"SQLite error: {e}") from e
        finally:
            conn.close()

    # -- previews used by the safety policy --------------------------------------
    def dry_run(self, stmt: str, kind: str) -> str:
        """Execute inside a transaction that is always rolled back; report the impact."""
        notes = []
        if kind in ("update", "delete") and not re.search(r"\bWHERE\b", stmt, re.I):
            notes.append(f"⚠️ No WHERE clause - this {kind.upper()} applies to EVERY row in the table.")
        if kind == "drop":
            m = re.match(r'\s*DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?["`\[]?(\w+)', stmt, re.I)
            if m:
                try:
                    _, rows, _ = self._select(f'SELECT COUNT(*) FROM "{m.group(1)}"', 1)
                    notes.append(f"Table '{m.group(1)}' currently holds {rows[0][0]} row(s); they will be permanently lost.")
                except ActionError:
                    pass
        conn = self._connect(read_only=False)
        try:
            before = conn.total_changes
            conn.execute("BEGIN")
            conn.execute(stmt)
            changed = conn.total_changes - before
            conn.execute("ROLLBACK")
        except sqlite3.Error as e:
            raise ActionError(f"SQLite error (dry run): {e}") from e
        finally:
            conn.close()
        if kind in ("update", "delete", "replace"):
            notes.insert(0, f"Dry run: this statement would affect {changed} row(s).")
        return "\n".join(notes) or "Dry run succeeded."

    # -- execution -------------------------------------------------------------
    def run_sql(self, query: str, **_) -> ExecutionResult:
        stmt, kind = classify_sql(query)
        if kind == "read":
            columns, rows, truncated = self._select(stmt, config.MAX_SQL_ROWS)
            payload = {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}
            note = f" (first {config.MAX_SQL_ROWS})" if truncated else ""
            return ExecutionResult(
                content=json.dumps(payload, default=str),
                summary=f"{len(rows)} row(s) returned{note}",
                display={"kind": "table", "columns": columns, "rows": rows},
            )
        conn = self._connect(read_only=False)
        try:
            before = conn.total_changes
            conn.execute("BEGIN")
            conn.execute(stmt)
            changed = conn.total_changes - before
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise ActionError(f"SQLite error: {e}") from e
        finally:
            conn.close()
        msg = f"{kind.upper()} committed; {changed} row(s) affected."
        return ExecutionResult(content=msg, summary=msg)

    def export_query_to_csv(self, query: str, path: str, **_) -> ExecutionResult:
        stmt, kind = classify_sql(query)
        if kind != "read":
            raise SandboxError("export_query_to_csv only accepts read-only SELECT queries.")
        if not path.lower().endswith(".csv"):
            raise ActionError("The destination must end with .csv.")
        target = self.files.check_writable(path)
        columns, rows, _ = self._select(stmt, None)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(buf.getvalue(), encoding="utf-8")
        where = rel(self.files.root, target)
        return ExecutionResult(
            content=f"Exported {len(rows)} row(s) with columns {columns} to {where}.",
            summary=f"Exported {len(rows)} row(s) to {where}",
            display={"kind": "table", "columns": columns, "rows": rows[:50]},
        )
