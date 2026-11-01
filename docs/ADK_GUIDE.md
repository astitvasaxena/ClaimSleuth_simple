# ClaimSleuth GOOGLE ADK investigation — guide

## What it is

The **GOOGLE ADK RESULT** is one of ClaimSleuth's four independent outputs. Two Google ADK agents
investigate the same original claim that the deterministic baseline sees. They use the MCP server
for policy terms and claim history, and produce structured, validated reports. They never see or
copy the baseline's decision.

| | Evidence Agent (`evidence_agent`) | Investigator Agent (`investigator_agent`) |
|---|---|---|
| ADK object | `google.adk.agents.LlmAgent` | `google.adk.agents.LlmAgent` |
| Job | Read the documents; extract facts with document id, line number and an exact quote; mark each fact VERIFIED / UNCERTAIN / CONFLICTING; find missing or conflicting evidence | Examine the claim; work out the policy questions; look up claim history; identify anomalies; decide policy eligibility; recommend a decision |
| MCP tools (filter) | `warranty_policy_search` | `warranty_policy_search`, `claim_history_lookup` |
| `output_schema` | `EvidenceReport` | `InvestigationReport` |
| `output_key` (session state) | `evidence_report` | `investigation_report` |
| Reads from state | — | `{evidence_report?}` in its instruction |

Both reports contain: claim ID, findings, evidence citations (`doc_id`, `line`, `quote`), policy
references (MCP source strings), missing information, confidence, recommended decision and reasoning
summary. The Investigator also has history references and policy eligibility. Models live in
`src/claimsleuth/schemas.py`; agent code is in `src/claimsleuth/adk_service/`.

## How a run works

`run_adk_investigation(claim_id)` in `adk_service/runner.py`:

1. **Configuration check.** No `GOOGLE_API_KEY`/`GEMINI_API_KEY`, or incomplete Vertex settings,
   gives status `NOT_CONFIGURED` with a clear message. Nothing runs, and there is no fallback.
2. **Same claim as the baseline:** `datastore.load_claim()`, which reads the same files the baseline reads.
3. **MCP check:** `GET /health` on the MCP server. If it is unavailable, the status is `FAILED`
   before any model call.
4. **Agents:** `build_agents()` creates fresh agents for every run and wraps them in a
   `SequentialAgent` (Evidence, then Investigator).
5. **Session:** `InMemorySessionService.create_session(state={"run_id", "claim_id"})`.
6. **Runner:** `Runner(app=App(...)).run_async(...)` with `RunConfig(max_llm_calls=30)` and a 300 s
   time limit. The user message contains the claim form and every document line as
   `doc_id#L<n>: text`, all inside `<untrusted ...>` blocks.
7. **Structured output:** an agent that has tools and an `output_schema` gets ADK's
   `set_model_response` tool (on the Gemini API, as opposed to Vertex). ADK validates the arguments
   with Pydantic and returns any validation error to the model so it can retry.
8. **Validation after the run:** both reports are re-validated from session state, and each must
   carry the correct claim ID. **Verification:** every document citation must appear on the cited
   line (`documents.quote_appears`), and every policy or history reference must have been returned by
   an MCP tool *in this run* (a policy pointer must also have the same value). Failures are listed in
   `unsupported_references`.
9. **Lifecycle:** `runner.close()` closes the MCP sessions, then `delete_session()`. The trace
   records `session_created`, `runner_closed` and `session_deleted`.
10. `COMPLETED` only when both reports are valid. Any model, MCP or ADK exception gives `FAILED`
    with the error text. No report is invented.

## Callbacks (real ADK callbacks, `adk_service/callbacks.py`)

| Callback | Records |
|---|---|
| `before_agent_callback` / `after_agent_callback` | per-agent start, end, latency, output summary |
| `before_model_callback` | LLM call count; the tools offered to the model (what ADK discovered over MCP, plus `set_model_response`) |
| `after_model_callback` | prompt and completion tokens from `usage_metadata` |
| `on_model_error_callback` | model errors (always propagated, never replaced) |
| `before_tool_callback` / `after_tool_callback` | each tool call: agent, tool, protocol (`mcp`/`internal`), arguments, parsed result, status, latency, run ID |
| `on_tool_error_callback` | a tool exception becomes `{"error": ...}`, which the agent sees; it is recorded as an error |

Estimated cost is **not** computed yet. Prices must be checked and dated first (evaluation phase).

## MCP integration

Each agent has a `google.adk.tools.mcp_tool.McpToolset` with
`StreamableHTTPConnectionParams(url="http://127.0.0.1:8102/mcp", timeout=10)` and a `tool_filter`.
ADK sends `initialize` and `tools/list`, turns the MCP tool schemas into function declarations for
the model, and sends `tools/call` when the model asks for a tool.

**Run-ID correlation:** the toolset's `header_provider` sends `X-ClaimSleuth-Run-Id: <run_id>`, read
from session state, on every MCP request. The MCP server logs it (`run_id_source: http_header`), so
`/tool-calls?run_id=...` on the server lists the same calls as the ADK result's `tool_calls`.

## Commands (PowerShell, from `D:\Claimsleuth_simple`)

One-time: put the key in `.env` (copy `.env.example`; never commit it):
```powershell
Copy-Item .env.example .env
notepad .env          # set GOOGLE_API_KEY=...
```

Start the MCP server (separate window, or background as in `docs/MCP_GUIDE.md`):
```powershell
.venv\Scripts\python -m claimsleuth.mcp_server --port 8102
```

Check configuration (key present? MCP up?):
```powershell
.venv\Scripts\python -m claimsleuth.adk_service --check-config
```

Run the ADK investigation of one claim (Markdown on screen; JSON and Markdown saved under `runtime\reports\<run_id>\`):
```powershell
.venv\Scripts\python -m claimsleuth.adk_service CS-007
.venv\Scripts\python -m claimsleuth.adk_service CS-001 --json
.venv\Scripts\python -m claimsleuth.adk_service CS-003 --model gemini-3.5-flash --run-id RUN-mytest-0001
```
Exit codes: 0 completed, 1 failed, 3 not configured.

Tests:
```powershell
.venv\Scripts\python -m pytest tests\test_adk_investigation.py -v      # offline (test-double model)
.venv\Scripts\python -m pytest -m live tests\test_adk_investigation.py -s  # real Gemini; needs key + MCP on 8102
```

## Manual verification

1. `--check-config` prints `credentials: configured` and `MCP: ok`.
2. Run a claim. The Markdown shows `Status: COMPLETED`, the model name (not `test-double`), both
   agents' reports, and a **Tool calls** table with `protocol = mcp` rows.
3. Compare with the MCP server's own record for the same run:
   ```powershell
   Invoke-RestMethod "http://127.0.0.1:8102/tool-calls?run_id=RUN-mytest-0001" | ConvertTo-Json -Depth 6
   ```
   Each entry has `run_id_source: http_header`, and the tools match the ADK table.
4. Check `## Unsupported references`. Any citation the model made up is listed there.
5. Stop the MCP server and run again: the status is `FAILED` with "MCP server not available".

## Offline tests and the test double

`tests/adk_test_double.py::ScriptedLlm` is a `BaseLlm` that replays scripted model turns. It is
**not an LLM** and only exists in `tests/`. With it, the offline tests run the real ADK Runner,
sessions, callbacks, `set_model_response` validation, `McpToolset` and a real MCP server subprocess.
Results from it carry `model: "test-double"`. The command-line tool never uses it: without a key it
reports `NOT_CONFIGURED`.

## Limitations

- **No live Gemini run has been done yet**: no key is configured in this workspace. Whether Gemini
  accepts the generated `set_model_response` declarations for the nested `EvidenceReport` and
  `InvestigationReport` schemas, and how well the agents follow the instructions, is **unverified**
  until `pytest -m live` passes.
- The default model name `gemini-3.5-flash` is google-adk 2.13.0's default. It has not been checked
  against a live API.
- `SequentialAgent` is deprecated in google-adk 2.13.0 in favour of `Workflow`. It still works here;
  the architecture phase will build the workflow-based pattern with `Workflow`.
- No estimated cost yet; token counts come from the provider when it reports them.
- Guardrail callbacks (injection/abuse screening before the model) are a later phase. For now,
  untrusted text is only delimited and covered by the instructions.
- Each run opens new MCP sessions, which add about a second per agent.
