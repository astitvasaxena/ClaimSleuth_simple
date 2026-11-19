# ClaimSleuth Simple — implementation checklist

Status legend: `[ ]` not started · `[~]` in progress · `[x]` done **with runtime evidence** (recorded in `docs/BUILD_LOG.md`).
A box is ticked only after the listed check has actually been run.

Teacher criteria (C-1 … C-11) are from `ClaimSleuth_Handoff_Gap_Analysis_and_Build_Plan.md` §2.1. That file is not in this
repository.

## Teacher criteria: status on 2026-11-19 (updated from 2026-11-06)

| Criterion | Status | Evidence or gap |
|---|---|---|
| C-1 Context sharing; Blackboard mandatory; messaging architectures tried | **Met for the comparison actually built (2026-11-19)** | Blackboard vs direct message passing vs no sharing, 40 cases each, offline, macOS (`docs/submission/results/messaging_comparison.csv`). Accuracy identical (40/40) by construction; they differ in time, context shared (4,740 vs 2,370 vs 0 chars) and receipts. A pub/sub broadcast arm was NOT built |
| C-2 At least two MCP tools | **Met** | `warranty_policy_search`, `claim_history_lookup` on a separate MCP server |
| C-3 Structured output with Pydantic | **Met** | Validated reports; final report as JSON and Markdown |
| C-4 Design patterns, evaluated against others | **Partly met (2026-11-19)** | Sequential-only, parallel-only and the hierarchical manager compared on 40 cases each (`results/architecture_comparison.csv`): accuracy identical by construction; median 24.7 s, 12.2 s, 24.3 s (laptop, offline). Worker-node on/off, workflow-based and event-driven variants do not exist |
| C-5 Callback monitoring and logging; heat map | **Met** | ADK callbacks, CrewAI events, unified trace, heat maps in the evaluation tab |
| C-6 A2A between ADK and CrewAI | **Met** | a2a-sdk service and ADK `RemoteA2aAgent`, across two environments |
| C-7 Both CrewAI and ADK, no other framework | **Met** | No other agentic framework is imported in `src` or `tests` |
| C-8 Security and guardrails; prompt variance; personas | **Mostly met** | Screening, restricted-output checks in callbacks, injection tests, persona claim variants. A standard vs evidence-first ReAct prompt structure was run (E5, 2026-11-19; 0 of 58 paired decisions changed, expected because the offline doubles ignore wording); no live prompt-variance result |
| C-9 Observability, auditability, provenance | **Met** | Trace, audit trail, Blackboard provenance, page and line citations |
| C-10 Performance, cost and time over about 40 runs; boxplots | **Mostly met (2026-11-19)** | 236 offline runs (E3/E4/E5) plus the 40-case baseline `EV-20261119-141842-e46c4a` with latency box plots and heat map (`docs/submission/results/`), personas x prompt structures included. Offline cost INR 0. Live cost over 40 runs NOT measured: 4 live runs used 9, 6, 7, 7 requests, 40 would need about 240 to 360 |
| C-11 Deploy on a cloud platform | **Met (2026-11-19)** | AWS EC2 t4g.small, ap-south-1, Docker Compose + Caddy HTTPS; evidence in `docs/submission/DEPLOYED.txt` and `results/deploy_test.md`. Offline mode only; live mode not enabled |

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
- [x] **Live Gemini run**: `RUN-live-adk-cs003`, then the complete runs in `docs/EVIDENCE.md`
- Check: `python -m claimsleuth.adk_service --check-config`, then `python -m claimsleuth.adk_service CS-007`

## Phase 4 — CrewAI Forensic Agent (offline, test-double model) — A2A deferred to a later phase
- [x] Separate `.venv-crew` (crewai 1.15.33, mcp 1.30.0, opentelemetry-api 1.45.2, pydantic 2.12.5); `.venv` unchanged
- [x] Real CrewAI `Agent` / `Task(guardrail validating ForensicsAnalysis)` / `Crew(sequential)` / `kickoff()` (C-7)
- [x] CrewAI native MCP (`Agent(mcps=[MCPServerHTTP])`) calls `claim_history_lookup` on the MCP server; run id via header (C-2)
- [x] `ForensicsReport` validated; model / provider / execution_status set by code; no payout field (C-3)
- [x] Recording via `step_callback`, `task_callback` and the CrewAI event bus (tool and LLM call events) (C-5)
- [x] Offline only: labelled `ForensicTestDoubleLLM`; loopback-only network guard; `NOT_CONFIGURED` for any other LLM mode
- [x] Tests in `.venv-crew`: 19 passed; main `.venv`: 144 passed, CrewAI module skipped
- [x] Live LLM mode for CrewAI: `crew_service/gemini.py`, `RUN-live-crew-cs003`, then the complete runs in `docs/EVIDENCE.md`
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
- [~] Communication-mode comparison (C-1): blackboard vs direct vs no sharing measured 2026-11-19 (`docs/submission/results/messaging_comparison.csv`); pub/sub broadcast arm not built
- [~] Architectures: sequential, parallel and hierarchical are implemented in one design and compared 2026-11-19 on 40 cases each
      (`results/architecture_comparison.csv`; accuracy identical by construction, differs in time and context shared). Workflow,
      event-driven and worker-node on/off are not built (C-4)
- Check: `python -m claimsleuth.integration CS-003` (MCP :8102 and CrewAI A2A :8103 running)

## Phase 6 — Local LLM (6A audit, 6B ollama-local for CrewAI) · security and personas
- [x] 6A readiness audit: Ollama 0.40.13 running on 127.0.0.1, CPU only, qwen2.5:3b local; CrewAI's built-in Ollama provider
- [x] 6B `ollama-local` mode for the CrewAI Forensic Agent (`crew_service/ollama.py`): loopback-only endpoint, local-model
      preflight, cloud-name rejection, bounded timeouts/iterations/retries, no fallback, labelled qwen2.5:3b / ollama-local
- [x] Offline tests (incl. fake-Ollama server fixture): .venv-crew 56 passed; main .venv 188 passed
- [~] Live CS-003 run executed (genuine inference, 2 model calls, real tokens) — **FAILED**: Ollama ran the model with a
      4096-token context and truncated the prompt; the model also skipped the MCP tool. Needs a context-length decision.
- [ ] ADK on a local model (not started; ADK still Gemini or test double)
- [x] Guardrails in ADK callbacks and the CrewAI guardrail (`security.restricted_output`); tool-output validation on the MCP
      server; untrusted-document wrapping (C-8)
- [~] Personas: honest, vague, abusive and injection claim variants exist in the dataset. Prompt variants (ReAct or
      evidence-first) were not built

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
- [~] The 11-section dashboard of the original brief: Blackboard monitoring, heat maps and evaluation are built. Communication
      modes and five architectures are not
- [x] Callback-based security validation: restricted-output checks on agent reports, added with Phase 4.2

## Security & Adversarial Intelligence

- [x] Phase 4.1 Evidence tampering: structured `SecurityEvent`; each check reported on its own; one failed entry withholds
      every report of the run; signed status required; damaged rows readable; A2A needs a ContextRef on a registered run;
      controlled test on disposable databases (attacks A-J); Security & Trust section
- [x] Phase 5 Reliability: run markers and all-or-nothing writes; unfinished runs classified and recovered only by a new investigation;
      MCP record deadline; idempotency keys; final human review; approvals refused on failed integrity; request census at the SDK boundary
- [x] Dashboard simplification: investigation and outcome first; Execution Overview, Blackboard & Agent Coordination and
      Security & Trust as visible summaries; every detailed record kept under Advanced Diagnostics; warnings always visible;
      front-end files only
- [x] Phase 4.2 Prompt injection: screen extended and run on a normalised copy; delimiters cannot be closed or opened from
      inside; agent output checked for credentials, key material and instructions; VERIFIED facts bound to their line; guardrail
      feedback repeats no model text; `injection_events`; controlled test on disposable PDF copies with an obedient test double

## Phase 8 — Evaluation (≥ 40 runs)
- [~] Built instead of 5 architectures × 4 personas × 2 repetitions: 40 synthetic cases in five categories, four approaches,
      offline by default, live opt-in (C-10)
- [x] CSV, JSON and Markdown export, accuracy, latency box plots, heat maps, request counts (C-5, C-10). Live cost over 40 runs is
      not measured
- [x] 2026-11-19 (macOS laptop, offline, 0 Gemini requests): E3 architecture 3x40, E4 messaging 3x40, E5 persona x prompt structure 116 runs;
      236 runs, 0 failed; baseline rerun `EV-20261119-141842-e46c4a` 40/40; boxplot and agent-latency heat map in `docs/submission/results/`
      (`summary.json`, `README.md`). Accuracy identical by construction; E5 persona counts unbalanced (38/6/6/8); 0 of 58 paired
      decisions changed. Laptop latencies (about 12 s per ADK agent) are not the AWS VM figures (0.27 to 1.14 s per agent, about 3 s per claim)
- [ ] Live cost over 40 runs (needs a price table and about 240 to 360 live requests; free tier is 20 per day per model)

## Phase 9 — Cloud deployment
- [x] One platform, simple deployment, no exposed credentials (C-11). Done 2026-11-19: AWS EC2 t4g.small (ap-south-1), single image (911 MB) + Caddy, HTTPS via Let's Encrypt on sslip.io, basic auth. Verified over HTTPS: wrong password rejected (401), four sample claims APPROVE/ESCALATE/ESCALATE/REQUEST_EVIDENCE, site recovered by itself after stop/start (new IP, new hostname). Evidence: `docs/submission/DEPLOYED.txt`. Not done: live mode on the VM, Elastic IP

## Final hardening and submission (2026-11-06)
- [x] Documentation audit, stale claims corrected, `docs/DEMO_GUIDE.md` and `docs/EVIDENCE.md` added
- [x] Fresh-install check in new environments, from the files git would carry

---

## Project structure (target)

The original target, kept for the record. The current layout is in the README.

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
