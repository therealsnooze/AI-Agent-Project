"""Exercise the real Anthropic SDK against a mock HTTP transport: checks the
exact request we send, history replay, and error/rate-limit handling."""

import json

import anthropic
import httpx2
import pytest

from agent import config, prompts, tools
from agent.audit import AuditLogger
from agent.core import Agent
from agent.llm import LLMClient, LLMError


def _message(content, stop_reason):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 80,
                      "cache_creation_input_tokens": 0}}


def _client_with(handler, monkeypatch, **kw) -> LLMClient:
    monkeypatch.setattr(config, "MIN_SECONDS_BETWEEN_REQUESTS", 0)
    llm = LLMClient(api_key="sk-ant-test", **kw)
    llm._client = anthropic.Anthropic(api_key="sk-ant-test", max_retries=1,
                                      http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    return llm


def test_request_shape_and_history_replay(monkeypatch, ex, tmp_path):
    sent = []
    replies = [
        _message([{"type": "thinking", "thinking": "List first.", "signature": "sig123"},
                  {"type": "tool_use", "id": "toolu_1", "name": "list_files",
                   "input": {"path": ".", "recursive": False, "rationale": "See what's there."}}], "tool_use"),
        _message([{"type": "text", "text": "Here are your files."}], "end_turn"),
    ]

    def handler(request):
        sent.append((dict(request.headers), json.loads(request.content)))
        return httpx2.Response(200, json=replies.pop(0))

    llm = _client_with(handler, monkeypatch)
    agent = Agent(llm, ex, AuditLogger(tmp_path / "a.jsonl", "t"))
    turn = agent.submit("What files do we have?")
    assert turn.final == "Here are your files."

    headers, body = sent[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "medium"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["fallbacks"] == "default" and config.FALLBACK_BETA in headers["anthropic-beta"]
    assert body["system"] == prompts.SYSTEM_PROMPT
    assert [t["name"] for t in body["tools"]] == [t["name"] for t in tools.API_TOOLS]
    assert all(t["strict"] for t in body["tools"]) and "temperature" not in body

    _, body2 = sent[1]
    assistant, tool_results = body2["messages"][1], body2["messages"][2]
    assert assistant["content"][0] == {"type": "thinking", "thinking": "List first.", "signature": "sig123"}
    assert assistant["content"][1]["type"] == "tool_use"
    assert tool_results["content"][0]["tool_use_id"] == "toolu_1"
    assert body2["system"] == body["system"] and body2["tools"] == body["tools"]  # stable prefix
    assert llm.usage.requests == 2 and llm.usage.cache_read_tokens == 160


def test_haiku_has_no_server_fallbacks(monkeypatch):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=_message([{"type": "text", "text": "hi"}], "end_turn"))

    llm = _client_with(handler, monkeypatch, model="claude-haiku-5-5")
    llm.create("sys", [], [{"role": "user", "content": "hi"}])
    assert "fallbacks" not in sent[0]


def test_fallback_beta_rejection_retries_without_it(monkeypatch):
    sent, notices = [], []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        if "fallbacks" in body:
            return httpx2.Response(400, json={"type": "error", "error": {
                "type": "invalid_request_error", "message": "fallbacks: not enabled for this organization"}})
        return httpx2.Response(200, json=_message([{"type": "text", "text": "ok"}], "end_turn"))

    llm = _client_with(handler, monkeypatch, on_notice=notices.append)
    assert llm.create("sys", [], [{"role": "user", "content": "hi"}]).content[0].text == "ok"
    assert len(sent) == 2 and "fallbacks" not in sent[1] and notices


@pytest.mark.parametrize("status,expected", [
    (401, "Invalid or missing Anthropic API key"),
    (404, "is not available"),
    (429, "Rate limit reached"),
    (529, "temporarily unavailable"),
])
def test_api_errors_become_friendly_llm_errors(monkeypatch, status, expected):
    def handler(request):
        return httpx2.Response(status, headers={"retry-after": "0"},
                               json={"type": "error", "error": {"type": "x", "message": "boom"}})

    llm = _client_with(handler, monkeypatch, on_notice=lambda m: None)
    with pytest.raises(LLMError, match=expected):
        llm.create("sys", [], [{"role": "user", "content": "hi"}])


def test_session_request_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_REQUESTS_PER_SESSION", 0)
    with pytest.raises(LLMError, match="limit"):
        LLMClient(api_key="x").create("s", [], [])
