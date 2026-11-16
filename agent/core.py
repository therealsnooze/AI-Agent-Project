"""The agent: an interpreter/executor loop around the LLM.

    user request ──► LLM ──► interpret response ──► for each proposed action:
                      ▲          (tool_use blocks)     1. validate arguments (schema)
                      │                                2. assess risk (policy, in code)
                      │                                3. pause for human approval if needed
                      │                                4. execute in the sandbox
                      └──────── tool results ◄──────── 5. audit-log the outcome

The loop is a resumable state machine rather than a blocking function, so a
GUI can pause on an approval, re-render, and continue when the user decides.
Conversation history is append-only (required for preserved thinking and
good prompt-cache hit rates).
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config, prompts, tools
from .audit import AuditLogger
from .executors import ActionError, Executors
from .llm import LLMClient, LLMError
from .policy import Assessment, Risk, assess, needs_approval
from .validation import validate

Event = Callable[[str, dict], None]


@dataclass
class ActionRecord:
    id: str
    tool: str
    input: dict
    domain: str
    status: str = "pending"  # pending|awaiting_approval|ready|success|error|blocked|denied
    risk: str | None = None
    reasons: list[str] = field(default_factory=list)
    preview: str | None = None
    preview_language: str = "text"
    decision: str | None = None  # auto|approved|denied
    note: str = ""
    summary: str = ""
    result_text: str = ""
    display: dict | None = None
    duration_ms: int | None = None

    @property
    def rationale(self) -> str:
        return self.input.get("rationale", "")

    @property
    def is_error(self) -> bool:
        return self.status in ("error", "blocked", "denied")

    def audit_fields(self) -> dict:
        return {"action_id": self.id, "tool": self.tool, "domain": self.domain, "input": self.input,
                "risk": self.risk, "status": self.status, "decision": self.decision, "summary": self.summary}


@dataclass
class Turn:
    user_text: str
    items: list[dict] = field(default_factory=list)  # thinking / text / action / notice, in order
    plan: list[dict] | None = None
    final: str | None = None
    status: str = "running"  # running|awaiting_approval|done|error


class Agent:
    def __init__(self, llm: LLMClient, executors: Executors, audit: AuditLogger,
                 approval_mode: str = "standard", on_event: Event | None = None):
        self.llm = llm
        self.ex = executors
        self.audit = audit
        self.approval_mode = approval_mode
        self.on_event = on_event
        self.messages: list[dict] = []
        self.turns: list[Turn] = []
        self.pending: ActionRecord | None = None
        self._queue: list[ActionRecord] = []
        self._results: list[dict] = []
        self._steps = 0
        llm.on_notice = lambda msg: self._notice("warning", msg)

    # ------------------------------------------------------------------ public
    @property
    def awaiting_approval(self) -> bool:
        return self.pending is not None

    def submit(self, text: str) -> Turn:
        if self.pending:
            raise RuntimeError("Resolve the pending approval first.")
        turn = Turn(user_text=text)
        self.turns.append(turn)
        self.audit.log("user_request", text=text, model=self.llm.model, effort=self.llm.effort,
                       approval_mode=self.approval_mode)
        self.messages.append({"role": "user", "content": prompts.user_turn(text, self.approval_mode)})
        self._steps = 0
        self._run()
        return turn

    def resolve_approval(self, approved: bool, note: str = "") -> None:
        action = self.pending
        if action is None:
            return
        self.pending = None
        action.note = note.strip()
        if approved:
            action.status, action.decision = "ready", "approved"
        else:
            action.status, action.decision = "denied", "denied"
            action.summary = "Declined by user" + (f": {action.note}" if action.note else "")
            action.result_text = (
                "The user declined this action"
                + (f" with the note: \"{action.note}\"" if action.note else "")
                + ". Do not retry it or work around it; acknowledge and offer alternatives."
            )
        self.audit.log("approval_decision", approved=approved, note=action.note, **action.audit_fields())
        self._emit("decision", action=action)
        self._run()

    # ------------------------------------------------------------------ loop
    def _turn(self) -> Turn:
        return self.turns[-1]

    def _run(self) -> None:
        turn = self._turn()
        turn.status = "running"
        while True:
            if not self._drain_queue():
                turn.status = "awaiting_approval"
                return
            if self._results:
                self.messages.append({"role": "user", "content": self._results})
                self._results = []
            if self._steps >= config.MAX_AGENT_STEPS:
                self._finish(turn, "error", f"Stopped after {config.MAX_AGENT_STEPS} steps to avoid a runaway loop. "
                                             "Ask me to continue if needed.")
                return
            self._steps += 1
            self._emit("thinking_start", step=self._steps)
            try:
                response = self.llm.create(prompts.SYSTEM_PROMPT, tools.API_TOOLS, self.messages)
            except LLMError as e:
                self.audit.log("llm_error", message=e.user_message, detail=e.detail, retryable=e.retryable)
                self._finish(turn, "error", e.user_message)
                return
            self.audit.log("llm_call", model=getattr(response, "model", self.llm.model),
                           stop_reason=response.stop_reason, request_id=getattr(response, "_request_id", None),
                           latency_ms=self.llm.last_latency_ms,
                           input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens,
                           cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0))

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                why = getattr(details, "explanation", None) or "The model declined this request."
                self._finish(turn, "error", f"Request declined by the model's safety system: {why}")
                return

            if response.content:
                self.messages.append({"role": "assistant", "content": response.content})
            actions, text = self._interpret(response, turn)

            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason == "max_tokens":
                self._notice("warning", "The model's response hit the output limit and may be incomplete.")
                for a in actions:  # a truncated tool call must not run
                    a.status, a.summary = "error", "Truncated tool call"
                    a.result_text = "Your response was cut off at max_tokens, so this call was not run. Retry with smaller steps."
            if not actions:
                self._finish(turn, "done", None, final=text or "(The model returned no text.)")
                return
            self._queue = actions

    def _drain_queue(self) -> bool:
        """Process proposed actions in order. Returns False when paused for approval."""
        while self._queue:
            action = self._queue[0]
            if action.status == "pending":
                self._prepare(action)
                if action.status == "awaiting_approval":
                    self.pending = action
                    self.audit.log("approval_requested", reasons=action.reasons, **action.audit_fields())
                    self._emit("approval", action=action)
                    return False
            if action.status == "ready":
                self._execute(action)
            self._queue.pop(0)
            self._results.append(self._tool_result(action))
            if action.tool != "update_plan":
                self.audit.log("action_" + action.status, duration_ms=action.duration_ms, reasons=action.reasons,
                               result=action.result_text[:500], **action.audit_fields())
            self._emit("action_done", action=action)
        return True

    # ------------------------------------------------------------- interpreter
    def _interpret(self, response: Any, turn: Turn) -> tuple[list[ActionRecord], str]:
        """Turn the model's content blocks into display items and ActionRecords."""
        actions: list[ActionRecord] = []
        texts: list[str] = []
        for block in response.content:
            kind = getattr(block, "type", None)
            if kind == "thinking" and getattr(block, "thinking", "").strip():
                turn.items.append({"type": "thinking", "text": block.thinking.strip()})
                self._emit("thinking", text=block.thinking.strip())
            elif kind == "text" and block.text.strip():
                texts.append(block.text.strip())
            elif kind == "fallback":
                frm = getattr(getattr(block, "from_", None), "model", "?")
                to = getattr(getattr(block, "to", None), "model", "?")
                self._notice("info", f"{frm} declined part of this request; {to} continued it.")
            elif kind == "tool_use":
                action = ActionRecord(id=block.id, tool=block.name, input=dict(block.input or {}),
                                      domain=tools.TOOL_DOMAINS.get(block.name, "unknown"))
                actions.append(action)
                if block.name != "update_plan":
                    turn.items.append({"type": "action", "action": action})
                    self.audit.log("action_proposed", rationale=action.rationale, **action.audit_fields())
        text = "\n\n".join(texts)
        if actions and text:  # narration between tool calls
            turn.items.insert(len(turn.items) - sum(1 for a in actions if a.tool != "update_plan"),
                              {"type": "text", "text": text})
            self._emit("text", text=text)
        return actions, text

    def _prepare(self, action: ActionRecord) -> None:
        """Validate, then assess risk. Leaves the action ready, blocked, errored or awaiting approval."""
        schema = tools.TOOL_SCHEMAS.get(action.tool)
        if schema is None:
            action.status, action.summary = "blocked", "Unknown tool"
            action.result_text = f"Unknown tool '{action.tool}'. Use only the tools provided."
            return
        errors = validate(schema, action.input)
        if errors:
            action.status, action.summary = "error", "Invalid arguments"
            action.result_text = "Invalid arguments: " + "; ".join(errors)
            return
        if action.tool == "update_plan":
            self._turn().plan = action.input["steps"]
            action.status, action.summary, action.result_text = "success", "Plan updated", "Plan updated."
            self._emit("plan", steps=action.input["steps"])
            return
        try:
            assessment: Assessment = assess(action.tool, action.input, self.ex)
        except ActionError as e:
            action.status, action.summary, action.result_text = "error", str(e), f"Error: {e}"
            return
        action.risk = assessment.risk.value
        action.reasons = assessment.reasons
        action.preview, action.preview_language = assessment.preview, assessment.preview_language
        if assessment.risk is Risk.BLOCKED:
            action.status, action.summary = "blocked", assessment.reasons[0]
            action.result_text = "Blocked by the safety policy: " + " ".join(assessment.reasons)
        elif needs_approval(assessment, self.approval_mode):
            action.status = "awaiting_approval"
        else:
            action.status, action.decision = "ready", "auto"

    def _execute(self, action: ActionRecord) -> None:
        self._emit("action_start", action=action)
        args = {k: v for k, v in action.input.items() if k != "rationale"}
        started = time.monotonic()
        try:
            result = self.ex.execute(action.tool, args)
            action.status = "success" if result.ok else "error"
            action.summary, action.display = result.summary, result.display
            action.result_text = result.content[: config.MAX_TOOL_RESULT_CHARS]
        except ActionError as e:
            action.status, action.summary, action.result_text = "error", str(e), f"Error: {e}"
        except Exception as e:  # never let an executor bug crash the agent
            self.audit.log("internal_error", tool=action.tool, traceback=traceback.format_exc())
            action.status, action.summary = "error", f"Internal error: {type(e).__name__}"
            action.result_text = f"Internal error while running {action.tool}: {type(e).__name__}: {e}"
        action.duration_ms = int((time.monotonic() - started) * 1000)

    @staticmethod
    def _tool_result(action: ActionRecord) -> dict:
        return {"type": "tool_result", "tool_use_id": action.id,
                "content": action.result_text or "(no output)", "is_error": action.is_error}

    # ----------------------------------------------------------------- helpers
    def _finish(self, turn: Turn, status: str, notice: str | None, final: str | None = None) -> None:
        if notice:
            self._notice("error" if status == "error" else "info", notice)
        turn.final = final
        turn.status = status
        self.audit.log("turn_complete", status=status, final=(final or notice or "")[:1000],
                       cost_usd=round(self.llm.usage.cost_usd, 4))
        self._emit("done", status=status)

    def _notice(self, level: str, text: str) -> None:
        if self.turns:
            self._turn().items.append({"type": "notice", "level": level, "text": text})
        self._emit("notice", level=level, text=text)

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.on_event:
            self.on_event(kind, payload)
