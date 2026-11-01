# ClaimSleuth Simple — implementation checklist

Status legend: `[ ]` not started · `[~]` in progress · `[x]` done **with runtime evidence** (recorded in `docs/BUILD_LOG.md`).
A box is ticked only after the listed check has actually been run.

Teacher criteria (C-1 … C-11) are from `ClaimSleuth_Handoff_Gap_Analysis_and_Build_Plan.md` §2.1.

## Phase 1 — Schemas, dataset, deterministic baseline (no LLM)
- [x] Pydantic schemas for claim, evidence, findings, baseline, MCP results, comparison, final report, blackboard (C-3)
      (LLM-output and MCP schemas may be adjusted once the ADK/CrewAI/MCP APIs are verified)
- [x] Synthetic dataset: 9 base claims + persona variants, policies, ground truth kept apart from claim data
- [x] SQLite claim-history table (one database file)
- [x] Deterministic rules: dates, serials, exclusions, settlement (Decimal)
- [x] Deterministic baseline engine → `BaselineResult` (JSON + Markdown)
- [x] Input screen for prompt injection / abusive text (reused later by ADK and CrewAI callbacks) (C-8)
- [x] Unit tests pass (87/87); baseline matches ground truth 18/18; baseline imports no LLM framework
- Check: `pytest`, `python -m claimsleuth.baseline CS-001`

## Phase 2 — MCP server (separate process)
- [x] MCP server (official SDK `mcp==2.3.0`, Streamable HTTP, port 8102) with exactly two tools:
      `warranty_policy_search` (keyword search over `data/policies.json`) and `claim_history_lookup` (SQLite) (C-2)
- [x] Input/output validation, tool errors (`isError`), tool-call JSONL log with run ID (MCP `_meta` or header), `/health`
- [x] Independent client `scripts/mcp_client_check.py` discovers and invokes both tools over MCP: 37/37 checks
- [x] Tests: 41 new (128 total, 0 failed); Phase 1's 87 unchanged and passing
- Check: `python -m claimsleuth.mcp_server`, then `python scripts/mcp_client_check.py` (see `docs/MCP_GUIDE.md`)
- Not yet: ADK and CrewAI as MCP clients (Phases 3–4); authentication on `/mcp` (before deployment)

## Phase 3 — Google ADK Investigator + Evidence agents (done before CrewAI; order changed by the user's brief)
- [x] Two real `LlmAgent`s (Evidence, Investigator) in a `SequentialAgent`, run by `Runner` with an
      `InMemorySessionService` session (create -> run -> read state -> close -> delete) (C-7)
- [x] `McpToolset` over Streamable HTTP to the MCP server; discovery and invocation from ADK; run id sent as
      `X-ClaimSleuth-Run-Id` via `header_provider` and found in the MCP server log (C-2, C-9)
- [x] Pydantic `output_schema` (`EvidenceReport`, `InvestigationReport`), re-validated after the run; citations and
      MCP references verified; Evidence report consumed by the Investigator via `{evidence_report?}` (C-1, C-3)
- [x] ADK callbacks: agent / model / tool / tool-error records with latency, tokens, arguments, results (C-5)
- [x] `NOT_CONFIGURED` without a key, `FAILED` without MCP; no deterministic fallback
- [x] Offline tests (labelled test-double model; real Runner, sessions, McpToolset, MCP server): 16 passed
- [ ] **Live Gemini run** — blocked: no `GOOGLE_API_KEY` configured. Check: `pytest -m live tests/test_adk_investigation.py`
- Check: `python -m claimsleuth.adk_service --check-config`, then `python -m claimsleuth.adk_service CS-007`

## Phase 4 — CrewAI Forensic Agent (offline, test-double model) — A2A deferred to a later phase
- [x] Separate `.venv-crew` (crewai 1.15.33, mcp 1.30.0, opentelemetry-api 1.45.2, pydantic 2.12.5); `.venv` unchanged
- [x] Real CrewAI `Agent` / `Task(output_pydantic=ForensicsAnalysis, guardrail)` / `Crew(sequential)` / `kickoff()` (C-7)
- [x] CrewAI native MCP (`Agent(mcps=[MCPServerHTTP])`) calls `claim_history_lookup` on the MCP server; run id via header (C-2)
- [x] `ForensicsReport` validated; model / provider / execution_status set by code; no payout field (C-3)
- [x] Recording via `step_callback`, `task_callback` and the CrewAI event bus (tool and LLM call events) (C-5)
- [x] Offline only: labelled `ForensicTestDoubleLLM`; loopback-only network guard; `NOT_CONFIGURED` for any other LLM mode
- [x] Tests in `.venv-crew`: 19 passed; main `.venv`: 144 passed, CrewAI module skipped
- [ ] Live LLM mode for CrewAI — not implemented (offline phase by the user's choice)
- [x] A2A server with Agent Card; ADK calls the CrewAI service over A2A (C-6) -- done in Phase 5
- Check: `.venv-crew\Scripts\python -m claimsleuth.crew_service CS-003` (MCP server running), `pytest tests\crew`

## Phase 5 — A2A communication and independent adjudication
- [x] Genuine A2A: a2a-sdk 1.3.0 server (Agent Card, JSON-RPC, AgentExecutor) for the CrewAI Forensic Agent in
      .venv-crew on :8103; ADK `RemoteA2aAgent` client in .venv (C-6)
- [x] Pydantic `WorkflowMessage` contract: run/claim/evidence-hash validation, ERROR replies (C-3)
- [x] Four independent outputs per claim under one run id; ADK offline test double for any claim (`--offline`)
- [x] MCP records from the server log, accepted only for this run and claim; caller recorded (C-2, C-9)
- [x] Deterministic adjudicator: Decimal money, verified evidence only, escalation rules, explanations
- [x] Audit trail + per-run files in `runtime/runs/<run_id>/` (C-9)
- [x] Tests: main .venv 182 passed (38 new); .venv-crew 25 passed (6 new)
- [x] Blackboard state model: shared coordination blackboard (SQLite) used by the ADK process and the CrewAI
      A2A service process; append-only, isolated per run/claim, concurrency-safe (Phase 7e,
      docs/BLACKBOARD_GUIDE.md). Coordination only: agents do not exchange findings.
- [ ] Communication-mode comparison (blackboard / peer-to-peer / broadcast) (C-1)
- [ ] Architectures: sequential · parallel · hierarchical · workflow · event-driven; worker-node on/off (C-4)
- Check: `python -m claimsleuth.integration CS-003` (MCP :8102 and CrewAI A2A :8103 running)

## Phase 6 — Local LLM (6A audit, 6B ollama-local for CrewAI) · security and personas
- [x] 6A readiness audit: Ollama 0.40.13 running on 127.0.0.1, CPU only, qwen2.5:3b local; CrewAI's built-in Ollama provider
- [x] 6B `ollama-local` mode for the CrewAI Forensic Agent (`crew_service/ollama.py`): loopback-only endpoint, local-model
      preflight, cloud-name rejection, bounded timeouts/iterations/retries, no fallback, labelled qwen2.5:3b / ollama-local
- [x] Offline tests (incl. fake-Ollama server fixture): .venv-crew 56 passed; main .venv 188 passed
- [~] Live CS-003 run executed (genuine inference, 2 model calls, real tokens) — **FAILED**: Ollama ran the model with a
      4096-token context and truncated the prompt; the model also skipped the MCP tool. Needs a context-length decision.
- [ ] ADK on a local model (not started; ADK still Gemini or test double)
- [ ] Guardrails wired into ADK + CrewAI callbacks; tool-output validation; untrusted-document wrapping (C-8)
- [ ] Personas: honest · vague · abusive · injection; prompt variants: standard · ReAct/evidence-first

## Phase 7 — Application and service orchestration (see docs/DASHBOARD_GUIDE.md)
- [x] One-page application (Claim Investigation) in the original prototype's layout (light theme); the 7-page
      technical dashboard (7b) and the Security & Guardrails page (7d) were removed on request
- [x] Security & Document Integrity in the investigation report: malicious instructions in sample PDFs 0412 (A)
      and 0413 (B) detected through real extraction, with page/line, agent attribution and MCP history scope (7d)
- [x] Human Approval (Approve / Reject / Request more information) recorded separately from the automated decision,
      with reviewer and timestamp, shown in the Audit Trail
- [x] Synthetic sample PDFs (real PDF 1.4) + separate ground truth; PDF upload with pypdf, page/line provenance,
      missing-evidence reporting; uploaded content runs through the unchanged integration (`test_dashboard_e2e.py`)
- [x] Real health checks → RUNNING / STOPPED / UNHEALTHY / NOT CONFIGURED (Gemini)
- [x] Launcher (`python -m claimsleuth.dashboard`) starts/stops MCP + CrewAI services, per-process logs
- [x] Six security scenarios on the real code (now backend tests only); MCP history access limited to the claim
      under investigation
- [x] Execution-mode transparency: test-double labels, the failed qwen2.5:3b run with tokens/latency
- [x] Sequential (ADK SequentialAgent) and parallel (asyncio.gather ADK ‖ A2A) execution, measured per run
- [x] All money in INR (₹, Indian digit grouping): dataset, sample PDFs, settlement schema, rules, adjudicator,
      reports and page; human-approval threshold ₹15,000 (Phase 7c)
- [ ] The 11-section dashboard of the original brief: Blackboard, communication modes, five architectures,
      heat maps and evaluation sections are not built (their back ends do not exist yet)
- [ ] Callback-based security validation (deliberately out of scope)

## Phase 8 — Evaluation (≥ 40 runs)
- [ ] Resumable harness: 5 architectures × 4 personas × 2 repetitions (C-10)
- [ ] CSV/JSONL, accuracy, latency boxplots, cost, agent latency/error/evaluation heat maps (C-5, C-10)

## Phase 9 — Cloud deployment (only after explicit approval)
- [ ] One platform, simple deployment, no exposed credentials (C-11)

---

## Project structure (target)

```
Claimsleuth_simple/
  pyproject.toml              shared package "claimsleuth" (installed in both venvs)
  requirements/               pinned requirement files per environment
  src/claimsleuth/
    config.py                 paths and fixed business limits
    schemas.py                all Pydantic models (shared contract)
    rules.py                  deterministic date / serial / exclusion / money functions
    documents.py              line-numbered document parsing → EvidenceRef
    security.py               prompt-injection and abusive-language screen
    history.py                SQLite claim-history access
    datastore.py              load claims, documents, policies, ground truth
    baseline.py               DETERMINISTIC BASELINE engine + CLI
    render.py                 Markdown rendering of results
    dataset.py                deterministic synthetic dataset builder
  services/                   (later phases) mcp_server/, crew_service/, adk_service/, dashboard/
  data/                       policies.json, claims/<id>/, ground_truth.json, claimsleuth.db
  runtime/                    generated reports, logs, run records (not source)
  eval/                       (phase 8) harness, results, charts
  tests/
  docs/                       CHECKLIST.md, BUILD_LOG.md
```

Environments: `.venv` (dashboard, ADK, MCP server) and `.venv-crew` (CrewAI service). They cannot be merged:
`crewai 1.15.33` requires `opentelemetry-api>=1.45.1` and `mcp<2`; `google-adk 2.13.0` requires `opentelemetry-api<=1.44`.
