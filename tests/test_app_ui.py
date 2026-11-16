"""Headless UI test: drives app.py with Streamlit's AppTest and a scripted LLM."""

from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from streamlit.testing.v1 import AppTest

from agent import config
from agent.llm import LLMClient

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def _resp(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop, model="fake",
              usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))


@pytest.fixture
def scripted(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WORKSPACES_DIR", tmp_path / "ws")
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.delenv("OPSPILOT_PASSCODE", raising=False)
    script = []
    monkeypatch.setattr(LLMClient, "create", lambda self, system, tools, messages: script.pop(0))
    return script


def test_gate_without_key(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WORKSPACES_DIR", tmp_path / "ws")
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert any("Paste an Anthropic API key" in i.value for i in at.info)


def test_full_flow_with_approval(scripted):
    scripted.extend([
        _resp(NS(type="thinking", thinking="The user wants old logs removed."),
              NS(type="tool_use", id="t1", name="update_plan",
                 input={"steps": [{"title": "Delete August log", "status": "in_progress"}]}),
              NS(type="tool_use", id="t2", name="delete_file",
                 input={"path": "logs/app-2026-08-30.log", "rationale": "It is an old log file."})),
        _resp(NS(type="text", text="Deleted `logs/app-2026-08-30.log`."), stop="end_turn"),
    ])
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    at.chat_input[0].set_value("Delete the August log").run()
    assert not at.exception
    assert any("approval is needed" in m.value for m in at.markdown)
    assert at.chat_input[0].disabled

    at.button(key="approve_t2").click().run()
    assert not at.exception
    assert any("Deleted `logs/app-2026-08-30.log`." in m.value for m in at.markdown)
    assert not at.chat_input[0].disabled

    workspace = next((config.WORKSPACES_DIR).iterdir())
    assert not (workspace / "logs/app-2026-08-30.log").exists()
