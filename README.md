# ☕ OpsPilot — an LLM agent that acts safely on real systems

OpsPilot turns plain-English requests ("reconcile the stock count with the database and fix mismatches")
into concrete, **validated, risk-assessed, human-approved and audited** actions across four domains:

| Domain | Actions | Guardrails |
|---|---|---|
| 📁 Files | list, read, write, move, delete | per-session sandbox, path-traversal blocking, diff preview before overwrite |
| 🗄️ Database (SQLite) | describe, query, insert/update/delete, export to CSV | SQL classification, single statement only, dry-run row counts, `query_only` connection for reads |
| 🌐 Web APIs | HTTPS GET to weather, currency, Wikipedia, GitHub | host allowlist, redirects re-checked, size/time limits |
| 💻 System | host info, allowlisted read-only commands | no shell, per-command argument rules, workspace-only file args, timeouts |

It runs as a **web app** (Streamlit; shareable URL) and as a **terminal app**.

---

## The problem

Small businesses keep their operations in scattered files, a database, logs and an email inbox. The people who
need answers usually can't write SQL or shell commands, and handing an LLM unrestricted access to real
systems is dangerous: models can misunderstand, hallucinate success, or be manipulated by text they read
(prompt injection). OpsPilot is built around one idea: **the model proposes, the code decides.**

The demo company is *Brightside Coffee Roasters*, a fictional online coffee shop with a product/customer/order
database, application logs, inventory counts, meeting notes and customer emails.

## Architecture

```
user request ──► LLM integration ──► Interpreter ──► Validate ──► Safety policy ──┬─► Executor ──► tool result ─┐
 (UI / CLI)      (agent/llm.py)      (core.py)       (schema)     (policy.py)     │   (executors/)               │
                       ▲                                                          └─► Human approval ──┘         │
                       └──────────────────────────────────────────────────────────────────────────────────────┘
                                       every step ──► append-only audit log (logs/audit.jsonl)
```

| Module | Responsibility |
|---|---|
| `agent/llm.py` | **LLM integration**: Anthropic Messages API (Claude Opus 5.5 by default), adaptive thinking + effort, prompt caching, server-side refusal fallbacks, SDK retries with exponential backoff, `retry-after` handling for 429s, client-side throttling and per-session request cap, friendly error mapping (401/403/404/429/5xx/timeouts/network) |
| `agent/tools.py` | Strict JSON-schema tool definitions = the *only* actions the model can express |
| `agent/prompts.py` | System prompt and per-turn context (see [docs/PROMPT_ENGINEERING.md](docs/PROMPT_ENGINEERING.md)) |
| `agent/core.py` | **Action interpreter/executor**: parses `tool_use` blocks into `ActionRecord`s, runs validate → assess → approve → execute → audit, and feeds results back. A resumable state machine, so a GUI can pause for approval |
| `agent/validation.py` | Re-validates every model-produced argument against its schema |
| `agent/policy.py` | Classifies each action SAFE / WRITE / DESTRUCTIVE / BLOCKED **in code** and builds previews (diffs, dry-run row counts) |
| `agent/executors/` | Domain executors: `files.py`, `database.py`, `web.py`, `system.py` |
| `agent/audit.py` | Thread-safe JSONL audit log |
| `agent/workspace.py` | Creates a private sandbox per session with seed files and a generated database |
| `app.py` | Streamlit web UI: chat, live progress, plan checklist, action cards, approval dialog, workspace browser, audit log, "How it works" |
| `cli.py` | Terminal UI with the same agent and approval flow |

### Risk policy

| Risk | Examples | Behaviour |
|---|---|---|
| SAFE | read file, SELECT, allowlisted GET, `df -h` | runs automatically |
| WRITE | create a file, INSERT, export CSV to a new file | automatic in *standard* mode, approval in *strict* mode |
| DESTRUCTIVE | overwrite, delete, UPDATE/DELETE/DROP/ALTER | **always** asks; shows a diff or dry-run row count |
| BLOCKED | `../` paths, `rm -rf`, non-allowlisted hosts, multi-statement SQL, ATTACH, PRAGMA writes | never runs; the reason is returned to the model so it can adapt |

## Run it locally

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt

# Web UI (opens http://localhost:8501). Paste your API key in the sidebar,
# or put it in .streamlit/secrets.toml (see secrets.toml.example).
streamlit run app.py

# Terminal UI
set ANTHROPIC_API_KEY=sk-ant-...  # macOS/Linux: export ANTHROPIC_API_KEY=...
python cli.py --effort medium     # add --strict to approve every change

# Tests (no API key needed — the LLM is mocked)
pytest -q
```

## Share it with a URL (Streamlit Community Cloud, free)

1. Push this folder to a GitHub repository.
2. Go to <https://share.streamlit.io>, sign in with GitHub, click **Create app** → *Deploy a public app from GitHub*.
3. Select the repo, branch `main`, main file `app.py`. Under **Advanced settings** pick Python 3.12 and paste:
   ```toml
   ANTHROPIC_API_KEY = "sk-ant-..."
   OPSPILOT_PASSCODE = "a-passcode-you-give-your-class"
   ```
4. Deploy. You get a URL like `https://<your-app>.streamlit.app`.

With a passcode set, visitors must enter it to use your key (or paste their own key), so strangers can't spend
your credits. Each visitor gets an isolated sandbox, and each session is capped at 80 model calls
(`OPSPILOT_MAX_REQUESTS`).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | API key (env var or Streamlit secret) |
| `OPSPILOT_PASSCODE` | — | Gate for using the server key on a public deployment |
| `OPSPILOT_MODEL` | `claude-opus-5-5` | Default model (Sonnet 5.5 and Haiku 5.5 are selectable in the UI) |
| `OPSPILOT_EFFORT` | `medium` | Default reasoning effort |
| `OPSPILOT_MAX_REQUESTS` | `80` | Model calls allowed per session |

## Demo

See [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) for a 6-minute live-demo run sheet that hits every requirement.

## Tests

`pytest -q` runs 56 tests covering the sandbox, SQL classification and dry runs, URL and command allowlists,
schema validation, the approve/deny/block flows of the agent loop, the exact HTTP request sent through the real
Anthropic SDK (via a mock transport), API error mapping, and a headless run of the Streamlit UI.
