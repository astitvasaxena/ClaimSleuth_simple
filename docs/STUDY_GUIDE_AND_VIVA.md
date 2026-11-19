# ClaimSleuth: Study Guide and Viva Preparation

For: Vaibhav Jain (enrolment 230697). Course: Agentic AI (CSE3101), BML Munjal University. Written 2026-11-20.
Status of facts: every number in this guide comes from `docs/EVIDENCE.md`, `docs/submission/results/`, `docs/submission/DEPLOYED.txt`, `docs/submission/CONTRACT.md`, or a file you can open. Line numbers (written `file:LINE`) were resolved against the files by a script on the day of writing; if a file is edited later they can drift, so re-find the function name if a line looks wrong.

---

# PART 0. How to use this guide

## 0.1 Reading order
1. Read Part 0 and Part 1 first (one evening). They give you the story and the vocabulary.
2. Read Part 4 (your own areas) slowly, twice. The professor will push hardest there.
3. Skim Part 2 and Part 5 (the other components) so you can explain them in one or two sentences each.
4. Part 3 is the criteria map (C-1 to C-11): learn which file and which results file prove each criterion.
5. Part 6 is the question bank (150+ questions). Cover the answer, say it aloud, then compare. Questions marked with a star are the ones most likely aimed at you.
6. Part 7 is limitations, the "never claim" list, short demo scripts, a cheat sheet and a glossary.
7. Part 8 is the presentation and live demo run-sheet. Rehearse it on the real site the day before.

## 0.2 Three rules for the viva
1. Say what is real and what is a test double. The default mode (offline) runs rule-based doubles, not an LLM. Never call that reasoning.
2. Never state a number you cannot point to (a results file, EVIDENCE.md, or a command you ran). "We did not measure that" is a good answer.
3. The agents recommend. The deterministic adjudicator decides, and every rupee comes from it.

## 0.3 The project in one paragraph
A customer uploads a consumer-electronics warranty claim as a PDF (claim form, invoice, repair report). Three AI agents look at it: an Evidence Agent and an Investigator Agent built with Google ADK, and a Forensic Agent built with CrewAI that is reached over the A2A protocol. They share context through a signed SQLite blackboard and use two MCP tools (policy search, claim history). A deterministic manager plans and supervises the run, and a deterministic adjudicator makes the final decision and computes the payout in rupees; a human reviews anything risky. By default the agents are labelled rule-based test doubles so the system is reproducible; in live mode they are Gemini (`gemini-3.5-flash`). The system runs on one AWS EC2 machine behind HTTPS and a password.

## 0.4 What is ClaimSleuth, in one sentence
ClaimSleuth is a multi-agent warranty-claim investigator in which ADK and CrewAI agents only recommend, and a deterministic adjudicator makes every decision and every rupee of the payout.

## 0.5 Where things are
- Code: `src/claimsleuth/` (about 16,600 lines of Python in 90 files, counted with `wc -l` on 2026-11-20).
- Tests: `tests/` (main suite) and `tests/crew/` (CrewAI suite, runs in the second venv).
- Deployment: `Dockerfile.single`, `docker-compose.yml`, `deploy/`, `.github/workflows/ci.yml`.
- Deliverables: `docs/submission/` (report builder, PPT builder, results).
- Part 8 (presentation and demo run-sheet) is at the end; read it before you rehearse.

## 0.6 About the team split
`docs/submission/TEAM.json` says the responsibility split is a PROPOSAL, to be confirmed by the team (field `matrix_note`). Git history shows one committer (Astitva). Say "proposed split" in the viva, never "agreed split", until the team confirms it.

---

# PART 1. Big picture and architecture from the basics

## 1.1 The running analogy: a small insurance office
Imagine a warranty-claims office with:
- a **front desk clerk** who scans the paper claim (PDF intake),
- three **junior investigators** with different habits: one lists the facts and cites page and line (Evidence Agent), one checks policy and history (Investigator Agent), one hunts for fraud smells (Forensic Agent),
- a **shared whiteboard with a lock and a sign-in sheet** where juniors pin their findings (the blackboard),
- a **library with two reference books** the juniors may consult (the two MCP tools),
- an **office manager** who decides who starts when and watches the clock but never judges a claim (the deterministic manager),
- a **cashier with a calculator and a rulebook** who alone decides pay or no pay and counts the money (the adjudicator),
- and a **senior supervisor** who signs off risky decisions (human review).
The juniors can be brilliant or sloppy; the cashier does not take orders from them. That is the design.

## 1.2 What is an agent? LLM vs agent vs multi-agent
- **LLM (large language model)**: a text predictor. You give it text, it returns text. It cannot act on its own.
- **Agent**: an LLM wrapped in a loop with a goal, instructions, and tools. It can decide to call a tool (search, database), read the result, and continue until it produces an answer. Analogy: a clerk who can open drawers, not only talk.
- **Multi-agent system**: several agents with different roles that exchange information. Reasons: different viewpoints (independent opinions), separation of duties, fault isolation (one failing does not erase the others). Costs: more calls, more latency, more failure modes.
- **Framework**: library that provides the loop, tool plumbing and sessions. We use exactly two: Google ADK and CrewAI (course rule C-7).

## 1.3 Why agents recommend and a deterministic adjudicator decides
Money and legal decisions must be reproducible and auditable. A language model can be manipulated by text hidden in a document (prompt injection) or simply be wrong. So:
- Agents produce structured reports with citations and a recommended outcome.
- The adjudicator (src/claimsleuth/integration/adjudicator.py:125) recomputes the rules decision itself, and can only let agents add caution: a verified HIGH finding turns APPROVE or REQUEST_EVIDENCE into ESCALATE. Agents can never approve, never lift an escalation and never touch money (Decimal arithmetic in src/claimsleuth/rules.py:112).
- REJECT, ESCALATE, an incomplete review, and any payout of 15,000 INR or more need a human (src/claimsleuth/config.py:32).
Consequence you must admit in the viva: on the 40-case benchmark the agents changed no decision (35 decided by rules, 5 by guardrail escalation, EVIDENCE.md). The benchmark validates the pipeline and rules, not agent reasoning.

## 1.4 Offline test doubles vs live Gemini (be honest)
| | Offline (default) | Live |
|---|---|---|
| Evidence and Investigator (ADK) | `OfflineAdkLlm`, a rule-based stand-in: src/claimsleuth/adk_service/test_double.py:90 | Gemini `gemini-3.5-flash` through ADK's own Gemini class |
| Forensic (CrewAI) | `ForensicTestDoubleLLM`: src/claimsleuth/crew_service/test_double.py:170 | Gemini through CrewAI's native Gemini provider |
| Needs | nothing | `GOOGLE_API_KEY` |
| What is real around it | ADK Runner/SequentialAgent/McpToolset, CrewAI Agent/Task/Crew, A2A, MCP, blackboard, rules, adjudicator | same |
The offline "agents" read the claim packet, follow a fixed tool plan (policy search, then history lookups) and write a report by fixed rules. They ignore the blackboard context and the prompt wording. The reports they write even say "Rule-based test double, not LLM reasoning." (src/claimsleuth/adk_service/test_double.py:205). Never call their output reasoning, intelligence or AI judgement. The model switch is `EXECUTION_MODE` (src/claimsleuth/execution_mode.py:24); live mode never falls back to a double (a failure marks that agent FAILED).

## 1.5 End-to-end flow of one claim
```
 PDF upload (browser, via Caddy HTTPS + password)
   |
   v
 pdf_intake.py (pypdf): claim form + invoice + repair report, each line keeps PDF page/line;
                        text screened by security.screen_text
   |
   v  "Run Investigation"  -> JobManager.start -> integration.run.run_claim_async
 InvestigationManager (deterministic, orchestration/manager.py)
   |  1. baseline rules (no LLM)
   |  2. Evidence Agent (ADK LlmAgent #1, MCP tool warranty_policy_search)
   |        -> report validated -> published to the blackboard (signed, ContextRef)
   |  3. in PARALLEL, once the Evidence report is published:
   |        Investigator Agent (ADK LlmAgent #2, reads Evidence report from the blackboard,
   |                             MCP tools: policy search + claim history)
   |        Forensic Agent (CrewAI, separate process, called over A2A with a ContextRef,
   |                             reads Evidence FACTS from the blackboard, MCP tool: claim history)
   |  4. controller seals the blackboard; every report is re-verified on it
   |  5. MCP server's own call log fetched (cross-check of the agents' tool claims)
   v
 Deterministic adjudicator: final decision + payout + needs-human flag
   |
   v
 Dashboard: report, trace, blackboard view; human review (Approve / Reject / Request More Information)
   recorded SEPARATELY, never changes the automated decision
```
Key facts with proof:
- The manager is deterministic code, not an LLM: src/claimsleuth/orchestration/manager.py:82; the parallel stage is `asyncio.gather` at src/claimsleuth/orchestration/manager.py:325.
- Sequential: ADK `SequentialAgent` at src/claimsleuth/adk_service/agents.py:122. Parallel is `asyncio.gather`, NOT ADK's `ParallelAgent`; hierarchical is our own plan/state machine (src/claimsleuth/orchestration/plan.py:38).
- The A2A caller is the manager through ADK's `RemoteA2aAgent`: src/claimsleuth/adk_service/forensic_a2a.py:92. Only one agent (Forensic) is served over A2A.

## 1.6 Diagram: who talks to whom
```
   Browser --HTTPS--> Caddy (443, basic auth) --> dashboard :7860 (container)
                                                    |
          +-----------------------------------------+--------------------------+
          |                                         |                          |
   baseline rules                          ADK workflow (in-process)    A2A client (RemoteA2aAgent)
   (rules.py/baseline.py)                  Evidence -> Investigator             |  JSON-RPC, A2A 1.0
                                                |   \                           v
                                                |    \ MCP (streamable HTTP)  CrewAI A2A service :8103
                                                |     \      :8102             Forensic Agent (Agent/Task/Crew)
                                                |      +--> MCP server <------------+
                                                |           warranty_policy_search, claim_history_lookup
                                                v
                      SQLite blackboard (WAL) <---- read/written by ADK side AND CrewAI service
                                                v
                                     Deterministic adjudicator
```
All inside ONE container on ONE VM: that is why a single machine is needed (SQLite WAL needs one host and a local disk).

## 1.7 Why two virtual environments
`google-adk 2.13.0` needs `opentelemetry-api<=1.44`; `crewai 1.15.33` needs `opentelemetry-api>=1.45.1` and `mcp<2`. They cannot be installed together, so there are `.venv` (ADK, MCP server, dashboard) and `.venv-crew` (CrewAI). They talk only over A2A and MCP (README, Architecture section; `requirements/main.txt`, `requirements/crew.txt`). In the Docker image both venvs live in `/app` (Dockerfile.single:15).

## 1.8 Vocabulary you will use in Part 2
- **MCP** (Model Context Protocol): a standard way for an agent to discover and call tools on a server.
- **A2A** (Agent2Agent): a standard way for one agent service to call another (Agent Card + JSON-RPC).
- **Blackboard**: shared workspace where agents post findings; ours is a signed, append-only SQLite store.
- **Provenance**: for every fact, which document, page and line it came from.
- **Guardrail**: a check that blocks or escalates unsafe input or output.
- **Fail closed**: when something cannot be verified, the system escalates to a human instead of guessing.

---

# PART 2. One chapter per component

Format of each chapter: plain explanation, analogy, where in the code, how it works, what the professor may ask, common traps.

## 2.1 Pydantic schemas (criterion C-3)

**Plain explanation.** Pydantic is a Python library that defines the exact shape of data (fields, types, allowed values) and rejects anything that does not fit. Every message in ClaimSleuth (claim, agent report, tool input and output, A2A message, final report) is a Pydantic model. Agents must return reports in these shapes ("structured output").

**Analogy.** A government form with boxes: if you write an essay in the "date" box, the clerk bounces the form.

| What | Where |
|---|---|
| Base class: unknown fields are forbidden (`extra="forbid"`) | src/claimsleuth/schemas.py:24 |
| Decision enum (APPROVE, REJECT, ESCALATE, REQUEST_EVIDENCE) | src/claimsleuth/schemas.py:30 |
| Evidence report (agent 1 output) | src/claimsleuth/schemas.py:276 |
| Investigation report (agent 2 output) | src/claimsleuth/schemas.py:289 |
| Forensics analysis / report (agent 3) | src/claimsleuth/schemas.py:319 |
| A2A message envelope | src/claimsleuth/schemas.py:719 |
| Final adjudicated report | src/claimsleuth/schemas.py:643 |
| Typed execution plan and task states | src/claimsleuth/schemas.py:806 |
| ADK uses it as `output_schema` | src/claimsleuth/adk_service/agents.py:103 |
| CrewAI guardrail validates `ForensicsAnalysis` | src/claimsleuth/crew_service/crew.py:120 |
| Post-run re-validation of ADK reports | src/claimsleuth/adk_service/runner.py:342 |

**How it works.** (1) ADK passes the schema to the model, which must finish by calling the internal `set_model_response` tool with matching JSON. (2) After the run the runner validates again (`check_evidence`, src/claimsleuth/adk_service/runner.py:126) and also checks every citation really quotes the cited line and every policy/history reference was actually returned by an MCP tool. (3) Money is `Decimal`, never float.

**Likely questions.** "Why strict models?" So a hallucinated extra field or wrong type is rejected, not silently accepted. "Why not CrewAI `output_pydantic`?" It is deliberately not set: it would add a large expanded schema to every prompt and trigger an extra converter model call; the guardrail does the validation (docstring src/claimsleuth/crew_service/crew.py:9).

**Traps.** Valid JSON is not true JSON: the schema proves shape, not that facts are correct. That is why citations are re-checked against the documents.

## 2.2 The blackboard (criterion C-1) [owner: Pulkit]

**Plain explanation.** A shared workspace where one agent publishes its validated report and others read it. Ours is a SQLite database in WAL mode, append-only (database triggers refuse UPDATE and DELETE), every entry signed with HMAC-SHA256 by an authenticated writer, with per-section read permissions and a signed receipt for every read.

**Analogy.** A whiteboard in a locked glass case: you can only add notes, each note is stamped with your badge, the sign-in sheet records who read what, and the cashier only reads the sealed final board.

| What | Where |
|---|---|
| Schema, append-only triggers | src/claimsleuth/blackboard/store.py:78 |
| WAL journal on every connection | src/claimsleuth/blackboard/store.py:154 |
| `class Blackboard` | src/claimsleuth/blackboard/store.py:119 |
| `consume()` : one atomic check-and-receipt | src/claimsleuth/blackboard/store.py:360 |
| Writer principals (orchestrator, adk, crewai) | src/claimsleuth/blackboard/auth.py:29 |
| HMAC signing | src/claimsleuth/blackboard/auth.py:110 |
| Read permissions (who may read which section) | src/claimsleuth/blackboard/models.py:32 |
| Run controller (register, seal, verified_inputs) | src/claimsleuth/blackboard/controller.py:361 |
| Agent-side participant | src/claimsleuth/blackboard/controller.py:197 |
| ADK plugin (publish Evidence, supply it to the Investigator) | src/claimsleuth/blackboard/adk_plugin.py:34 |
| What the Forensic Agent is shown (facts only) | src/claimsleuth/blackboard/controller.py:109 |
| Read-only monitoring summary | src/claimsleuth/blackboard/monitor.py:25 |
| Enabled by env var | src/claimsleuth/blackboard/store.py:45 |

**How it works, step by step.**
1. The manager registers the run with its claim and a fingerprint (hash) of the documents (`RunController.register`).
2. The Evidence Agent finishes; its report is validated and published to section `report:evidence_agent`; a `ContextRef` (run, section, version, hash) is returned.
3. The manager releases the Investigator, which consumes the report through `consume()`; the verified copy, wrapped as untrusted data, replaces the ADK session-state key `evidence_report`.
4. The A2A request to CrewAI carries only the `ContextRef` (a claim-check, not the content). The CrewAI service reads the Evidence FACTS from the blackboard with its own key.
5. Each agent publishes its own report. The controller seals the run. The adjudicator receives only reports it verified on the sealed board.
6. Any failed check raises, writes nothing, and the report is not used: fail closed, a human decides.

**Design choices to defend.** The Forensic Agent may NOT read the Investigator's report (src/claimsleuth/blackboard/models.py:34) so ADK and CrewAI stay two independent opinions. The Forensic Agent sees only the facts and the missing-information list, not the Evidence Agent's findings or recommendation.

**Likely questions.**
- "Is this really a blackboard architecture?" Honest answer: it is a shared, signed, append-only store that carries intermediate context. Control is a fixed task graph in the manager, not a control component choosing knowledge sources by watching the board. Call it "blackboard as shared state and context channel".
- "Does the context change what an offline agent does?" No: the doubles ignore it. Only a live model can use it.
- "Why SQLite? What if two users run at once?" WAL plus `BEGIN IMMEDIATE` give atomic writes across two processes with only the standard library; it needs one machine. The dashboard runs one investigation at a time (`JobBusyError` in src/claimsleuth/dashboard/jobs.py:35), so two simultaneous users get "busy", not corruption.
- "Can someone tamper?" Raw SQL edits are detected: signature or hash mismatch makes the read fail. Limitation: key files sit on disk readable by the same OS user (docstring of `auth.py`), so a malicious process running as that user could forge entries.

**Traps.** Do not say "the blackboard decides" or "agents negotiate through it". Do not say "the blackboard is coordination only": it carries the Evidence report to two later agents (the old CHECKLIST and design docs say otherwise; they are stale).

## 2.3 MCP server and the two tools (criterion C-2) [owner: Sibi]

**Plain explanation.** MCP is a protocol that lets agents discover and call tools served by a separate process. Our server exposes exactly two read-only tools over Streamable HTTP at `/mcp` on port 8102:
- `warranty_policy_search`: keyword search over `data/policies.json` (3 policies: POL-TECH-CARE-01, POL-LAPTOP-STD-02, POL-APPLIANCE-PLUS-03). Results quote stored terms with a JSON-pointer `source`.
- `claim_history_lookup`: records from the SQLite `claim_history` table, filters combined with AND.

**Analogy.** A reference library with two catalogues; the librarian writes every request in a log book and refuses to fetch another customer's file for a clerk assigned to your case.

| What | Where |
|---|---|
| Tool 1 | src/claimsleuth/mcp_server/server.py:227 |
| Tool 2 | src/claimsleuth/mcp_server/server.py:246 |
| Scope rule (claimant and serials must belong to the claim) | src/claimsleuth/mcp_server/server.py:84 |
| Per-call log middleware (run id, claim id, caller, status, duration) | src/claimsleuth/mcp_server/server.py:178 |
| `/health` and `/tool-calls` plain routes | src/claimsleuth/mcp_server/server.py:272 |
| ADK side: McpToolset with header provider | src/claimsleuth/adk_service/agents.py:91 |
| CrewAI side: MCPServerHTTP, tool filter | src/claimsleuth/crew_service/crew.py:4 |
| Adjudicator cross-checks agents' tool calls against the server log | src/claimsleuth/integration/adjudicator.py:38 |
| Search implementation | src/claimsleuth/policy_search.py:59 |

**How it works.** The framework (not the model) attaches headers with run id, claim id and caller (src/claimsleuth/adk_service/agents.py:80). The server middleware sets a context variable with the claim id; the history tool refuses a claimant id or serial that is not on that claim ("Out of scope"). The adjudicator accepts a history reference only if the agent's own recorded call returned it and the server's log confirms that call for this run and claim.

**Likely questions and honest limits.** "Is the MCP server authenticated?" No; it listens on loopback inside the container. "Can a manipulated agent read other customers?" Not through an investigation call because scope is checked server side from a header set by code; but a call WITHOUT a claim-id header is treated as an operator call without scope (src/claimsleuth/mcp_server/server.py:87). "Is policy search retrieval/RAG?" No: keyword matching over three policies.

## 2.4 Google ADK (agents.py, runner, callbacks, test double) [owner: Astitva / Sibi]

**Plain explanation.** ADK (Agent Development Kit) is Google's framework. We use: `LlmAgent` (an agent with a model, instruction, tools and output schema), `SequentialAgent` (runs sub-agents in order), `Runner` (executes an app), `InMemorySessionService` (per-run state), `McpToolset` (tools from an MCP server), callbacks and plugins.

| What | Where |
|---|---|
| Evidence and Investigator instructions | src/claimsleuth/adk_service/agents.py:44 and src/claimsleuth/adk_service/agents.py:59 |
| `LlmAgent` construction with schema and callbacks | src/claimsleuth/adk_service/agents.py:99 |
| `SequentialAgent([evidence, investigator])` | src/claimsleuth/adk_service/agents.py:108 |
| Runner, session, timeout, max LLM calls | src/claimsleuth/adk_service/runner.py:274 |
| Limits (30 calls offline, 10 live, 300 s / 600 s) | src/claimsleuth/adk_service/config.py:13 to src/claimsleuth/adk_service/config.py:16 |
| ExecutionRecorder: all 7 callbacks | src/claimsleuth/adk_service/callbacks.py:71 and src/claimsleuth/adk_service/callbacks.py:97 |
| before_model: takes a Gemini ledger slot (cost guard) | src/claimsleuth/adk_service/callbacks.py:191 |
| on_model_error: the one 503 retry | src/claimsleuth/adk_service/callbacks.py:253 |
| EvidenceIsolation plugin (direct mode wraps as untrusted) | src/claimsleuth/adk_service/callbacks.py:320 |
| Offline double | src/claimsleuth/adk_service/test_double.py:90 |
| Evidence required-tool check in live mode | src/claimsleuth/adk_service/runner.py:53 |

**How one ADK run works.**
1. Load the claim, check `/health` of the MCP server.
2. Create a session with state `run_id`, `claim_id`.
3. `Runner.run_async` runs Evidence then Investigator. Each calls MCP tools; callbacks record agent start/end, every model call (with tokens only when the provider reports them), every tool call.
4. Each agent ends with `set_model_response` (structured report); stored under its `output_key`.
5. The runner validates both reports, verifies references, and returns an `AgentSystemResult` (COMPLETED only if both validate).

**Callbacks: be precise.** The callbacks RECORD (build the execution record) and `before_model` ENFORCES a budget (refuses when the request ledger is exhausted). They do not screen or rewrite prompts. Enforcement of content rules happens elsewhere: the restricted-output refusal (`refuse_restricted`), CrewAI's guardrail, and the adjudicator.

**Likely questions.** "ReAct?" The Investigator instruction asks for all tool calls together in one turn; ADK loops model, tool, model. "What if the Evidence Agent invents a quote?" `check_evidence` rejects the report; nothing is published; Investigator and Forensic become BLOCKED; a human decides (fail-closed but brittle: one bad quote loses all three agents; ADK has no repair retry). "Temperature?" We set none, so live reports vary; the decision does not because it is deterministic.

**Traps.** The README says offline is the default, while the docstring in `test_double.py` says the default ADK mode requires Gemini credentials. Both are true in different paths: standalone `python -m claimsleuth.adk_service` with `EXECUTION_MODE` unset defaults ADK to Gemini, while the manager forces the double (src/claimsleuth/orchestration/manager.py:194).

## 2.5 CrewAI and the A2A protocol (criteria C-6, C-7) [A2A owner: Pulkit]

**CrewAI plain explanation.** CrewAI models work as an `Agent` (role, goal, backstory, LLM, tools), a `Task` (description, expected output, guardrail), and a `Crew` (agents plus tasks plus process). Ours is a crew of ONE agent with ONE task, run sequentially: the course requires both frameworks, and we wanted a real cross-framework boundary.

| What | Where |
|---|---|
| Agent / Task / Crew built together | src/claimsleuth/crew_service/crew.py:141 |
| Task output guardrail (schema, claim id, citations, history sources) | src/claimsleuth/crew_service/crew.py:99 |
| Guardrail retries: 2 offline | src/claimsleuth/crew_service/crew.py:37 |
| Live Gemini settings (1 guardrail retry, max 4 calls, 300 s budget) | src/claimsleuth/crew_service/gemini.py:43 |
| Budgeted Gemini LLM (one retry after 503, ledger slot per call) | src/claimsleuth/crew_service/gemini.py:117 |
| Runner: callbacks, event bus, blackboard consume | src/claimsleuth/crew_service/runner.py:221 |
| Network guard (loopback only, plus Gemini host in live) | src/claimsleuth/crew_service/network_guard.py:25 |
| Guardrail test harness (corrupted answers) | src/claimsleuth/crew_service/guardrail_harness.py:94 |
| Offline double | src/claimsleuth/crew_service/test_double.py:170 |

**A2A plain explanation.** A2A (Agent2Agent protocol 1.0, a2a-sdk 1.3.0) lets one agent service discover another through an **Agent Card** (a JSON business card at `/.well-known/agent-card.json`) and then send JSON-RPC messages to `/a2a`. Our CrewAI service is the A2A server; ADK's `RemoteA2aAgent` is the client.

| What | Where |
|---|---|
| Agent Card | src/claimsleuth/crew_service/a2a_server.py:100 |
| Executor: validate request, run crew, reply | src/claimsleuth/crew_service/a2a_server.py:165 |
| Runs serialised (CrewAI event bus is process-wide) | src/claimsleuth/crew_service/a2a_server.py:187 |
| Routes: card, /a2a, /health, /messages | src/claimsleuth/crew_service/a2a_server.py:225 |
| Client | src/claimsleuth/adk_service/forensic_a2a.py:76 |
| "Not sent" when no verified Evidence report | src/claimsleuth/adk_service/forensic_a2a.py:68 |
| Message contract (build/parse/check) | src/claimsleuth/contract.py:42 |
| Evidence digest check (SHA-256) | src/claimsleuth/contract.py:77 |
| Context required on registered runs | src/claimsleuth/contract.py:96 |

**How it works.** The manager builds a `WorkflowMessage` of type FORENSIC_REQUEST containing the claim id, a SHA-256 digest per document, and ContextRefs. The CrewAI service loads ITS OWN copy of the claim, refuses if the digests differ (`EVIDENCE_MISMATCH`), refuses a registered run without a context reference, runs the crew, and answers FORENSIC_RESPONSE or ERROR. The client validates the reply (same run, same claim, right direction).

**Questions.**
- "Why A2A between two of your own processes?" The frameworks cannot share a Python environment, and A2A gives a discoverable, standard interface. Honest: the caller is the manager's code through a non-LLM `RemoteA2aAgent`, not an LLM deciding to delegate; only ONE agent uses A2A.
- "Is /a2a authenticated?" No (loopback inside the container).
- "Does the request carry documents?" No, only digests; both services read the same data directory, another single-machine coupling.
- "Why does the offline Forensic Agent take about 0.5 s but ADK agents about 12 s on the laptop?" Measured (results/summary.json); the cause was not isolated. Do not invent one.

**Traps.** Guardrail retry count differs by mode: 2 offline, 1 live (`gemini.py` default). Say "per mode". A guardrail retry restarts the agent with fresh messages.

## 2.6 Orchestration: the manager and the three patterns (criterion C-4) [owner: Astitva]

**Plain explanation.** `InvestigationManager` is deterministic Python (not an LLM) that: builds a typed plan of five tasks (baseline, evidence, investigation, forensics, adjudication), moves each through a validated state machine (PENDING, READY, RUNNING, COMPLETED, FAILED, TIMED_OUT, BLOCKED), releases the Investigator only when the Evidence report is published, dispatches the Forensic request at the same moment, applies deadlines, seals the blackboard, and hands only verified reports to the adjudicator. It never judges a claim.

| What | Where |
|---|---|
| Manager class | src/claimsleuth/orchestration/manager.py:82 |
| Plan builder and task graph | src/claimsleuth/orchestration/plan.py:38 |
| State machine transitions | src/claimsleuth/orchestration/plan.py:26 and src/claimsleuth/orchestration/plan.py:95 |
| Investigator release gate | src/claimsleuth/orchestration/manager.py:161 |
| Evidence published -> both tasks READY | src/claimsleuth/orchestration/manager.py:174 |
| Parallel stage | src/claimsleuth/orchestration/manager.py:325 |
| Deadlines (component timeout + 60 s) | src/claimsleuth/orchestration/manager.py:64 |
| Reconcile MCP records (contradiction -> human) | src/claimsleuth/orchestration/manager.py:72 |
| Overlap test | tests/test_hierarchical_orchestration.py:201 |

**The three patterns, honestly.**
- Sequential: Evidence then Investigator inside one ADK `SequentialAgent` (framework native).
- Parallel: Investigator and Forensic run at the same time after Evidence is published, using `asyncio.gather` (not ADK `ParallelAgent`).
- Hierarchical: the manager (Level 1) over specialist agents (Level 2): assigns, gates, monitors, collects. It is deterministic code on purpose, so no model can reorder, skip or approve work. If the course means "an LLM supervisor that delegates", ours is the deterministic variant. We did not use CrewAI's hierarchical process or an ADK coordinator agent.

**Why is hierarchical not faster than sequential?** In E3 the medians are 24.7 s sequential and 24.3 s hierarchical (results/architecture_comparison.csv). The critical path is Evidence then Investigator (about 12 s each on the laptop doubles); the Forensic Agent is only about 0.5 s, so overlapping it saves almost nothing. Parallel-only is 12.2 s because it also starts Evidence and Investigator together, giving up the Evidence-to-Investigator context.

## 2.7 Deterministic rules and the adjudicator (criteria C-3, C-8) [owner: Astitva]

**Rules engine.** `baseline.evaluate` (src/claimsleuth/baseline.py:59) runs 12 named checks and a settlement: policy_known, required_documents, document_consistency, serial_format, serial_consistency, policy_consistency, claimant_consistency, inspection_after_incident, incident_after_purchase, coverage_window, exclusions, claim_history, settlement (names in the code between src/claimsleuth/baseline.py:76 and src/claimsleuth/baseline.py:227).

**Do not say "35 rules".** The phrase in CONTRACT.md "35 rules, 5 guardrail escalations" means: of the 40 benchmark cases, 35 were decided by `DETERMINISTIC_RULES` and 5 by `GUARDRAIL_ESCALATION` (EVIDENCE.md). There are 12 checks plus settlement.

**Decision precedence** (src/claimsleuth/baseline.py:227):
1. prompt injection in any untrusted text: ESCALATE;
2. evidence conflict or unknown policy (`CONFLICT_CHECKS`, src/claimsleuth/baseline.py:34): ESCALATE;
3. ineligibility (incident before purchase, outside coverage window, excluded cause): REJECT;
4. a required document or field missing: REQUEST_EVIDENCE;
5. otherwise APPROVE with a settlement.
**Settlement**: payout = min(max(cost - deductible, 0), limit) x (1 - depreciation), rounded half-up (src/claimsleuth/rules.py:112). Example from the stored live record: repair 18,900.00 minus deductible 2,500.00 = 16,400.00, within limit 80,000, depreciation 0 -> payout 16,400.00; it is at or above 15,000, so human sign-off is required.

**Adjudicator** (src/claimsleuth/integration/adjudicator.py:125):
- Recomputes the rules; if the baseline workflow result is missing or differs: ESCALATE (`BASELINE_UNAVAILABLE` / `INTEGRITY_ESCALATION`).
- Agent findings count only if verified: citations must quote the cited line; history references must be returned by the agent's own MCP call and confirmed by the server log (src/claimsleuth/integration/adjudicator.py:53).
- Deterministic ESCALATE or REJECT stand. Deterministic REQUEST_EVIDENCE becomes ESCALATE only on a verified HIGH concern. Deterministic APPROVE needs at least one completed agent review and no verified HIGH concern, else ESCALATE (`INSUFFICIENT_REVIEW` / `CONFLICT_ESCALATION`).
- An investigation without both agent reviews is INCOMPLETE: `review_status` PARTIAL or NONE, human approval required, whatever the decision (src/claimsleuth/integration/adjudicator.py:196).
- `decided_by` values you will see: DETERMINISTIC_RULES, GUARDRAIL_ESCALATION, CONFLICT_ESCALATION, INSUFFICIENT_REVIEW, INTEGRITY_ESCALATION, BASELINE_UNAVAILABLE.

**Likely questions.** "Whose accuracy is 40/40?" The rules engine's. "Then what do agents add?" On this benchmark nothing changes and we say so; by design they add cited fact extraction, an explanation trail and an independent check that can only add caution; we have no measured case showing a live agent changing a decision. "Why does an incomplete run still show APPROVE?" The decision never relies on a missing review; the review status and human approval flag carry the caution (see run RUN-dash-6d15294eca: APPROVE 16,400 INR, INCOMPLETE, human approval required).

## 2.8 Security (criterion C-8) [owner: Sibi]

**Layers (defence in depth).**
1. Input screen: a list of regex patterns for injection (src/claimsleuth/security.py:26) plus abusive-language patterns that are only logged (src/claimsleuth/security.py:80). The screen first normalises text (invisible characters, look-alike letters, spaced-out words): src/claimsleuth/security.py:96. A hit makes the rules ESCALATE.
2. Untrusted wrapping: claim text goes to models inside `<untrusted>` blocks whose delimiter cannot be closed from inside (src/claimsleuth/security.py:154, src/claimsleuth/security.py:148).
3. Output check: an agent report carrying credentials, key material or its own instructions is refused (src/claimsleuth/security.py:171), used in the ADK runner and the CrewAI guardrail.
4. MCP scope rule (2.3) so a manipulated agent cannot read other customers.
5. Blackboard integrity (signatures, receipts, read permissions) and verification before adjudication.
6. An adjudicator no agent can make approve.
7. Cost guard: Gemini request ledger (2.12).
**Controlled tests**: the evidence-integrity lab (tampers disposable databases) and the prompt-injection lab (plants synthetic instructions in disposable copies of the sample PDFs); the latter uses an "obedient" double (src/claimsleuth/obedient_double.py:1) that does what a document says, to show the safeguards around a compromised agent. They show how the safeguards behave, not how Gemini resists.
**Personas** (honest, vague `-VAG`, abusive `-ABU`, injection `-INJ`): built from CS-001, CS-003, CS-004 (`data/claims/CS-001-VAG` etc.); E5 uses them.
**Limits to state:** the screen is a pattern list and misses paraphrases; offline the "agents" detect injection with the same regex; MCP and A2A are unauthenticated; the reviewer name is typed, not authenticated; the review lock works within one process.

## 2.9 Observability (criteria C-5, C-9) [owner: Sibi; you support]

**Plain explanation.** After a run, one correlated trace is assembled from what each component recorded: manager task events, ADK callbacks, CrewAI event bus, the MCP server's log, A2A messages and blackboard entries with measured latencies. Durations, overlap, token counts (only when the provider reports them) and cost (only from a verified price table; none ships) come from it.

| What | Where |
|---|---|
| Trace builder | src/claimsleuth/observability/trace.py:63 |
| Collect trace | src/claimsleuth/observability/trace.py:390 |
| Run metrics (latency stats, model usage) | src/claimsleuth/observability/metrics.py:76 |
| Run outcome (BLOCKED, INCOMPLETE, ESCALATED, COMPLETED) | src/claimsleuth/observability/outcome.py:53 |
| Provider failure classification (503, 429, auth, timeout) | src/claimsleuth/observability/provider_errors.py:176 |
| Pricing: no prices shipped, cost None | src/claimsleuth/observability/pricing.py:3 |
| Page/line provenance from the PDF | src/claimsleuth/dashboard/pdf_intake.py:70 |
| Audit trail | src/claimsleuth/integration/run.py:167 |

**Heat maps (C-5).** There are two kinds: (a) dashboard heat maps in the Evaluation tab (comparative, scenario, reliability, src/claimsleuth/evaluation/report.py:112), which show evaluation metrics per approach, and (b) the file `docs/submission/results/heatmap_agent_latency.png` (agent by persona latency, built by `results/make_plots.py`). An examiner who wants a live per-agent runtime heat map from callback data will find the nearest thing in (b) and in the Execution Monitoring panel; say so plainly.
**Monitoring never decides** (`manager.py` records monitoring failures in `monitoring_errors` and never acts on them).

## 2.10 `execution_mode.py` and `config.py`
- src/claimsleuth/execution_mode.py:28: `gemini-3.5-flash`, ADK 2.13's default model, verified with one call.
- `forced()` context manager fixes a mode for one piece of work without changing the environment (used so an OFFLINE evaluation can run inside a LIVE dashboard): src/claimsleuth/execution_mode.py:41.
- `_resolve` refuses contradictory settings instead of silently picking one: src/claimsleuth/execution_mode.py:74.
- `config.py` has the paths (overridable by environment variables), `HUMAN_APPROVAL_THRESHOLD = 15000.00`, repeat-claim window 365 days and limit 2.

## 2.11 Dashboard (`app.py`, evaluation tab, `pdf_intake.py`) [owner: Pulkit]
Starlette app (Python ASGI web framework). One static page with two views: "Claim Investigation" and "Agent Evaluation & Performance Analytics" (src/claimsleuth/dashboard/static/index.html:25).
| Route | Purpose |
|---|---|
| `GET /api/status` | mode (LIVE/OFFLINE), services health: src/claimsleuth/dashboard/app.py:177 |
| `POST /api/upload` | PDF -> extraction preview: src/claimsleuth/dashboard/app.py:207 |
| `POST /api/runs` | start a real investigation: src/claimsleuth/dashboard/app.py:224 |
| `GET /api/runs/{id}` , `/blackboard`, `/trace` | report, blackboard monitor, trace |
| `POST /api/runs/{id}/review` | human review: src/claimsleuth/dashboard/app.py:402 |
| `/api/evidence-integrity`, `/api/prompt-injection` (+ `/run`) | the two labs |
| `/api/evaluation...` | evaluation home, estimate, run, results, exports (route table src/claimsleuth/dashboard/app.py:499) |
The dashboard has NO login of its own; on the VM Caddy provides basic auth (deploy/Caddyfile:4). It adds security headers (CSP, nosniff): src/claimsleuth/dashboard/app.py:62. Jobs: one run at a time, progress from real signals (src/claimsleuth/dashboard/jobs.py:65). PDF intake: pypdf text layer, limits 5 MB and 20 pages (src/claimsleuth/dashboard/pdf_intake.py:27); scanned PDFs are rejected; a missing field is reported missing, never guessed. Human review is stored in `human_review.jsonl`, never changes the automated result (src/claimsleuth/dashboard/reviews.py:103). Interrupted runs are classified, never resumed (src/claimsleuth/dashboard/recovery.py:6).

## 2.12 `gemini_rate.py`: the request ledger [you use this in cost answers]
Google counts requests per project and model, per minute (RPM) and per day (RPD, day starting at midnight Pacific time). Two agent processes run at once, so one SQLite ledger (`runtime/gemini_requests.sqlite3`) is shared.
- Defaults RPM 5, RPD 20: src/claimsleuth/gemini_rate.py:31.
- `acquire()` reserves a slot inside `BEGIN IMMEDIATE`; waits for the minute window; refuses (nothing sent) when the daily budget is used or the wait exceeds `max_wait_s`: src/claimsleuth/gemini_rate.py:126.
- One retry after HTTP 503, 15 s wait, takes its own slot: src/claimsleuth/gemini_rate.py:34.
- `run_requests(run_id)` gives an exact per-agent count: src/claimsleuth/gemini_rate.py:116.
- Env: `CLAIMSLEUTH_GEMINI_RPM`, `CLAIMSLEUTH_GEMINI_RPD` (`0` turns the daily check off); compose passes `GEMINI_RPD`/`GEMINI_RPM` (docker-compose.yml:19).
- The ledger only knows requests ClaimSleuth made from this machine; Google's real quota may be lower.
Quota math: a live claim costs 6 to 9 requests (the four complete runs used 9, 6, 7, 7; the partial VM run used 7), so 20/day allows about 2 to 3 live claims.

## 2.13 Evaluation: runner, experiments, dataset [you own this]
- Dataset: 40 synthetic cases, 8 per category (valid, missing_evidence, policy_violation, suspicious, conflict_injection), neutral ids EVAL-01..EVAL-40 in a fixed shuffled order (seed 20261102), expected decisions 16 ESCALATE, 8 REJECT, 8 APPROVE, 8 REQUEST_EVIDENCE (computed from `data/evaluation/eval_dataset_v1.json` with a command on 2026-11-20). 18 reuse the older scenarios, 22 are new. src/claimsleuth/evaluation/dataset.py:43 and src/claimsleuth/evaluation/dataset.py:45.
- Ground truth: written by hand next to each case, never computed by ClaimSleuth, never given to an agent (src/claimsleuth/evaluation/dataset.py:6). Weakness: the same team wrote the rules and the labels.
- Scoring: each approach on its own task (Evidence: facts, missing evidence, citations; Investigator: policy violation, history; Forensic: anomalies; combined: final 4-class decision), exact match, NO LLM-as-judge (src/claimsleuth/evaluation/scoring.py:3).
- Runner: starts its own MCP and CrewAI services on ports 8122/8123, offline by default, live only with `--confirm-live` and at most 5 cases from the dashboard (src/claimsleuth/dashboard/evaluation_jobs.py:18), phrase `RUN LIVE EVALUATION` (src/claimsleuth/dashboard/evaluation_jobs.py:17). It stops at the first 429, 503 or budget refusal: src/claimsleuth/evaluation/runner.py:174. Live estimate: 10 requests per case (src/claimsleuth/integration/run.py:55), upper bound 18 (src/claimsleuth/evaluation/runner.py:160).
- Statistics: Tukey box plots, whiskers at 1.5 x IQR, at least 5 observations (src/claimsleuth/evaluation/metrics.py:109).
- Experiments (E3, E4, E5) are explained in Part 4(h).

## 2.14 Tests (numbers from EVIDENCE.md only)
| Suite | Result | Date and machine |
|---|---|---|
| Main (`.venv`) | 801 passed, 0 failed, 1 deselected | 2026-11-06, Windows 11, Python 3.12.10 |
| CrewAI (`.venv-crew`) | 155 passed, 0 failed, 2 deselected | 2026-11-06, Windows |
| Browser flows | 239 of 239 checks, six flows | 2026-11-06 (temporary scripts, not in repo) |
| Mutation checks | 6 of 6 detected | 2026-11-06 |
| Main, macOS rerun | 795 passed, 1 failed, 5 skipped, 1 deselected (4,537 s) | 2026-11-19 |
| CrewAI, macOS rerun | 155 passed, 2 deselected (79 s) | 2026-11-19 |
| `tests/test_experiments.py` | 8 passed (run alone) | 2026-11-19 |
The one macOS failure is `tests/test_provider_reliability.py::test_the_managers_deadline_keeps_the_agents_record`; it was not edited and its cause is not investigated (it passed on Windows; the difference is unexplained). "Deselected" tests are marked `live` and need a real model (`-m 'not live'` in `pyproject.toml`). Test suites exist per area: schemas, rules, adjudicator, blackboard (85 passed in the Windows run), dashboard (46), evidence integrity (56), prompt injection (150), reliability and faults (77), provider reliability (72), orchestration (25), human approval (53). CI does NOT run these (see Part 4e).

---

# PART 3. Course criteria C-1 to C-11 mapped to evidence, with honest status

| # | Criterion | Status | Code evidence | Results / doc evidence | What is NOT done |
|---|---|---|---|---|---|
| C-1 | Blackboard shared state; messaging styles compared | DONE, with a gap | src/claimsleuth/blackboard/store.py:119; src/claimsleuth/blackboard/models.py:32 | E4: `results/messaging_comparison.csv` (blackboard vs direct message passing vs no sharing, 3 x 40 runs) | The publish/subscribe broadcast arm was NOT built. Accuracy is identical by construction offline |
| C-2 | At least two MCP tools | DONE | src/claimsleuth/mcp_server/server.py:227, src/claimsleuth/mcp_server/server.py:246 | `scripts/mcp_client_check.py` | MCP unauthenticated; keyword search, not retrieval |
| C-3 | Pydantic structured output | DONE | src/claimsleuth/schemas.py:24 | tests/test_schemas.py | Shape is validated, truth is checked separately |
| C-4 | Sequential, parallel, hierarchical patterns, compared | DONE (implemented in one design) and compared in E3 | src/claimsleuth/orchestration/manager.py:82; src/claimsleuth/adk_service/agents.py:122 | E3: `results/architecture_comparison.csv`, 40 cases each, offline | Accuracy identical by construction; only time, calls and context differ. Manager is deterministic code, parallel is `asyncio.gather` |
| C-5 | Callbacks, monitoring, heat map | DONE | src/claimsleuth/adk_service/callbacks.py:97; src/claimsleuth/observability/trace.py:390 | `results/heatmap_agent_latency.png`; Evaluation tab heat maps | No live per-agent runtime heat map from callbacks inside the dashboard |
| C-6 | A2A between ADK and CrewAI | DONE | src/claimsleuth/crew_service/a2a_server.py:100; src/claimsleuth/adk_service/forensic_a2a.py:76 | `docs/submission/results/aws_run_RUN-dash-6ac716c9bd.json` has the two A2A messages | One A2A link; unauthenticated |
| C-7 | Only CrewAI and ADK | DONE | `requirements/main.txt`, `requirements/crew.txt`; two venvs | no other agent framework imported in `src/` | CrewAI crew is one agent, one task |
| C-8 | Security, guardrails, prompt variance, personas | DONE (offline) | src/claimsleuth/security.py:171; src/claimsleuth/crew_service/crew.py:99 | E5 `results/persona_prompt_runs.csv` (116 runs, 0 of 58 paired decisions changed) | Pattern-based screen; doubles ignore prompt wording, so no real prompt-variance claim; no live security test |
| C-9 | Observability, provenance, audit | DONE | src/claimsleuth/observability/trace.py:63; PDF page/line provenance | `results/aws_live_run_RUN-dash-6d15294eca.json` (trace, audit trail, timeline) | None significant |
| C-10 | 40+ runs, cost, time, box plot | PARTIAL | src/claimsleuth/evaluation/experiments.py:243; src/claimsleuth/evaluation/metrics.py:109 | 236 experiment runs `results/experiment_runs.csv`; baseline `EV-20261119-141842-e46c4a`; `boxplot_latency.png` | Live cost over 40 runs NOT measured; no live accuracy; offline cost is 0 INR |
| C-11 | Cloud deployment | DONE | `Dockerfile.single`, `docker-compose.yml`, `deploy/` | `docs/submission/DEPLOYED.txt`; `results/aws_run_RUN-dash-6ac716c9bd.json` (offline VM run); `results/aws_live_run_RUN-dash-6d15294eca.json` (live, partial) | Only 4 complete live runs exist (all local) plus 1 partial live run on the VM (Forensic Agent got HTTP 503); no live accuracy |

**Live evidence, exactly.** Four complete live runs (all `gemini-3.5-flash`, all three agents, run locally on the Windows machine): `RUN-dash-90e0f64064` (0411, APPROVE, 9 requests), `RUN-dash-5c40c24a5a` (0411, APPROVE, 6), `RUN-dash-9f819f9dbc` (0412, ESCALATE, 7), `RUN-dash-bcdbabaf97` (0412, ESCALATE, 7 including one Forensic retry that recovered after a 503) (EVIDENCE.md, "Live runs"). Those run folders live in `runtime/`, which git does not carry, so you cannot open them from the repository. ONE PARTIAL live run on the deployed VM, 2026-11-19: `RUN-dash-6d15294eca`, claim WC-2026-0411: Evidence Agent COMPLETED 57.8 s (2 calls, 8,068 prompt and 1,181 completion tokens), Investigator COMPLETED 23.6 s (2 calls, 23,696 and 661 tokens), Forensic Agent FAILED after 107.3 s with Google HTTP 503 UNAVAILABLE after its retry; 7 Gemini requests; status INCOMPLETE, human approval required, decision APPROVE (16,400.00 INR) from the adjudicator over the two preserved reports. Never call it a complete live run. No live accuracy figure exists.

---

# PART 4. MY PART IN DEPTH (Vaibhav's areas)

Your areas (TEAM.json, proposed): cloud deployment (Docker, AWS, proxy, secrets); end-to-end evaluation of time and cost; the architecture and messaging-style comparison experiments; integration testing; the report and presentation pipeline. This part assumes you know nothing about the technology.

## 4(a) AWS from zero

### The big idea
You rent a computer from Amazon, in a data centre in Mumbai, by the hour. You install Docker on it and run the project there. Anyone on the internet can then open it in a browser.

### Vocabulary (each term, in plain words)
- **EC2 (Elastic Compute Cloud)**: Amazon's service for renting virtual computers.
- **Instance**: one such rented computer. Ours: id `i-01e12e3df2c70f60f` (DEPLOYED.txt).
- **Region**: a geographic area with data centres. Ours: `ap-south-1` (Mumbai).
- **Availability zone (AZ)**: one data centre (or group) inside a region. We did not pick one explicitly; AWS puts the instance in a default subnet's zone.
- **Instance type `t4g.small`**: the size. `t` = burstable (cheap, can use more CPU for short bursts), `4` = generation, `g` = Graviton processor, `small` = 2 virtual CPUs and 2 GB RAM.
- **ARM / Graviton**: Amazon's own ARM-based CPU (like Apple silicon, unlike a typical Intel/AMD PC). Cheaper per hour. Consequence: images must be ARM64; the base images (`python:3.12-slim`, `caddy`) are multi-arch, so they build natively. Build ON the VM if your laptop is x86 (aws/README line 10).
- **AMI (Amazon Machine Image)**: the template for the operating system. We used Ubuntu 24.04 arm64, looked up from a public SSM parameter so it is always current (aws/README step 3).
- **EBS (Elastic Block Store), gp3**: the virtual hard disk. 20 GB gp3, encrypted. Billed even when the instance is stopped.
- **Security group**: a firewall around the instance. Ours: ports 80 and 443 open to everyone, port 22 (SSH) only from the deployer's IP (AWS plan, section 2). Port 7860 (the app) is not exposed; only Caddy listens publicly.
- **Key pair**: a pair of cryptographic keys for SSH login (no password). The private key `.pem` stays on the deployer's machine, outside the repo. Name: `claimsleuth-key`.
- **IMDSv2**: the instance metadata service (an internal address 169.254.169.254 that tells the instance facts about itself, such as its public IP). Version 2 requires a token for each request, which blocks a class of attacks. We set `HttpTokens=required` (aws/README step 4). `boot.sh` uses the token to read the public IP (deploy/boot.sh:20).
- **Elastic IP**: a fixed public IPv4 address you own until you release it. Ours: `13.204.3.14`, allocated 2026-11-19. **It is billed**: about 0.005 USD per hour like any public IPv4, also while the instance is stopped (DEPLOYED.txt; aws/README line 14). Without it the public IP changes after a stop/start (a plain reboot keeps it).
- **Public IPv4 pricing**: since February 2024 AWS charges for every public IPv4 address, attached or not (about 0.005 USD/h, about 3.6 USD/month).
- **User data**: a script handed to the instance at first boot, run once as root. Ours: `deploy/aws/user-data.sh` installs Docker, creates the swap file, sets Docker log rotation and installs the systemd unit `claimsleuth.service`.
- **IAM (Identity and Access Management)**: who/what may call AWS APIs. An instance can be given a "role" so programs on it may call AWS services (S3 etc.). Our instance has **no role**: the app calls no AWS service, so nothing needs permission (aws/README "Secrets"). Less privilege means less to steal.
- **VPC (Virtual Private Cloud)**: your private network in AWS. We used the default VPC and default subnet; no custom networking, no NAT gateway, no load balancer.
- **Stop vs terminate**: *stop* powers the machine off; the disk stays (billed), the instance can be started again, and the public IP is lost unless it is an Elastic IP. *Terminate* destroys the instance and (with `DeleteOnTermination=true`, as we set) its root disk. It cannot be undone.
- **Snapshot**: a point-in-time copy of a disk, kept (and billed) separately. We created none; teardown does not delete snapshots, so if you ever make one, delete it yourself.

### Why this machine, these choices
- **t4g.small**: measured stack about 400 MiB at peak (deploy_test.md: 382.7 MiB peak app container, Caddy 13.6 MiB, on Docker Desktop Apple silicon, NOT measured on the VM), so 2 GB is enough; a 2 GB swap file is added as a safety margin and for image builds.
- **One VM, not Cloud Run / ECS / Kubernetes**: the blackboard is a SQLite file in WAL mode written by two processes. That requires ONE host and a local disk. Multi-container platforms with separate replicas (or a network file system) would break the locking guarantees. Scaling out would mean moving to Postgres or Redis (AWS plan, section 1; CONTRACT).
- **No load balancer (ALB)**: one instance needs none; Caddy terminates HTTPS itself; an ALB adds cost (hourly plus its own public IPs) and nothing to a single machine. The plan states: "Not created: load balancer, NAT gateway, S3, ECR, IAM roles, RDS, budgets".
- **ARM on purpose**: cheaper per hour; the image builds natively.
- **Cost (AWS plan, Pricing API, ap-south-1)**: t4g.small 0.0112 USD/h (about 8 USD/month), public IPv4 0.005 USD/h (about 3.6), 20 GB gp3 (about 2): roughly 14 USD per month; a stopped instance costs only its disk (and the Elastic IP). The account is on the AWS Free plan with credits (plan ends 2027-01-30). Note: `deploy/aws/README.md` line 81 estimates 0.017 to 0.02 USD/h for the same instance, contradicting the Pricing API figure; quote DEPLOYED.txt (0.0112 USD/h).

### Real commands used (from `deploy/aws/README.md`), explained
| Step | Command (abridged) | What it does |
|---|---|---|
| 1 | `aws ec2 create-key-pair ... > claimsleuth-key.pem; chmod 400` | Creates the SSH key; `chmod 400` so only you can read it |
| 2 | `aws ec2 create-security-group` then `authorize-security-group-ingress` (80, 443 world, 22 from your IP /32) | Builds the firewall |
| 3 | `aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id` | Finds the latest Ubuntu 24.04 ARM image id |
| 4 | `aws ec2 run-instances --instance-type t4g.small --metadata-options HttpTokens=required ... --block-device-mappings ...VolumeSize=20,VolumeType=gp3,DeleteOnTermination=true --user-data file://deploy/aws/user-data.sh` | Launches the VM with IMDSv2, 20 GB disk, first-boot script |
| 5 | `tar ... | scp`, `cloud-init status --wait`, extract into `/opt/claimsleuth`, copy `deploy/.env.example` to `deploy/.env`, `chmod 600` | Copies the code to the VM (excludes venvs, docs, tests, secrets) |
| 6 | `docker run --rm caddy:2-alpine caddy hash-password --plaintext '...'` then edit `deploy/.env`, `sudo bash deploy/boot.sh` | Creates the hashed password, starts the stack (first build 5 to 10 minutes) |
| 7 | `aws ec2 describe-instances ... PublicIpAddress` | Finds the IP; dots become dashes: `13.204.3.14` -> `13-204-3-14.sslip.io` |
Note the contradiction inside the repo: `deploy/aws/README.md` lines 4 and 101 still say "Nothing here was run against a real AWS account... NOT tested: any aws command", while DEPLOYED.txt and EVIDENCE.md record a real deployment on 2026-11-19. The deployment is real; that sentence in the README is stale.

### What the teardown script does (simple version)
deploy/aws/teardown.sh:7 asks you to type `yes`, then: (1) terminates the instance and waits until it is gone (its disk is deleted with it); (2) releases the Elastic IP if `EIP_ALLOC` is set in the resource file (so the hourly IP charge stops); (3) deletes the security group; (4) deletes the key pair and the local `.pem`. It reads the ids from `~/.claimsleuth-aws/resources.env`. Traps: the Elastic IP is released ONLY if `EIP_ALLOC` is recorded in that file (deploy/aws/teardown.sh:10), so after teardown check in the AWS console (EC2, Elastic IPs) that nothing is left; it does not delete snapshots; the password file and notes stay in `~/.claimsleuth-aws` until you delete them; the AWS access key used for deployment belongs to an administrator user and should be rotated or deleted after the assessment (AWS plan section 6).

## 4(b) Docker from zero

- **Image**: a frozen package of an application plus everything it needs (files, Python, libraries). Like a recipe turned into a sealed meal kit. Built from a `Dockerfile`.
- **Container**: a running instance of an image, isolated from the host. Like a meal cooked from the kit. Many containers can run from one image.
- **Docker Compose**: a file (`docker-compose.yml`) describing several containers and how they connect, started with one command.
- **Volume**: storage that outlives the container, so data survives a restart or rebuild.

### `Dockerfile.single`, walk-through (Dockerfile.single:10)
1. **Builder stage** (`FROM python:3.12-slim AS builder`): a temporary workspace.
2. It creates TWO virtual environments, `/app/.venv` from `requirements/main.txt` and `/app/.venv-crew` from `requirements/crew.txt`. Two venvs because of the opentelemetry/mcp conflict (1.7).
3. It installs the project itself in each (`pip install --no-deps -e .`).
4. **Pruning**: removes packages that were checked as unused at runtime (pytest, kubernetes, onnxruntime, lancedb, pyarrow, chromadb-rust-bindings, textual, pdfplumber, pdfminer-six, pypdfium2, openpyxl, hf-xet, uvloop), pip, `tests` directories and `.pyi` stubs. Result: 1.74 GB down to 911 MB (deploy_test.md).
5. **Runtime stage** (second `FROM python:3.12-slim`): copies only the venvs, `src`, `pyproject.toml`, `data`. This is a "multi-stage build": the final image carries none of the build tools.
6. Creates non-root user `user` (uid 1000) and a `/app/runtime` folder owned by it; runs as that user (Dockerfile.single:44).
7. `CMD python -m claimsleuth.dashboard --host 0.0.0.0 --port 7860` starts the dashboard, which itself starts the MCP server (port 8102) and the CrewAI A2A service (port 8103) as child processes on 127.0.0.1 inside the container. One container therefore holds the whole system.
8. `HEALTHCHECK` (Dockerfile.single:47) fetches `http://127.0.0.1:7860/` every 30 s (timeout 10 s, start period 120 s, 5 retries) and marks the container healthy or unhealthy.

### `docker-compose.yml` (docker-compose.yml:10)
- Service **app**: built from `Dockerfile.single`; image tag `claimsleuth-single:latest`; `restart: unless-stopped` (restarts after a crash or reboot unless you stopped it by hand); environment `EXECUTION_MODE=${MODE:-offline}`, `GOOGLE_API_KEY`, `CLAIMSLEUTH_BLACKBOARD_DB=/app/runtime/blackboard.sqlite3`, the two Gemini caps; volume `runtime:/app/runtime` (blackboard, runs, ledger); `expose: 7860` (visible to other containers, NOT published to the internet); healthcheck as above with start period 90 s; log rotation 10 MB x 3 files (the `x-logging` block, so logs cannot fill the 20 GB disk).
- Service **caddy**: `caddy:2-alpine`; `depends_on: app`; the only service that publishes ports (80, 443); gets `SITE_ADDRESS`, `BASIC_AUTH_USER`, `BASIC_AUTH_HASH` (compose refuses to start if the hash is unset: `:?set BASIC_AUTH_HASH`); mounts `deploy/Caddyfile` read-only; volumes `caddy_data` (certificates, so Let's Encrypt is not re-asked on each restart) and `caddy_config`.
- Volumes: `runtime`, `caddy_data`, `caddy_config`. `docker compose down -v` would DELETE them; plain `down` keeps them.
- Other compose files: `docker-compose.two-image.yml` (older app + crew layout) and `docker-compose.test.yml` (no Caddy, no password: local test only, never on a public machine).
- Why one image, not two: same measured memory, 1.04 GB less disk, one container fewer (deploy_test.md decision table).
- Bug found by running the stack: in the old two-container layout the Forensic Agent was reported "not run" because the CrewAI container lacked `CLAIMSLEUTH_BLACKBOARD_DB`; fixed (deploy_test.md). Good story: "a real deployment bug caught only by running the stack".

## 4(c) Caddy reverse proxy
- **Reverse proxy**: a server that stands in front of your app, receives the visitor's request and forwards it to the app. Analogy: the receptionist who checks your ID before letting you into the office.
- **Caddy** gets HTTPS certificates automatically. **Let's Encrypt** is a free certificate authority; it proves you control the domain and issues a certificate, renewed automatically.
- **sslip.io trick**: a public DNS service where `13-204-3-14.sslip.io` always resolves to `13.204.3.14`. We have no domain, but a real hostname is needed for a real certificate; sslip.io supplies one for free. Caveat: sslip.io is shared by many users and Let's Encrypt rate-limits it; Caddy retries and can fall back to another CA (aws/README step 7).
- **Basic auth**: the browser asks for username and password before any page loads. The password is stored only as a hash (`caddy hash-password`, bcrypt) in `deploy/.env` on the VM, never in plain text and never in the repo.
- `deploy/Caddyfile` (deploy/Caddyfile:3): site address from the environment; `basic_auth` with user and hash; `encode gzip`; `reverse_proxy app:7860` with a health check every 30 s on `/`; logs to stdout. `SITE_ADDRESS=:80` serves plain HTTP (local test only). The comment states: "The dashboard itself has no login."
- What is protected: everything. What is not: Caddy's basic auth is one shared login; the reviewer name inside the app is only typed text.

## 4(d) `boot.sh`, systemd units, swap
- **boot.sh** (deploy/boot.sh:10) runs as root at every boot and by hand. It: reads `SITE_ADDRESS` from `deploy/.env`; if empty or `auto` it asks IMDSv2 for the current public IPv4 (retries 10 times, 3 s apart because the network may not be ready), writes `SITE_ADDRESS=<ip-with-dashes>.sslip.io` to `deploy/site.env` (chmod 600); if no IP is found, serves plain HTTP on `:80`; finally `exec docker compose --env-file deploy/.env --env-file deploy/site.env up -d --build --remove-orphans` (the later env file wins). With the Elastic IP, IMDS returns that address, so the name is stable.
- **systemd**: the Linux service manager. A *unit* describes a service. `claimsleuth.service` (created by user-data) is `Type=oneshot`, `RemainAfterExit=yes`, `TimeoutStartSec=1800`, runs `boot.sh`, enabled at boot, requires Docker. So after a reboot the stack comes back by itself.
- **Swap file**: disk space used as emergency memory. user-data creates a 2 GB `/swapfile`, adds it to `/etc/fstab` so it survives reboots, and sets `vm.swappiness=10` (use swap only when really needed). Needed because the VM has only 2 GB RAM and image builds are memory hungry.
- Docker log rotation (10 MB x 3) is set in `/etc/docker/daemon.json` and in compose.

## 4(e) CI/CD from zero
- **CI (continuous integration)**: every time code is proposed, an automatic robot checks it quickly. **CD (continuous delivery/deployment)**: when code is accepted, it is automatically delivered to the running system.
- **GitHub Actions**: GitHub's built-in robot. Concepts: a *workflow* is a YAML file in `.github/workflows/`; a *trigger* (`on:`) says when it runs; a *job* is a group of steps that run on one *runner* (a temporary machine, here `ubuntu-latest`); a *step* is one command or reusable action.

### What `ci.yml` does (.github/workflows/ci.yml:1)
- Triggers: every pull request and every push to `main` (.github/workflows/ci.yml:6).
- `concurrency` cancels an older run of the same ref when a newer one starts; `permissions: contents: read` (least privilege); one job `checks`, 20-minute timeout.
- Steps: (1) check out the code; (2) set up Python 3.12; (3) **Python compiles**: `python -m compileall -q src tests` (catches syntax errors only); (4) **Shell scripts parse**: `bash -n` on `deploy/*.sh`, `deploy/aws/*.sh`, `deploy/gcp/*.sh` (syntax check, does not run them); (5) **No secrets committed**: fails if a tracked file is `.env`, `.pem` or `.tgz`, or if `git grep` finds `AKIA...` (AWS key id) or `AIza...` (Google API key) patterns outside `tests`; (6) set up Docker Buildx and **build `Dockerfile.single`** without pushing, using the GitHub Actions layer cache.
- Evidence: CI passed on PR #2 (3 min 46 s) and on merge commit `186f279` (3 min 25 s) (SUPERVISOR_AUDIT check 5e; EVIDENCE.md).

### What CI does NOT do
- **No pytest.** The suites take about 45 minutes and start real services; they are run locally. So a green CI means "compiles, scripts parse, no obvious secret, image builds", not "tests pass".
- It does not deploy anything and does not block a merge by itself: branch protection could not be confirmed (the API returned 404, SUPERVISOR_AUDIT). The file's header says "merge only green PRs", but nothing enforces it.

### Pull-based vs push-based deployment
- *Push-based*: GitHub's runner logs into the server (SSH key stored as a GitHub secret) and deploys. Needs a secret in GitHub and an open SSH port to the world.
- *Pull-based* (ours): the server itself checks GitHub every minute and updates itself. Why we chose it: the repo is public, so the VM needs no credentials to read it; no secrets in GitHub; no inbound SSH from GitHub (port 22 stays limited to one IP).

### `deploy/auto_update.sh`, line by line (deploy/auto_update.sh:9)
- Lines 2-8: header; runs as root from the timer; no secrets printed; log with `journalctl -u claimsleuth-update`.
- Line 13-14: `exec 9>/var/lock/claimsleuth-update.lock; flock -n 9 || exit` takes a file lock so two updates never run together (a rebuild can take minutes).
- Lines 16-19: `getv` reads settings from `deploy/.env`; `REPO_URL` (default `https://github.com/astitvasaxena/ClaimSleuth_simple.git`), `BRANCH` (default `main`); `AUTO_UPDATE=off` exits at once (the pause switch).
- Lines 21-25: clone into `/opt/claimsleuth-src` on first run; then `git fetch` the branch; if fetch fails (no internet) keep the current version.
- Lines 26-29: `NEW` = latest commit on origin; `OLD` = contents of `deploy/.deployed_sha`; if equal, nothing to do; if `NEW` equals `deploy/.skip_sha`, that commit already failed and was rolled back, so skip it.
- Lines 31-36: `sync_to`: checks out the commit in the source folder and **`rsync -a --delete`** into `/opt/claimsleuth`, with excludes: `.git`, `deploy/.env`, `deploy/site.env`, the sha files, `runtime`, `docs`, `tests`, `.claude`, `.github`, `*.bak-*`, `__pycache__`. So secrets and state are never overwritten, and docs/tests are not copied to the VM. (Docker volumes live outside this folder.)
- Lines 37-44: `healthy`: polls the app container's Docker health status every 15 s, up to 40 times (about 10 minutes, because the first build on a t4g.small is slow).
- Lines 46-50: on success, writes `NEW` into `deploy/.deployed_sha`, prunes old images.
- Lines 51-61: on failure it prints DEPLOY FAILED, rolls back (`sync_to OLD`, `boot.sh`, wait healthy), writes `NEW` into `.failed_sha` and `.skip_sha`, and restores `OLD` into `.deployed_sha`. A newer commit is tried automatically.
- Note: the 2026-11-20 supervisor audit (H3) said `.deployed_sha` held the failed commit after a rollback; in the current script it holds the previous (running) commit, so that audit item is out of date.

### The systemd timer
`deploy/install_autoupdate.sh` creates `claimsleuth-update.service` (oneshot, `TimeoutStartSec=2400`) and `claimsleuth-update.timer` with `OnBootSec=120` (first run 2 minutes after boot) and `OnUnitInactiveSec=60` (60 s after the previous run finishes, so runs never overlap and a long rebuild simply delays the next check). Enabled with `systemctl enable --now`.

### Honest caveats
- The VM deploys `main` regardless of the CI result. NEVER say "only green builds deploy".
- Detection takes up to about 60 s; the rebuild time on the VM was NOT measured. NEVER say "deploys within one minute".
- A bad commit can cost up to about 10 minutes of failed build plus up to 10 more for the rollback rebuild, during which the site may be down.
- Set `AUTO_UPDATE=off` in `deploy/.env` on the VM for the assessment window, and push before it, not during it.
- Branch protection (a GitHub setting that requires CI to pass and a review before merging into `main`) would make "only green builds deploy" true; it is not confirmed here.

## 4(f) Secrets handling
- `deploy/.env` on the VM holds the Caddy password HASH, `GOOGLE_API_KEY` (live only), `MODE`, `GEMINI_RPD`/`GEMINI_RPM`, `REPO_URL`, `BRANCH`, `AUTO_UPDATE`. It is `chmod 600` (only its owner can read it) and is NOT tracked (`.gitignore`, `.dockerignore`, auto_update excludes it, and CI fails if a `.env` file is tracked).
- Key reaches the container only as an environment variable via compose (`GOOGLE_API_KEY: ${GOOGLE_API_KEY:-}`); it is never in the image or repo. The code only checks that a key is present and never prints it (src/claimsleuth/execution_mode.py:103). Logs redact secrets (`observability/provider_errors.py: redact`).
- **Rotating the Gemini key**: create a new key in Google AI Studio, SSH to the VM, edit `deploy/.env` (`GOOGLE_API_KEY=`), run `sudo bash deploy/boot.sh` (recreates the container with the new value), test one claim, then revoke the old key. If a key was ever committed, treat it as leaked and rotate even after deleting the commit.
- **Rotating the site password**: run `docker run --rm caddy:2-alpine caddy hash-password --plaintext '...'`, paste the hash (single-quoted) into `BASIC_AUTH_HASH`, run `boot.sh`.
- Alternative store: AWS Secrets Manager or SSM Parameter Store (`SecureString`), fetched into that file by hand; not used.
- **Quota math**: the app caps requests at `GEMINI_RPD=20` per day and `GEMINI_RPM=5` per minute (defaults = free tier). A complete live claim needs 6 to 9 requests (the code estimates 10 per case, upper bound 14 for one claim: ADK 10 + CrewAI 4 model calls), so about 2 to 3 live claims per day. After that every agent is marked FAILED and the claim escalates (fail closed, but it looks broken). The day resets at midnight Pacific time. Never share the site password or key in chat, email or the repo.

## 4(g) The end-to-end evaluation: time and cost

### How latency is measured
- Per agent: ADK callbacks `before_agent`/`after_agent` start and stop a `perf_counter` timer and store `latency_ms` (src/claimsleuth/adk_service/callbacks.py:143); CrewAI uses its event bus; the whole run time is the manager's `execution_ms`.
- Per model call: each call is a record with start, end, latency and tokens ONLY when the provider reported usage (otherwise `None`, never 0).
- Per experiment run: wall time is measured around `run_claim_async` with `time.perf_counter` (src/claimsleuth/evaluation/experiments.py:223).
- Tokens: only live runs report them (offline doubles report none). Cost: no price table ships, so cost is "unavailable", never an estimate.

### What exists (with source)
| Item | Value | Source |
|---|---|---|
| Experiment runs | 236 (E3 120, E4 120, E5 116; E3 and E4 share runs), 0 failed | `results/summary.json`, `results/experiment_runs.csv` |
| 40-case baseline benchmark | 40 of 40 expected decisions, 0 Gemini requests, id `EV-20261119-141842-e46c4a` | `results/baseline_eval_40_*` |
| Per-agent median latency, 236 offline runs, macOS laptop | Evidence 12.1 s, Investigator 12.1 s, Forensic 0.54 s, system 24.3 s | EVIDENCE.md "Final experiments" |
| AWS VM, OFFLINE, 2026-11-19, `RUN-dash-6ac716c9bd` (claim WC-2026-0412) | about 3 s end to end; agents 445 ms and 266 ms (ADK), 1,142 ms (CrewAI) | `results/aws_run_RUN-dash-6ac716c9bd.json` (values re-read from the file) |
| Partial live run on the VM, `RUN-dash-6d15294eca` | Evidence 57.8 s, Investigator 23.6 s, Forensic failed at 107.3 s (HTTP 503), 7 requests | `results/aws_live_run_RUN-dash-6d15294eca.json` |
| Complete live runs | 4, using 9, 6, 7, 7 requests | EVIDENCE.md |

### What does NOT exist
- No live accuracy figure (4 complete live runs on 2 claims cannot support one).
- No live cost over 40 runs. Forty live runs would need about 240 to 360 requests (6 to 9 each) against a free tier of 20 per day, i.e. 12 to 18 days, or a paid key. The code's own estimate is about 400 (10 per case). No token or rupee figure for 40 live runs exists.
- No repeated runs per case (offline is deterministic, so repeats would be identical); variance exists only across cases.

### Timing caveats (say these exactly)
1. The laptop figure (about 24 s per claim, about 12 s per ADK agent) is a laptop measurement with the doubles; the cause of the 12 s was NOT isolated (the 236 unloaded runs show the same, so "test-suite load" is not an explanation and must not be claimed).
2. The VM figure (about 3 s) is OFFLINE, 2026-11-19. The VM now runs live mode, where the one partial run shows 57.8 s and 23.6 s on Gemini. Label VM numbers "offline".
3. Offline latency is not Gemini latency and must never be quoted as live latency.
4. Low vague-persona mean in E5 (21.3 s): one run (EVAL-25, 6.1 s) that did not repeat; cause unknown; do not explain it as "missing-evidence claims finish early" (the results README says that, but CONTRACT.md says not to, because the repeat runs did not show it; trust CONTRACT).

### How to answer "what does a claim cost?" honestly
"Offline it is zero rupees: no model is billed. Live, one claim needs about 6 to 9 Gemini requests; we recorded that from the request ledger for four complete runs. On the free tier nothing was billed. We ship no price table, so we cannot give a rupee figure, and we did not run 40 live claims, so there is no live cost-per-claim or live accuracy number. We recorded token counts for the one partial VM run (Evidence 8,068 prompt and 1,181 completion; Investigator 23,696 and 661)."

## 4(h) Architecture, messaging and prompt experiments (E3, E4, E5)

### Where the code is
Harness: src/claimsleuth/evaluation/experiments.py:134 (variants are temporary in-process substitutions around the unchanged product code, restored after each run), run loop src/claimsleuth/evaluation/experiments.py:243, aggregation src/claimsleuth/evaluation/experiments.py:343. Run: `.venv/bin/python -m claimsleuth.evaluation.experiments run --out docs/submission/results`. It forces OFFLINE and removes any API key from the environment first (src/claimsleuth/evaluation/experiments.py:235).

### What was varied
| Config | Topology | Context between agents |
|---|---|---|
| `hier_blackboard` | the product default: Evidence first, then Investigator and Forensic in parallel, gated and monitored | signed blackboard; both later agents read the Evidence report |
| `seq_direct` | strictly Evidence -> Investigator -> Forensic, one after the other (the Forensic call is delayed until ADK finishes) | Evidence report via ADK session state; Forensic gets none |
| `par_direct` | Evidence, Investigator and Forensic start together (`ParallelAgent`) | none: the "no sharing" arm |
| `p2p_direct` | Evidence -> Investigator in one ADK sequence, Forensic in parallel (the old direct mode) | ADK session state, no store |
E3 (architecture) = `seq_direct`, `par_direct`, `hier_blackboard`. E4 (messaging) = `hier_blackboard` (blackboard), `p2p_direct` (direct message passing), `par_direct` (no sharing). Each config ran once per case on the 40 dataset cases and both views read the same runs (src/claimsleuth/evaluation/experiments.py:53).
E5 (persona x prompt structure) = `hier_blackboard` on the 40 dataset cases and the 18 sample claims, each under `standard` and `evidence_first_react` structures: 116 runs (honest 38, vague 6, abusive 6, injection 8 per structure; unbalanced). `evidence_first_react` reorders the real ADK packet (documents first, claim form last) and appends a ReAct (Thought, Action, Observation) instruction; the doubles ignore both.

### What the CSVs contain
- `experiment_runs.csv` (236 rows): one row per run: config, structure, case_id, persona, category, expected and final decision, `correct`, `decided_by`, `wall_ms`, per-agent latency (`evidence_ms`, `investigator_ms`, `forensic_ms`), `llm_calls`, `agent_tool_calls`, `mcp_calls_logged`, `a2a_messages`, `blackboard_reads`, whether each agent saw the Evidence report, `context_chars_shared`, `adk_crew_overlap_ms`, packet sizes, model.
- `architecture_comparison.csv`, `messaging_comparison.csv`: one row per config with mean/median/stdev/p95 wall time and the means above.
- `architecture_comparison_runs.csv`, `messaging_comparison_runs.csv`, `persona_prompt_runs.csv`: the per-run views.
- `experiments_summary.json`: aggregates; `summary.json`: everything the report and PPT read.

### Headline numbers (40 runs per config, offline, macOS laptop, source `results/*.csv`)
| Config | Correct | Median ms | Mean ms | Stdev ms | Context chars shared | Investigator saw Evidence | Forensic saw Evidence | Blackboard reads |
|---|---|---|---|---|---|---|---|---|
| sequential-only | 40/40 | 24,747 | 24,267 | 2,862 | 2,370 | 40 | 0 | 0 |
| parallel-only / no sharing | 40/40 | 12,204 | 11,986 | 1,407 | 0 | 0 | 0 | 0 |
| hierarchical + blackboard | 40/40 | 24,252 | 23,416 | 3,975 | 4,740 | 40 | 40 | 2 |
| direct message passing | 40/40 | 24,115 | 23,863 | 1,964 | 2,370 | 40 | 0 | 0 |
Every config: 3 agents, 9 model calls, 8 agent tool calls, 6 MCP calls logged, 2 A2A messages per claim on average. E5: all 116 runs matched ground truth; 0 of 58 paired comparisons changed decision between structures.

### How to read the plots
- `boxplot_latency.png`: one box per config (wall time). The box spans the 25th to 75th percentile, the line inside is the median, whiskers reach the furthest points within 1.5 x the box height, dots beyond are outliers. Observe: parallel-only sits near 12 s, the other three near 24 s; the left-tail low points (3 to 7 s) are a few short runs.
- `heatmap_agent_latency.png`: colour = average latency, rows = agents, columns = personas (blue scale). Darker means slower. Observe: the two ADK agents are slow everywhere, Forensic is fast.
- Dashboard heat maps (Evaluation tab) show accuracy/precision/recall/F1/completion per approach and per scenario, not latency.

### Why accuracy is identical, and what that does and does not prove
Because the offline agents read the claim packet, not other agents' reports or the prompt wording, and the decision is deterministic, no topology, messaging style or prompt structure can change a decision. Identical accuracy therefore proves: nothing broke (all 236 runs completed, 0 errors, wiring variants are faithful) and the deterministic rules are stable. It does NOT prove one architecture is smarter or safer, and it says nothing about real model behaviour. The choice of the hierarchical design with a blackboard is justified by measured time, calls and context passed, plus design heuristics: independence of opinions (Forensic cannot read the Investigator), fault isolation (a failed agent does not erase another's report), auditability (signed, receipted context) and fail-closed behaviour. The measured cost of that design: about 2x the wall time of parallel-only offline (24.3 s vs 12.2 s), because Evidence must finish before the Investigator starts. A true publish/subscribe broadcast arm was not built.

## 4(i) The report and PPT pipeline
- `docs/submission/build_report.py` (1,429 lines) builds the Word report from the course template `docs/Final_Report_Format.docx` (copied in memory, never modified), `TEAM.json`, `results/summary.json`, CSVs, PNGs and `DEPLOYED.txt`.
- `docs/submission/build_ppt.py` (644 lines) builds the 15-slide deck (numbered 0 to 14).
- `docs/submission/content_shared.py` holds the literature table, requirements R1..R11 and references; both builders import it. Edit it there only.
- `docs/submission/report_figures.py` draws the greyscale diagrams into `docs/submission/figures/`.
- `docs/submission/results/make_plots.py` draws the box plot and heat map; `build_summary.py` assembles `summary.json` from the experiment files.
- `docs/submission/sync.sh` (docs/submission/sync.sh:3): runs both builders with a tools venv OUTSIDE the repo (python-docx, python-pptx, matplotlib), converts both files to PDF with LibreOffice (`soffice --headless`), renders PNG pages with `pdftoppm`, to `docs/submission/render/`.
- Regenerate: `bash docs/submission/sync.sh`. Rules (CLAUDE.md): never edit the `.docx` or `.pptx` by hand; change the builders. Never state a number not in EVIDENCE.md, `results/` or a command output. Back up before overwriting. The default tools-venv path in `sync.sh` is a scratch path from an earlier session; set `TOOLS_VENV` to your own venv.
- Open items from the supervisor audit (2026-11-20, verdict NOT READY at that time): deployment text must say Elastic IP and live mode, CI/CD must appear in the deliverables, code ZIP not built (`git archive`), slide 12 needs deployed-site screenshots, contents page numbers are estimates until opened in Word. Check the current state of `docs/submission/SUPERVISOR_AUDIT.md` before the assessment.

---

# PART 5. The parts others own, explained so you can defend them

**Plain statement.** The TEAM.json responsibility split is a PROPOSAL to be confirmed by the team; git history shows a single committer (Astitva). In the viva say "in our proposed split X leads this" and be ready to explain every component yourself.

## Script: "If the professor asks me about X and I am not the owner"
1. Do not freeze and do not bluff. Say: "In our proposed split [name] leads this, but I can explain how it works."
2. Give the one-sentence version (below), then the analogy, then ONE concrete detail with a file name.
3. If asked something deeper than you know: "I would need to check the code for that detail; the file is [path]. What I can say for sure is [safe fact]." Offer to show it live.
4. Never invent a number or a capability; use the "never claim" list in Part 7.

## Pulkit: blackboard, A2A, dashboard
- **One sentence (blackboard):** a signed, append-only SQLite store where agents publish validated reports and read each other's, with permissions and receipts, so intermediate context is shared and tamper-evident (see 2.2).
- **One sentence (A2A):** the CrewAI Forensic Agent is a small web service with an Agent Card; ADK's `RemoteA2aAgent` sends it a validated JSON message carrying digests and blackboard references, and gets a validated reply (see 2.5).
- **One sentence (dashboard):** a Starlette web app with a PDF intake, one-at-a-time investigation jobs, report/blackboard/trace views, human review, two security labs, and an evaluation tab (see 2.11).
- **Detail to remember:** the Forensic Agent gets the Evidence FACTS and missing information only; it cannot read the Investigator's report (`CONTEXT_READERS`).

## Astitva: agent design, orchestration, adjudicator
- **One sentence (design):** three specialist agents with different frameworks, tools and information, producing independent opinions, none of which can decide.
- **One sentence (orchestration):** a deterministic manager builds a typed five-task plan with a state machine, gates the Investigator and Forensic tasks on the published Evidence report, and runs them in parallel (2.6).
- **One sentence (adjudicator):** it recomputes the rules decision, accepts only verified agent findings, and can only escalate, never approve; money is Decimal (2.7).
- **Detail to remember:** "35 rules" means 35 of 40 cases decided by rules, 5 by guardrail escalation.

## Sibi: MCP, guardrails/callbacks/security, observability
- **One sentence (MCP):** two read-only tools on a separate server with a per-call log and a server-side scope rule that stops history lookups outside the claim (2.3).
- **One sentence (guardrails):** injection screen, untrusted wrapping, output check, MCP scope, blackboard integrity, and an adjudicator that cannot be talked into approving (2.8).
- **One sentence (observability):** a single trace per run assembled from callbacks, event bus, MCP log, A2A messages and blackboard entries, with measured latencies and provider-reported tokens only (2.9).
- **Detail to remember:** callbacks mostly record; `before_model` enforces the request budget.

---

# PART 6. Viva question bank

Stars mark questions most likely aimed at your own area. Answers cite file:line or a results file. Dates: test counts 2026-11-06 (Windows) and 2026-11-19 (macOS); experiments 2026-11-19; deployment 2026-11-19/20.

## 6.A Concepts (1-20)
1. **What is ClaimSleuth?** A multi-agent warranty-claim investigator: ADK and CrewAI agents recommend; a deterministic adjudicator decides and computes the payout (0.4).
2. **What is an agent versus an LLM?** An LLM only turns text into text; an agent is an LLM with instructions, tools and a loop that can call tools and use results (1.2).
3. **Why multi-agent instead of one big agent?** Independent opinions, separation of duties, fault isolation, different tool sets. Cost: more calls and latency. Honest: the benchmark shows no decision change from the agents.
4. **Why do agents only recommend?** Money and legal outcomes must be reproducible and manipulation-proof; the adjudicator recomputes the rules (src/claimsleuth/integration/adjudicator.py:125).
5. **What is structured output and why?** The model must return JSON matching a Pydantic schema; validated again afterwards (2.1).
6. **What is MCP?** A protocol for discovering and calling tools on a server (2.3).
7. **What is A2A?** Agent Card plus JSON-RPC between agent services (2.5).
8. **What is a blackboard?** A shared workspace for intermediate results; ours is signed, append-only and permissioned (2.2).
9. **What is prompt injection?** Text in untrusted input that tries to instruct the model ("ignore all previous instructions"). The sample WC-2026-0412 has one in the repair report (PDF p.3, lines 12-14, README).
10. **What is provenance?** For each fact: document, page and line. Citations are re-checked against the document text.
11. **What does "fail closed" mean here?** If anything cannot be verified, the run becomes incomplete and a human decides; nothing is silently approved.
12. **What is the difference between COMPLETED, INCOMPLETE, BLOCKED, ESCALATED?** src/claimsleuth/observability/outcome.py:4 (the docstring defines them).
13. **What is a guardrail?** A check on input or output that blocks, refuses or escalates. Examples: injection screen, CrewAI task guardrail, restricted-output refusal.
14. **What is an Agent Card?** A JSON description of an A2A agent at `/.well-known/agent-card.json` (src/claimsleuth/crew_service/a2a_server.py:100).
15. **Define ReAct.** Reason and act loop: Thought, Action (tool call), Observation, repeat. Our E5 variant adds this wording; the doubles ignore it.
16. **What is WAL?** Write-ahead logging in SQLite: readers do not block the writer; needs one host.
17. **What does deterministic mean for the adjudicator?** Same input, same output, no model involved (rules.py docstring).
18. **Why is money a Decimal?** Floats round wrongly; payouts must be exact (`Decimal`, half-up rounding).
19. **What is human-in-the-loop here?** REJECT, ESCALATE, incomplete reviews and payouts of 15,000 INR or more need a human; the review is recorded separately and never changes the automated decision (src/claimsleuth/dashboard/reviews.py:4).
20. **What does the Evidence Agent output?** `EvidenceReport`: facts with citations, findings, missing information, recommendation, confidence (src/claimsleuth/schemas.py:276).

## 6.B Architecture (21-45)
21. **Draw the architecture.** Use 1.5/1.6.
22. **Where is the manager agent in the hierarchical pattern?** It is deterministic code, not an LLM, so no model can reorder, skip or approve work (src/claimsleuth/orchestration/manager.py:3). Say it openly.
23. **Which patterns are framework-native?** Only sequential (`SequentialAgent`). Parallel is `asyncio.gather`; hierarchical is our plan and state machine.
24. **Prove Investigator and Forensic run at the same time.** Test tests/test_hierarchical_orchestration.py:201; live run `RUN-dash-bcdbabaf97` both began at 08:53:32 UTC (EVIDENCE.md). Limits: the CrewAI service serialises its own runs; the dashboard runs one investigation at a time.
25. **Why does Forensic wait for Evidence?** In blackboard mode it consumes the Evidence facts; the manager waits for the published report (src/claimsleuth/orchestration/manager.py:287).
26. **Why does Forensic see less than the Investigator?** Independence of opinions (src/claimsleuth/blackboard/controller.py:109).
27. **Why two virtual environments?** Dependency conflict between ADK and CrewAI (1.7).
28. **Why A2A if both are your processes?** Different environments; standard discoverable interface; honest limits (2.5).
29. **How many agents use A2A?** One (Forensic). The client is the manager via a non-LLM proxy.
30. **What exactly is in an A2A request?** Claim id, SHA-256 digest per document, ContextRefs, metadata (src/claimsleuth/contract.py:42).
31. **What if the digests differ?** The service replies ERROR `EVIDENCE_MISMATCH` (src/claimsleuth/contract.py:84).
32. **How does the Investigator get the Evidence report?** Through `consume()` on the blackboard; the verified copy replaces the session-state key (src/claimsleuth/blackboard/adk_plugin.py:116).
33. **What is a ContextRef?** A claim-check: run, claim, section, version, hash, fingerprint; not content.
34. **What is sealing?** After agents finish, the controller seals the run; agents can no longer consume; the adjudicator reads only sealed inputs (`consume` conditions).
35. **What if the blackboard fails?** Reports are not consumed or adjudicated; the run is incomplete; a human decides (controller docstring).
36. **Why SQLite and what breaks in the cloud?** It needs one host and a local disk; so one VM; scaling out needs Postgres/Redis (4(a)).
37. **What if two users run at once?** The dashboard allows one investigation at a time (`JobBusyError`); the CrewAI service also serialises runs; SQLite WAL and `BEGIN IMMEDIATE` protect writes.
38. **What does the baseline do in the pipeline?** It runs first, deterministically; the adjudicator recomputes it and escalates on a mismatch.
39. **What is the execution plan?** Five typed tasks with dependencies and a validated state machine (src/claimsleuth/orchestration/plan.py:26).
40. **What happens if the Evidence Agent fails?** Investigator and Forensic are BLOCKED; no A2A request is sent (`not_sent`); the adjudicator sees no agent review; the run is INCOMPLETE/BLOCKED and a human decides.
41. **What are the deadlines?** Component timeout plus 60 s margin; a passed deadline cancels the task and marks it TIMED_OUT (src/claimsleuth/orchestration/manager.py:64).
42. **What is the A2A timeout?** 180 s default, 450 s in live (src/claimsleuth/integration/run.py:51), valid range 30 to 3600 s.
43. **How are MCP calls tied to a run?** Headers with run id, claim id, caller; the server logs each call; the manager fetches that log and the adjudicator cross-checks it.
44. **Which ports?** 8110 dashboard (local), 7860 in the container, 8102 MCP, 8103 CrewAI A2A, 8122/8123 evaluation services, 8142/8143 experiments (defaults in `experiments.py`).
45. **Why is the blackboard "not just coordination"?** It carries the Evidence report to two later agents.

## 6.C Frameworks (46-75)
46. **What ADK classes do you use?** `LlmAgent`, `SequentialAgent`, `Runner`, `App`, `InMemorySessionService`, `McpToolset`, `RemoteA2aAgent`, `BasePlugin`.
47. **What does `output_key` do?** Stores the agent's structured output in session state under that key (`evidence_report`, `investigation_report`).
48. **What is `{evidence_report?}` in the Investigator instruction?** A template placeholder filled from session state; `?` means optional (src/claimsleuth/adk_service/agents.py:5).
49. **What ADK callbacks are registered?** before/after agent, model, tool and on_tool_error/on_model_error (src/claimsleuth/adk_service/callbacks.py:97).
50. **Do callbacks enforce anything?** `before_model` takes a ledger slot and refuses when the budget is gone; others record. Do not claim they screen prompts.
51. **What is `max_llm_calls`?** 30 offline, 10 live per run (src/claimsleuth/adk_service/config.py:14).
52. **How does the 503 retry work in ADK?** `on_model_error` re-sends once after 15 s through the ledger; a success replaces the error (src/claimsleuth/adk_service/callbacks.py:228).
53. **What CrewAI classes?** `Agent`, `Task`, `Crew`, `Process.sequential`, `MCPServerHTTP`, event bus.
54. **Why a crew of one?** Course needs both frameworks and a real A2A boundary (2.5).
55. **What is `max_retry_limit=0`?** CrewAI will not re-run the agent after an error; failures must surface (src/claimsleuth/crew_service/crew.py:148).
56. **Guardrail retries?** 2 offline, 1 live.
57. **What does the CrewAI guardrail check?** Restricted content, strict schema, claim id, citations quote the documents, history references returned by MCP (src/claimsleuth/crew_service/crew.py:113).
58. **What is the network guard?** Python socket patch that refuses non-loopback connects (plus the Gemini host in live); a safety check, not a sandbox (src/claimsleuth/crew_service/network_guard.py:9).
59. **How does CrewAI call MCP?** Native `MCPServerHTTP` with headers and a static tool filter allowing only `claim_history_lookup`.
60. **Which Gemini model?** `gemini-3.5-flash` for all three agents (src/claimsleuth/execution_mode.py:28).
61. **Why not Gemini 2.5?** Old viva documents say 2.5; they are stale. The code uses 3.5.
62. **Temperature/seed?** None set; live outputs vary, the decision does not.
63. **What is `set_model_response`?** ADK's internal tool by which the model returns its structured output when tools and schema are both used.
64. **What does `McpToolset` do?** Connects to the MCP server, lists tools, and exposes allowed ones (`tool_filter`) to the agent.
65. **How are headers set per call?** `header_provider=run_id_header` reads session state (src/claimsleuth/adk_service/agents.py:95).
66. **What does the offline ADK double do exactly?** Plans tool calls (policy search; for the Investigator two history lookups), then builds a report by fixed rules (src/claimsleuth/adk_service/test_double.py:121).
67. **Does the double read blackboard context?** No; it reads only the packet in the request contents. Blackboard on or off gives identical offline outputs.
68. **What is the obedient double?** A test double that obeys planted instructions, to test the safeguards (src/claimsleuth/obedient_double.py:1).
69. **What is `RunConfig`?** ADK per-run configuration; we set `max_llm_calls`.
70. **How are sessions cleaned up?** Runner closed (closes MCP sessions) and session deleted in `finally` (src/claimsleuth/adk_service/runner.py:310).
71. **Why "fresh agent objects per run"?** ADK agents cannot have two parents.
72. **What does the A2A server serialise?** CrewAI runs, because its event-bus handlers are process-wide.
73. **What A2A version?** A2A 1.0, a2a-sdk 1.3.0, JSON-RPC (src/claimsleuth/contract.py:27).
74. **Where do MCP SDK versions come from?** 2.3 for the server and ADK side; 1.30 inside CrewAI's environment (README, Technologies).
75. **What is ExecutionRecorder?** The object that implements all ADK callbacks and accumulates the run record (src/claimsleuth/adk_service/callbacks.py:71).

## 6.D Security (76-95)
76. **What stops prompt injection?** Seven layers (2.8). The decisive one is the adjudicator that no agent can make approve.
77. **Can your regex screen be bypassed?** Yes: paraphrases without a pattern are missed; normalisation covers invisible characters, look-alike letters, spaced words; nothing downstream relies on detection alone.
78. **Does offline detect injection with an LLM?** No; the doubles use the same regex (`detect_injection`).
79. **What does the lab prove?** Safeguards around a manipulated (obedient) double, not how Gemini behaves.
80. **Can an agent read another customer's history?** Not inside an investigation (scope rule); except unscoped operator calls without a claim header (src/claimsleuth/mcp_server/server.py:87).
81. **What is checked on agent output?** Credentials, 64-hex key material, its own instructions (src/claimsleuth/security.py:164).
82. **What happens to an abusive claim?** Logged, never decides (`abusive_language`, action "logged").
83. **How is the blackboard protected?** Signed entries, append-only triggers, read permissions, receipts, seal; weakness: keys readable by the same OS user.
84. **Is MCP authenticated?** No. Inside one container on loopback.
85. **Is A2A authenticated?** No.
86. **Is the website protected?** Yes: Caddy basic auth over HTTPS (hashed password). The dashboard has no login itself.
87. **Is the reviewer authenticated?** No; the name is typed.
88. **What if an agent claims a tool result it did not get?** The adjudicator accepts a history reference only if the agent's recorded call returned it and the server log confirms it (src/claimsleuth/integration/adjudicator.py:38).
89. **What if the MCP log is empty but agents recorded calls?** Contradiction: records count as unavailable; human review; decision unchanged (src/claimsleuth/orchestration/manager.py:72).
90. **What if only some MCP records are lost?** A known gap: judged call by call, not flagged (EVIDENCE.md).
91. **What are the CSP headers?** CSP, nosniff, no-referrer (`SecurityHeaders`).
92. **Is TLS used?** Yes, Let's Encrypt via Caddy.
93. **Where is the Gemini key?** `deploy/.env` on the VM only, chmod 600.
94. **Could the key be in the repo?** CI fails on `AIza...` patterns outside tests; the audit found none in tracked files.
95. **What does `restricted_output` NOT flag?** Values the claim's own documents contain (quoting a serial is the agent's job).

## 6.E Evaluation and statistics (96-115)
96. **How many cases and categories?** 40 synthetic, 8 per category (5 categories).
97. **How is ground truth made?** By hand next to each case; not computed by the system; never shown to agents.
98. **Did you use LLM-as-judge?** No; exact match against reference labels.
99. **Why 100% accuracy?** The rules engine decides; offline agents are doubles; 35 decided by rules, 5 by guardrail escalation (EVIDENCE.md). It validates the pipeline, not reasoning.
100. **Single-agent accuracies?** Baseline rerun: Evidence 65.0%, Investigator 92.5%, Forensic 75.0% on the decision, reference = stored expected decision (EVIDENCE.md). They are advisory recommendations from doubles.
101. **Why is accuracy identical across architectures?** By construction (2.13/4(h)).
102. **What do E3 and E4 show then?** Time, calls and context passed (4(h) table).
103. **What is E5?** Persona x prompt structure; 116 runs; 0 of 58 changed.
104. **Is the persona set balanced?** No (honest 38, vague 6, abusive 6, injection 8 per structure).
105. **What is variance?** Across cases only; offline is deterministic, so repeats would be identical; no live variance.
106. **Median wall time per architecture?** 24.7 s sequential, 12.2 s parallel-only, 24.3 s hierarchical + blackboard (laptop, offline).
107. **Why is parallel-only about half the time?** Evidence and Investigator overlap; no Evidence-to-Investigator context.
108. **Why is hierarchical not faster than sequential?** Forensic is 0.5 s; critical path is Evidence then Investigator.
109. **How is a box plot read?** 4(h).
110. **What is the p95?** The value below which 95% of runs fall; computed by linear interpolation (src/claimsleuth/evaluation/experiments.py:285).
111. **Offline cost?** 0 INR: no model billed, 0 Gemini requests (ledger delta 0).
112. **Live cost?** Not measured over 40 runs; 6 to 9 requests per claim recorded.
113. **Tokens?** Only provider-reported; the offline doubles report none; the partial VM run has them.
114. **Dataset leakage?** Case ids are neutral and shuffled so id/order do not reveal category; labels are separate; but the same team wrote rules and labels, so agreement is expected.
115. **What would a stronger evaluation need?** Live runs on a paid key, repeated runs, cases where rules approve but a planted signal should escalate, an independent labeller.

## 6.F Deployment, AWS, Docker, CI/CD (116-150, mostly yours)
116. ★ **What is deployed where?** One AWS EC2 t4g.small in ap-south-1: Docker Compose with Caddy and the single app image; URL https://13-204-3-14.sslip.io/ (DEPLOYED.txt).
117. ★ **Why one VM and not Cloud Run, ECS or Kubernetes?** SQLite WAL blackboard needs one host and a local disk (4(a)).
118. ★ **Why no load balancer?** One instance; Caddy does TLS; an ALB adds cost and no benefit.
119. ★ **Why ARM/Graviton?** Cheaper; images are multi-arch; builds natively.
120. ★ **What is an Elastic IP and is it free?** A fixed public IPv4; billed about 0.005 USD/h, also while stopped; `teardown.sh` releases it.
121. ★ **Why did the URL change on 2026-11-19?** The stop/start test happened before the Elastic IP was attached; a plain stop/start gives a new public IP. After the Elastic IP the URL is stable. The old `65-2-175-255` URL is dead.
122. ★ **What does the instance role do?** There is none; the app calls no AWS API.
123. ★ **What is IMDSv2 and why required?** Token-based metadata service; blocks simple request-forgery access to instance metadata.
124. ★ **What is user data?** First-boot script (Docker, swap, log rotation, systemd unit).
125. ★ **What does `boot.sh` do?** Derives the sslip.io hostname from the public IP, then `docker compose up -d --build` (4(d)).
126. ★ **How does the site survive a reboot?** `claimsleuth.service` runs `boot.sh`; compose `restart: unless-stopped`.
127. ★ **What does teardown do?** Terminates the instance (disk deleted), releases the Elastic IP if recorded, deletes the security group and key pair. Not snapshots.
128. ★ **How much does it cost?** About 14 USD/month at the Pricing API rates in the plan; roughly 35 USD until the plan ends; stopped costs disk plus Elastic IP.
129. ★ **Stop vs terminate?** Stop keeps the disk (billed); terminate destroys it.
130. ★ **Why two venvs in Docker?** Dependency conflict; both in one image.
131. ★ **Why multi-stage build and pruning?** Smaller image: 1.74 GB to 911 MB.
132. ★ **Why non-root user?** Least privilege; uid 1000 (also a Hugging Face convention).
133. ★ **What is the healthcheck?** Fetch `/` every 30 s; unhealthy after 5 failures; auto_update waits for it.
134. ★ **What are volumes for?** Keep the blackboard, runs and certificates across container rebuilds.
135. ★ **How does HTTPS work without a domain?** sslip.io resolves the IP-with-dashes name; Let's Encrypt issues a certificate.
136. ★ **Where is the password stored?** Only as a bcrypt hash in `deploy/.env` on the VM; the plain password is held by the team.
137. ★ **What does CI do?** compileall, `bash -n`, secret grep, Docker build (4(e)).
138. ★ **Does CI run the tests?** No: they take about 45 minutes; run locally.
139. ★ **If CI fails, does it still deploy?** Yes: the VM pulls `main` regardless of CI. A bad build is caught only by the container healthcheck, which triggers a rollback.
140. ★ **Why pull-based deployment?** Public repo, no secrets in GitHub, no inbound SSH.
141. ★ **How fast does a merge reach the VM?** Detection up to about 60 s; rebuild time not measured. Say "picked up within about a minute".
142. ★ **What does rollback do?** If the new commit is not healthy within about 10 minutes, restore the previous commit, rebuild, mark the bad commit to skip.
143. ★ **What if the rollback also fails?** The script prints "rollback also failed; inspect: docker compose logs app"; the site stays down until someone fixes it.
144. ★ **How do you pause auto-deploy?** `AUTO_UPDATE=off` in `deploy/.env` on the VM.
145. ★ **How do you rotate the Gemini key?** New key, edit `deploy/.env`, `sudo bash deploy/boot.sh`, test, revoke old (4(f)).
146. ★ **What if two auto-updates overlap?** `flock` prevents it; the timer also waits 60 s after the previous run ends.
147. ★ **What is branch protection and is it on?** A GitHub rule requiring checks and reviews before merging to `main`; not confirmed.
148. ★ **Why does rsync exclude docs and tests?** They are not needed to run the app and keep the VM copy small; also `deploy/.env` and runtime are excluded so state and secrets survive.
149. ★ **What happens when Gemini returns 503?** The agent retries once after 15 s through the ledger; if that fails the agent is FAILED with the classified error, valid upstream reports are preserved, the run is INCOMPLETE and a human decides. Real example: `RUN-dash-6d15294eca`.
150. ★ **What if the daily quota is exhausted?** `GeminiQuotaGuard` refuses the request before sending; every agent FAILED; the adjudicator still ESCALATEs; switch to offline (`MODE=offline`, `boot.sh`).

## 6.G Cost (151-158)
151. ★ **What does one live claim cost?** 6 to 9 Gemini requests (4 complete runs: 9, 6, 7, 7); free tier: nothing billed.
152. ★ **What would 40 live runs cost?** 240 to 360 requests by the recorded range; about 400 by the code estimate; 12 to 18 days on the free tier; no rupee figure exists.
153. ★ **Why no rupee figure?** No verified price table ships and tokens are reported only for some runs.
154. ★ **What are the request caps?** 20 per day, 5 per minute, enforced in the app (src/claimsleuth/gemini_rate.py:31).
155. ★ **When does the day reset?** Midnight Pacific time (src/claimsleuth/gemini_rate.py:68).
156. ★ **Infrastructure cost per month?** About 14 USD (instance 8, IPv4 3.6, disk 2).
157. ★ **Does the ledger match Google's quota?** Not necessarily; it counts only this app's requests.
158. ★ **Could the Elastic IP surprise you?** Yes: it is billed even while stopped; release it at teardown.

## 6.H Limitations (159-170)
159. **What is the biggest weakness?** Offline the agents are scripted and changed 0 of 40 decisions; live evidence is 4 complete runs plus 1 partial.
160. **Is the offline mode "AI"?** No. Rule-based test doubles; everything around them is real.
161. **Why only 4 live runs?** Free-tier quota, 20 requests/day/model.
162. **What did the live runs show?** The system works end to end on a real model; not accuracy.
163. **Is the manager an agent?** No.
164. **Is the pub/sub arm built?** No.
165. **Is the deployment production-grade?** No: one VM, one login, no autoscaling, SQLite, unauthenticated internal services.
166. **Is the PDF intake general?** No: fixed layout with `Label: value` lines; scanned PDFs rejected.
167. **Is the data real?** All synthetic; currency INR.
168. **Were the tests run on the VM?** No; the VM was checked with the four sample claims.
169. **Is the macOS test result clean?** One existing test fails there (795 passed, 1 failed); unexplained difference from Windows.
170. **Is the Ollama mode working?** An older experiment; its one recorded run failed (4,096-token context truncated the prompt).

## 6.I Gotcha and trap questions (171-190)
171. **"Is this really LLM reasoning offline?"** No. `OfflineAdkLlm` and `ForensicTestDoubleLLM` follow fixed rules; the reports even say so. Real reasoning exists only in the 4 live runs and the partial VM run.
172. **"Why is accuracy 100%?"** See 99.
173. **"Delete the agents; does anything change?"** On the benchmark, no. By design they add cited extraction, an audit trail and a one-way safety check. We have no measured case where a live agent changed a decision.
174. **"What happens when Gemini returns 503?"** See 149.
175. **"What if two users run at once on SQLite?"** See 37: second user is told the system is busy; WAL handles concurrent processes safely.
176. **"What if CI fails, does it still deploy?"** Yes (139).
177. **"How do you rotate the API key?"** 145.
178. **"What does teardown do?"** 127.
179. **"Your blackboard is coordination only, right?"** No; that is an outdated claim in old docs; it carries Evidence to two later agents.
180. **"Show me where the 28% token saving with ReAct came from."** It never existed; it is in an old unverified document. We built a ReAct/evidence-first variant (E5) and measured no decision change and no token claim.
181. **"Your latency was 2,450 ms for LLM inference?"** No such measurement exists; the old viva file is unverified. Real numbers: Part 4(g).
182. **"Is it on Cloud Run?"** No: AWS EC2. Old documents mention Cloud Run; stale.
183. **"Which Gemini model and where does it say?"** `gemini-3.5-flash` (src/claimsleuth/execution_mode.py:28).
184. **"35 rules?"** 12 checks plus settlement; "35" is a count of cases decided by rules.
185. **"The VM took 3 s per claim; live took a minute. Which is the deployment performance?"** The 3 s is OFFLINE (doubles); live is dominated by Gemini (57.8 s and 23.6 s in the partial run).
186. **"Does the site run live or offline now?"** Live since 2026-11-19; offline stays selectable.
187. **"Can I open the site now and run a live claim?"** Yes, within the 20 requests/day cap; about 2 to 3 claims; offline is the fallback.
188. **"Where is the evidence of the four live runs?"** The run folders are in `runtime/` on the Windows machine, not in git; EVIDENCE.md has the table; the VM partial run is saved as a JSON in `results/`.
189. **"Who wrote the code?"** Git shows one committer; the responsibility split is a proposal. Do not claim authorship you cannot defend.
190. **"Did you use any other agent framework?"** No: only ADK and CrewAI (no langchain/autogen etc. imported in `src/`).

## 6.J Extreme / follow-up (191-205)
191. **How would you scale this?** Replace SQLite with Postgres/Redis, split MCP and CrewAI into services with authentication, add a queue for runs, add an ALB and an autoscaling group.
192. **How would you make A2A secure?** mTLS or tokens, signed agent cards, network policies.
193. **How would you prove agents add value?** Ablation: rules only vs rules plus agents on live runs with cases where planted signals are not encoded in rules.
194. **What if the model paraphrases an injection?** The screen misses it; the untrusted wrapping, output check, scope rule and adjudicator still hold; the model could still produce a wrong recommendation, but it cannot approve.
195. **What if Gemini changes its output format?** Pydantic validation fails; the agent is marked output-invalid; fail closed.
196. **How do you test fail-closed behaviour?** Injected faults at local boundaries (reliability, service faults suites); mutation checks 6 of 6 detected (2026-11-06).
197. **What is a mutation check?** Deliberately break the code and confirm a test fails.
198. **How would you reduce the 12 s per ADK agent offline?** First find the cause (not isolated); do not guess.
199. **How would you reduce live latency?** Fewer calls (already batched tool calls), smaller prompts, a faster model, parallelising Evidence and Investigator at the price of context.
200. **Why not use the blackboard for everything?** Control flow in a fixed graph is safer and auditable; a control component choosing knowledge sources would be less predictable.
201. **What is the hash and signature check cost?** Measured per operation and shown in the trace (blackboard publish/consume latencies); not quoted here because no figure was extracted for this guide.
202. **Why Decimal and half-up?** Exact money arithmetic, deterministic rounding.
203. **How do you stop a runaway agent?** Per-run LLM-call caps, timeouts, manager deadlines, request ledger.
204. **What is idempotency in the dashboard?** An idempotency store prevents duplicate requests (`IdempotencyStore` in `app.py`).
205. **What would you do with another week?** Live runs on a paid key, branch protection, measure merge-to-healthy time, a pub/sub arm, authenticated internal services.

---

# PART 7. Honest limitations, "never claim" list, demo scripts, cheat sheet, glossary

## 7.1 Honest limitations (say them before the professor does)
- Default mode is offline: the agents are rule-based doubles. Real LLM behaviour exists only in 4 complete live runs (local, 2 claims) and 1 partial live run on the VM.
- No live accuracy, no live cost over 40 runs, no repeated runs, no live variance.
- The 40-case accuracy (40/40) is the rules engine's, on synthetic cases labelled by the same team; the agents changed 0 of 40 decisions.
- E3/E4/E5 differ only in time, calls, context passed and failure behaviour; accuracy is identical by construction. The publish/subscribe broadcast arm was not built.
- The manager is deterministic code; parallel is `asyncio.gather`; the crew has one agent; only one A2A link.
- MCP and A2A are unauthenticated; the reviewer name is typed; the review lock works inside one process; the injection screen is a pattern list.
- Partial MCP-record loss is not flagged (known gap).
- One VM, one shared login, SQLite, one investigation at a time. Rebuild time on the VM and merge-to-healthy time were not measured. CI runs no tests and the VM deploys `main` whatever CI says.
- The live site on the free tier allows about 2 to 3 live claims per day.
- Laptop offline latency (about 12 s per ADK agent) has an unisolated cause; VM offline latency is about 3 s; neither is Gemini latency.
- macOS test rerun: 795 passed, 1 failed (unexplained); the suites were not run on the VM.

## 7.2 Things never to claim
1. That offline agents "reason", "think", "decide" or are "AI".
2. Any live accuracy percentage.
3. "Only green builds deploy" or "deploys within one minute".
4. "28% fewer tokens with ReAct", "2,450 ms inference", or anything from `MASTER_VIVA_QA_COMPREHENSIVE.md` or the two older `.docx` design files.
5. That it runs on Cloud Run, Kubernetes, or Gemini 2.5.
6. That the blackboard is "coordination only", or that agents negotiate through it.
7. That the manager is an LLM agent, or that parallel uses ADK `ParallelAgent`.
8. That `RUN-dash-6d15294eca` is a complete live run, or that there are 5 complete live runs.
9. That a pub/sub broadcast comparison exists.
10. That the team split is agreed (it is proposed).
11. That the VM numbers (3 s, 445/266/1,142 ms) are live numbers.
12. That the site has per-user logins, or that internal services are authenticated.
13. That "35 rules" exist; say 35 of 40 cases were decided by the rules.
14. Never say or write the site password or the Gemini key anywhere (slides, chat, repo).

## 7.3 60-second demo script (site up, offline or live)
1. Open https://13-204-3-14.sslip.io/ , log in as `claimsleuth` (password held by the team). Point at the mode pill: "Mode: LIVE" or "OFFLINE".
2. Click sample card WC-2026-0412 ("Use this claim"), click **Run Investigation**.
3. While it runs say: "Three agents, two frameworks, A2A and MCP; they only recommend."
4. When done: show the decision ESCALATE, the injection flagged with PDF page and line, and "Human Review & Evidence".
5. Close: "The decision and every rupee come from the deterministic adjudicator."

## 7.4 5-minute demo script
Follow Part 8, section 8.4 steps 1 to 9 and skip the labs; keep one sentence per panel.

## 7.5 One-page cheat sheet
- **Agents:** Evidence (ADK, policy search), Investigator (ADK, policy search + history), Forensic (CrewAI over A2A, history). Recommend only.
- **Decider:** deterministic adjudicator; REQUEST_EVIDENCE/ESCALATE/REJECT/APPROVE; human if reject, escalate, incomplete or payout >= 15,000 INR.
- **Mode:** offline default (doubles); live = `gemini-3.5-flash`; no fallback.
- **Numbers:** 801 + 155 tests (Windows, 2026-11-06); 795 + 1 failed + 155 (macOS 2026-11-19); 40 cases, 40/40, 35 rules + 5 guardrail; 236 experiment runs; E3 medians 24.7 / 12.2 / 24.3 s; E4 same; E5 116 runs, 0 of 58 changed; context shared 4,740 / 2,370 / 0 chars; 4 complete live runs (9, 6, 7, 7 requests); 1 partial VM run (7 requests, Forensic 503); VM offline claim about 3 s; image 911 MB; cost about 14 USD/month; quota 20/day, 5/min.
- **Deploy:** EC2 t4g.small, ap-south-1, Ubuntu 24.04 ARM, 20 GB gp3, IMDSv2, Elastic IP 13.204.3.14, Docker Compose, Caddy + Let's Encrypt on sslip.io, basic auth.
- **CI/CD:** GitHub Actions (compile, bash -n, secret grep, docker build; no pytest) + pull-based systemd timer every minute, rollback if unhealthy; `AUTO_UPDATE=off` for the assessment.
- **Honesty lines:** accuracy identical by construction; no live accuracy; no live cost over 40 runs; no pub/sub arm.

## 7.6 Glossary
- **ADK**: Google's Agent Development Kit (framework).
- **Adjudicator**: deterministic final decision-maker.
- **Agent**: LLM plus instructions plus tools plus loop.
- **Agent Card**: JSON description of an A2A agent.
- **AMI**: machine image (OS template) for EC2.
- **A2A**: Agent2Agent protocol.
- **Append-only**: rows may be added, never changed or deleted.
- **ARM / Graviton**: AWS's ARM CPU family.
- **Availability zone**: a data centre inside a region.
- **Basic auth**: browser username/password prompt.
- **Blackboard**: shared signed workspace.
- **Box plot**: chart of median, quartiles, whiskers, outliers.
- **Caddy**: web server/reverse proxy with automatic HTTPS.
- **CI/CD**: automatic checking and delivery.
- **Container / image**: running instance / frozen package of an app.
- **ContextRef**: reference (not content) to a blackboard entry.
- **CrewAI**: agent framework (Agent, Task, Crew).
- **Decimal**: exact decimal number type.
- **EBS / gp3**: virtual disk / its type.
- **EC2**: AWS rented computers.
- **Elastic IP**: fixed public IP you own (billed).
- **Fail closed**: when in doubt, escalate to a human.
- **flock**: file lock preventing two runs at once.
- **Graviton**: see ARM.
- **Guardrail**: input/output safety check.
- **HMAC-SHA256**: keyed hash used to sign entries.
- **IAM**: AWS permissions system.
- **IMDSv2**: token-protected instance metadata service.
- **INR**: Indian rupees.
- **JSON-RPC**: request/response protocol using JSON.
- **Ledger (request)**: SQLite record of every Gemini request.
- **LLM**: large language model.
- **MCP**: Model Context Protocol.
- **Offline test double**: deterministic stand-in for a model.
- **Persona**: style of claimant text (honest, vague, abusive, injection).
- **Provenance**: source page/line of each fact.
- **Prompt injection**: hidden instructions in untrusted text.
- **Pydantic**: data validation library.
- **Region**: geographic AWS area.
- **Reverse proxy**: front server forwarding to the app.
- **rsync**: file-sync tool (`--delete` removes files not in source).
- **Runner (GitHub)**: machine that executes workflow jobs.
- **Security group**: AWS firewall.
- **Seal**: freezing a blackboard run.
- **sslip.io**: DNS service mapping `a-b-c-d.sslip.io` to IP a.b.c.d.
- **systemd**: Linux service manager; timer = scheduled unit.
- **t4g.small**: 2 vCPU ARM, 2 GB instance type.
- **Terminate / stop**: destroy / power off an instance.
- **User data**: first-boot script.
- **VPC**: private AWS network.
- **Volume**: persistent Docker storage.
- **WAL**: SQLite write-ahead logging mode.
- **Workflow (GitHub Actions)**: YAML pipeline file.

---

# PART 8. Presentation and live demo flow

Slides come from `docs/submission/build_ppt.py` (15 slides, numbered 0 to 14, structure fixed by the course brief in `.claude/agents/ppt-agent.md`). Presenter assignments below are a PROPOSAL (like the team split); agree them with the team. Slide count is 15; the deck footer numbers slides 1 to 14 after the title slide (title is slide 0).

## 8.1 Recommended slide order, what to say, who presents (13 to 14 minutes without the demo; the demo is interleaved, see 8.6)
| Slide | Title (from `new_slide`) | Say (one to three sentences) | Presenter (proposed) | Time |
|---|---|---|---|---|
| 0 | Title, names, enrolments | "ClaimSleuth, multi-agent warranty claim investigation; four team members." Say the team split is proposed. | Vaibhav | 0:20 |
| 1 | Objectives and problem statement | Manual checking is slow and manipulable; objective is agents that recommend while deterministic code decides money. Expect "why not one big LLM?": narrow agents fail independently and can be cross-checked. | Astitva | 1:00 |
| 2 | Literature review and gap | One table; the gap: few systems combine two frameworks, shared signed state, deterministic decision authority and cloud deployment. Source rows are in `content_shared.py`. | Astitva | 0:45 |
| 3 | Agent requirements mapped to gaps | R1..R11 mapped to rows. Do not read all; pick three. | Astitva | 0:45 |
| 4 | MAS architecture: hierarchical manager | Walk the flow: Evidence, then Investigator and Forensic in parallel, blackboard, MCP, A2A, adjudicator. Say "deterministic manager, sequential stage then parallel stage". START THE LIVE CLAIM HERE (see 8.6). | Astitva | 1:30 |
| 5 | Architecture evaluation | E3 medians 24.7 / 12.2 / 24.3 s; accuracy 40/40 identical by construction; the choice rests on time, context passed and design heuristics. | Vaibhav | 1:15 |
| 6 | Agent-tool communication via MCP | Two tools; scope rule; per-call log; adjudicator cross-checks. | Sibi | 1:00 |
| 7 | Messaging: blackboard versus peer-to-peer | E4 table (4,740 / 2,370 / 0 chars shared); only Forensic uses A2A; pub/sub arm not built. | Pulkit with Vaibhav for the numbers | 1:15 |
| 8 | Securing the multi-agent system | Seven layers; state the regex limit; labs are offline. | Sibi | 1:00 |
| 9 | Experimental design | 40 cases, 8 per category, personas, 236 runs, no live variance. | Vaibhav | 0:45 |
| 10 | Validation | Ground truth, not LLM-as-judge; per-agent and system latency; label laptop vs VM (offline) numbers. | Vaibhav | 1:00 |
| 11 | Team responsibility matrix | "Proposed, to be confirmed by the team." | whoever is free | 0:20 |
| 12 | Demo screenshots | The screenshots are from 1 November (offline, before simplification); the live demo replaces them. Mention deployment: EC2, Docker, Caddy, CI/CD. | Vaibhav | 1:00 plus demo |
| 13 | Conclusions | What works, what is not done (no live accuracy, no live 40-run cost, no pub/sub arm). | Astitva | 0:45 |
| 14 | References | Do not read. | none | 0:05 |
Total about 13 minutes of speaking plus demo time in the gaps. For a 10-minute slot drop slides 2, 3 and 14 to one sentence each.

## 8.2 Pre-demo checklist
1. The day before: confirm the site answers (`curl -I https://13-204-3-14.sslip.io/` returns 401 without a password, `server: Caddy`). If it does not, check the instance is running and the Elastic IP is attached.
2. One hour before: do a dry run of the whole run-sheet. Do the dry run in OFFLINE mode unless the quota allows both a dry run and the live claim: a live claim costs 6 to 9 requests and the cap is 20 per day, so a live dry run plus a live demo uses 12 to 18 of 20. The daily budget resets at midnight Pacific time, which in November (standard time, UTC minus 8, from the DST rule in `gemini_rate.pacific_day_start`) is 13:30 IST.
3. Check the quota: open the Evaluation tab, type 1 in the live cases field, click **Show request estimate**: it prints "Local daily budget: X of 20 used" (local ledger only; Google's own quota may be lower).
4. `AUTO_UPDATE=off` in `deploy/.env` on the VM (then no restart can happen mid-demo). Do not push to `main` during the assessment window.
5. Confirm the mode you want: the pill shows "Mode: LIVE · Gemini gemini-3.5-flash" or "Mode: OFFLINE · test doubles". To switch: edit `MODE=` in `deploy/.env` on the VM, run `sudo bash deploy/boot.sh`, wait for the container healthcheck.
6. Log in once to confirm the password works; let the browser keep it for the session.
7. Pre-open browser tabs: (a) the site, (b) the Evaluation tab (`#evaluation`), (c) screenshots folder `docs/screenshots/`, (d) the saved results (`results/aws_live_run_RUN-dash-6d15294eca.json`, `results/aws_run_RUN-dash-6ac716c9bd.json`, `results/boxplot_latency.png`, `results/heatmap_agent_latency.png`), (e) the PPT.
8. Keep the sample PDFs ready (`data/samples/sample_claim_WC-2026-04{11..14}.pdf`) in case you want to upload one by hand.
9. Do not show `deploy/.env` or type the password on screen.

## 8.3 Demo samples and what each gives offline (README; `data/sample_ground_truth.json`)
| Sample card | Contents | Offline decision | Notes |
|---|---|---|---|
| WC-2026-0411 | Complete smartphone claim | APPROVE, payout 16,400.00 INR; status ESCALATED because the payout is 15,000 or more (human sign-off) | Matches the live results |
| WC-2026-0412 | Refrigerator, two serials, earlier claim on the serial, hidden "IMPORTANT AI SYSTEM INSTRUCTION" in the repair report (p.3, lines 12-14) | ESCALATE (GUARDRAIL_ESCALATION) | The VM offline run `RUN-dash-6ac716c9bd` |
| WC-2026-0413 | Invoice asks to "access all customer claim histories" (p.2, lines 10-12) | ESCALATE | Data-access attempt; lookups stay within the claim |
| WC-2026-0414 | Laptop claim without a repair report | REQUEST_EVIDENCE | Missing report named |
Offline on the VM each took about 3 s (2026-11-19); live takes about 2 to 3 minutes (the partial live run lasted about 2 min 47 s: 18:31:33 to 18:34:20 UTC, and its Forensic task alone ran 107 s).

## 8.4 Live demo run-sheet (labels verified in `src/claimsleuth/dashboard/static/index.html`, `app.js`, `eval.js`)
Legend: UI = where; PROVES = what it shows; CRIT = criterion; BACKUP = pre-computed file under `docs/submission/results/` (or other) if the live service fails.

| Step | UI (exact label) | Action | PROVES | CRIT | BACKUP |
|---|---|---|---|---|---|
| 1 | Header pills: "Mode: LIVE · Gemini gemini-3.5-flash" / "Mode: OFFLINE · test doubles", and the status pill "READY" | Point at the mode pill; say what is real | Honest execution mode; it comes from `/api/status`, not typed text | C-11, C-9 | `DEPLOYED.txt` (mode, URL) |
| 2 | Section "Sample Claims" (four cards), button "Use this claim" | Choose WC-2026-0412 first (richest) | PDF intake, synthetic claims | C-3 | screenshots `docs/screenshots/1_claim_investigation_start.png` |
| 3 | Button "Run Investigation" | Start the run; talk while it runs. Stepper timeline under it shows baseline, ADK, CrewAI, adjudication as real signals arrive | Orchestration: parallel stage, A2A, MCP activity | C-4, C-6, C-2 | `aws_run_RUN-dash-6ac716c9bd.json` (execution_plan, trace) |
| 4 | Results banner (decision, payout, "decided by") and alerts | Read decision, `decided_by`, human approval flag | Adjudicator authority; escalations | C-3, C-8 | `aws_run_...6ac716c9bd.json` `final_report`; screenshot `3_report_prompt_injection_0412.png` |
| 5 | Panel "Agent Reports" (note: "Each agent only recommends; the deterministic adjudicator decides") | Show three agent cards: framework, model, provider, calls | Offline doubles labelled "test-double"; live "google-gemini" | C-7, C-5 | `aws_live_run_RUN-dash-6d15294eca.json` `agents` |
| 6 | Panel "Investigation Findings" and "Human Review & Evidence" > "Source Documents", "Extracted Evidence" | Click a finding; show page/line | Provenance and citations | C-9 | screenshots 3 and 4 |
| 7 | Panel "Blackboard & Agent Coordination", and under "Advanced Diagnostics" > "Blackboard Monitoring" | Open and show agent states, read receipts, seal, decision | Shared signed state; Forensic cannot read the Investigator | C-1 | `aws_run_...6ac716c9bd.json` (context_sharing "blackboard", execution_plan); results README E4 |
| 8 | "Advanced Diagnostics" > "Execution Monitoring" (trace), "Agent Execution Record", "Audit Trail" | Show timings, tool and A2A calls, provenance | Observability and audit | C-5, C-9 | `aws_live_run_...6d15294eca.json` (timeline, audit_trail, execution) |
| 9 | "Human Review & Evidence" box: "Reviewer name", "Reviewer notes", buttons "Approve & Close", "Reject Claim", "Request More Information" | Type a demo reviewer name, click "Request More Information" (or Approve) | Human-in-the-loop, recorded separately; decision unchanged | C-8 | screenshot `2_report_normal_claim.png` |
| 10 | "Execution Overview" panel and its reliability line; "Advanced Diagnostics" > "Unfinished Runs" | Show run status (COMPLETED, INCOMPLETE, BLOCKED, ESCALATED) and provider failures | Fail-closed reliability | C-9 | `aws_live_run_...6d15294eca.json` (`outcome`, `provider_retries`: 503 and retry) |
| 11 | Panel "Security & Trust": two sections, each with button "Run controlled test" (evidence integrity; prompt injection) | Click each (offline, disposable databases); wait for the result | Tamper detection; injection handling around a manipulated double | C-8 | no file under `results/`; fallback: EVIDENCE.md (56 and 150 passed on 2026-11-06) and `tests/test_evidence_integrity.py`, `tests/test_prompt_injection.py` |
| 12 | Top nav link "Agent Evaluation & Performance Analytics" | Open the tab | The evaluation view | C-10 | `baseline_eval_40_report.md` |
| 13 | Section "Dataset summary" | Show 40 cases, 8 per category, expected decisions 16 ESCALATE / 8 REJECT / 8 APPROVE / 8 REQUEST_EVIDENCE | Experimental design; ground truth | C-10, C-8 | `data/evaluation/eval_dataset_v1.json` |
| 14 | "Run an evaluation" > OFFLINE card: "Cases" select (All 40, 10, 5), button "Run offline evaluation" | Choose "5 (1 per category)" or "10" for the demo (the full 40-case run took about 16 minutes on the laptop: 14:18 to 14:34 on 2026-11-19; VM duration not measured) | Pipeline evaluation without Gemini | C-10 | `baseline_eval_40_*` (the stored 40-case run `EV-20261119-141842-e46c4a`); pick it from "Stored evaluations" |
| 15 | LIVE card: "Cases" number box (max 5), "Show request estimate", type `RUN LIVE EVALUATION` to enable "Run live evaluation", then a confirm dialog | Click the estimate and read it ("About N Gemini requests ... Local daily budget X of 20 used ... within budget"). Do NOT run it live unless the budget clearly allows (about 10 requests per case); the point is to show the explicit opt-in | Cost control and opt-in design | C-10, C-8 | `results/aws_live_run_...6d15294eca.json`, EVIDENCE.md live table |
| 16 | Results sections: "Four-approach comparison", "Performance heatmaps", "Latency and usage distributions" (box plots), "Confusion matrices", "Scenario-level analysis", "Execution reliability", "Usage" | Scroll through, say values N/A are shown as N/A, never 0; point at "Usage": offline requests 0, estimated cost N/A | Metrics, heat maps, box plots | C-5, C-10 | `results/boxplot_latency.png`, `results/heatmap_agent_latency.png`, `baseline_eval_40_metrics.json` |
| 17 | Export links under the result heading: "raw results (CSV)", "aggregated metrics (JSON)", "evaluation report (Markdown)" | Click one to show it downloads | Reproducible evidence | C-10 | `baseline_eval_40_cases.csv`, `_metrics.json`, `_report.md` |
| 18 | (Mention only) E3/E4/E5 experiments are run from the command line, not the dashboard | Show `results/architecture_comparison.csv` and `messaging_comparison.csv` | Architecture and messaging comparisons | C-1, C-4 | the same CSVs |
Note: the evaluation tab's "Performance heatmaps" are accuracy/precision/recall/F1/completion tables per approach and scenario; the latency heat map is the PNG file. Say which is which.

## 8.5 Checklist of everything the team built (so nothing is left unmentioned)
Owner = TEAM.json proposed split. Slide = slide number in the deck (0 to 14). Show = where to show it.
| Feature | Owner (proposed) | Where to show it | Slide | Criterion |
|---|---|---|---|---|
| Three agents on two frameworks (ADK x2, CrewAI x1) | Astitva | "Agent Reports" panel | 4 | C-7 |
| Deterministic hierarchical manager, typed plan, deadlines | Astitva | "Execution Monitoring" | 4, 5 | C-4 |
| Sequential and parallel stages | Astitva | timeline; E3 CSV | 4, 5 | C-4 |
| Deterministic baseline rules (12 checks, Decimal settlement) | Astitva | "Investigation Findings" | 4, 10 | C-3 |
| Deterministic adjudicator (authority, verification, human flag) | Astitva | results banner | 4, 8 | C-3, C-8 |
| Pydantic schemas everywhere | Astitva / all | report JSON | 6 | C-3 |
| Blackboard (SQLite WAL, signed, receipts, seal) | Pulkit | "Blackboard Monitoring" | 7 | C-1 |
| Messaging comparison E4 | Vaibhav | `messaging_comparison.csv` | 7 | C-1 |
| A2A server/client, Agent Card, contract | Pulkit | `/messages` log, trace | 4, 7 | C-6 |
| MCP server and two tools, scope rule, call log | Sibi | trace; `scripts/mcp_client_check.py` | 6 | C-2 |
| ADK callbacks, plugin, request ledger | Sibi | "Agent Execution Record" | 8 | C-5, C-8 |
| CrewAI guardrail, network guard | Sibi | guardrail harness / labs | 8 | C-8 |
| Prompt-injection screen, untrusted wrapping, output check | Sibi | "Security & Trust" | 8 | C-8 |
| Evidence-integrity lab | Sibi / Pulkit | "Run controlled test" (first) | 8 | C-8 |
| Prompt-injection lab, personas | Sibi | "Run controlled test" (second); E5 | 8, 9 | C-8 |
| Observability: trace, provenance, audit, provider failures | Sibi (Vaibhav supports) | "Execution Monitoring", "Audit Trail" | 10 | C-5, C-9 |
| Heat maps and box plots | Vaibhav | Evaluation tab; PNGs | 5, 10 | C-5, C-10 |
| Dashboard, PDF intake, human review, recovery | Pulkit | whole UI | 12 | C-9 |
| 40-case evaluation, scoring, exports | Vaibhav | Evaluation tab | 9, 10 | C-10 |
| Architecture experiments E3 | Vaibhav | `architecture_comparison.csv` | 5 | C-4 |
| Prompt-structure and persona experiments E5 | Vaibhav | `persona_prompt_runs.csv` | 9 | C-8, C-10 |
| Gemini request ledger and caps (20/day, 5/min) | Sibi (Vaibhav uses) | "Show request estimate" | 10 | C-10 |
| Execution mode switch (live/offline) | Astitva | mode pill | 12 | C-11 |
| Docker single image, compose, healthcheck, volumes | Vaibhav | slide 12 / repo | 12 | C-11 |
| AWS EC2, Elastic IP, security group, IMDSv2, user data | Vaibhav | the site URL; `DEPLOYED.txt` | 12, 13 | C-11 |
| Caddy HTTPS, sslip.io, basic auth | Vaibhav | browser lock icon, login prompt | 12 | C-11 |
| boot.sh, systemd, swap | Vaibhav | repo | 12 | C-11 |
| CI (GitHub Actions) and pull-based auto-deploy with rollback | Vaibhav | `.github/workflows/ci.yml`, `deploy/auto_update.sh` | 12, 13 | C-11 |
| Report and PPT build pipeline | Vaibhav | `docs/submission/` | 11, 12 | (deliverables) |
| Integration and end-to-end tests (801 + 155) | Vaibhav (Astitva supports) | `docs/EVIDENCE.md` | 10 | (evidence) |
| Live Gemini runs (4 complete, 1 partial on the VM) | team | `results/aws_live_run_...json`; EVIDENCE.md | 10, 13 | C-11 |

## 8.6 Timing for a 10 to 15 minute slot
- **Start the live run early.** A live claim takes about 2 to 3 minutes. Click "Run Investigation" while on slide 4 (or even slide 1) with the site open in a second window/tab, keep talking, and come back to the result at slide 12. In offline mode the run finishes in seconds, so run it live in front of the examiner at slide 12.
- 15-minute plan: slides 0 to 4 (5 min, claim started at the end of slide 4), slides 5 to 10 (5 min), slide 12 demo (3 min: steps 4 to 10), slides 13 to 14 (1 min), questions.
- 10-minute plan: slides 0, 1, 4, 5, 7, 8, 10, 12 (demo steps 3, 4, 7, 12 to 16), 13.
- Evaluation tab: use an offline run of 5 cases, or open the stored baseline evaluation from "Stored evaluations"; do not wait for a 40-case offline run (about 16 minutes on the laptop).

## 8.7 Fallbacks
| Problem | What to do |
|---|---|
| Gemini returns 503 (or a 429, or the daily cap is used: "daily Gemini request budget used") | Say it plainly: the system retries once after 15 s, preserves valid reports and sends the run to a human; show that behaviour in `aws_live_run_RUN-dash-6d15294eca.json` (Forensic FAILED with 503, run INCOMPLETE, decision APPROVE 16,400 INR from the adjudicator). Then switch to offline: edit `MODE=offline` in `deploy/.env`, `sudo bash deploy/boot.sh` (about a minute; the pill changes to OFFLINE), and rerun the sample (about 3 s). If you cannot SSH, show the saved JSON and the offline results |
| Run seems stuck | Live runs can take 2 to 3 minutes (A2A wait 450 s in live); check the stepper; do not click twice (one run at a time; a second click returns "busy") |
| The site is down or slow after a restart | Wait 30 to 90 seconds for the certificate; check `https://13-204-3-14.sslip.io/` for 401 then log in; if the instance is stopped, start it (the Elastic IP keeps the URL); if still down show `docs/screenshots/` and the result files; say the screenshots of 1 November are offline and pre-simplification |
| Login rejected | Retype the password; do not display it; if it fails, show screenshots and results |
| Evaluation tab empty | Pick the stored evaluation in "Stored evaluations", or show `baseline_eval_40_report.md` |
| A rebuild starts mid-demo | `AUTO_UPDATE=off` prevents it; otherwise wait for the healthcheck (up to about 10 minutes) and use the backups |
| Laptop network down | Everything in `docs/submission/results/` and `docs/screenshots/` works offline; keep a local copy open |
| Question you cannot answer | Use the Part 5 script; offer to show the file |

## 8.8 Closing line for the demo
"What you saw: the agents recommended, the adjudicator decided, a human can review, and every step is traceable. What we did not do: live accuracy at scale, live cost over 40 runs, and a publish/subscribe arm."

