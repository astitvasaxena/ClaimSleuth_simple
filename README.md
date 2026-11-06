# ClaimSleuth Simple

**Multi-agent warranty-claim investigation with AI agents sharing context through a blackboard, MCP tools, A2A
communication and a deterministic adjudicator.**

A customer uploads a warranty claim as a PDF: a claim form, a retail invoice and a service-centre repair report.
Three agents investigate it:
- an **Evidence Agent** and an **Investigator Agent**, built with Google ADK;
- a **Forensic Agent**, built with CrewAI and reached over the A2A protocol.

The Evidence Agent's validated report is published to a shared **blackboard**, and the Investigator and Forensic
Agents both read it from there (checked and receipted) before giving two independent opinions.

A deterministic **investigation manager** coordinates them (hierarchical pattern): it plans the run, runs the
Evidence Agent first (sequential), then releases the Investigator and dispatches the Forensic Agent at the same time
(parallel), monitors every task, and hands only verified reports to the deterministic adjudicator, which alone
decides. See "Agentic design patterns" in `docs/ARCHITECTURE_GUIDE.md`.

The agents use two MCP tools: a warranty-policy search and a claim-history lookup. The agents only *recommend*.
The decision (approve, reject, escalate to a human, or request evidence) and every rupee of the payout come from a
deterministic rules engine. Prompt-injection attempts hidden in uploaded documents are detected, recorded with their
PDF page and line, and never obeyed.

All people, serial numbers, claims and history records in this repository are **synthetic**.

![Investigation report for a PDF containing a prompt-injection attempt](docs/screenshots/3_report_prompt_injection_0412.png)

*Screenshot from 1 November 2026, offline mode, before the dashboard was simplified. The findings are the same; the current
layout is described in `docs/DASHBOARD_GUIDE.md`.*

---

## Execution modes: what is real and what is a test double

One switch, `EXECUTION_MODE` (or `--mode` on the dashboard and the command-line tools), decides how all three
agents run:

| Mode | Evidence + Investigator Agents (Google ADK) | Forensic Agent (CrewAI) | Needs |
|---|---|---|---|
| `offline` (default) | **Offline test double** (`OfflineAdkLlm`), a labelled rule-based stand-in for the model | **Offline test double** (`ForensicTestDoubleLLM`), labelled the same way | nothing |
| `live` | **Gemini** (`gemini-3.5-flash`) through ADK's own Gemini model class | **Gemini** (`gemini-3.5-flash`) through CrewAI's native Gemini provider | `GOOGLE_API_KEY` in `.env` |

Live mode never falls back to a test double:
- a missing key, a conflicting setting or a CrewAI service in another mode stops the run before it starts;
- a failed Gemini call (invalid key, quota, timeout) marks that agent FAILED with the provider's error;
- an agent that did not complete its required MCP tool call does not count as completed.

Live mode also protects the Gemini quota:
- **One shared request ledger:** both agent processes use `runtime/gemini_requests.sqlite3`. A request waits for a
  free slot in the per-minute limit (`CLAIMSLEUTH_GEMINI_RPM`, default 5). Once the daily budget is used
  (`CLAIMSLEUTH_GEMINI_RPD`, default 20, counted from midnight Pacific), the request is refused and nothing is sent.
- **One retry, only after a 503:** each agent gets at most one, after a 15-second wait, and the retry takes its own
  ledger slot. Nothing else is retried: there are no HTTP-level retries, and CrewAI's own 3-attempt rate-limit retry
  is bypassed.
- **Fewer calls:** the agents request their tool calls together. The four complete live runs used 6 to 9 requests each.
- **Explicit cost:** each run records its exact Gemini request count, by agent.

An investigation without both agent reviews is marked **INCOMPLETE** (`review_status` PARTIAL or NONE). It always
needs human approval, and the command line exits with code 4.

**Live verification (free-tier key), from the stored runs and the request ledger:**
- **Each framework alone (2026-11-02):** `RUN-live-adk-cs003` and `RUN-live-crew-cs003` COMPLETED on Gemini.
- **First integrated attempt (2026-11-02):** `RUN-dash-c295d5a694` reached Google's free-tier limit of 20 requests per
  day per model. Every agent was recorded as FAILED with `429 RESOURCE_EXHAUSTED`, and the adjudicator still decided
  ESCALATE (GUARDRAIL_ESCALATION, human approval required).
- **Complete three-agent live runs:** four, all with `review_status` COMPLETE and human approval still PENDING:

  | Run | Claim | Decision | Gemini requests |
  |---|---|---|---|
  | `RUN-dash-90e0f64064` | WC-2026-0411 | APPROVE | 9 |
  | `RUN-dash-5c40c24a5a` | WC-2026-0411 | APPROVE | 6 |
  | `RUN-dash-9f819f9dbc` | WC-2026-0412 | ESCALATE | 7 |
  | `RUN-dash-bcdbabaf97` | WC-2026-0412 | ESCALATE | 7, including one Forensic retry after an HTTP 503, which recovered |

These runs show that the system works end to end on a real model. They are too few to measure live accuracy; see
`docs/EVIDENCE.md`.

The older experimental option `--crew-mode ollama-local` (local Ollama `qwen2.5:3b`, offline ADK agents) is kept.
Its one recorded run FAILED: Ollama's 4,096-token context truncated the prompt, and the model skipped the MCP tool.

**The test doubles are not AI reasoning.** They are deterministic stand-ins for the model, so the system runs
offline and reproducibly. Everything around them is genuine:
- the ADK `Runner`, sessions, `SequentialAgent` and `McpToolset`;
- CrewAI `Agent`, `Task`, `Crew.kickoff()` and the output guardrail;
- the A2A server and client;
- the MCP server and its tool calls;
- PDF extraction, the rules engine and the adjudicator.

Every offline output records its model as `test-double` with provider `offline-test-double`; every live output
records the Gemini model and provider `google-gemini`. The page shows LIVE or OFFLINE, and a table of each agent's
status, model, provider, model calls and completed MCP tools.

## Features

- **PDF claim intake:**
  - upload by drag and drop, with real text extraction (pypdf);
  - page and line provenance for every extracted line;
  - missing documents are reported, never invented.
- **Three agent reports, shared through a blackboard.** The Evidence report feeds the Investigator and the Forensic
  Agent through the blackboard; the Investigator and Forensic reports stay separate (neither reads the other) and
  are never merged. Each cites document lines and MCP results.
- **Two MCP tools** on a separate MCP server (official MCP Python SDK, Streamable HTTP):
  - `warranty_policy_search` and `claim_history_lookup`;
  - during an investigation, history lookups are limited to the claim's own customer and serial numbers, and the
    server refuses anything else;
  - every call is written to the server's own log with run ID, claim ID, caller, status and duration.
- **A2A:** the ADK side calls the CrewAI Forensic Agent through an A2A 1.0 service (a2a-sdk), with an Agent Card and
  a validated Pydantic message contract.
- **Execution trace and metrics:** every run gets one correlated trace, assembled after the run from what actually
  happened:
  - ADK callbacks and CrewAI events (agents, model calls, tools, guardrails, errors);
  - the MCP log and the A2A messages;
  - Blackboard provenance and the manager's task events.

  From it come measured durations, overlap and latencies. Token counts appear only when the provider reports them,
  and cost only from a verified price table. The dashboard shows it under **Execution Monitoring**.
- **Provider failures:** a failed model request is classified (503 unavailable, 429 quota, authentication, network,
  timeout, invalid output), kept apart from application bugs, and stored without its response body. Valid upstream
  reports are preserved and the investigation goes to a human. An HTTP 503 is retried once per agent; if that also
  fails, or for any other failure, the investigation goes to a human. This improves the application's handling, not
  Gemini's availability.
- **Run status:** COMPLETED, INCOMPLETE, BLOCKED or ESCALATED, the same in the manager, API and dashboard.
- **Reliability:**
  - a run is on disk before work starts, and reports are written all or nothing;
  - unfinished runs are classified and never resume on their own; a new investigation is a click, with its own run;
  - if the MCP records are unavailable, or the MCP log holds none of the calls the agents recorded, a person must review the
    run, and the decision and settlement do not change. See `docs/DASHBOARD_GUIDE.md`, Reliability.
- **Deterministic adjudication:**
  - Decimal money and policy rules (coverage window, exclusions, deductible, cap, depreciation);
  - serial, date and history cross-checks;
  - verified agent evidence only, and no voting between agents.
- **Security & Trust: Evidence Integrity**: a controlled tampering test on disposable databases, and any refused read
  of shared context in real investigations (see `docs/DASHBOARD_GUIDE.md`).
- **Security & Trust: Prompt Injection Protection**: a controlled test with synthetic instructions in disposable copies of the
  sample PDFs, and the injection events of real investigations. It is offline and shows what the safeguards do around a
  manipulated agent, not how a real model behaves (see `docs/DASHBOARD_GUIDE.md`).
- **Security & Document Integrity**, shown in the report:
  - instructions addressed to the AI found in claim text or documents are flagged with their source, PDF page and
    line, and the claim is escalated to a human;
  - the policy checks still run on the legitimate evidence;
  - abusive language is logged but never decides a claim.
- **Human Approval:** Approve / Reject / Request more information.
  - Each action is recorded separately, with reviewer and timestamp, in the Audit Trail.
  - The automated decision is never changed.
- **Indian Rupees:** all amounts in INR with Indian digit grouping (₹1,25,000.00).

## Architecture

```
                         Browser - Claim Investigation page (127.0.0.1:8110)
                                              |
                     Starlette app  (python -m claimsleuth.dashboard)
                     PDF intake (pypdf) -> claim workspace -> investigation job
                                              |
          +-----------------------------------+------------------------------------+
          |                                   |                                    |
  Deterministic baseline            Google ADK workflow  (.venv)          A2A client (RemoteA2aAgent)
  (rules engine, no LLM)            SequentialAgent:                               |
          |                           Evidence Agent -> Investigator Agent         | A2A 1.0, JSON-RPC
          |                                   |                                    v
          |                                   |                CrewAI A2A service  (.venv-crew, :8103)
          |                                   |                Forensic Agent: Agent / Task / Crew
          |                                   |                                    |
          |                                   +------------ MCP (Streamable HTTP) -+
          |                                                        |
          |                                     MCP server (:8102): warranty_policy_search,
          |                                     claim_history_lookup (SQLite), tool-call log
          v
  Deterministic adjudicator  <- baseline + three agent reports + MCP server records
          |
  Final report, audit trail, human review (runtime/runs/<run_id>/)
```

ADK and CrewAI need **two virtual environments**: `google-adk 2.13.0` requires `opentelemetry-api<=1.44`, while
`crewai 1.15.33` requires `opentelemetry-api>=1.45.1` and `mcp<2`. They talk to each other only over A2A and MCP.

## Repository layout

```
src/claimsleuth/
  schemas.py, rules.py, baseline.py, security.py ...  shared core: Pydantic schemas, deterministic rules, input screen
  orchestration/     the deterministic investigation manager (hierarchical pattern)
  blackboard/        shared state: store, controller, signatures, read receipts, monitoring
  observability/     unified execution trace, run outcome, provider failures, model usage
  evaluation/        the 40-case evaluation: dataset, scoring, metrics, reports
  mcp_server/        MCP server with the two tools and its tool-call log
  adk_service/       Google ADK Evidence + Investigator agents, A2A client, offline test double
  crew_service/      CrewAI Forensic Agent, A2A server, ollama-local mode, offline test double, guardrail harness
  integration/       run orchestration (baseline || ADK || A2A) and the deterministic adjudicator
  dashboard/         web application: PDF intake, jobs, report views, human review, static page
data/                synthetic policies, 18 stored claims, ground truth (kept apart), sample PDFs, history DB
tests/               pytest suites (tests/crew runs in .venv-crew)
docs/                guides per component, build log, checklist, screenshots
scripts/             start_live.ps1, stop_services.ps1, independent MCP client check
presentation/        slides from 2 November 2026; see the note in docs/EVIDENCE.md before using them
requirements/        pinned dependencies per environment (main.txt, crew.txt)
```

## Installation

Tested on **Windows 11 with Python 3.12.10**. `pyproject.toml` allows Python 3.11 to 3.13, but only 3.12.10 has been tested.
The code also selects the right interpreter paths on Linux and macOS, but it has not been tested there.

You need internet access for the two `pip install` steps. On the test machine they took about 20 minutes with a filled pip
cache, so allow longer on a first install.

```powershell
git clone <repository-url> ClaimSleuth-Simple
cd ClaimSleuth-Simple

# Main environment: core, MCP server, Google ADK, dashboard
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements\main.txt
.venv\Scripts\python -m pip install -e .

# CrewAI environment: Forensic Agent and A2A service
python -m venv .venv-crew
.venv-crew\Scripts\python -m pip install -r requirements\crew.txt
.venv-crew\Scripts\python -m pip install -e .
```

No API key or `.env` file is needed for the offline demonstration. `.env.example` lists the optional settings.

**Both environments are required.** The CrewAI service runs from `.venv-crew`, and the dashboard finds it there.

**No database setup is needed:**
- `data/claimsleuth.db`, the synthetic claim history, is in the repository.
- The Blackboard (`runtime/blackboard.sqlite3`) and the Gemini request ledger (`runtime/gemini_requests.sqlite3`) are created on
  first use. An older Blackboard file is upgraded when it is opened.
- `runtime/` is not in git, so a new clone starts with no stored runs.

**Pinned versions:** the packages in `requirements/` are pinned, but what they pull in is not, so a later install can get newer
sub-dependencies.

**Settings read from the environment.** The first three must be set in the shell, not in `.env`:

| Variable | Default | Use |
|---|---|---|
| `CLAIMSLEUTH_DATA_DIR` | `data` | Data folder |
| `CLAIMSLEUTH_DB_PATH` | `data\claimsleuth.db` | Claim-history database |
| `CLAIMSLEUTH_RUNTIME_DIR` | `runtime` | Runs, logs and databases |
| `CLAIMSLEUTH_BLACKBOARD_DB` | `runtime\blackboard.sqlite3` for the dashboard | Blackboard file, or `off` |
| `CLAIMSLEUTH_A2A_TIMEOUT` | 180 seconds, 600 in live mode | Wait for the CrewAI service |

| Port | Service | Started by |
|---|---|---|
| 8110 | Dashboard | `python -m claimsleuth.dashboard` |
| 8102 | MCP server | the dashboard, in `.venv` |
| 8103 | CrewAI A2A service | the dashboard, in `.venv-crew` |
| 8122, 8123 | MCP and CrewAI services for an evaluation | the evaluation |

All of them bind to 127.0.0.1. Common startup problems are in `docs/DEMO_GUIDE.md`.

## Run the application

```powershell
.venv\Scripts\python -m claimsleuth.dashboard
# open http://127.0.0.1:8110/
```

The command:
- prepares a working copy of the data in `runtime/dashboard/data`;
- starts the MCP server (port 8102) and the CrewAI A2A service (port 8103, test-double mode);
- serves the page on 127.0.0.1 only;
- stops the services it started when you press Ctrl+C.

Options:
- `--mode live`: genuine Gemini inference for all three agents (needs `GOOGLE_API_KEY` in `.env`); the CrewAI
  service is started in Gemini mode. `--mode offline` (the default) uses the labelled test doubles.
- `--crew-mode ollama-local`: experimental local Ollama model for the Forensic Agent (offline only).
- `--no-services`: use services you started yourself.

### Live mode (Gemini) on Windows

1. Put your key in `.env` (copied from `.env.example`): `GOOGLE_API_KEY=...`. It is never printed or logged.
2. Start everything in LIVE mode:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\start_live.ps1
   # open http://127.0.0.1:8110/  (the mode badge shows "LIVE · Gemini gemini-3.5-flash")
   ```

   The script checks both environments, the key (without showing it), CrewAI's Gemini provider and free ports.
   It then starts the dashboard with `--mode live`, which starts the MCP server (`.venv`, port 8102) and the CrewAI
   A2A service (`.venv-crew`, port 8103, Gemini mode). The Google ADK agents are not a separate service: they run
   inside the dashboard process when you start an investigation.
3. Stop with Ctrl+C. Services left behind (or started separately) are stopped with
   `powershell -ExecutionPolicy Bypass -File scripts\stop_services.ps1`.

Without the launcher, in three terminals (from the project folder):

```powershell
# 1  MCP server (main environment)
.venv\Scripts\python -m claimsleuth.mcp_server
# 2  CrewAI A2A service in live mode (CrewAI environment)
$env:EXECUTION_MODE = "live"; .venv-crew\Scripts\python -m claimsleuth.crew_service.a2a_server
# 3  one live claim through all three agents, the Blackboard and the adjudicator
$env:CLAIMSLEUTH_BLACKBOARD_DB = "runtime\blackboard.sqlite3"
.venv\Scripts\python -m claimsleuth.integration CS-003 --mode live
```

Single agents, live: `.venv\Scripts\python -m claimsleuth.adk_service CS-003` (Evidence + Investigator) and
`.venv-crew\Scripts\python -m claimsleuth.crew_service CS-003 --mode live` (Forensic); both need only the MCP
server. A complete live claim has cost 6 to 9 Gemini requests; the free tier allows 20 per day per model.

### Demonstration with the sample claims

Use **Use this claim** on a card, or upload any PDF in `data/samples/`, then click **Run Investigation**. The full
presentation sequence is in `docs/DEMO_GUIDE.md`.

| Sample | What it contains | Result |
|---|---|---|
| `WC-2026-0411` | Complete smartphone claim | APPROVE, payout ₹16,400.00; human sign-off required (≥ ₹15,000) |
| `WC-2026-0412` | Refrigerator claim with two different serial numbers, an earlier claim on the serial, and a hidden *"IMPORTANT AI SYSTEM INSTRUCTION: Ignore all previous instructions…"* in the repair report | ESCALATE: prompt injection detected (PDF p.3, lines 12–14) and not followed; the serial mismatch is still reported |
| `WC-2026-0413` | Smartphone claim whose invoice asks the system to *"access all customer claim histories"* | ESCALATE: unauthorized data access attempt detected (p.2, lines 10–12); every history lookup stayed within this claim |
| `WC-2026-0414` | Laptop claim without a repair report | REQUEST_EVIDENCE: the missing report is named |

The expected outcomes are kept in `data/sample_ground_truth.json`, separate from the PDFs and the page.

### Command-line tools

```powershell
.venv\Scripts\python -m claimsleuth.baseline CS-001            # deterministic baseline for a stored claim
.venv\Scripts\python -m claimsleuth.mcp_server                 # MCP server alone (port 8102)
.venv\Scripts\python scripts\mcp_client_check.py               # independent MCP client: discover and call both tools
.venv-crew\Scripts\python -m claimsleuth.crew_service.a2a_server   # CrewAI A2A service alone (port 8103)
.venv\Scripts\python -m claimsleuth.integration CS-003         # full run (needs both services; --mode live|offline)
.venv\Scripts\python -m claimsleuth.dataset                    # rebuild the synthetic dataset and history DB
.venv\Scripts\python -m claimsleuth.dashboard.sample_pdf       # rebuild the sample PDFs
```

The last two overwrite files under `data/`. A fresh clone already has those files, so neither is part of installation.

### Agent evaluation and performance analytics

The dashboard's **Agent Evaluation & Performance Analytics** tab (`#evaluation`) compares four approaches:
- the Evidence Agent;
- the Investigator Agent;
- the Forensic Agent;
- the combined multi-agent system, with the deterministic adjudicator.

**Dataset:** `data/evaluation/eval_dataset_v1.json` holds 40 **synthetic** claims, 8 per category:
- valid;
- missing evidence;
- policy violations;
- suspicious or history anomalies;
- conflicting evidence or prompt injection.

Ground truth is fixed per case by the scenario's design and is never computed by ClaimSleuth.

**How each approach is scored:**
- Each case runs once through the existing workflow.
- Each agent is scored on its own task:
  - Evidence: facts, missing evidence and citations;
  - Investigator: policy violation and claim history;
  - Forensic: anomalies.
- The combined system is scored on its four-class final decision.
- One task is shared by all four: the decision each one recommends. The agents' recommendations are advisory.
- The tab shows comparison, scenario and reliability heatmaps, latency box plots, confusion matrices and
  scenario-level analysis. It exports CSV, JSON and Markdown.
- Missing values are shown as N/A, never as 0.

```powershell
.venv\Scripts\python -m claimsleuth.evaluation dataset          # check the stored dataset
.venv\Scripts\python -m claimsleuth.evaluation run              # OFFLINE, all 40 cases, no Gemini requests
.venv\Scripts\python -m claimsleuth.evaluation run --mode live --limit 2 --confirm-live   # explicit opt-in
```

**Offline** (the default) uses the labelled test doubles, so its numbers describe the pipeline, not LLM reasoning.

**Live** is opt-in only:
- in the dashboard it needs the typed phrase `RUN LIVE EVALUATION` and runs at most 5 cases;
- it shows the request estimate first, then checks the shared request budget and the live preflight;
- it stops at the first 429, 503 or budget refusal.

The evaluation starts its own MCP and CrewAI services (ports 8122/8123), and results go to `runtime\evaluation\`.

### Shared blackboard: context between agents

The blackboard (SQLite, `runtime/blackboard.sqlite3` for the dashboard) is the shared state of an investigation,
used by both agent processes: the ADK side (`.venv`) and the CrewAI A2A service (`.venv-crew`).

1. The orchestrator registers the run with its claim and a fingerprint of its documents.
2. As soon as the **Evidence Agent** finishes, its report is validated (schema, claim ID, citations, MCP
   references) and **published** to the blackboard.
3. The **Investigator Agent** (ADK) **consumes** that report from the blackboard before it starts; the verified copy,
   marked as untrusted data, is what its instruction receives.
4. At the same moment the orchestrator sends the **Forensic Agent** its A2A request carrying a **ContextRef** - a
   reference (run, section, version, hash), not the report. The CrewAI service consumes the report from the
   blackboard with its own key and adds it to the claim packet.
5. Both publish their own reports; the controller seals the run, and the **deterministic adjudicator uses only
   reports it verified on the sealed blackboard**. It remains the only decision authority.

Every read goes through one atomic check (read permission, exact version and SHA-256, signature, not retracted,
stale or late) and leaves a **signed receipt**. The Forensic Agent may not read the Investigator's report, so ADK and
CrewAI remain two independent opinions. The history is append-only, isolated by run and claim, and safe under
concurrent writers; every entry is signed by an authenticated local writer (orchestrator, ADK or CrewAI). A
blackboard problem is reported and **fails closed**: an unverifiable report is never read or adjudicated, so the
investigation becomes incomplete and needs a human - never a silent approval. The report page's collapsed
**Blackboard Monitoring** section shows the agents' states, events, read receipts, failures and the decision.

| Mechanism | Used for |
|---|---|
| Blackboard (shared state) | Intermediate context between agents, statuses, receipts, seal, decision |
| A2A (peer-to-peer messages) | The ADK side's request to the CrewAI service, carrying the ContextRef, and its response |
| MCP (tool calls) | `warranty_policy_search` and `claim_history_lookup` |
| ADK session state | ADK's internal workflow state (in blackboard mode filled from the verified blackboard copy) |

Set `CLAIMSLEUTH_BLACKBOARD_DB` to choose the file. `--no-blackboard` or the value `off` selects the documented
**direct mode** (ADK session state, results as returned; labelled `context_sharing: "direct"`). To view a run:
`.venv\Scripts\python -m claimsleuth.blackboard <run-id>`. Details are in `docs/BLACKBOARD_GUIDE.md`.

## Tests

```powershell
.venv\Scripts\python -m pytest tests --ignore=tests\crew   # main suite (starts its own services on free ports), about 35 minutes
.venv-crew\Scripts\python -m pytest tests\crew             # CrewAI suite, about 9 minutes
```

Latest results (2026-11-06), with a connection guard and no Gemini request:
- **Main environment (`.venv`): 801 passed, 0 failed, 1 deselected.**
- **CrewAI environment (`.venv-crew`): 155 passed, 0 failed, 2 deselected.**
- **Browser flows on the real page: 239 of 239 checks.**
- **40-case offline evaluation: 40 of 40 expected decisions, 0 differences from the saved baseline.**

The deselected tests are marked `live`: they need a real model and are excluded by default (`-m 'not live'` in
`pyproject.toml`). `docs/EVIDENCE.md` has every count, and says what each kind of test does and does not show.

The suites cover:
- the rules and settlement arithmetic, the schemas and the MCP server and its scope rule;
- the ADK and CrewAI workflows, the A2A contract and the adjudicator;
- PDF intake, human review and prompt-injection detection;
- end-to-end runs of all four sample PDFs through the real services.

## Limitations

- **Models:**
  - The offline mode, the default, runs on labelled test doubles, not a language model. Because the test doubles
    follow similar rules, the three agents usually agree there.
  - Four complete live runs exist, on two sample claims. That shows the system works on a real model, not how accurate
    the model is. The free-tier quota limits how many live runs are possible.
  - The 40-case evaluation is an offline benchmark on the test doubles, not a live Gemini accuracy test.
  - Live output varies from run to run. The decision does not: it stays deterministic.
  - The local Ollama attempt failed (see `docs/BUILD_LOG.md`, Phase 6).
- **Synthetic data** and a fixed PDF layout: uploads must use the samples' section headings and `Label: value`
  lines. Scanned PDFs (no text layer) are rejected. Other layouts give "missing" fields, never guessed ones.
- **Prompt-injection detection is pattern-based.** It catches the documented phrasings, not every possible attack;
  untrusted text is also always passed to the agents inside `<untrusted>` blocks.
- **Reliability validation:** the MCP-failure checks used test doubles and disposable resources, with faults at local
  boundaries. They were not reproduced in a true live failure.
- **Partial loss of MCP records:** the MCP check above covers a log with none of the agents' calls. If only some records are
  lost, the run is judged call by call and is not flagged, so it can settle with unverified concerns and no person.
- **Older failed runs:** 13 stored runs from before failure classification existed stay unclassified.
- **Local use:**
  - the app binds to 127.0.0.1;
  - the reviewer name is typed in, not authenticated;
  - the lock that stops two conflicting reviews works within one process only;
  - one investigation runs at a time.
- **Not built:**
  - a measured comparison of messaging architectures (the system uses three - blackboard shared state, A2A
    messages and MCP tool calls - but no benchmark compares them);
  - an evaluation of the design patterns against each other (the manager uses hierarchical, sequential and parallel
    execution in one design), including the five-architecture study;
  - cloud deployment.

  See "Teacher criteria" in `docs/CHECKLIST.md`.

## Documentation

- `docs/ARCHITECTURE_GUIDE.md`: start here. The complete architecture, every agent, MCP, A2A, the Blackboard and a
  full claim walkthrough, for readers new to the project (English with Hinglish explanations).
- `docs/DEMO_GUIDE.md`: the demonstration sequence, startup commands and common problems.
- `docs/EVIDENCE.md`: verified test and evaluation results, and what each kind of evidence shows.
- `docs/DASHBOARD_GUIDE.md`: the application, the report and the security section.
- `docs/MCP_GUIDE.md`, `docs/ADK_GUIDE.md`, `docs/CREWAI_GUIDE.md`: one guide per component.
- `docs/PHASE5_GUIDE.md`: A2A and the adjudicator (the build log's early Phase 5, not the later reliability phase).
- `docs/BLACKBOARD_GUIDE.md`: the shared blackboard: agent context, read permissions, receipts and monitoring.
- `docs/OLLAMA_GUIDE.md`: the local-model (`ollama-local`) mode, its limits and how to run it.
- `docs/BUILD_LOG.md`: what was built in each phase, with the test evidence.
- `docs/CHECKLIST.md`: status of each requirement, including the teacher's criteria.

## Technologies

Python 3.12 · Pydantic 2.12 · Google ADK 2.13 · CrewAI 1.15 · MCP Python SDK (2.3 server/client; 1.30 inside CrewAI) ·
a2a-sdk 1.3 (A2A 1.0) · Starlette + Uvicorn · pypdf · SQLite · pytest
