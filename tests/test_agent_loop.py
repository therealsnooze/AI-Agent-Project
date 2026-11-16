"""Agent loop tests with a scripted fake LLM (no network, no API key)."""

from types import SimpleNamespace as NS

from agent.audit import AuditLogger
from agent.core import Agent
from agent.llm import LLMError, Usage


def text(t):
    return NS(type="text", text=t)


def tool(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def response(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop, model="fake",
              usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.model, self.effort, self.usage, self.last_latency_ms, self.on_notice = "fake", "low", Usage(), 0, None

    def create(self, system, tools, messages):
        self.calls.append([dict(m) for m in messages])
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_agent(ex, tmp_path, script, mode="standard"):
    return Agent(FakeLLM(script), ex, AuditLogger(tmp_path / "audit.jsonl", "test"), approval_mode=mode)


def last_tool_results(llm):
    return llm.calls[-1][-1]["content"]


def test_safe_action_runs_without_approval(ex, tmp_path):
    agent = make_agent(ex, tmp_path, [
        response(tool("t1", "update_plan", steps=[{"title": "Count", "status": "in_progress"}]),
                 tool("t2", "run_sql", query="SELECT COUNT(*) AS n FROM products", rationale="count")),
        response(text("There are 15 products."), stop="end_turn"),
    ])
    turn = agent.submit("How many products?")
    assert turn.status == "done" and turn.final == "There are 15 products."
    assert turn.plan[0]["title"] == "Count"
    results = last_tool_results(agent.llm)
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert '"rows": [[15]]' in results[1]["content"] and results[1]["is_error"] is False


def test_destructive_action_pauses_then_runs_when_approved(ex, tmp_path):
    agent = make_agent(ex, tmp_path, [
        response(tool("t1", "delete_file", path="logs/app-2026-08-30.log", rationale="cleanup")),
        response(text("Deleted."), stop="end_turn"),
    ])
    turn = agent.submit("delete the august log")
    assert turn.status == "awaiting_approval" and agent.pending.tool == "delete_file"
    assert (ex.root / "logs/app-2026-08-30.log").exists()  # nothing ran yet
    agent.resolve_approval(True)
    assert turn.status == "done"
    assert not (ex.root / "logs/app-2026-08-30.log").exists()


def test_denied_action_is_reported_to_model_and_not_run(ex, tmp_path):
    agent = make_agent(ex, tmp_path, [
        response(tool("t1", "run_sql", query="DELETE FROM order_items", rationale="x")),
        response(text("Okay, I won't."), stop="end_turn"),
    ])
    agent.submit("wipe orders")
    agent.resolve_approval(False, "too risky")
    result = last_tool_results(agent.llm)[0]
    assert result["is_error"] is True and "declined" in result["content"] and "too risky" in result["content"]
    assert ex.execute("run_sql", {"query": "SELECT COUNT(*) FROM order_items"}).display["rows"][0][0] > 0


def test_blocked_and_invalid_actions_return_errors(ex, tmp_path):
    agent = make_agent(ex, tmp_path, [
        response(tool("t1", "read_file", path="../../etc/passwd", rationale="x"),
                 tool("t2", "write_file", path="a.txt", rationale="x"),  # missing fields
                 tool("t3", "format_disk", rationale="x")),
        response(text("Could not do that."), stop="end_turn"),
    ])
    agent.submit("do bad things")
    results = last_tool_results(agent.llm)
    assert all(r["is_error"] for r in results)
    assert "safety policy" in results[0]["content"]
    assert "Invalid arguments" in results[1]["content"]
    assert "Unknown tool" in results[2]["content"]


def test_strict_mode_requires_approval_for_creates(ex, tmp_path):
    agent = make_agent(ex, tmp_path, [
        response(tool("t1", "write_file", path="reports/r.md", content="hi", mode="create", rationale="x")),
        response(text("done"), stop="end_turn"),
    ], mode="strict")
    agent.submit("write a report")
    assert agent.awaiting_approval


def test_llm_error_is_surfaced_gracefully(ex, tmp_path):
    agent = make_agent(ex, tmp_path, [LLMError("Rate limit reached.", retryable=True)])
    turn = agent.submit("hi")
    assert turn.status == "error"
    assert any(i["type"] == "notice" and "Rate limit" in i["text"] for i in turn.items)


def test_audit_log_records_the_session(ex, tmp_path):
    agent = make_agent(ex, tmp_path, [
        response(tool("t1", "list_files", path=".", recursive=False, rationale="look")),
        response(text("ok"), stop="end_turn"),
    ])
    agent.submit("list files")
    events = [r["event"] for r in agent.audit.read()]
    assert events[0] == "user_request"
    assert "action_proposed" in events and "action_success" in events and events[-1] == "turn_complete"
