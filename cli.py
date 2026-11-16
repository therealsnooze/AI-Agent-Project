"""OpsPilot terminal interface.

    python cli.py                      # standard approval mode, default model
    python cli.py --strict --effort high
"""

from __future__ import annotations

import argparse
import os
import textwrap
import uuid

from agent import config, tools
from agent.audit import AuditLogger
from agent.core import Agent
from agent.executors import Executors
from agent.llm import LLMClient
from agent.workspace import create_workspace

os.system("")  # enable ANSI colours on Windows terminals
C = {"dim": "\033[2m", "bold": "\033[1m", "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
     "cyan": "\033[36m", "reset": "\033[0m"}
ICON = {"success": "✔", "error": "✘", "blocked": "⛔", "denied": "🚫"}


def color(text: str, *styles: str) -> str:
    return "".join(C[s] for s in styles) + text + C["reset"]


def on_event(kind: str, p: dict) -> None:
    if kind == "thinking":
        print(color("  💭 " + textwrap.shorten(p["text"], 160), "dim"))
    elif kind == "text":
        print(color("  " + p["text"], "cyan"))
    elif kind == "plan":
        print(color("  🧭 Plan: ", "bold") + " → ".join(f"[{s['status']}] {s['title']}" for s in p["steps"]))
    elif kind == "action_done" and p["action"].tool != "update_plan":
        a = p["action"]
        style = "green" if a.status == "success" else "red"
        print(color(f"  {ICON.get(a.status, '•')} {tools.DOMAIN_ICONS.get(a.domain, '')} {a.tool}", style)
              + color(f" — {a.summary}", "dim"))
    elif kind == "notice":
        print(color(f"  ⚠ {p['text']}", "yellow"))


def ask_approval(agent: Agent) -> None:
    a = agent.pending
    print(color(f"\n  ⚠ APPROVAL NEEDED [{a.risk.upper()}] {a.tool}", "yellow", "bold"))
    if a.rationale:
        print(f"    Why: {a.rationale}")
    for r in a.reasons:
        print(f"    - {r}")
    args = {k: v for k, v in a.input.items() if k not in ("rationale", "content")}
    print(color(f"    Input: {args}", "dim"))
    if a.preview:
        print(textwrap.indent(a.preview[:2500], "    | "))
    answer = input(color("  Approve? [y/N] (or type a note to deny with a reason): ", "bold")).strip()
    approved = answer.lower() in ("y", "yes")
    agent.resolve_approval(approved, "" if approved or answer.lower() in ("", "n", "no") else answer)


def main() -> None:
    parser = argparse.ArgumentParser(description="OpsPilot terminal agent")
    parser.add_argument("--model", default=config.DEFAULT_MODEL, choices=list(config.MODELS))
    parser.add_argument("--effort", default=config.DEFAULT_EFFORT, choices=config.EFFORT_LEVELS)
    parser.add_argument("--strict", action="store_true", help="require approval for every change")
    args = parser.parse_args()

    session_id = "cli-" + uuid.uuid4().hex[:8]
    root = create_workspace(session_id)
    agent = Agent(LLMClient(model=args.model, effort=args.effort), Executors(root),
                  AuditLogger(config.AUDIT_LOG_PATH, session_id),
                  approval_mode="strict" if args.strict else "standard", on_event=on_event)

    print(color("☕ OpsPilot", "bold") + color(f"  ({config.MODELS[args.model]['label']}, effort {args.effort})", "dim"))
    print(color(f"Workspace: {root}\nType a request, or 'exit'. Audit log: {config.AUDIT_LOG_PATH}\n", "dim"))
    while True:
        try:
            text = input(color("you › ", "bold")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in ("exit", "quit"):
            break
        if not text:
            continue
        turn = agent.submit(text)
        while agent.awaiting_approval:
            ask_approval(agent)
        if turn.final:
            print("\n" + color("opspilot ›", "bold", "green") + "\n" + turn.final + "\n")
        u = agent.llm.usage
        print(color(f"[{u.requests} calls · {u.input_tokens + u.cache_read_tokens:,} in / {u.output_tokens:,} out tokens"
                    f" · ~${u.cost_usd:.3f}]\n", "dim"))


if __name__ == "__main__":
    main()
