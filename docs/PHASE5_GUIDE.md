# Phase 5 — A2A communication and independent adjudication

## Status at a glance

| Feature | Status |
|---|---|
| Genuine A2A between ADK and CrewAI (`a2a-sdk 1.3.0`, A2A protocol 1.0, JSON-RPC over HTTP, Agent Card) | **Implemented and tested** across two venvs and two processes |
| ADK side uses ADK's own `RemoteA2aAgent` under an ADK Runner | **Implemented and tested** |
| Pydantic communication contract (`WorkflowMessage`) with claim, run and evidence-hash validation | **Implemented and tested** |
| Four independent outputs per claim under one run ID | **Implemented and tested** |
| MCP records fetched from the server's own log, checked against run **and** claim | **Implemented and tested** |
| Deterministic adjudicator (Decimal money, verified evidence only, escalation rules) | **Implemented and tested** |
| Audit trail (JSONL) and per-run output files | **Implemented and tested** |
| ADK model | **Test double** (`OfflineAdkLlm`). No LLM, no key. |
| CrewAI model | **Test double** (`ForensicTestDoubleLLM`). No LLM, no key. |
| Live LLM runs, LLM-based adjudication, dashboard, architectures, evaluation, deployment | **Deferred** (later phases or awaiting your decision) |

## Architecture and message flow

```
 .venv  (orchestrator process: python -m claimsleuth.integration CS-003)
 ├─ 1 DETERMINISTIC BASELINE            run_baseline()                      (in process)
 ├─ 2 GOOGLE ADK RESULT                 Evidence + Investigator LlmAgents, Runner, McpToolset ──MCP──┐
 ├─ 3 CREWAI RESULT via A2A             RemoteA2aAgent (own Runner+session)                          │
 │        GET  /.well-known/agent-card.json ─────────────▶ ┌────────────────────────────────────┐    │
 │        POST /a2a  SendMessage(text = FORENSIC_REQUEST) ─▶│ .venv-crew: CrewAI A2A service :8103│    │
 │                                                          │  AgentExecutor: parse + validate    │    │
 │        ◀── Message(text = FORENSIC_RESPONSE | ERROR) ── │  (contract, claim, SHA-256 evidence)│    │
 │                                                          │  run_forensic_investigation():      │    │
 │                                                          │  Agent/Task/Crew.kickoff ──MCP──────┼──┐ │
 │                                                          └────────────────────────────────────┘  │ │
 ├─ 4 MCP TOOL RESULTS   tools/list + GET /tool-calls?run_id ◀── MCP server :8102 (.venv) ◀─────────┴─┘
 └─ 5 Final report       adjudicate()  → runtime/runs/<run_id>/
```
Steps 2 and 3 run concurrently, and neither sees the other's output. The baseline runs first; it
takes milliseconds.

### Is it genuine A2A?

**Yes, at the transport and protocol level.**
- The CrewAI service is an `a2a-sdk` server: `create_agent_card_routes` and `create_jsonrpc_routes`,
  a `DefaultRequestHandler`, an `AgentExecutor`, and a protobuf `AgentCard` with a `JSONRPC`
  interface at protocol version 1.0.
- The ADK side is `google.adk.agents.remote_a2a_agent.RemoteA2aAgent`, which resolves the card and
  sends `SendMessage` over HTTP.
- The application payload is ClaimSleuth's own `WorkflowMessage` JSON, carried in the A2A message's
  text part. That contract is ours, not part of the A2A standard; A2A carries it.

Caveats:
- google-adk 2.13.0 marks its A2A components as **experimental** and prints a notice on each call; it
  is silenced in `forensic_a2a.py`.
- The service replies with a single A2A `Message` and does no task streaming.

## The communication contract (`schemas.py`, `contract.py`)

`WorkflowMessage`:
- `message_id`, `run_id`, `claim_id`;
- `sender` and `recipient` (`adk_orchestrator` / `crewai_forensic_service`);
- `message_type` (`FORENSIC_REQUEST` / `FORENSIC_RESPONSE` / `ERROR`);
- `timestamp`;
- `payload`, discriminated by `kind`:
  - `ForensicRequestPayload` (claim ID and the SHA-256 of every document);
  - `ForensicResponsePayload` (the CrewAI `AgentSystemResult`);
  - `ErrorPayload` (`code`, `detail`);
- `metadata`, an `ExecutionMetadata` (framework, model, provider, LLM mode, protocol).

| Check | Where | Result |
|---|---|---|
| Malformed JSON, unknown fields, bad ID patterns, a payload that doesn't match `message_type`, sender = recipient, payload claim ≠ envelope claim | `WorkflowMessage` validators / `contract.parse` | `INVALID_MESSAGE` |
| Request for a claim the service doesn't hold | service | `CLAIM_MISMATCH` |
| Service's copy of the evidence differs (SHA-256 per document) | service, `contract.check_request` | `EVIDENCE_MISMATCH` |
| Reply for another run / another claim / wrong direction | ADK side, `contract.check_response` | `RUN_MISMATCH` / `CLAIM_MISMATCH` / `INVALID_MESSAGE` |
| Response result's `claim_id`/`run_id` ≠ envelope | `WorkflowMessage` validator | rejected |

An error is never turned into a result. When no valid response arrives, the orchestrator records a
`FAILED` CrewAI result carrying the error text.

## Independence and metadata

All three workflows load the same claim files (`datastore.load_claim`), and the LLM agents get the
identical packet text (`claim_text.claim_message`). The CrewAI service checks the evidence hashes to
prove it read the same bytes. Each output is stored as returned (`baseline.json`, `adk.json`,
`crewai.json`, `mcp.json`) and the adjudicator never edits them. `ClaimRunBundle.workflow_metadata`
records framework, model, provider and LLM mode per workflow.

## MCP records

- `integration/mcp_records.py` discovers the tools (`tools/list` through `mcp.Client`).
- It reads the server's own log for the run (`/tool-calls?run_id=`).
- It accepts a record only when the server logged **both** the run ID and the claim ID. Those arrive
  as headers, `X-ClaimSleuth-Run-Id` and `X-ClaimSleuth-Claim-Id`, sent by ADK's `header_provider` and
  CrewAI's `MCPServerHTTP(headers=...)`.
- Other records are rejected and listed.
- Each record also names its caller (`adk:evidence_agent`, `adk:investigator_agent`,
  `crewai:forensic_agent`).
- MCP records carry **no decision**.

## Adjudication rules (`integration/adjudicator.py`, deterministic)

1. Recompute the rules from the claim, documents, policy and SQLite history using the rules engine:
   coverage window (expiry day inclusive), exclusions (negation-aware), serial, date and history
   checks, and the Decimal settlement.
2. If the baseline workflow result is missing or failed → **ESCALATE** (`BASELINE_UNAVAILABLE`). If
   it differs from the recomputation (decision or payout) → **ESCALATE** (`INTEGRITY_ESCALATION`).
3. An agent finding counts only when verified:
   - each document citation must quote the cited line;
   - a history finding needs its `record_sources` to come from the agent's own recorded MCP call
     **and** be confirmed by a matching call in the MCP server's log for this run and claim.
   - Everything else goes into `unsupported_claims` and is ignored.
4. Deterministic **ESCALATE** and **REJECT** stand. Agents can't approve coverage. A forensic APPROVE
   only means "no forensic objection" and never overrides a rejection.
5. Deterministic **REQUEST_EVIDENCE** stands unless an agent raises a verified HIGH concern →
   **ESCALATE** (`CONFLICT_ESCALATION`).
6. Deterministic **APPROVE** needs at least one completed agent review (otherwise **ESCALATE**,
   `INSUFFICIENT_REVIEW`) and no verified HIGH concern (otherwise **ESCALATE**, `CONFLICT_ESCALATION`).
   Unverified objections are recorded as disagreements only.
7. Money comes only from the rules engine's Decimal settlement, and only on a final APPROVE. REJECT
   and ESCALATE always need a human; APPROVE at or above ₹15,000 needs a human.

The report explains itself with:
- `rationale`;
- `rule_checks` and `evidence_references` (document line quotes with stable `EV-` IDs);
- `policy_references` (`policies.json#/<policy>/<term>`, plus the matched exclusion);
- `agreements`, `disagreements` and `unsupported_claims`;
- the four-row `comparison`, in which MCP is shown as "Tool results, not a decision".

## Audit and traceability

`runtime/runs/<run_id>/` contains:
- `baseline.json`, `adk.json`, `crewai.json`, `mcp.json`;
- `messages.jsonl` (both A2A messages);
- `audit.jsonl` (`run_started`, `workflow_started` / `workflow_finished` per workflow,
  `message_sent` / `message_received`, `mcp_records_fetched`, `validation_failure`, `adjudication`,
  `run_finished`);
- `final_report.json`, `final_report.md`, `bundle.json`.

Audit times are the real times of each step; workflow ends use each workflow's own `finished_at`. The
CrewAI service keeps its own message log (`/messages?run_id=`), and the MCP server keeps its tool-call
log. All of them share the run ID.

## Commands (PowerShell, from `D:\Claimsleuth_simple`)

Install `a2a-sdk` (already done here):
```powershell
.venv\Scripts\python -m pip install "a2a-sdk[http-server]==1.3.0" "opentelemetry-api==1.44.0"
.venv-crew\Scripts\python -m pip install "a2a-sdk[http-server]==1.3.0"
```

Start the two services (two windows):
```powershell
.venv\Scripts\python -m claimsleuth.mcp_server --port 8102
$env:PYTHONIOENCODING = "utf-8"; .venv-crew\Scripts\python -m claimsleuth.crew_service.a2a_server --port 8103
```
Check them:
```powershell
Invoke-RestMethod http://127.0.0.1:8102/health | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:8103/health | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:8103/.well-known/agent-card.json | ConvertTo-Json -Depth 6
```
Run one claim through everything:
```powershell
.venv\Scripts\python -m claimsleuth.integration CS-003 --run-id RUN-demo-cs003
Get-Content runtime\runs\RUN-demo-cs003\audit.jsonl
Invoke-RestMethod "http://127.0.0.1:8103/messages?run_id=RUN-demo-cs003" | ConvertTo-Json -Depth 5
Invoke-RestMethod "http://127.0.0.1:8102/tool-calls?run_id=RUN-demo-cs003" | ConvertTo-Json -Depth 6
```
ADK alone, offline: `.venv\Scripts\python -m claimsleuth.adk_service CS-007 --offline`.

Stop the services: Ctrl+C in each window. Each service writes its PID to `runtime\pids\` (`mcp_server.pid`,
`crew_a2a.pid`) for `Stop-Process -Id (Get-Content <file>)`.

Tests:
```powershell
.venv\Scripts\python -m pytest                      # 182 passed, 2 skipped (crew modules), 1 deselected (live)
.venv-crew\Scripts\python -m pytest tests\crew      # 25 passed
```

## Port note

Port **8101** on this machine is already answering with the **original ClaimSleuth** CrewAI service
("ClaimSleuthCrewAIForensics"). It was not started or stopped by this project, so the ClaimSleuth
Simple CrewAI A2A service uses **8103** (`CLAIMSLEUTH_CREW_PORT` to change it).

## Limitations

- Both agent systems run on **test doubles**. Their conclusions are scripted rules that overlap with
  the deterministic rules, so agreement between systems here says nothing about LLM quality.
- Injection findings that refer to the claim form (not a numbered document line) can't be cited.
  They are listed as unsupported; the deterministic guardrail still escalates those claims.
- ADK's A2A support is marked experimental in google-adk 2.13.0.
- The CrewAI service runs one investigation at a time (process-wide CrewAI event handlers).
- No authentication on the A2A, MCP or health endpoints. They bind to 127.0.0.1 and must be secured
  before any deployment.
- The adjudicator is fully deterministic by design. No LLM judge is used.
