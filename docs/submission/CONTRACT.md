# Submission contract (read first, every agent)

Project: ClaimSleuth Simple. Course: Agentic AI (CSE3101), BML Munjal University, final evaluation Nov 2026.
Repo state: origin/main @ 186f279 (PR #2 merged: CI and auto-deploy), branch final/live-and-cicd-docs for the 2026-11-20 documentation sync. Earlier tag submission-2026-11-07 (4ff2306).
Working dir: the repo root. Everything new goes under `docs/submission/` unless your brief says otherwise.

## Deliverables (all three mandatory)
1. Final report: Word document in the EXACT format of `docs/Final_Report_Format.docx` (PDF export is made by the user).
2. PPT: 15 slides numbered 0 to 14, structure fixed by the course brief (see `.claude/agents/ppt-agent.md`).
3. Code: ZIP of the working code, plus the system deployed to a cloud platform before the assessment.

## Course minimum criteria (C-1..C-11) and where the repo stands
C-1 blackboard shared state: DONE (`blackboard/`). Messaging comparison DONE as E4 (blackboard vs direct passing vs no sharing, 3 x 40 offline runs, results/messaging_comparison.csv); the publish/subscribe broadcast arm was NOT built.
C-2 two MCP tools: DONE (`mcp_server/`: warranty_policy_search, claim_history_lookup).
C-3 Pydantic structured output: DONE (`schemas.py`).
C-4 design patterns sequential/parallel/hierarchical: implemented in one design (`orchestration/manager.py`); COMPARISON DONE as E3 (results/architecture_comparison.csv, 40 cases each, offline; accuracy identical by construction, only time differs).
C-5 callbacks + monitoring + heat map: DONE (`adk_service/callbacks.py`, `observability/`, dashboard heat maps).
C-6 A2A ADK<->CrewAI: DONE (`crew_service/a2a_server.py`, `adk_service/forensic_a2a.py`).
C-7 only CrewAI and ADK: DONE (two venvs).
C-8 security: DONE (guardrails, callbacks, injection screen, evidence-integrity lab). Persona variants exist (honest/vague/abusive/injection).
C-9 observability / provenance: DONE (`observability/trace.py`, audit trail).
C-10 40+ runs, cost, time, boxplot: 40-case offline benchmark DONE (plus 236 experiment runs); E5 prompt-structure variants (standard vs evidence-first ReAct) built, 0 of 58 paired decisions changed; live cost over 40 runs NOT measured.
C-11 cloud deployment: DONE on AWS EC2 t4g.small, Elastic IP 13.204.3.14, URL https://13-204-3-14.sslip.io/, live mode (docs/submission/DEPLOYED.txt; offline checks verified 2026-11-19). Course rule: nothing may run from a local machine during the demo; the VM runs live mode on the free tier, so only about 2 to 3 live claims per day fit (a claim costs 6 to 9 requests, app caps GEMINI_RPD 20 / GEMINI_RPM 5), and offline mode is selectable on the VM (MODE=offline, then boot.sh).

## Facts that must never be misstated
- Default mode is OFFLINE: the agents run on labelled rule-based test doubles (`OfflineAdkLlm`, `ForensicTestDoubleLLM`), not an LLM. Everything around them (ADK Runner/SequentialAgent/McpToolset, CrewAI Agent/Task/Crew, A2A, MCP, blackboard, rules, adjudicator) is real.
- Live mode uses Gemini `gemini-3.5-flash` for all three agents. Only 4 complete live runs exist (free tier: 20 requests/day/model; a run costs 6 to 9 requests), plus ONE PARTIAL live run on the deployed VM (RUN-dash-6d15294eca, 2026-11-19: ADK agents completed, CrewAI Forensic Agent failed with HTTP 503 after its retry; INCOMPLETE, human approval required, APPROVE from the adjudicator; results/aws_live_run_RUN-dash-6d15294eca.json). Never call that a complete live run. No live accuracy figure exists.
- The decision and every rupee come from the deterministic adjudicator. Agents only recommend.
- Test counts on the user's Windows machine (docs/EVIDENCE.md, 2026-11-06): 801 passed (.venv), 155 passed (.venv-crew). Re-quote only numbers you re-ran or that EVIDENCE.md states, with their date.
- The two venvs cannot merge (opentelemetry-api and mcp conflict between google-adk 2.13.0 and crewai 1.15.33).
- The blackboard is SQLite (WAL); all writers must be on ONE machine. This drives the deployment design (one VM / one compose stack, shared volume).
- All data is synthetic. Currency is INR.
- Never invent a measurement. Every number in a deliverable must come from a file under `docs/submission/results/`, `docs/EVIDENCE.md`, or a command you ran. If a number does not exist, say so plainly.

## Exchange files
- `docs/submission/TEAM.json`: team names/enrollments (placeholders until the user fills them). Read it; never invent names.
- `docs/submission/results/summary.json`: written by the code agent; the report and PPT builders read it if present. Build scripts must be re-runnable once it appears.
- Build scripts live in `docs/submission/` (`build_report.py`, `build_ppt.py`) so outputs can be regenerated.

## Rules for everyone
- No `git add/commit/push`. Back up any file you overwrite (`<name>.bak-YYYYMMDD`). Never print secrets; never read `.env`.
- Do not touch files owned by another agent (see each brief). Report needed changes instead.
- Never modify existing tests to make them pass. Default behaviour of the product must not change.
- Python tooling for the deliverable builders (python-docx, python-pptx, matplotlib) goes in a venv OUTSIDE the repo, at `/private/tmp/claude-501/-Users-vaibhav-Study-Sem-7-Agentic-AI-Project-ClaimSleuth-simple/25c06bc2-3a5d-4974-8d3d-db532a5a4632/scratchpad/tools-venv` (create it if missing; the project's own `.venv` and `.venv-crew` are for the product only).

## Added 2026-11-19 after the professor review (`PROFESSOR_REVIEW.md`)
- DO NOT quote `docs/MASTER_VIVA_QA_COMPREHENSIVE.md` or the two older `.docx` design files for any number or claim: they contain latencies, token counts and a "28% fewer tokens with ReAct" result that were never measured, plus stale claims (gemini-2.5, Cloud Run, "blackboard is coordination only"). Use them only for wording ideas, after checking the code.
- Offline, the agents are rule-based doubles that ignore blackboard context and prompt wording. Accuracy is identical across architectures and messaging styles by construction (40/40 comes from the rules engine: 35 of the 40 cases are decided by the rules, 5 by guardrail escalations; the engine itself has 12 checks plus settlement). Architecture and messaging comparisons may therefore differ only in time, call counts, context passed and failure behaviour. Say so; justify the architecture choice with those measurements plus design heuristics (independence of opinions, fault isolation, auditability), not with accuracy.
- Team split is in `TEAM.json` (`matrix`, L = lead, S = support); it is a PROPOSAL to be confirmed by the team (TEAM.json matrix_note). Report Section 8 and slide 11 must say "proposed, to be confirmed by the team", never "agreed by the team", until the team confirms.
- Tooling: LibreOffice and poppler are being installed; `bash docs/submission/sync.sh` rebuilds and renders the report and PPT.

## Added 2026-11-19 (AWS deployment verified), updated 2026-11-20
- Deployed on AWS EC2 t4g.small (see `docs/submission/DEPLOYED.txt`, `docs/AWS_DEPLOYMENT_PLAN.md`). Verified 2026-11-19 in OFFLINE mode; stop/start test passed BEFORE the Elastic IP was attached (the IP changed then). Since 2026-11-19: Elastic IP 13.204.3.14 (billed about 0.005 USD/h, also while stopped; teardown.sh releases it), URL https://13-204-3-14.sslip.io/ (the old 65-2-175-255 URL is dead), basic auth user claimsleuth (never write the password), MODE=live.
- CI/CD (2026-11-20): .github/workflows/ci.yml on every PR and push to main: compileall, bash -n of deploy scripts, grep secret scan, Docker build of Dockerfile.single; NO pytest (suites take about 45 min, run locally). Both runs passed (PR #2, merge 186f279). Pull-based deploy: systemd timer claimsleuth-update.timer runs deploy/auto_update.sh every minute against origin/main of astitvasaxena/ClaimSleuth_simple, never touches deploy/.env or the runtime volume, runs boot.sh, waits for the healthcheck, rolls back if unhealthy. Detection up to about 60 s; rebuild time NOT measured. NEVER write 'only green builds deploy' (the VM deploys main regardless of CI) or 'deploys within one minute'. AUTO_UPDATE=off in deploy/.env on the VM pauses it (recommended during the assessment).
- Timing note for report and slides (corrected 2026-11-19 after the supervisor audit): on the macOS laptop (Docker or native) a claim takes about 24 s, with about 12 s per ADK agent; the cause was NOT isolated (the 236 unloaded experiment runs show the same 24 s, so "test-suite load" is not an explanation and must not be claimed). On the AWS VM in OFFLINE mode (2026-11-19) a claim took about 3 s end to end with agent latencies of 445 and 266 ms (ADK) and 1,142 ms (CrewAI), run RUN-dash-6ac716c9bd, saved as `docs/submission/results/aws_run_RUN-dash-6ac716c9bd.json`. Quote the VM figures as the OFFLINE deployment measurement (label them offline; the VM now runs live mode, where the one partial run shows 57.8 s Evidence and 23.6 s Investigator on Gemini) and the laptop figures as laptop measurements.
- Low vague-persona mean in E5 (21.3 s, standard structure): it comes from ONE run (EVAL-25, 6.1 s) that did not repeat (24.1 s under evidence-first, 21.8 and 24.1 s in other wirings); cause unknown. Only EVAL-24 is consistently short. Do not explain it as "missing-evidence claims finish early".
- Literature table, requirements R1..R11 and references live in `docs/submission/content_shared.py`, imported by both build scripts; edit them there only.
