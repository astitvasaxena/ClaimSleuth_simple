# ClaimSleuth: examiner's review (Agentic AI, CSE3101)

Reviewer role: Agentic AI professor acting as viva examiner. Date: 2026-11-19.
Scope: `README.md`, `docs/ARCHITECTURE_GUIDE.md`, `docs/EVIDENCE.md`, `docs/CHECKLIST.md`, `docs/submission/CONTRACT.md`, all of
`src/claimsleuth/` that carries the agent design, plus the untracked viva documents in `docs/` (because the report builder
reads them). Every claim was checked against code at the working tree of 2026-11-19 (HEAD `4ff2306`, plus uncommitted work
from the parallel agents: `src/claimsleuth/dashboard/__main__.py`, `src/claimsleuth/evaluation/experiments.py`, deploy files).

What I did not do: I did not re-run the test suites or the app (the code agent owns the venvs and was installing them).
Test and evaluation numbers below are quoted from `docs/EVIDENCE.md` (Windows, 2026-11-06) and are marked as such.
`docs/submission/results/` held only `STATUS.md` when this review was written, so no experiment number exists yet.

---

## (a) The system in one page

**Problem.** A customer files a consumer-electronics warranty claim as a PDF (claim form, invoice, repair report). The
system must extract the facts, check them against the policy and claim history, look for fraud signals and prompt
injection, and produce an auditable decision (APPROVE / REJECT / ESCALATE / REQUEST_EVIDENCE) with an INR payout.

**Agents (three, two frameworks).**
| Agent | Framework | Tools (MCP) | Output schema | Code |
|---|---|---|---|---|
| Evidence Agent | Google ADK `LlmAgent`, step 1 of a `SequentialAgent` | `warranty_policy_search` | `EvidenceReport` | `adk_service/agents.py:44-57,115-117` |
| Investigator Agent | Google ADK `LlmAgent`, step 2 | `warranty_policy_search`, `claim_history_lookup` | `InvestigationReport` | `adk_service/agents.py:59-77,118-121` |
| Forensic Agent | CrewAI: 1 `Agent`, 1 `Task`, 1 `Crew`, separate process behind an A2A service | `claim_history_lookup` | `ForensicsReport` | `crew_service/crew.py:141-164`, `crew_service/a2a_server.py:165-201` |

**Orchestration (hierarchical manager over sequential and parallel stages).** `orchestration/manager.py` is a
*deterministic* Level-1 manager (not an LLM). It builds a typed `ExecutionPlan` with five tasks and a validated state
machine (`orchestration/plan.py:26-31,38-65`), runs the deterministic baseline, dispatches the ADK workflow, and when the
Evidence report is published to the blackboard it releases the Investigator (`manager.py:161-172`) and, at the same time,
dispatches the Forensic Agent over A2A with a `ContextRef` (`manager.py:284-325`, `asyncio.gather` at `:325`). It then seals
the blackboard, verifies each report on it, and hands only verified reports to the adjudicator (`manager.py:343-375`).

**Shared state.** A SQLite (WAL) blackboard (`blackboard/store.py:1-28,154`), append-only by trigger, HMAC-signed per writer
principal (`blackboard/auth.py:29-33,110-128`), with per-section read permissions (`blackboard/models.py:32-36`) and a signed
receipt for every read (`store.py:360-434`). The Investigator gets the full verified Evidence report
(`blackboard/adk_plugin.py:75-92,121-129`); the Forensic Agent gets only its facts and missing-information list
(`blackboard/controller.py:109-116`, `crew_service/runner.py:300-309`) and may never read the Investigator's report.

**Protocols.** MCP server (official SDK, Streamable HTTP, `:8102`) with exactly two read-only tools, a per-call log and a
server-side scope rule (`mcp_server/server.py:84-106,178-206,227-270`). A2A 1.0 via a2a-sdk: Agent Card + JSON-RPC on `:8103`
(`a2a_server.py:100-116,204-227`); the ADK side calls it with `RemoteA2aAgent` (`adk_service/forensic_a2a.py:76-132`). The
message is a Pydantic `WorkflowMessage` (`schemas.py:719`, rules in `contract.py:77-115`).

**Decision authority.** The deterministic adjudicator (`integration/adjudicator.py:125-218`) recomputes the rules decision
itself. Agents can only *add caution*: a verified HIGH finding turns APPROVE or REQUEST_EVIDENCE into ESCALATE
(`adjudicator.py:163-178`). They can never approve, never lift an escalation, and never touch money (Decimal settlement,
`adjudicator.py:182`). REJECT, ESCALATE, incomplete reviews and payouts of ₹15,000 or more need a human (`adjudicator.py:196-197`,
`config.py:32`).

**Execution modes.** Default OFFLINE: the agents' "model" is a labelled rule-based test double (`adk_service/test_double.py`,
`crew_service/test_double.py`); everything around it is real. LIVE: `gemini-3.5-flash` for all three agents
(`execution_mode.py:28`), with a shared SQLite request ledger (RPM 5, RPD 20, `gemini_rate.py:31`) and one retry after HTTP 503.
Four complete live runs exist (EVIDENCE.md:77-82, 6 to 9 requests each). No live accuracy figure exists.

**Evidence (from EVIDENCE.md, 2026-11-06, Windows).** 801 passed (.venv), 155 passed (.venv-crew); 40-case offline benchmark
40/40 expected decisions, decided by deterministic rules 35 and guardrail escalation 5.

**State against the course.** Strong on C-2, C-3, C-6, C-7, C-9; implemented but not *compared* for C-1 and C-4; C-10 is an
offline benchmark only; C-11 (cloud) is not done. The single biggest academic weakness: in the default mode the agents are
scripted, ignore the shared context, and changed none of the 40 benchmark decisions.

---

## (b) How each criterion and each slide is satisfied

### Course criteria C-1 to C-11 (examiner's verdict, not the contract's)

| # | Criterion | Verdict | Evidence (file:line) | What an examiner will push on |
|---|---|---|---|---|
| C-1 | Blackboard / shared state; messaging architectures tried | **PARTIAL** | `blackboard/store.py:12-28,137,154,360-434`; `models.py:32-36`; `auth.py:29-33`; `adk_plugin.py:121-129`; `crew_service/runner.py:300-309`; `manager.py:174-185,343-357` | The comparison blackboard vs peer-to-peer vs broadcast is not in the repo yet (`evaluation/experiments.py` is being written, no results). Offline, the context cannot change any output (see Q4), so a comparison can only report time, calls and bytes, never quality |
| C-2 | ≥ 2 MCP tools | **MET** | `mcp_server/server.py:227-270`; ADK `McpToolset` `adk_service/agents.py:91-96`; CrewAI `MCPServerHTTP` `crew_service/crew.py:152-154`; log cross-check `integration/adjudicator.py:38-50` | MCP has no authentication; calls without a claim-id header skip the scope rule (`server.py:87-88`). Policy search is keyword matching, not retrieval |
| C-3 | Pydantic structured output | **MET** | `schemas.py:24` (StrictModel), `:276`, `:289`, `:319`, `:334`, `:643`, `:719`; ADK `output_schema` `agents.py:99-105`; CrewAI guardrail validates `ForensicsAnalysis` `crew.py:113-136`; post-run re-validation `adk_service/runner.py:126-151,341-355` | Solid. Be ready to explain why `output_pydantic` is not used in CrewAI (`crew.py:9-11`) |
| C-4 | Sequential / parallel / hierarchical, evaluated against each other | **PARTIAL** | Sequential: `adk_service/agents.py:122`; parallel: `manager.py:284-325`; hierarchical: `manager.py:82-222`, `plan.py:26-118`; overlap test `tests/test_hierarchical_orchestration.py:201-206` | No comparison with numbers yet. Only "sequential" is framework-native; "parallel" is `asyncio.gather`, not ADK `ParallelAgent`; the "hierarchical manager" is deterministic code, not a manager agent |
| C-5 | Callbacks, monitoring, heat map | **MET, with a caveat** | ADK callbacks `adk_service/callbacks.py:97-104,185-295`; ADK plugin `blackboard/adk_plugin.py:116-148`; CrewAI event bus `crew_service/runner.py:122-205`; unified trace `observability/trace.py:357-398`; metrics `observability/metrics.py:76-152`; heat maps `evaluation/report.py:102-144`, `dashboard/static/eval.js:156-172` | The heat maps are evaluation-metric tables (approach × metric, scenario × approach, reliability). There is no per-agent runtime heat map (agent × phase latency) built from callback data. An examiner may ask for exactly that |
| C-6 | A2A between ADK and CrewAI | **MET** | `crew_service/a2a_server.py:100-116,165-227`; `adk_service/forensic_a2a.py:76-132`; `contract.py:42-115` | One A2A link only, and the caller is the manager's code through a `RemoteA2aAgent` proxy, not an LLM agent deciding to delegate. `/a2a` is unauthenticated |
| C-7 | Only CrewAI and ADK | **MET** | `requirements/main.txt` (google-adk 2.13.0), `requirements/crew.txt` (crewai 1.15.33); no other agent framework imported in `src/` (grep for langchain/autogen/llama_index/semantic_kernel/litellm: none) | CrewAI is used for one agent with one task; expect "why CrewAI at all?" |
| C-8 | Security, guardrails, callbacks; prompt variance; personas | **PARTIAL** | Screen `security.py:25-74,114-146`; untrusted wrapping `security.py:148-168`, `claim_text.py:25-32`; output check `security.py:171` used in `adk_service/callbacks.py:138-141` and `crew_service/crew.py:113-118`; MCP scope `server.py:84-106`; blackboard integrity `store.py:360-434`; network guard `crew_service/network_guard.py:25-68`; budget gate in `before_model` `callbacks.py:185-195`; obedient double `obedient_double.py`; personas `dataset.py:143-176` | Personas exist (honest, `-VAG`, `-ABU`, `-INJ` on CS-001/003/004). Prompt-structure variants (ReAct vs standard) are not built. The screen is a regex list, and offline the "agents" detect injection with the same regex (Q3) |
| C-9 | Observability, auditability, provenance | **MET** | `observability/trace.py:63-398`; `schemas.py:828` (TraceEvent); audit `integration/run.py:168-175`; per-run files `integration/run.py:199`; page/line provenance `dashboard/pdf_intake.py:273-276`; MCP log `server.py:178-206`; A2A log `a2a_server.py:119-136` | Strong. Monitoring never decides (`manager.py:118-124,428-447`) |
| C-10 | 40+ runs, cost, time, box plot | **PARTIAL** | `evaluation/runner.py`, `evaluation/scoring.py`, `evaluation/report.py:146-160`; 40 cases, 8 per category (`evaluation/dataset.py:44-51`); EVIDENCE.md:56-70 | Offline only, each case once, so no run-to-run variance; no live cost (no price table ships, `observability/pricing.py:1-10`); 4 live runs only |
| C-11 | Cloud deployment | **NOT MET** | Deploy artefacts appearing in the working tree (`Dockerfile`, `Dockerfile.crew`, `docker-compose.yml`, `deploy/`), nothing deployed | Course rule: nothing may run from a local machine during the demo. This is the blocker |

### PPT slides 0 to 14: what in the repo supports each one

| Slide | Content required | Supported by | Status |
|---|---|---|---|
| 0 | Title, names, enrollments | `docs/submission/TEAM.json` | **Missing**: names are placeholders |
| 1 | Objectives / problem | `README.md:1-24`; `docs/ARCHITECTURE_GUIDE.md:34-104` | Ready |
| 2 | Literature review in one table + gap | Nothing in the repo | **Missing**: needs verifiable sources (report agent) |
| 3 | Agent requirements mapped to gaps (R-numbers) | Agent roles only: `adk_service/agents.py:44-77`, `crew_service/crew.py:39-61`, adjudicator rules `integration/adjudicator.py:3-19` | **Missing**: no R1..Rn list exists |
| 4 | MAS architecture diagram, orchestration, name | `manager.py:1-19`; `plan.py:3-13`; `docs/ARCHITECTURE_GUIDE.md:110-156,259-282` | Ready. Name it "deterministic hierarchical manager with a sequential stage (Evidence) then a parallel stage (Investigator ‖ Forensic)". Show that Forensic waits for Evidence (`manager.py:286-298`) |
| 5 | Architecture evaluation with numbers | `evaluation/experiments.py` (in progress), `results/architecture_comparison.csv` (absent) | **Pending**. Accuracy is identical by construction offline; only time / calls / context can differ |
| 6 | Agent-tool communication via MCP | `mcp_server/server.py:84-106,178-270`; `agents.py:80-96`; `crew.py:152-154`; `adjudicator.py:38-50`; diagram `docs/ARCHITECTURE_GUIDE.md:644-658` | Ready |
| 7 | Messaging: blackboard vs P2P; how many agents use A2A | `models.py:32-36`; `store.py:360-434`; `forensic_a2a.py:92`; `results/messaging_comparison.csv` (absent) | Partly ready. A2A count: **one** agent (Forensic) is served over A2A; the client is the manager via a non-LLM `RemoteA2aAgent` |
| 8 | Securing the MAS | See C-8 row | Ready (state the regex limit) |
| 9 | Experimental design | `evaluation/dataset.py:1-51`; 40 cases (22 new, 18 reused, of which 9 persona variants: 3 VAG, 3 ABU, 3 INJ; from `data/evaluation/eval_dataset_v1.json`); expected decisions 16 ESCALATE, 8 REJECT, 8 APPROVE, 8 REQUEST_EVIDENCE (same file) | Partly ready. One run per case; no repeats; no live set |
| 10 | Validation; system and per-agent response time; ground truth vs LLM-as-judge | Reference ground truth, not LLM-as-judge (`evaluation/dataset.py:6-14`, `evaluation/scoring.py:1-13`); per-agent latency from `observability/metrics.py:76-129` and eval box plots `evaluation/report.py:146-153` | **Numbers pending** (`results/summary.json` absent). State "reference labels written by the developers" |
| 11 | Team responsibility matrix | Nothing in the repo | **Missing** |
| 12 | Demo screenshots | `docs/screenshots/1-5*.png` (1 Nov 2026, offline, pre-simplification per `README.md:28-29`) | Partly ready. No live-run screenshot and no deployed-system screenshot exist |
| 13 | Conclusions | `README.md:435-466` (limitations) | Ready |
| 14 | References | Framework names only (`README.md:482-485`) | **Missing**: real citations needed |

---

## (c) Documents that contradict the code

Severity: CRITICAL = invented measurement; HIGH = wrong architecture claim that a reader would repeat in the viva;
MEDIUM = misleading detail; LOW = nuance.

| # | Document:line | What it says | What the code does | Severity |
|---|---|---|---|---|
| 1 | `docs/MASTER_VIVA_QA_COMPREHENSIVE.md:535-545` | A latency heat map with values (e.g. "LLM Inference 1850 ms / 2450 ms / 2100 ms"), labelled "Illustrative" | No such measurement exists in the repo or EVIDENCE.md. Offline doubles report no tokens (`adk_service/test_double.py:7`) | **CRITICAL** |
| 2 | `MASTER_VIVA_QA_COMPREHENSIVE.md:614-617,627` | "40+ runs": baseline ~5 ms, MCP 80-150 ms, run 2.5-3.2 s, ~3,200 input tokens, "2200 ± 400 ms" | No source. EVIDENCE.md has no latency or token figures | **CRITICAL** |
| 3 | `MASTER_VIVA_QA_COMPREHENSIVE.md:619-623` | A ReAct vs Plan-and-Execute vs Sequential comparison; "Sequential reduced tokens by 28%" | No such experiment was built (`docs/CHECKLIST.md:97-98`: prompt variants not built) | **CRITICAL** |
| 4 | `MASTER_VIVA_QA_COMPREHENSIVE.md:399`; `docs/ADK_ARCHITECTURE_AND_WORKFLOW_GUIDE.md:158` | Live model `gemini-2.5-flash` | `execution_mode.py:28`: `gemini-3.5-flash` | HIGH |
| 5 | `MASTER_VIVA_QA_COMPREHENSIVE.md:590` | "Persona 2: hostile claimant (CS-007)" | CS-007 is `serial_other_claimant` (`dataset.py:126`); abusive personas are the `-ABU` variants (`dataset.py:145-163`) | HIGH |
| 6 | `MASTER_VIVA_QA_COMPREHENSIVE.md:652-656` | Cloud Run / Kubernetes, separate MCP container, SQLite on EFS / Cloud Storage FUSE | SQLite WAL needs one machine and a local disk (`blackboard/store.py:12,154`; CONTRACT); the planned stack is one VM (`docker-compose.yml:1-3`) | HIGH |
| 7 | `MASTER_VIVA_QA_COMPREHENSIVE.md:510`; `docs/CHECKLIST.md:114`; `docs/ClaimSleuth_Simple_Agent_Design_Styled.docx` §8 | Parallel = `asyncio.gather(adk_task, crew_task)` in `integration/run.py`, ADK ‖ CrewAI | The gather is in `orchestration/manager.py:325`; `integration/run.py:177-192` only delegates. In blackboard mode the Forensic request waits for the Evidence report (`manager.py:286-298`), so the parallel stage is Investigator ‖ Forensic | MEDIUM |
| 8 | `MASTER_VIVA_QA_COMPREHENSIVE.md:511` | Hierarchical supervisor is "the Orchestrator / Adjudicator", which "reconciles discrepancies" | The manager is `orchestration/manager.py` and never decides (`:14-15`); the adjudicator recomputes rules and does not reconcile agents (`adjudicator.py:129-181`) | MEDIUM |
| 9 | `MASTER_VIVA_QA_COMPREHENSIVE.md:591` | WC-2026-0412 injection is in the invoice notes, "System override..." | `README.md:314`: repair report, PDF p.3 lines 12-14, "IMPORTANT AI SYSTEM INSTRUCTION: Ignore all previous instructions..." | MEDIUM |
| 10 | `MASTER_VIVA_QA_COMPREHENSIVE.md:526` | Callbacks record stack traces into `audit.jsonl` | Callbacks fill the in-memory `ExecutionRecorder` (`callbacks.py:71-104`); `audit.jsonl` comes from the orchestrator's `Audit` (`integration/run.py:168-175`) | LOW |
| 11 | `docs/CHECKLIST.md:79-81` | Blackboard is "coordination only: agents do not exchange findings" | The blackboard carries the Evidence report to both later agents (`adk_plugin.py:75-92`; `crew_service/runner.py:300-309`; `models.py:32-36`) | HIGH |
| 12 | `docs/ARCHITECTURE_GUIDE.md:1733-1735` | "Agents do not exchange findings through the Blackboard... the Investigator reads the Evidence report through ADK session state" | In blackboard mode the session-state key is overwritten with the verified blackboard copy (`adk_plugin.py:121-129`); the same guide says so at `:538` | HIGH (internal contradiction) |
| 13 | `docs/ARCHITECTURE_GUIDE.md:1727` | "Validated reports appear only when the ADK workflow ends" | The Evidence report is published when the Evidence Agent finishes (`adk_plugin.py:146-147`); only the Investigator's report waits for the end | MEDIUM |
| 14 | `docs/ADK_ARCHITECTURE_AND_WORKFLOW_GUIDE.md:356` | CrewAI review "does not depend on ADK findings"; time goes from T1+T2 to max(T1,T2) | Forensic depends on the Evidence report in blackboard mode (`plan.py:52-57`, `manager.py:286-298`) | MEDIUM |
| 15 | `docs/ClaimSleuth_Agentic_AI_Design_and_Viva_Answers.docx` Q14-Q15 (status of 2 Nov) | Blackboard is coordination only; "Successful live Gemini reasoning has not been verified"; ADK reports published after the workflow | Superseded: four live runs (EVIDENCE.md:77-82); Evidence report published mid-run (`adk_plugin.py:146-147`) | HIGH (the report agent's brief tells it to read this file) |
| 16 | `docs/ClaimSleuth_Simple_Agent_Design_Styled.docx` §8, §9, Criterion 1 | "A dedicated hierarchical manager-worker pattern has not been established"; "CrewAI step/task callbacks have not been demonstrated"; Evidence→Investigator via session state, not blackboard | `orchestration/manager.py` exists; `crew_service/runner.py:122-130` registers `step_callback` and `task_callback`; `adk_plugin.py:121-129` | HIGH (stale) |
| 17 | `docs/ARCHITECTURE_GUIDE.md:564` | CrewAI guardrail retries "up to 2 times" | 2 in offline (`crew_service/crew.py:37`), 1 in live (`crew_service/gemini.py:49,73`); the guide says 1 at `:391` | LOW (say "per mode") |
| 18 | `README.md:190` | `integration/`: "run orchestration (baseline ‖ ADK ‖ A2A)" | Baseline runs first and synchronously (`manager.py:227-236`); orchestration lives in `orchestration/manager.py` | LOW |
| 19 | `src/claimsleuth/adk_service/test_double.py:7-8` vs `README.md:40` | Docstring: "the default ADK mode still requires Gemini credentials"; README: offline is the default | Both true in different paths: standalone `python -m claimsleuth.adk_service` with `EXECUTION_MODE` unset defaults ADK to Gemini (`execution_mode.py:83-89`), while the manager forces the test double (`manager.py:193-194`) | LOW (know it) |
| 20 | `README.md:252` | "All of them bind to 127.0.0.1" | Still the default, but the working tree now adds an opt-in `--host` / `CLAIMSLEUTH_HOST` (`dashboard/__main__.py:47`, deploy agent) | LOW (update when deploy lands) |
| 21 | `presentation/ClaimSleuth_Simple_Presentation.pptx` | 301/64 tests, live not verified, blackboard coordination only | Already flagged stale by `docs/EVIDENCE.md:122-126` | Known |

**Recommendation.** Do not submit, quote or rehearse from `MASTER_VIVA_QA_COMPREHENSIVE.md`,
`ADK_ARCHITECTURE_AND_WORKFLOW_GUIDE.md` or the two `.docx` files until rows 1-10 and 14-16 are fixed. Rows 1-3 are
invented numbers, which the contract forbids, and an examiner who asks "show me where 28% came from" ends the discussion.

---

## (d) The 25 hardest viva questions, with short honest answers

1. **"40 of 40 correct. Whose accuracy is that?"**
   The deterministic rules engine's. The adjudicator recomputes the decision itself (`adjudicator.py:129`); agents can only
   escalate an APPROVE or REQUEST_EVIDENCE with a verified HIGH finding (`:163-178`). In the benchmark every decision was made by
   DETERMINISTIC_RULES (35) or GUARDRAIL_ESCALATION (5) (EVIDENCE.md:66), so the agents changed none. And offline the agents
   are test doubles. The number validates the pipeline and the rules, not agent reasoning.

2. **"Then what do your agents add? Delete them and nothing changes."**
   On this benchmark, nothing changes, and we say so. By design they add cited fact extraction, an explanation trail and an
   independent check that can only add caution. In live mode they can raise concerns the rules do not encode (a verified HIGH
   finding forces ESCALATE). We have no measured case that shows this yet.

3. **"Are your agents agents? What does the test double actually do?"**
   Offline, no. `OfflineAdkLlm` and `ForensicTestDoubleLLM` follow fixed rules and a fixed tool plan (`adk_service/test_double.py:121-128`,
   `crew_service/test_double.py:202-210`). They detect injection with the same regex the baseline uses
   (`adk_service/test_double.py:27,70-78`; `crew_service/test_double.py:113-121`). What is real: ADK Runner/SequentialAgent/McpToolset,
   CrewAI Crew.kickoff and guardrail, A2A, MCP, blackboard, adjudicator. Real reasoning exists only in the 4 live runs.

4. **"Does the blackboard context change anything an agent does?"**
   Offline, no. The doubles parse only the claim-form JSON and `doc#Ln:` lines (`packet.py:13-15,53-66`). The Investigator's
   Evidence report goes into its instruction (`agents.py:62-63`), which `OfflineAdkLlm` never reads (it reads only
   `llm_request.contents`, `test_double.py:101-108`). The Forensic facts block is compact JSON (`controller.py:113-116`) that the
   packet parser ignores. Blackboard on or off gives identical offline outputs; only a live model can use the context.

5. **"Is this really a blackboard architecture?"**
   It is a shared, signed, append-only store with read permissions and receipts that carries intermediate context. Control is
   a fixed task graph in the manager, not a control component that picks knowledge sources by watching the board. Call it
   "blackboard as shared state and context channel".

6. **"Where is the manager agent in your hierarchical pattern?"**
   It is deterministic code (`manager.py:1-15`), not an LLM. We chose that so that no model can reorder, skip or approve work.
   We did not use CrewAI's hierarchical process or an ADK coordinator `LlmAgent`. If the course means "an LLM supervisor that
   delegates", ours is the deterministic variant.

7. **"Which of your three patterns is native to a framework?"**
   Only sequential (`SequentialAgent`, `agents.py:122`). Parallel is `asyncio.gather` over the ADK workflow and the A2A call
   (`manager.py:325`), not `ParallelAgent`. Hierarchical is our own plan and state machine (`plan.py:26-118`).

8. **"Prove the Investigator and Forensic Agent run at the same time."**
   `tests/test_hierarchical_orchestration.py:201-206` asserts overlap; in live run `RUN-dash-bcdbabaf97` both began at 08:53:32 UTC
   (EVIDENCE.md:85). Limits: the CrewAI service serialises forensic runs (`a2a_server.py:187`) and the dashboard runs one
   investigation at a time (`README.md:458`).

9. **"Why A2A between two of your own processes? How many agents use it?"**
   The two frameworks cannot share an environment (opentelemetry-api and mcp pins). A2A gives a discoverable Agent Card and a
   standard JSON-RPC exchange. One agent (Forensic) is served over A2A; the client is the manager through a non-LLM
   `RemoteA2aAgent` (`forensic_a2a.py:92`). The Evidence and Investigator agents do not use A2A. The endpoint has no auth.

10. **"Your A2A request carries no documents. How does CrewAI know the claim?"**
    It loads its own copy from its data directory and refuses if the SHA-256 digests differ (`contract.py:77-95`,
    `a2a_server.py:179-181`). That means both services share the filesystem: another single-machine coupling.

11. **"Why SQLite? What breaks when you move to the cloud?"**
    WAL and `BEGIN IMMEDIATE` give atomic writes across two processes with only the standard library (`store.py:12,137,154`).
    It needs one host and a local disk, and the Gemini ledger is SQLite too (`gemini_rate.py`). So: one VM, one compose stack, a
    shared volume; no multi-replica, no Cloud Run multi-service. Scaling out means Postgres or Redis.

12. **"Can a manipulated agent read another customer's history?"**
    Not through an investigation call: the MCP server checks claimant and serials against the claim named in a header set by
    framework code, not by the model (`server.py:84-106`; `agents.py:80-88`; `crew.py:152-153`). Weaknesses: a call without the
    claim-id header is treated as an operator call with no scope (`server.py:87-88`), and the MCP server has no authentication;
    we rely on loopback / the compose network.

13. **"What actually stops prompt injection?"**
    Five layers: a regex screen in the baseline that makes the rules ESCALATE (`baseline.py:68-74`, `adjudicator.py:149,158-159`);
    `<untrusted>` wrapping whose delimiters cannot be closed from inside (`security.py:148-168`); an output check on every report
    (`security.py:171`, used in `callbacks.py:138-141` and `crew.py:113-118`); the MCP scope rule; and an adjudicator no agent can
    make approve. The screen is a pattern list and misses paraphrases. Our controlled test uses an "obedient" double
    (`obedient_double.py`); it shows the safeguards around a compromised agent, not how Gemini resists.

14. **"Your callbacks only log. Where do they enforce anything?"**
    `before_model` takes a request-ledger slot and refuses when the budget is gone (`callbacks.py:185-195`), which is a cost
    guardrail. The restricted-output refusal and CrewAI's task guardrail enforce. The ADK `before_model` callback does not screen
    or rewrite prompts, and we should not claim it does.

15. **"What does one claim cost?"**
    Live: 6 to 9 Gemini requests per complete run (EVIDENCE.md:77-82). Tokens are stored only when the provider reports them.
    We ship no price table, so cost shows as "unavailable" (`observability/pricing.py:1-10`). On the free tier nothing was billed.
    We have no 40-run live cost figure.

16. **"C-10 asks for variance over 40+ runs. What variance do you have?"**
    Across cases only. Offline is deterministic, so each case ran once and repeats would be identical. No live variance (4 runs on
    2 claims). ReAct vs standard prompts were not built. Personas are 9 of the 40 cases (3 vague, 3 abusive, 3 injection).

17. **"Did you use LLM-as-judge?"**
    No. Each case has a reference label written by hand (`evaluation/dataset.py:6-14`), scored by exact match
    (`evaluation/scoring.py`). Weakness: the same team wrote the rules and the labels, so agreement is expected.

18. **"In live mode the Evidence Agent invents one quote. What happens?"**
    `check_evidence` rejects the whole report (`adk_service/runner.py:126-151`), nothing is published, Investigator and Forensic
    become BLOCKED (`manager.py:174-185`), review status NONE, a human decides. Fail-closed, but brittle: one bad quote loses all
    three agents, and ADK has no repair retry (CrewAI gets one guardrail retry in live mode).

19. **"Why does the Forensic Agent see less than the Investigator?"**
    Independence. The Forensic Agent gets facts and missing information only, not findings or the recommendation
    (`controller.py:109-116`), so it reaches its own view; it cannot read the Investigator's report (`models.py:32-36`). The
    Investigator gets the full Evidence report (`controller.py:103-106`). That asymmetry is a design choice we should defend
    openly.

20. **"How do you know the tool calls an agent reports really happened?"**
    The adjudicator accepts a history reference only if the agent's own recorded call returned it and the MCP server's log
    confirms that call for this run and claim (`adjudicator.py:38-50`; `manager.py:72-79`). Known gap: partial loss of MCP records is
    not flagged (EVIDENCE.md:110-111).

21. **"Is the human in the loop real?"**
    The review is recorded separately with reviewer and time and never changes the automated decision
    (`dashboard/reviews.py:103`). The reviewer's name is typed, not authenticated, and the review lock works in one process only
    (EVIDENCE.md:112-113).

22. **"Why `gemini-3.5-flash`, and is the output reproducible?"**
    It is ADK 2.13's default model, checked with one call (`execution_mode.py:28`). We set no temperature or seed in
    either path (`adk_service/config.py:31-36`, `crew_service/gemini.py:206-211`), so live reports vary; the decision does
    not, because it is deterministic.

23. **"Why CrewAI for one agent with one task?"**
    The course requires both frameworks, and we wanted a real cross-framework A2A boundary. We use CrewAI's native MCP, its task
    guardrail and event bus. It is a crew of one (`crew.py:162`), not a multi-agent crew.

24. **"Can I open the deployed system now?"**
    Not as of 2026-11-19. Containers and a one-VM compose design are being prepared (`docker-compose.yml`, `deploy/`); nothing is
    deployed. This is our open blocker.

25. **"Where is the evidence for the live runs and the test counts? Can you reproduce them here?"**
    The 4 live runs live in `runtime/`, which git does not carry (EVIDENCE.md:74-75); they are on the Windows machine only, and
    `runtime/` does not exist on this Mac. 801 + 155 tests are from Windows on 2026-11-06; macOS is untested (`README.md:201-202`).
    The browser flows and mutation checks ran from temporary scripts that are not in the repo (EVIDENCE.md:54).

---

## (e) What is still missing, ranked

1. **Cloud deployment (C-11): blocker.** Nothing is deployed. The course forbids running the demo locally. Needed: deploy the
   one-VM stack, run the smoke test, keep the URL and screenshots for slide 12. Watch the coupling points: blackboard and
   Gemini ledger on one SQLite volume, CrewAI service reading the same data directory, CrewAI's loopback network guard
   (`crew_service/network_guard.py:17,34`; the compose file answers it with `network_mode: "service:app"`).
2. **Evidence that the agents matter.** Agents changed 0 of 40 benchmark decisions and, offline, ignore shared context. At
   minimum: say this plainly on slides 9-10 and 13. Better: report a "rules only vs rules + agents" ablation honestly, and add a
   few cases where the rules approve but a planted signal the rules do not encode should escalate, run live on a small budget.
3. **The two comparisons (C-1 messaging, C-4 architecture) with numbers.** `evaluation/experiments.py` is in progress and
   `results/` is empty. Report time, agent/tool/message counts and context actually passed; state that offline accuracy is
   identical by construction.
4. **C-10 depth.** No live cost over runs, no repeated runs, no prompt-structure variants, unbalanced personas (9 of 40). Add a
   heuristic cost model from the request ledger (requests per run) and say clearly that no price table is used.
5. **Invented and stale claims in the team's own viva material** (section c, rows 1-16). Fix or withdraw before anyone reads
   them aloud; the report builder must not draw from them.
6. **Portable live evidence.** Copy the four live run folders (`final_report.json`, `trace.json`, request counts) from the
   Windows machine into `docs/submission/` and take one live-run screenshot. Today the only proof is a table in a document.
7. **Report and slide content that does not exist in the repo:** literature review table with verifiable sources (slide 2),
   requirements R1..Rn mapped to gaps (slide 3), team responsibility matrix (slide 11), team names (`TEAM.json`), references
   (slide 14).
8. **Security gaps an examiner can probe:** no auth on MCP and A2A; unscoped MCP calls without a claim header; typed reviewer
   name; regex-only injection screen; key files readable by the same OS user (`auth.py:14-16`); no ADK repair retry.
9. **Pattern justification.** The hierarchical manager is deterministic code; prepare a one-slide argument (safety,
   reproducibility, auditability) and a sentence on what an LLM supervisor would add and risk.
10. **Fresh verification on the submission machine.** Re-run both suites and the 40-case evaluation (code agent) and quote only
    those numbers with their date; until then quote EVIDENCE.md with "2026-11-06, Windows".
11. **Per-agent runtime heat map.** Optional but cheap: an agent × phase latency heat map from the trace data would answer C-5
    exactly as worded.
