# ClaimSleuth evidence

This page records what was tested and what each result does and does not show. Every figure below was checked against a stored
report, log or run. The raw regression output is in [`evidence/phase5_final_regression.txt`](evidence/phase5_final_regression.txt).

## Four kinds of evidence

| Kind | What it means | What it does not show |
|---|---|---|
| **Implemented** | The code exists and runs through the real frameworks and protocols | That it is production-grade |
| **Offline validation** | Automated tests and the 40-case evaluation, on labelled test doubles instead of a model. ADK, CrewAI, MCP, A2A, the Blackboard and the rules are real | Anything about real model reasoning or accuracy |
| **Simulated tests** | Faults and attacks injected at local boundaries, on disposable resources | Behaviour in a true live failure, or real model resistance to attack |
| **Live demonstration** | Stored runs on Gemini, counted in the request ledger | Accuracy or reliability at scale: the runs are few |

## Test results

Final regression on 6 November 2026, on the final Phase 5 code. A connection guard refused anything that was not a local test
service. No test sent a Gemini request.

| Suite | Result |
|---|---|
| Main suite (`.venv`) | **801 passed**, 0 failed, 1 deselected |
| CrewAI suite (`.venv-crew`) | **155 passed**, 0 failed, 2 deselected |
| Browser flows on the real page | **239 of 239 checks** across six flows, no JavaScript errors |
| Mutation checks | **6 of 6 detected**: each deliberate fault failed a test |

Deselected tests are marked `live`, need a real model, and were not run.

The counts below come from the single main run, so they are part of the 801, not extra tests:

| Area | Passed | Test files |
|---|---|---|
| Focused D8 tests | 12 | the MCP-records tests in `test_reliability.py` |
| Reliability, service faults, request census | 77 | `test_reliability.py`, `test_service_faults.py`, `test_request_census.py` |
| Provider reliability | 72 | `test_provider_reliability.py` |
| Manager and orchestration | 25 | `test_hierarchical_orchestration.py`, `test_integration_e2e.py` |
| Adjudicator | 16 | `test_adjudicator.py` |
| Human approval | 53 | tests across 13 files with approval, review or human in their name |
| Blackboard | 85 | `test_blackboard.py`, `test_blackboard_e2e.py`, `test_blackboard_context.py` |
| Dashboard | 46 | `test_dashboard.py`, `test_dashboard_e2e.py`, `test_dashboard_layout.py` |
| Phase 4.1 evidence integrity | 56 | `test_evidence_integrity.py` |
| Phase 4.2 prompt injection | 150 | `test_prompt_injection.py` |

Phase names differ between documents: Phase 4.1 and 4.2 are Phases 13 and 14 in `BUILD_LOG.md`, and the reliability phase, Phase 5
here, is Phases 16 to 16.4 there.

To repeat the two suites:

```powershell
.venv\Scripts\python -m pytest tests --ignore=tests\crew
.venv-crew\Scripts\python -m pytest tests\crew
```

The browser flows, mutation checks and connection guard were run from temporary scripts, not from this repository.

## 40-case evaluation

**This is an offline benchmark, not a live Gemini accuracy test.** It uses the test doubles, on synthetic cases written by the same
developers. It shows that the pipeline and deterministic rules work as designed on those cases, not that they generalise.

| Measure | Result |
|---|---|
| Cases | 40 synthetic cases, 8 in each of five categories |
| Expected final decisions | **40 of 40** |
| Comparison with baseline `EV-20261102-135347-0a89f7` | **0 differences** across 55 fields and 10 final report fields |
| Decided by | Deterministic rules 35, guardrail escalation 5 |
| Payouts | 8 of 40, total ₹1,29,240 |
| Approval status | 28 PENDING, 12 NOT_REQUIRED |
| Review status | COMPLETE on all 40 |
| Gemini requests | 0 |

## Live runs

These are stored in `runtime/runs/`, which git does not carry, so they can be opened only on the machine that holds them. Request
counts come from the ledger, `runtime/gemini_requests.sqlite3`. All four used `gemini-3.5-flash` for all three agents.

| Run | Claim | Decision | Review status | Human approval | Requests |
|---|---|---|---|---|---|
| `RUN-dash-90e0f64064` | WC-2026-0411 | APPROVE | COMPLETE | PENDING | 9 |
| `RUN-dash-5c40c24a5a` | WC-2026-0411 | APPROVE | COMPLETE | PENDING | 6 |
| `RUN-dash-9f819f9dbc` | WC-2026-0412 | ESCALATE | COMPLETE | PENDING | 7 |
| `RUN-dash-bcdbabaf97` | WC-2026-0412 | ESCALATE | COMPLETE | PENDING | 7 |

`RUN-dash-bcdbabaf97` is the reference run:
- **Execution:** the Investigator and Forensic Agent both began at 08:53:32 UTC on 6 November, after the Evidence Agent finished.
- **Requests:** Evidence 2, Investigator 2, Forensic 3. The Forensic count includes the retry.
- **Provider failure:** the Forensic Agent received an HTTP 503. Its one retry, after 15 seconds, recovered. The original error stays
  in the record.
- **Decision:** ESCALATE by GUARDRAIL_ESCALATION. No review has been recorded, and it stays pending on purpose.

Earlier live runs that failed are kept, including `RUN-dash-c295d5a694`, which reached the free-tier quota.

## D8 validation

D8 is the rule that a run needs human review when an agent review or the MCP records are unavailable. **It was validated with test
doubles and disposable resources**, with real faults at local boundaries, not in a true live failure.

What the tests show:
- An empty MCP log with agent-recorded calls stores INCOMPLETE and PARTIAL, with approval PENDING. The decision, decider and
  settlement match the clean run, and the page explains why a person is needed.
- An empty MCP log with no agent-recorded calls is not flagged, so normal runs are not misclassified.
- Genuine MCP failures keep their own error and wording.

## Stored dashboard runs

37 dashboard runs: 3 completed, 20 escalated, 14 incomplete, 1 recovered 503. No stored run has the contradictory MCP record case.

## Known limitations

- **Partial MCP record loss:** if some records survive, the run is judged call by call and is not flagged. It can settle with
  unverified HIGH concerns and no person. A test pins this behaviour; it documents the gap and does not endorse it.
- **Reviewer authentication:** the reviewer's name is typed in, not authenticated.
- **Review locking:** the lock that stops two conflicting reviews works within one process only.
- **Older failed runs:** 13 runs from 2 to 4 November stay unclassified, because they predate failure classification.
- **Live evidence:** four complete runs on two sample claims. There is no measure of live accuracy.
- **Prompt injection:** the screen is a pattern list. The controlled tests show the safeguards around a manipulated test double,
  not how a real model behaves.
- **Deployment:** local only, on one machine. Windows 11 and Python 3.12.10 are the only tested platform.
- **Teacher criteria:** the status of each criterion is in `CHECKLIST.md`. Cloud deployment is not done. The comparisons of messaging
  styles and design patterns are not done.

## Presentation

`presentation/ClaimSleuth_Simple_Presentation.pptx` and its PDF date from 2 November 2026 and were left unchanged. Slide 8 is out of
date: it gives 301 and 64 passed tests and says live Gemini reasoning has not been verified. Slides 5 and 6 describe the Blackboard
as coordination only, but it now carries the Evidence report to the later agents. Use the figures on this page instead.

## Fresh install

Checked on 6 November 2026 in new virtual environments, built outside the working folder from only the files git would carry, with
no `.env`. The README's steps ran as written. PyPI was the only outside service used, and no Gemini request was sent.

| Check | Result |
|---|---|
| Install steps, both environments | All succeeded, about 20 minutes with a filled pip cache |
| `pip check`, both environments | No broken requirements |
| Pinned packages | Same versions as the working environments |
| Unpinned sub-dependencies | 7 of 71 newer in `.venv`, 12 of 152 newer in `.venv-crew` |
| Dashboard start | MCP and CrewAI services started, OFFLINE, on alternative ports |
| All four sample PDFs through the page's API | All gave their expected decision: APPROVE, ESCALATE, ESCALATE, REQUEST_EVIDENCE |
| `scripts\stop_services.ps1` | Stopped all three services |
| Tests in the new environments | 133 passed in `.venv`, 27 passed in `.venv-crew`, 0 failed |

Those 160 tests are a selection: adjudicator, rules, integration, dashboard end-to-end and reliability, plus the CrewAI crew and
Gemini-mode files. The full suites ran in the working environments. The stored live runs do not come with a clone, because
`runtime/` is not in git. Live mode was not tried in the new environments, to avoid Gemini requests.
