# ClaimSleuth CREWAI forensic investigation — guide

## What it is

The **CREWAI RESULT** is one of ClaimSleuth's four independent outputs. One CrewAI agent, the
**Forensic Agent**, investigates the same original claim the baseline and ADK see. It looks for:
- serial-number inconsistencies;
- conflicting purchase, incident and inspection dates;
- suspicious details, such as instructions aimed at the system;
- previous-claim patterns, using the MCP tool `claim_history_lookup`;
- evidence gaps;
- contradictions between the invoice and the repair report.

Every finding cites document lines. It never sees the baseline's or ADK's result, and it can only
**recommend** an outcome. `ForensicsReport` has no payout or approval field.

> **Two modes (Phase 6B).** `CLAIMSLEUTH_CREW_LLM=test-double` (default) uses `ForensicTestDoubleLLM`,
> a deterministic **test double, not a language model**. `CLAIMSLEUTH_CREW_LLM=ollama-local` uses genuine
> local inference with qwen2.5:3b in Ollama on 127.0.0.1; see `docs/OLLAMA_GUIDE.md`. The rest of this
> note describes the test-double mode. CrewAI's Agent, Task, `Crew.kickoff()`, native MCP client, guardrail and
> callbacks are real. The *conclusions* come from fixed rules inside the test double. Every result says
> `model: "test-double"` and `provider: "offline-test-double"`, and the Markdown starts with a warning
> banner. Offline CrewAI results therefore say nothing about LLM quality.

## Architecture

```
.venv-crew  (crewai 1.15.33, mcp 1.30.0)                .venv  (mcp 2.3.0)
┌────────────────────────────────────────────┐          ┌──────────────────────────────┐
│ run_forensic_investigation(claim_id)        │          │ MCP server :8102 /mcp        │
│  load_claim()  -> same claim as baseline/ADK│          │  claim_history_lookup        │
│  Agent(role, goal, backstory,               │  MCP     │  (SQLite claim_history)      │
│        llm=ForensicTestDoubleLLM,           │ ───────▶ │  logs run id from header     │
│        mcps=[MCPServerHTTP(url, headers=    │ Streamable└──────────────────────────────┘
│              {X-ClaimSleuth-Run-Id}, filter)])  HTTP
│  Task(output_pydantic=ForensicsAnalysis,    │
│       guardrail=..., guardrail_max_retries=2)
│  Crew(process=sequential, step_callback,    │
│       task_callback).kickoff(inputs=packet) │
│  -> ForensicsReport (+model/provider/status set by code)
└────────────────────────────────────────────┘
```

| File | Purpose |
|---|---|
| `src/claimsleuth/crew_service/__init__.py` | Turns off CrewAI telemetry, trace upload and the interactive first-run tracing prompt before `crewai` is imported |
| `crew_service/crew.py` | `build_crew()` (Agent, Task, Crew); task guardrail |
| `crew_service/test_double.py` | `ForensicTestDoubleLLM` (a `crewai.llms.base_llm.BaseLLM` subclass) |
| `crew_service/runner.py` | `run_forensic_investigation()`; recording via CrewAI callbacks and event bus |
| `crew_service/network_guard.py` | Loopback-only network guard for offline runs |
| `crew_service/__main__.py` | CLI |
| `claim_text.py` (shared) | The claim packet: the same untrusted, line-numbered text the ADK agents receive |
| `schemas.py` | `ForensicsAnalysis` (what the task returns), `ForensicsReport` (validated result), `ConsistencyCheck`, `HistoryPatternFinding` |

## How the test double is connected

CrewAI accepts any `BaseLLM` subclass as `Agent(llm=...)`. The test double:
- implements `call(messages, tools, ...)`;
- declares `supports_function_calling() = False`, so CrewAI uses its ReAct text protocol;
- emits CrewAI's standard `LLMCallStarted`/`LLMCallCompleted` events inside `llm_call_context()`,
  as CrewAI's built-in providers do.

The conversation runs like this:

1. CrewAI sends its ReAct prompt, which contains the task text (claim packet) and the tool list.
2. The double answers `Action: mcp_..._claim_history_lookup` / `Action Input: {"serial_number": ...}`.
   **CrewAI** executes the MCP call and returns the result as an `Observation`.
3. The same again for `{"claimant_id": ...}`.
4. The double writes `Final Answer: {ForensicsAnalysis JSON}`, computed by fixed rules from the
   packet and the observations in its transcript.
5. CrewAI converts it with `output_pydantic`. The task guardrail then checks it: valid
   `ForensicsAnalysis`, correct `claim_id`, every citation quote on the cited document line, and every
   history reference among the `record_sources` that MCP actually returned. On failure CrewAI retries
   (`guardrail_max_retries=2`), then raises; the run is then `FAILED`.

Test-only modes (`invalid_json`, `fabricated_citation`, `wrong_claim`) check that CrewAI rejects bad
output.
- Text that isn't JSON fails in CrewAI's **output converter** (`ConverterError`), before the guardrail.
- A made-up citation or a wrong claim ID fails in the **guardrail**, after 2 retries.

**In test-double mode no real LLM is called.** `CLAIMSLEUTH_CREW_LLM` defaults to `test-double`; the only
other accepted value is `ollama-local` (local Ollama, see `docs/OLLAMA_GUIDE.md`); anything else returns
`NOT_CONFIGURED`. In addition, runs in **both** modes execute inside
`loopback_only()`: DNS lookups and socket connects to anything other than localhost are refused and
listed in the trace (`network_guard`, `blocked_hosts`). A test shows that calls to
`generativelanguage.googleapis.com`, `openrouter.ai` and `api.openai.com` are blocked while the
local MCP server stays reachable. Every recorded run has `blocked_hosts: []`.

## Execution records

| Source (real CrewAI hooks) | Recorded |
|---|---|
| `Crew(step_callback=...)` | each agent step (`AgentAction` with the tool, `AgentFinish`) with a timestamp |
| `Crew(task_callback=...)` | task completion |
| event bus `ToolUsageFinishedEvent` / `ToolUsageErrorEvent` | each tool call: tool, arguments, output, `started_at`, latency, status, run ID |
| event bus `LLMCallStartedEvent` / `LLMCallFailedEvent` | model call count and errors |
| `CrewOutput.token_usage` | tokens, only when the provider reports real requests (the test double reports none, so `None`) |

Handlers are registered inside `crewai_event_bus.scoped_handlers()` and flushed after `kickoff()`.

## Environment

CrewAI **cannot** share `.venv`. `crewai 1.15.33` requires `mcp<2` and `opentelemetry-api>=1.45.1`,
while `google-adk 2.13.0` requires `opentelemetry-api<=1.44` (and `.venv` uses `mcp 2.3.0`). So:

| | `.venv` | `.venv-crew` |
|---|---|---|
| Used for | baseline, MCP server, ADK agents | CrewAI Forensic Agent |
| Key packages | google-adk 2.13.0, mcp 2.3.0, opentelemetry-api 1.44.0 | crewai 1.15.33, mcp 1.30.0, opentelemetry-api 1.45.2 |
| Shared | `pydantic 2.12.5`; the `claimsleuth` package installed in editable mode in both; the same `data/` folder and SQLite database | |

The CrewAI side never opens the SQLite database itself; it reads history only through the MCP server.

Setup (already done here):
```powershell
python -m venv .venv-crew
.venv-crew\Scripts\python -m pip install -r requirements\crew.txt
.venv-crew\Scripts\python -m pip install -e .
```

## Commands (PowerShell, from `D:\Claimsleuth_simple`)

Start the MCP server from the **main** environment (second window):
```powershell
.venv\Scripts\python -m claimsleuth.mcp_server --port 8102
```

Run the forensic investigation of a claim (offline test double). Markdown goes to the screen; JSON and
Markdown are saved under `runtime\reports\<run_id>\crewai.*`:
```powershell
$env:PYTHONIOENCODING = "utf-8"
.venv-crew\Scripts\python -m claimsleuth.crew_service CS-003
.venv-crew\Scripts\python -m claimsleuth.crew_service CS-007 --run-id RUN-crewdemo-0001
.venv-crew\Scripts\python -m claimsleuth.crew_service CS-009 --json
```
Exit codes: 0 completed, 1 failed, 3 not configured. `PYTHONIOENCODING=utf-8` stops CrewAI's console
emoji from failing on a cp1252 console.

Tests:
```powershell
.venv-crew\Scripts\python -m pytest tests\crew -v        # CrewAI tests (starts its own MCP server with .venv)
.venv\Scripts\python -m pytest                           # everything else; tests\crew shows as 1 skipped
```

## Manual verification

1. Run `CS-003`. The banner says TEST DOUBLE; `Status: COMPLETED`; the serial check is
   `INCONSISTENT` with citations `invoice#L10` and `repair_report#L10`.
2. The **Tool calls** table shows two `claim_history_lookup` rows with `protocol = mcp`. The MCP
   server's own log lists the same calls under the run ID with `run_id_source: http_header`:
   ```powershell
   Invoke-RestMethod "http://127.0.0.1:8102/tool-calls?run_id=RUN-crewdemo-0001" | ConvertTo-Json -Depth 6
   ```
3. In `runtime\reports\<run_id>\crewai.json`, the trace entry `network_guard` shows `blocked_hosts: []`,
   and `agent_runs[0].llm_calls` is 3 (2 tool-requesting turns + the final answer).
4. `CS-007` shows `SAME_SERIAL_OTHER_CLAIMANT` with the source of record `HST-2025-1102`. `CS-009` shows
   the chronology conflict. `CS-006` shows the missing repair report.
5. Stop the MCP server and run again: `FAILED`, "MCP server not available", no model call.
6. `$env:CLAIMSLEUTH_CREW_LLM = "live"` (an unknown mode) then run: `NOT_CONFIGURED`. Clear it again with
   `Remove-Item Env:CLAIMSLEUTH_CREW_LLM`.

## Limitations

- **Test-double mode** has no token counts. Live local inference (`ollama-local`) is described in
  `docs/OLLAMA_GUIDE.md`, including its measured speed and limits.
- The test double's conclusions are scripted rules. They overlap in spirit with the deterministic
  baseline, so agreement between them in offline runs proves nothing about LLM behaviour.
- The forensic review does not judge the coverage period, exclusions or money, by design. For CS-002
  (expired) and CS-004 (excluded cause) it recommends APPROVE (no forensic objection) and says so in
  `uncertainty`. The later adjudication step must not treat that as a coverage decision.
- CrewAI-side tool latency (about 0.7–1.0 s) is much larger than the MCP server's execution time (3–15 ms),
  because the client side includes connection set-up.
- CrewAI emits deprecation warnings from its own code (`function_calling_llm`, the MCP client's
  `streamablehttp_client`). These don't come from this project's code.
- `crewai_event_bus.scoped_handlers()` is process-wide, so one process should run one forensic
  investigation at a time (the A2A service will need to serialise requests).
- The network guard patches Python's `socket` module only. It is a safety check, not a sandbox.
