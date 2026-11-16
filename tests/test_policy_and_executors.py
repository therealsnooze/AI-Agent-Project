import pytest

from agent import tools
from agent.executors import ActionError
from agent.executors.database import classify_sql
from agent.policy import Risk, assess, needs_approval
from agent.validation import validate


# ---------------------------------------------------------------- validation
def test_schema_validation_catches_bad_arguments():
    schema = tools.TOOL_SCHEMAS["write_file"]
    assert validate(schema, {"path": "a.txt", "content": "x", "mode": "create", "rationale": "r"}) == []
    errors = validate(schema, {"path": "a.txt", "content": 5, "mode": "explode", "extra": 1})
    joined = " ".join(errors)
    assert "content: expected string" in joined
    assert "not one of" in joined
    assert "rationale: required" in joined
    assert "extra: unexpected" in joined


def test_every_tool_schema_is_strict_compatible():
    for spec in tools.API_TOOLS:
        schema = spec["input_schema"]
        assert spec["strict"] is True
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert not any(k.startswith("_") for k in spec)


# ------------------------------------------------------------------- sandbox
@pytest.mark.parametrize("path", ["../secret.txt", "/etc/passwd", "C:/Windows/win.ini", "~/x", "notes/../../x"])
def test_paths_outside_workspace_are_blocked(ex, path):
    assert assess("read_file", {"path": path, "rationale": ""}, ex).risk is Risk.BLOCKED


def test_database_file_is_protected_from_file_tools(ex):
    a = assess("delete_file", {"path": "store.db", "rationale": ""}, ex)
    assert a.risk is Risk.BLOCKED


# --------------------------------------------------------------------- files
def test_overwrite_is_destructive_with_diff(ex):
    a = assess("write_file", {"path": "README.txt", "content": "new text\n", "mode": "overwrite", "rationale": ""}, ex)
    assert a.risk is Risk.DESTRUCTIVE
    assert a.preview_language == "diff" and "+new text" in a.preview
    assert needs_approval(a, "standard")


def test_create_is_write_and_only_needs_approval_in_strict_mode(ex):
    a = assess("write_file", {"path": "reports/x.md", "content": "hi", "mode": "create", "rationale": ""}, ex)
    assert a.risk is Risk.WRITE
    assert not needs_approval(a, "standard")
    assert needs_approval(a, "strict")


def test_delete_missing_file_is_an_error_not_a_block(ex):
    with pytest.raises(ActionError):
        assess("delete_file", {"path": "nope.txt", "rationale": ""}, ex)


def test_file_roundtrip(ex):
    ex.execute("write_file", {"path": "reports/a/b.txt", "content": "hello", "mode": "create"})
    assert ex.execute("read_file", {"path": "reports/a/b.txt"}).content == "hello"
    with pytest.raises(ActionError):
        ex.execute("write_file", {"path": "reports/a/b.txt", "content": "again", "mode": "create"})
    ex.execute("move_file", {"source": "reports/a/b.txt", "destination": "reports/c.txt"})
    ex.execute("delete_file", {"path": "reports/c.txt"})
    assert not (ex.root / "reports/c.txt").exists()


# ------------------------------------------------------------------ database
@pytest.mark.parametrize("sql,kind", [
    ("SELECT * FROM products", "read"),
    ("  -- comment\n select 1;", "read"),
    ("WITH x AS (SELECT 1) SELECT * FROM x", "read"),
    ("INSERT INTO products VALUES (1)", "insert"),
    ("UPDATE products SET price = 1 WHERE id = 1", "update"),
    ("DELETE FROM orders WHERE id = 3", "delete"),
    ("DROP TABLE orders", "drop"),
    ("SELECT 'a;b' FROM products", "read"),
])
def test_sql_classification(sql, kind):
    assert classify_sql(sql)[1] == kind


@pytest.mark.parametrize("sql", [
    "SELECT 1; DROP TABLE products",
    "ATTACH DATABASE 'x.db' AS x",
    "PRAGMA query_only = OFF",
    "CREATE TRIGGER t AFTER INSERT ON orders BEGIN DELETE FROM products; END",
])
def test_dangerous_sql_is_blocked(ex, sql):
    assert assess("run_sql", {"query": sql, "rationale": ""}, ex).risk is Risk.BLOCKED


def test_update_dry_run_counts_rows_and_rolls_back(ex):
    before = ex.execute("run_sql", {"query": "SELECT SUM(price) FROM products"}).content
    a = assess("run_sql", {"query": "UPDATE products SET price = 0", "rationale": ""}, ex)
    assert a.risk is Risk.DESTRUCTIVE
    assert "affect 15 row(s)" in a.preview and "No WHERE clause" in a.preview
    assert ex.execute("run_sql", {"query": "SELECT SUM(price) FROM products"}).content == before


def test_read_connection_cannot_write_even_if_misclassified(ex):
    # A CTE classified as 'read' that actually writes must be refused by SQLite.
    with pytest.raises(ActionError):
        ex.execute("run_sql", {"query": "WITH x AS (SELECT 1) DELETE FROM order_items"})


def test_export_csv(ex):
    r = ex.execute("export_query_to_csv", {"query": "SELECT sku, stock FROM products", "path": "reports/stock.csv"})
    assert "15 row(s)" in r.content
    assert (ex.root / "reports/stock.csv").read_text().startswith("sku,stock\n")


# --------------------------------------------------------------- web & system
@pytest.mark.parametrize("url", [
    "http://api.open-meteo.com/v1/forecast",
    "https://evil.example.com/",
    "https://user:pw@api.github.com/",
    "https://169.254.169.254/latest/meta-data",
])
def test_http_allowlist(ex, url):
    assert assess("http_get", {"url": url, "rationale": ""}, ex).risk is Risk.BLOCKED


def test_allowlisted_url_is_safe(ex):
    assert assess("http_get", {"url": "https://api.github.com/repos/python/cpython", "rationale": ""}, ex).risk is Risk.SAFE


@pytest.mark.parametrize("cmd", ["rm -rf /", "ls; rm x", "cat ../../etc/passwd", "python script.py", "echo $HOME", "ls | wc"])
def test_command_allowlist(ex, cmd):
    assert assess("run_command", {"command": cmd, "rationale": ""}, ex).risk is Risk.BLOCKED


def test_allowed_command_is_safe(ex):
    assert assess("run_command", {"command": "wc -l notes/meeting-notes-november.md", "rationale": ""}, ex).risk is Risk.SAFE
