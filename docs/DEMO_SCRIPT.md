# Live demo run sheet (≈ 6–8 minutes)

**Before you present**
- Open the deployed URL and unlock it. Click **🔄 Reset workspace & chat** so the data is fresh.
- Model: Claude Opus 5.5, effort `medium`, approval mode *Approve destructive actions only*.
- Keep the **🧾 Audit log** and **🗂️ Workspace** tabs in mind; you will switch to them.
- Backup: if the Wi-Fi fails, run `python cli.py` locally with the same prompts.

---

### 1. The problem (30 s, no clicks)
> "Small shops keep their operations in files, a database, logs and an inbox. Staff can't write SQL, and letting an
> AI loose on real systems is risky. OpsPilot lets them ask in English, while code decides what the AI may do."

### 2. Multi-domain read + file write — *📊 Sales snapshot* (1 min)
Click the sidebar button. Point out:
- the **🧭 Plan** checklist (plan-and-execute prompting)
- 💭 **Reasoning** expanders (adaptive thinking)
- action cards: `describe_database` → `run_sql` (expand one to show the SQL and the result table) → `write_file` → `read_file` (the **Verify** step)
- the final report follows the output contract (✅ list, table, file path)

Then open **🗂️ Workspace** and preview the new file in `reports/`.

### 3. Destructive action with human approval — *📦 Reconcile inventory* (1.5 min)
The agent reads the CSV, compares it with the database, then proposes `UPDATE`s.
- The approval card shows a **DESTRUCTIVE** badge, the rationale, and a **dry run: "would affect 1 row(s)"**.
- **Approve** the first update. For another, type the note *"leave the tote bags alone"* and click **Deny**: the agent
  acknowledges the denial and does not try a workaround.

### 4. Prompt-injection defence — *📧 Triage the inbox* (1.5 min)
- Email 2 contains hidden instructions to export the customer list and delete the logs. The agent summarises the
  customer's real issue and **flags the injection** instead of obeying it.
- Email 1 asks for prices in pesos: watch a 🌐 `http_get` to the currency API (cross-domain: file → web → file).
- Replies are drafted in `reports/`.

### 5. Hard safety boundaries — *🛡️ Safety test* (45 s)
`/etc/passwd`, `rm -rf /` and `example.com` all come back **⛔ Blocked by policy** with the reason, without ever
reaching the OS. Stress: "this is enforced in code, not by politely asking the model."

### 6. Auditability (45 s)
Open **🧾 Audit log**: every request, model call (tokens, request id), proposal, approval decision and result,
with timestamps. Download the JSONL.

### 7. Under the hood (45 s)
Open **🧠 How it works**: the architecture diagram, the prompt-technique table, the full system prompt, the tool
schemas, and the **raw conversation** sent to the API.

### Extra prompts if time allows
- "Which products are below 10 units in stock? Export them to a CSV for the supplier." (`export_query_to_csv`)
- "We're doing a pop-up in Portland this weekend — what's the forecast and what should we bring?" (geocode → forecast → DB)
- "Show system info and disk usage." (💻 system domain)
- Switch the approval mode to **strict** and ask for a new report: even file creation now asks first.

### Likely questions
- *What if the model ignores the rules?* Schemas, the policy, allowlists, the sandbox and approvals are all code;
  the prompt is not the security boundary.
- *Rate limits/outages?* The SDK retries with exponential backoff; OpsPilot honours `retry-after`, throttles
  requests, caps calls per session, and shows a friendly message instead of crashing.
- *Cost?* The sidebar shows tokens and estimated cost; prompt caching re-uses the system prompt and tools across calls.
