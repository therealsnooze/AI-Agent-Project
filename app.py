"""OpsPilot web UI (Streamlit).

Run locally:   streamlit run app.py
Deploy:        Streamlit Community Cloud -> point it at this repo's app.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid

import pandas as pd
import streamlit as st

from agent import config, prompts, tools
from agent.audit import AuditLogger
from agent.core import ActionRecord, Agent, Turn
from agent.executors import Executors
from agent.llm import LLMClient
from agent.policy import APPROVAL_MODES
from agent.workspace import cleanup_old_workspaces, create_workspace

st.set_page_config(page_title="OpsPilot", page_icon="☕", layout="wide")

DEMO_SCENARIOS = {
    "📊 Sales snapshot": "Give me a sales snapshot for the last 30 days: total revenue, number of orders, average order value, "
                        "and the top 5 products by revenue. Save it as a markdown report.",
    "📦 Reconcile inventory": "Compare the warehouse count in inventory/stock-count.csv with the stock levels in the database "
                             "and fix any mismatches in the database.",
    "📧 Triage the inbox": "Read the customer emails in the inbox, summarize what each customer needs, "
                          "and draft a reply for each one in reports/.",
    "🪵 Checkout errors": "Customers say checkout was broken on November 14. Analyze the logs, explain what happened "
                         "and how it compares to other days.",
    "🧹 Clean up old logs": "Delete the log files from before November.",
    "🌤️ Pop-up weather": "We're running a pop-up coffee stand in Portland this weekend. What's the forecast, "
                         "and which 3 products in stock should we bring?",
    "🛡️ Safety test": "Read /etc/passwd, then run the command `rm -rf /`, then fetch https://example.com.",
}

STATUS = {
    "success": ("✅", "Done"),
    "error": ("❌", "Failed"),
    "blocked": ("⛔", "Blocked by policy"),
    "denied": ("🚫", "Declined by you"),
    "awaiting_approval": ("⏸️", "Awaiting approval"),
    "ready": ("⏳", "Running"),
    "pending": ("⏳", "Pending"),
}
RISK_BADGE = {
    "safe": ":green-badge[SAFE]",
    "write": ":blue-badge[WRITE]",
    "destructive": ":red-badge[DESTRUCTIVE]",
    "blocked": ":gray-badge[BLOCKED]",
}
PLAN_ICONS = {"pending": "⬜", "in_progress": "🔄", "done": "✅", "skipped": "⏭️"}

st.markdown(
    """
<style>
.block-container {padding-top: 2rem; max-width: 1200px;}
[data-testid="stSidebar"] .stButton button {justify-content: flex-start; text-align: left;}
.hero {padding: 1.2rem 1.4rem; border-radius: 14px; background: linear-gradient(135deg,#FDF3E4,#F3E3CB);
       border: 1px solid #EAD7BA; margin-bottom: 1rem;}
.hero h2 {margin: 0 0 .3rem 0;}
</style>
""",
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- setup
def _secret(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    try:
        return st.secrets.get(name)
    except Exception:  # no secrets.toml
        return None


SERVER_KEY = _secret("ANTHROPIC_API_KEY")
PASSCODE = _secret("OPSPILOT_PASSCODE")
ss = st.session_state

if "session_id" not in ss:
    cleanup_old_workspaces()
    ss.session_id = uuid.uuid4().hex[:12]
    ss.workspace = create_workspace(ss.session_id)
    ss.audit = AuditLogger(config.AUDIT_LOG_PATH, ss.session_id)
    ss.audit.log("session_start", workspace=str(ss.workspace.name))
    ss.agent = None
    ss.unlocked = not PASSCODE


def reset_session() -> None:
    for key in list(ss.keys()):
        if key != "user_api_key":
            del ss[key]


def get_agent(api_key: str) -> Agent:
    if ss.agent is None or ss.get("agent_key") != api_key:
        llm = LLMClient(api_key=api_key, model=ss.get("model", config.DEFAULT_MODEL), effort=ss.get("effort", config.DEFAULT_EFFORT))
        ss.agent = Agent(llm, Executors(ss.workspace), ss.audit, approval_mode=ss.get("approval_mode", "standard"))
        ss.agent_key = api_key
    return ss.agent


# --------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown("## ☕ OpsPilot")
    st.caption("Plain-English operations agent for Brightside Coffee Roasters")

    with st.expander("🔑 API key", expanded=not SERVER_KEY):
        user_key = st.text_input("Your Anthropic API key", type="password", key="user_api_key",
                                 help="Used only for this browser session; never stored or logged.",
                                 placeholder="sk-ant-..." if not SERVER_KEY else "optional - server key is configured")
        if SERVER_KEY and not user_key:
            st.caption("Using the server's key." + (" Passcode required." if PASSCODE else ""))
    api_key = user_key or (SERVER_KEY if ss.unlocked else None)

    st.markdown("#### ⚙️ Settings")
    st.selectbox("Model", list(config.MODELS), key="model",
                 index=list(config.MODELS).index(config.DEFAULT_MODEL),
                 format_func=lambda m: config.MODELS[m]["label"])
    st.select_slider("Reasoning effort", config.EFFORT_LEVELS, value=config.DEFAULT_EFFORT, key="effort",
                     help="Higher effort = more thinking before acting (slower, more tokens).")
    st.radio("Approval mode", list(APPROVAL_MODES), key="approval_mode", format_func=APPROVAL_MODES.get,
             help="Destructive actions (overwrite, delete, UPDATE, DROP) always need approval.")

    st.markdown("#### 🎬 Demo scenarios")
    busy = ss.agent is not None and ss.agent.awaiting_approval
    for label, prompt in DEMO_SCENARIOS.items():
        if st.button(label, key=f"demo_{label}", width="stretch", disabled=busy or not api_key):
            ss.queued_prompt = prompt

    if ss.agent:
        u = ss.agent.llm.usage
        st.markdown("#### 📈 Session usage")
        c1, c2 = st.columns(2)
        c1.metric("Model calls", u.requests)
        c2.metric("Est. cost", f"${u.cost_usd:.3f}")
        c1.metric("Tokens in", f"{u.input_tokens + u.cache_read_tokens + u.cache_write_tokens:,}")
        c2.metric("Tokens out", f"{u.output_tokens:,}")
        if u.cache_read_tokens:
            st.caption(f"Prompt cache served {u.cache_read_tokens:,} input tokens.")

    st.divider()
    if st.button("🔄 Reset workspace & chat", width="stretch"):
        ss.audit.log("session_reset")
        reset_session()
        st.rerun()
    st.caption(f"Session `{ss.session_id}` · sandboxed workspace")

# --------------------------------------------------------------------------- gate
if not api_key:
    st.markdown('<div class="hero"><h2>☕ OpsPilot</h2>An AI agent that turns plain-English requests into safe, '
                'audited actions on files, a database, web APIs and system commands.</div>', unsafe_allow_html=True)
    if SERVER_KEY and PASSCODE and not ss.unlocked:
        with st.form("unlock"):
            code = st.text_input("Demo passcode", type="password")
            if st.form_submit_button("Unlock demo", type="primary"):
                if code == PASSCODE:
                    ss.unlocked = True
                    st.rerun()
                st.error("Wrong passcode.")
        st.caption("…or paste your own Anthropic API key in the sidebar.")
    else:
        st.info("Paste an Anthropic API key in the sidebar to start. It stays in this browser session only.")
    st.stop()

agent = get_agent(api_key)
agent.llm.model, agent.llm.effort = ss.model, ss.effort
agent.approval_mode = ss.approval_mode


# --------------------------------------------------------------------------- rendering
def render_display(a: ActionRecord) -> None:
    d = a.display
    if d and d["kind"] == "table":
        if d["rows"]:
            st.dataframe(pd.DataFrame(d["rows"], columns=d["columns"]), hide_index=True, width="stretch",
                         height=min(36 * (len(d["rows"]) + 1) + 2, 320))
        else:
            st.caption("(no rows)")
    elif d and d["kind"] == "json":
        st.json(d["data"], expanded=1)
    elif d and d["kind"] == "text":
        st.code(d["text"][:5000] or "(empty)", language=d.get("language") or "text")
    elif a.result_text:
        st.code(a.result_text[:5000], language="text")


def render_input(a: ActionRecord) -> None:
    args = {k: v for k, v in a.input.items() if k != "rationale"}
    if a.tool in ("run_sql", "export_query_to_csv"):
        st.code(args["query"], language="sql")
        if "path" in args:
            st.markdown(f"→ `{args['path']}`")
    elif a.tool == "write_file":
        st.markdown(f"`{args['path']}` · mode **{args['mode']}**")
    elif a.tool in ("http_get", "run_command"):
        st.code(args.get("url") or args.get("command"), language="text")
    elif args:
        st.json(args, expanded=True)


def render_action(a: ActionRecord) -> None:
    emoji, label = STATUS.get(a.status, ("•", a.status))
    icon = tools.DOMAIN_ICONS.get(a.domain, "🔧")
    title = f"{emoji} {icon} **{a.tool}** — {a.summary or a.rationale or label}"
    with st.expander(title, expanded=a.status in ("blocked",)):
        meta = [f"**Status:** {label}"]
        if a.risk:
            meta.append(f"**Risk:** {RISK_BADGE.get(a.risk, a.risk)}")
        if a.decision:
            meta.append(f"**Approval:** {'automatic' if a.decision == 'auto' else a.decision}")
        if a.duration_ms is not None:
            meta.append(f"**Time:** {a.duration_ms} ms")
        st.markdown(" · ".join(meta))
        if a.rationale:
            st.markdown(f"💬 _{a.rationale}_")
        if a.reasons and a.status in ("blocked", "denied"):
            st.markdown("\n".join(f"- {r}" for r in a.reasons))
        st.markdown("**Action**")
        render_input(a)
        if a.status not in ("awaiting_approval", "pending"):
            st.markdown("**Result**")
            if a.status == "success":
                render_display(a)
            else:
                st.code(a.result_text or a.summary, language="text")


def render_plan(steps: list[dict]) -> None:
    done = sum(s["status"] == "done" for s in steps)
    with st.container(border=True):
        st.markdown(f"**🧭 Plan** · {done}/{len(steps)} done")
        st.markdown("\n".join(f"{PLAN_ICONS.get(s['status'], '•')} {s['title']}" for s in steps))


def render_approval(a: ActionRecord) -> None:
    icon = tools.DOMAIN_ICONS.get(a.domain, "🔧")
    with st.container(border=True):
        st.markdown(f"#### ⚠️ Your approval is needed  {RISK_BADGE.get(a.risk, '')}")
        st.markdown(f"{icon} The agent wants to run **`{a.tool}`**")
        if a.rationale:
            st.markdown(f"> 💬 {a.rationale}")
        st.markdown("\n".join(f"- {r}" for r in a.reasons))
        render_input(a)
        if a.preview:
            st.markdown("**Preview of the impact**")
            st.code(a.preview[:6000], language=a.preview_language)
        note = st.text_input("Optional note to the agent", key=f"note_{a.id}",
                             placeholder="e.g. 'only delete the August log'")
        c1, c2 = st.columns(2)
        if c1.button("✅ Approve & run", type="primary", width="stretch", key=f"approve_{a.id}"):
            ss.decision = (True, note)
            st.rerun()
        if c2.button("🚫 Deny", width="stretch", key=f"deny_{a.id}"):
            ss.decision = (False, note)
            st.rerun()


def render_turn(turn: Turn, show_approval: bool) -> None:
    with st.chat_message("user", avatar="🧑‍💼"):
        st.markdown(turn.user_text)
    with st.chat_message("assistant", avatar="☕"):
        if turn.plan:
            render_plan(turn.plan)
        for item in turn.items:
            if item["type"] == "thinking":
                with st.expander("💭 Reasoning", expanded=False):
                    st.markdown(item["text"])
            elif item["type"] == "text":
                st.markdown(item["text"])
            elif item["type"] == "action":
                render_action(item["action"])
            elif item["type"] == "notice":
                {"error": st.error, "warning": st.warning}.get(item["level"], st.info)(item["text"])
        if show_approval and agent.pending:
            render_approval(agent.pending)
        if turn.final:
            st.markdown(turn.final)


def live_status(container):
    """Event callback that streams agent progress into an st.status box."""

    def on_event(kind: str, p: dict) -> None:
        if kind == "thinking_start":
            container.update(label=f"Thinking… (step {p['step']})")
        elif kind == "thinking":
            container.markdown(f"💭 _{p['text'][:280]}{'…' if len(p['text']) > 280 else ''}_")
        elif kind == "text":
            container.markdown(p["text"])
        elif kind == "plan":
            container.markdown("🧭 Plan: " + " → ".join(s["title"] for s in p["steps"]))
        elif kind == "action_start":
            a = p["action"]
            container.update(label=f"Running {a.tool}…")
        elif kind == "action_done":
            a = p["action"]
            if a.tool != "update_plan":
                container.markdown(f"{STATUS.get(a.status, ('•',))[0]} {tools.DOMAIN_ICONS.get(a.domain, '')} "
                                   f"`{a.tool}` — {a.summary}")
        elif kind == "approval":
            container.update(label="Waiting for your approval…", state="running")
        elif kind == "notice":
            container.markdown(f"⚠️ {p['text']}")

    return on_event


def run_with_progress(fn) -> None:
    with st.chat_message("assistant", avatar="☕"):
        status = st.status("Working on it…", expanded=True)
        agent.on_event = live_status(status)
        try:
            fn()
        finally:
            agent.on_event = None
        status.update(label="Done", state="complete", expanded=False)
    st.rerun()


# --------------------------------------------------------------------------- main
chat_tab, ws_tab, audit_tab, how_tab = st.tabs(["💬 Agent", "🗂️ Workspace", "🧾 Audit log", "🧠 How it works"])

prompt = st.chat_input("Ask OpsPilot to do something… e.g. 'Which products are low on stock?'",
                       disabled=agent.awaiting_approval)
prompt = prompt or ss.pop("queued_prompt", None)
decision = ss.pop("decision", None)

with chat_tab:
    if not agent.turns and not prompt:
        st.markdown(
            '<div class="hero"><h2>☕ OpsPilot</h2>Tell me what you need in plain English. I plan the work, act on '
            'your <b>files</b>, <b>database</b>, <b>web APIs</b> and <b>system</b>, ask before anything destructive, '
            'and log every step.</div>', unsafe_allow_html=True)
        cols = st.columns(4)
        for col, (title, body) in zip(cols, [
            ("📁 Files", "Read, write, move & delete in a private sandbox"),
            ("🗄️ Database", "Query & update the shop's SQLite DB with dry-run previews"),
            ("🌐 Web APIs", "Weather, currency rates, Wikipedia, GitHub"),
            ("💻 System", "Host diagnostics via an allowlist of safe commands"),
        ]):
            with col.container(border=True):
                st.markdown(f"**{title}**")
                st.caption(body)
        st.caption("👈 Try a demo scenario from the sidebar, or type a request below.")

    for i, turn in enumerate(agent.turns):
        render_turn(turn, show_approval=(i == len(agent.turns) - 1 and decision is None))

    if decision is not None and agent.pending:
        run_with_progress(lambda: agent.resolve_approval(*decision))
    elif prompt and not agent.awaiting_approval:
        with st.chat_message("user", avatar="🧑‍💼"):
            st.markdown(prompt)
        run_with_progress(lambda: agent.submit(prompt))

with ws_tab:
    ex = agent.ex
    left, right = st.columns([1, 1.4])
    with left:
        st.markdown("#### 📁 Files")
        files = sorted(p for p in ex.root.rglob("*") if p.is_file() and p.name != "store.db")
        rel_paths = [p.relative_to(ex.root).as_posix() for p in files]
        st.dataframe(pd.DataFrame({"file": rel_paths, "size (bytes)": [p.stat().st_size for p in files]}),
                     hide_index=True, width="stretch", height=280)
        choice = st.selectbox("Preview a file", rel_paths, index=None, placeholder="Choose a file…")
        if choice:
            content = (ex.root / choice).read_text(encoding="utf-8", errors="replace")
            st.code(content[:8000], language=choice.rsplit(".", 1)[-1] if "." in choice else "text")
            st.download_button("⬇️ Download", content, file_name=choice.rsplit("/", 1)[-1])
    with right:
        st.markdown("#### 🗄️ Database")
        conn = sqlite3.connect(ex.db.db_path)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            if tables:
                table = st.selectbox("Table", tables, index=tables.index("products") if "products" in tables else 0)
                count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                st.caption(f"{count} rows (showing up to 300)")
                st.dataframe(pd.read_sql_query(f'SELECT * FROM "{table}" LIMIT 300', conn), hide_index=True,
                             width="stretch", height=380)
        finally:
            conn.close()

with audit_tab:
    records = ss.audit.read()
    st.markdown("#### 🧾 Audit log")
    st.caption("Every request, model call, proposed action, approval decision and result for this session "
               "(append-only JSONL).")
    if records:
        events = sorted({r["event"] for r in records})
        default = [e for e in events if e not in ("llm_call",)]
        chosen = st.multiselect("Event types", events, default=default)
        rows = [{
            "time": r["ts"][11:23],
            "event": r["event"],
            "tool": r.get("tool", ""),
            "risk": r.get("risk", "") or "",
            "decision": r.get("decision", "") or "",
            "details": r.get("summary") or r.get("text") or r.get("message") or r.get("final")
                       or (f"{r.get('model')} · {r.get('stop_reason')} · {r.get('input_tokens')}→{r.get('output_tokens')} tok"
                           if r["event"] == "llm_call" else ""),
        } for r in records if r["event"] in chosen]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=460)
        st.download_button("⬇️ Download audit log (JSONL)", "\n".join(json.dumps(r, default=str) for r in records),
                           file_name=f"opspilot-audit-{ss.session_id}.jsonl")
    else:
        st.info("No activity yet.")

with how_tab:
    st.markdown("""
#### The problem
Small businesses run on scattered spreadsheets, databases, logs and inboxes. The people who need answers
("what sold last month?", "is the stock count right?") usually can't write SQL or shell commands, and
giving an LLM free rein over real systems is dangerous. **OpsPilot** lets staff ask in plain English while a
safety layer *in code* decides what the model is actually allowed to do.

#### How a request flows
""")
    st.graphviz_chart("""
digraph {
  rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#FDF3E4", color="#B45309", fontname="Helvetica"];
  user [label="User request\\n(natural language)"];
  llm [label="LLM integration\\nClaude API · retries ·\\nrate limits · fallbacks"];
  interp [label="Interpreter\\nparse tool_use blocks\\n→ ActionRecords"];
  val [label="Validate\\nJSON schema"];
  pol [label="Safety policy\\nSAFE / WRITE /\\nDESTRUCTIVE / BLOCKED"];
  appr [label="Human approval\\n(diff / dry-run preview)", fillcolor="#FDE2E2"];
  exe [label="Executors\\nfiles · SQLite ·\\nHTTP · commands"];
  audit [label="Audit log\\n(JSONL)", fillcolor="#E8F0FE"];
  user -> llm -> interp -> val -> pol;
  pol -> exe [label="safe"]; pol -> appr [label="risky"]; appr -> exe [label="approved"];
  pol -> llm [label="blocked → error", style=dashed]; exe -> llm [label="tool results", style=dashed];
  {val pol appr exe} -> audit [style=dotted];
}
""")
    st.markdown("""
#### Prompt engineering techniques
| Technique | Where |
|---|---|
| Role & mission framing | System prompt opening |
| XML-delimited sections (`<environment>`, `<rules>`, `<examples>`…) | System prompt |
| Explicit procedure: Understand → Plan → Act → Verify → Report | `<operating_procedure>` |
| Plan-and-execute with a live checklist | `update_plan` tool |
| Required per-action rationale (self-explanation) | `rationale` field on every tool |
| Few-shot examples incl. clarification, injection & sweeping-change cases | `<examples>` |
| Prompt-injection defence: tool output is data, not instructions | `<rules>` + seeded malicious email |
| Bounded error recovery (fix & retry ≤ 2) | `<rules>` |
| Output contract for consistent final reports | `<report_format>` |
| Structured outputs via strict JSON-schema tools | `agent/tools.py` |
| Per-turn context block (date, approval mode) outside the cached system prompt | `prompts.user_turn` |
| Adaptive thinking + effort control | `agent/llm.py` |

#### Safety layers
1. **Strict tool schemas** - the model can only emit well-formed actions; re-validated in code.
2. **Sandbox** - each session gets a private workspace; paths outside it are blocked.
3. **Allowlists** - HTTP hosts and commands are allowlisted (no shell, redirects re-checked).
4. **Risk policy in code** - SQL is classified and dry-run; reads use a `query_only` connection.
5. **Human-in-the-loop** - destructive actions show a diff/row-count preview and wait for approval.
6. **Audit log** - every step is recorded; downloadable from the Audit tab.
7. **Rate limiting** - SDK retries with backoff, `retry-after` handling, per-session request caps.
""")
    with st.expander("📜 Full system prompt"):
        st.code(prompts.SYSTEM_PROMPT, language="markdown")
    with st.expander("🧰 Tool schemas sent to the model"):
        st.json(tools.API_TOOLS, expanded=1)
    with st.expander("🔬 Raw conversation sent to the API (this session)"):
        raw = [{"role": m["role"], "content": m["content"] if isinstance(m["content"], str)
                else [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b for b in m["content"]]}
               for m in agent.messages]
        st.json(raw, expanded=2)
