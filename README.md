# ClaimSleuth Simple

**Multi-agent warranty-claim investigation with independent AI agents, MCP tools, A2A communication and a
deterministic adjudicator.**

A customer uploads a warranty claim as a PDF: a claim form, a retail invoice and a service-centre repair report.
Three agents investigate it independently:
- an **Evidence Agent** and an **Investigator Agent**, built with Google ADK;
- a **Forensic Agent**, built with CrewAI and reached over the A2A protocol.

The agents use two MCP tools: a warranty-policy search and a claim-history lookup. The agents only *recommend*.
The decision (approve, reject, escalate to a human, or request evidence) and every rupee of the payout come from a
deterministic rules engine. Prompt-injection attempts hidden in uploaded documents are detected, recorded with their
PDF page and line, and never obeyed.

All people, serial numbers, claims and history records in this repository are **synthetic**.

![Investigation report for a PDF containing a prompt-injection attempt](docs/screenshots/3_report_prompt_injection_0412.png)

---

## Execution modes: what is real and what is a test double

| Component | Default (used by the demo) | Optional real model | Status of the real-model path |
|---|---|---|---|
| Evidence + Investigator Agents (Google ADK) | **Offline test double** (`OfflineAdkLlm`), a labelled rule-based stand-in for the model | Gemini, via `GOOGLE_API_KEY` | Implemented, **never run live** (no API key was available) |
| Forensic Agent (CrewAI) | **Offline test double** (`ForensicTestDoubleLLM`), labelled the same way | Local Ollama `qwen2.5:3b` (`--crew-mode ollama-local`) | Implemented; the one recorded live run **FAILED** (Ollama's 4,096-token context truncated the prompt, and the model skipped the MCP tool) |

**The test doubles are not AI reasoning.** They are deterministic stand-ins for the model, so the system runs
offline and reproducibly. Everything around them is genuine:
- the ADK `Runner`, sessions, `SequentialAgent` and `McpToolset`;
- CrewAI `Agent`, `Task`, `Crew.kickoff()` and the output guardrail;
- the A2A server and client;
- the MCP server and its tool calls;
- PDF extraction, the rules engine and the adjudicator.

Every output records its model as `test-double` with provider `offline-test-double`, and the page shows the mode.
A live mode never falls back to a test double.

## Features

- **PDF claim intake:**
  - upload by drag and drop, with real text extraction (pypdf);
  - page and line provenance for every extracted line;
  - missing documents are reported, never invented.
- **Three independent agent reports.** They are kept separate and never merged; each cites document lines and MCP
  results.
- **Two MCP tools** on a separate MCP server (official MCP Python SDK, Streamable HTTP):
  - `warranty_policy_search` and `claim_history_lookup`;
  - during an investigation, history lookups are limited to the claim's own customer and serial numbers, and the
    server refuses anything else;
  - every call is written to the server's own log with run ID, claim ID, caller, status and duration.
- **A2A:** the ADK side calls the CrewAI Forensic Agent through an A2A 1.0 service (a2a-sdk), with an Agent Card and
  a validated Pydantic message contract.
- **Deterministic adjudication:**
  - Decimal money and policy rules (coverage window, exclusions, deductible, cap, depreciation);
  - serial, date and history cross-checks;
  - verified agent evidence only, and no voting between agents.
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
  mcp_server/        MCP server with the two tools and its tool-call log
  adk_service/       Google ADK Evidence + Investigator agents, A2A client, offline test double
  crew_service/      CrewAI Forensic Agent, A2A server, ollama-local mode, offline test double, guardrail harness
  integration/       run orchestration (baseline || ADK || A2A) and the deterministic adjudicator
  dashboard/         web application: PDF intake, jobs, report views, human review, static page
data/                synthetic policies, 18 stored claims, ground truth (kept apart), sample PDFs, history DB
tests/               pytest suites (tests/crew runs in .venv-crew)
docs/                guides per component, build log, checklist, screenshots
scripts/             independent MCP client check
requirements/        pinned dependencies per environment (main.txt, crew.txt)
```

## Installation

Tested on **Windows 11 with Python 3.12.10**. The code also selects the right interpreter paths on Linux and
macOS, but it has not been tested there.

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
- `--crew-mode ollama-local`: use a local Ollama model; it must already be installed and pulled.
- `--no-services`: use services you started yourself.

### Demonstration with the sample claims

Use **Use this claim** on a card, or upload any PDF in `data/samples/`, then click **Run Investigation**.

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
.venv\Scripts\python -m claimsleuth.integration CS-003         # full run (needs both services running)
.venv\Scripts\python -m claimsleuth.dataset                    # rebuild the synthetic dataset and history DB
.venv\Scripts\python -m claimsleuth.dashboard.sample_pdf       # rebuild the sample PDFs
```

## Tests

```powershell
.venv\Scripts\python -m pytest                    # main suite (starts its own MCP and CrewAI services on free ports)
.venv-crew\Scripts\python -m pytest tests\crew    # CrewAI suite
```

Results on 2026-11-01:
- `.venv`: **246 passed, 5 skipped, 1 deselected**;
- `.venv-crew`: **61 passed, 1 deselected**.

The 5 skipped tests in `.venv` are the CrewAI test files, which run in `.venv-crew` instead. The deselected tests are
marked `live`: they need a real model (a Gemini key or a local Ollama run) and are excluded by default.

The suites cover:
- the rules and settlement arithmetic, the schemas and the MCP server and its scope rule;
- the ADK and CrewAI workflows, the A2A contract and the adjudicator;
- PDF intake, human review and prompt-injection detection;
- end-to-end runs of all four sample PDFs through the real services.

## Limitations

- **Models:** in normal use the agents run on labelled offline test doubles, not a language model.
  - Live Gemini has not been verified.
  - The local Ollama attempt failed (see `docs/BUILD_LOG.md`, Phase 6).
  - Because the test doubles follow similar rules, the three agents usually agree.
- **Synthetic data** and a fixed PDF layout: uploads must use the samples' section headings and `Label: value`
  lines. Scanned PDFs (no text layer) are rejected. Other layouts give "missing" fields, never guessed ones.
- **Prompt-injection detection is pattern-based.** It catches the documented phrasings, not every possible attack;
  untrusted text is also always passed to the agents inside `<untrusted>` blocks.
- **Local use:**
  - the app binds to 127.0.0.1;
  - the reviewer name is typed in, not authenticated;
  - one investigation runs at a time.
- **Not built:**
  - the blackboard / communication-mode comparison;
  - the five-architecture study;
  - the 40-run evaluation;
  - cloud deployment.

  See `docs/CHECKLIST.md`.

## Documentation

- `docs/DASHBOARD_GUIDE.md`: the application, the report and the security section.
- `docs/MCP_GUIDE.md`, `docs/ADK_GUIDE.md`, `docs/CREWAI_GUIDE.md`: one guide per component.
- `docs/PHASE5_GUIDE.md`: A2A and the adjudicator.
- `docs/OLLAMA_GUIDE.md`: the local-model (`ollama-local`) mode, its limits and how to run it.
- `docs/BUILD_LOG.md`: what was built in each phase, with the test evidence.
- `docs/CHECKLIST.md`: status of each requirement.

## Technologies

Python 3.12 · Pydantic 2.12 · Google ADK 2.13 · CrewAI 1.15 · MCP Python SDK (2.3 server/client; 1.30 inside CrewAI) ·
a2a-sdk 1.3 (A2A 1.0) · Starlette + Uvicorn · pypdf · SQLite · pytest
