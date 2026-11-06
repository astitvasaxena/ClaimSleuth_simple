# ClaimSleuth application guide

The application has one page: **Claim Investigation**. All the agent, protocol and tool machinery (ADK, CrewAI, A2A,
MCP, PDF parsing, the deterministic adjudicator) runs behind it. Security is shown inside the investigation itself:
the report's **Security & Document Integrity** section. There is no separate security testing page.

**Design reference:** the original minimal prototype,
`claim_sleuth/backups/dashboard_before_cinematic_redesign/index.html`, read only and never modified. This version keeps
its single-page layout and shows the investigation and its outcome first:
1. sample claim cards, then a toolbar with the claim chips and **Run Investigation**;
2. the investigation status strip;
3. the four-card results banner, with any warnings directly under it;
4. **Agent Reports**: the three reports side by side;
5. two columns: **Investigation Findings** on the left; **Human Approval**, documents, extracted evidence and the
   **Audit Trail** on the right;
6. **Execution Overview** and **Blackboard & Agent Coordination**;
7. **Security & Trust**: Evidence Integrity and Prompt Injection Protection;
8. **Advanced Diagnostics**, collapsed: every detailed record.

**Summaries are visible, records are one click away.** Nothing was removed from the page: the tables moved into
expandable sections. An expanded section stays open when its contents are refreshed. A problem never waits behind one:
security findings, provider or agent failures and blackboard problems (authentication, refused reads, a decision that
differs from the report, late entries, monitoring failures) appear under the results banner, and the Advanced Diagnostics
heading shows how many were recorded.

The prototype's dark theme is replaced by a light theme with navy text, as requested.

## Start

```powershell
cd D:\Claimsleuth_simple
.venv\Scripts\python -m claimsleuth.dashboard          # http://127.0.0.1:8110/
```

The command prepares the workspace `runtime/dashboard/data` and starts two services on it: the MCP server (port 8102)
and the CrewAI A2A service (port 8103, `test-double` mode). It then serves the application on 127.0.0.1:8110. When you
stop it, it stops the services it started.

Options:
- `--mode live` uses Gemini for all three agents and needs `GOOGLE_API_KEY`; `scripts\start_live.ps1` checks the setup
  first. The default is offline test doubles.
- `--crew-mode ollama-local` uses local qwen2.5:3b for the Forensic Agent; never automatic, never a fallback.
- `--no-services` uses services you started yourself with `--data-dir runtime\dashboard\data`.

## Claim Investigation

1. **Sample claims:** *Use this claim* sends the sample PDF through the same upload path as a user's own file.
   *Download PDF* saves it.
   The cards are neutral, so they don't give the demonstration away:
   - `WC-2026-0411` (smartphone camera): a complete, clean claim.
   - `WC-2026-0412` (refrigerator cooling): two different serial numbers, an earlier claim on the serial in the
     history database, and **instruction A** as a "Technician Note" in the repair report (PDF page 3).
   - `WC-2026-0413` (smartphone charging): **instruction B**, asking for other customers' claim histories, as a
     "Notes" line in the invoice (PDF page 2).
   - `WC-2026-0414` (laptop boot failure): no repair report; clean.

   The expected outcomes are only in `data/sample_ground_truth.json`, never in the PDFs or the page.
   Amounts in the PDFs are written `INR 18,900.00`: the standard Helvetica font they use has no ₹ glyph.
2. **Upload:** drag a PDF onto the box or browse for one. The preview shows the claim chips, the product and serial,
   the documents found and anything missing. Security findings are shown only in the report.
3. **Run Investigation:** five stages, each marked done only on a real signal.

   | Stage | Marked done when |
   |---|---|
   | Document extraction | the PDF was parsed before the run was accepted |
   | Evidence Agent | the Investigator makes its first MCP call (the ADK agents run in sequence), or ADK finishes |
   | Investigator Agent | the ADK workflow has finished (progress callback) |
   | Forensic Agent | the A2A result has been received and validated (progress callback) |
   | Final adjudication | the deterministic adjudicator has finished (progress callback) |

4. **Report:**
   - **Results banner:** automated decision, coverage, settlement payout, human approval. When a new run finishes, the
     page moves here.
   - **Agent Reports:** the Evidence Agent (Google ADK), the Investigator Agent (Google ADK) and the Forensic Agent (CrewAI)
     side by side and never merged: status, model label, recommendation and reasoning. Findings and checks expand.
   - **Investigation Findings:** why this decision; settlement breakdown; **Security & Document Integrity** (below);
     suspicious inconsistencies; missing evidence; claim summary; warranty policy (retrieved through MCP) with its rule
     checks; claim history (real SQLite records through MCP, or "no earlier claims").
   - **Right column:** Human Approval; source documents with line numbers and PDF pages; extracted evidence with its
     source line; Audit Trail (expandable, with its entry count).
5. **Human Approval:** Approve & Close / Reject Claim / Request More Information, with the reviewer's name and notes.
   - Allowed only when the automated decision requires human approval.
   - Each action is appended to `runtime/runs/<run_id>/human_review.jsonl`, with a timestamp, the reviewer and a
     copy of the automated decision it was made against.
   - `final_report.json`, `bundle.json` and `audit.jsonl` are never written to.
   - The page shows *Automated decision: ESCALATE* next to *Human review: Approved – <reviewer>*.
6. **Execution Overview:** run status, LIVE or OFFLINE, the three agent statuses, duration, Gemini requests and test-double
   calls, MCP and A2A activity, the overlap of the Investigator and Forensic Agents, and the manager's task progression.
7. **Blackboard & Agent Coordination:** run registration, entries, seal, authentication, the recorded decision, task
   progression as stored on the board, and which report was published and read with a signed receipt.
8. **Advanced Diagnostics** (collapsed):
   - **Agent Execution Record:** execution mode, model, calls and MCP tools for each agent, and the result of the latest
     genuine local-model attempt (`RUN-live-cs003-01`: FAILED, 4,096-token context).
   - **Execution Monitoring:** run outcome and provider failures, then expandable records: timeline, task lifecycle, tool
     calls (ADK and CrewAI callbacks and the MCP server's own log), A2A communication, blackboard provenance, errors, model
     usage and cost, and all recorded events.
   - **Blackboard Monitoring:** run state, agent events, failures and the recorded decision.

Two sets of records that look alike are both kept, because each checks the other: tool calls recorded by callbacks and by
the MCP server, and task states from the manager's trace and from the blackboard.

**Currency:** all money is Indian Rupees.
- **Backend:** the settlement carries `currency: "INR"`, the rules write ₹ with Indian digit grouping
  (`₹1,25,000.00`), and the human-approval threshold is ₹15,000 (`config.HUMAN_APPROVAL_THRESHOLD`).
- **Page:** shows the same through `/api/status` (`currency`, `human_approval_threshold`).
- **Other currencies:** an amount marked in another currency (`$`, `USD`, `€`, `£`) is treated as unreadable, which
  leads to REQUEST_EVIDENCE. It is never taken as rupees.

## Reliability

**A run is on disk before any work starts** (`dashboard_meta.json`), and reports are written all or nothing (`atomic_write`), so a
process that dies leaves evidence and never half a report. `dashboard/recovery.py` reads that evidence and the blackboard:

| State | Meaning |
|---|---|
| Interrupted | Started, no report, not running now. The last stage comes from the blackboard: started, agents running, report published, blackboard sealed, or decided but not stored |
| Failed | The job ended with an error and stored no report |
| Damaged | A report is stored but cannot be read. It is never repaired or guessed at |
| Recovered | Was one of these, and a later investigation requested for it stored a report |

**Nothing resumes on its own.** No agent report from an unfinished run is reused, and no model request is made without a click.
**Start new investigation** under Advanced Diagnostics, Unfinished Runs, creates a new run with its own id, verification and
decision. The original records stay as they were. Runs from scripts are listed, marked, and left out of the totals.

**What the reviewer is told.** `review_status` is PARTIAL both when an agent review did not finish and when both finished but the
MCP server's records could not confirm their tool calls, so the page does not show it directly. `views.reviewer_view` says which
happened, and the status badge reads SUPPORTING RECORDS UNAVAILABLE in the second case. The stored run status stays INCOMPLETE. If the MCP server reports no fault but its log holds none of the tool calls the agents recorded, the manager treats the records as unavailable: the same badge and the same stored status, the decision and settlement unchanged, and a person must review the run. An empty log with no recorded agent calls is left as it is. A run stored before this check keeps its stored status, and the page says no person was asked. A report's HIGH findings appear under Agent concerns for the reviewer, verified and unverified apart, in the agent's own
words: agent, severity, why it could not be verified, and its effect on the decision. A finding that repeats a document's instruction
is withheld, and credentials are redacted. An unverified concern requires human review only: it never changes the decision or settlement.

**Repeated requests.** Each click on Run Investigation carries an `Idempotency-Key`. A retry with the same key returns the first run,
even after a restart. A new click is a new investigation, and a request without a key is always new.

**Human review.** The first Approve or Reject stands. The same action again changes nothing and is recorded as repeated; a different
one is refused and recorded. More information can always be requested. An approval is refused when the run has refused reads of
shared context, when the stored decision differs from the one signed on the blackboard, or when blackboard entries fail authentication.
No review changes the automated decision or authorises a payout.

## Security & Document Integrity (inside the report)

**How a malicious PDF is handled:**
1. pypdf extracts the text.
2. The deterministic screen (`security.screen_text`) checks the claim text and every document.
3. Each agent checks the untrusted text for itself and reports what it finds.
4. A detected instruction makes the adjudicator escalate the claim to a human under the existing security rule
   (`GUARDRAIL_ESCALATION`, no payout). It is never treated as proof of fraud, and the policy checks still run on the
   legitimate evidence.

**What the section shows**, built only from the run's records (`views.document_integrity`):
- **Status:** *Clean*, *Suspicious* (an out-of-scope history request was refused, or an agent flagged text the
  screen did not) or *Prompt Injection Detected*.
- **For each instruction:**
  - its type: *Prompt injection*, or *Unauthorized data access attempt* when the `cross_customer_data` /
    `bulk_data_request` rules matched;
  - the source document and file, the PDF page and lines, and the quoted text with wrapped lines joined;
  - the matched rules, and which agents reported it in their own findings;
  - the system's response.
- **Claim-history access**, from the MCP server's log: how many lookups were made, how many were limited to this
  claim's customer and serial numbers, and how many were refused.

The page doesn't show a refusal that did not happen. The test doubles never ask for other customers' data, so the
report says "0 refused". `test_dashboard_e2e.py` shows the MCP server refusing instruction B's requests under
0413's claim ID.

**Security lab:** `security_lab.py` and the CrewAI guardrail harness are kept as backend tests
(`test_dashboard.py`, `test_dashboard_e2e.py`, `tests/crew/test_guardrail_harness.py`). Their page and the
`/api/security/*` routes were removed. The scenarios it runs:

| # | Scenario | What runs |
|---|---|---|
| 1 | Normal claim | `screen_text` and the baseline rules on stored claim CS-008 |
| 2 | Direct prompt injection | `screen_text`, baseline, agent packet (`claim_message`), adjudicator |
| 3 | Instruction inside a PDF | pypdf extraction of sample 0413, per-line screen, baseline, packet, adjudicator |
| 4 | Unauthorised history request | six real MCP `claim_history_lookup` calls scoped to CS-005 (live MCP server) |
| 5 | Fabricated evidence | Pydantic, A2A contract, adjudicator, plus a **controlled demonstration**: scripted invalid output through a genuine `Crew.kickoff()` with the real guardrail |
| 6 | Abusive customer | `screen_text`; the baseline on CS-008 with and without the abusive statement (same decision) |

## Security & Trust: Evidence Integrity

A collapsible section at the bottom of the Claim Investigation page. It reads only what the backend stored
(`GET /api/evidence-integrity`) and keeps two things apart:

- **Controlled adversarial test:** the latest result file in `runtime/security/evidence_integrity/`. **Run controlled
  test** (`POST /api/evidence-integrity/run`) starts `dashboard/evidence_integrity_lab.py` as its own process, one at a
  time and not during an investigation or an evaluation. It takes about a minute and is always offline, whatever mode the
  dashboard runs in.
- **Real claim investigations:** `security.event` entries from the stored run traces, without those marked `test_mode`.
  A run's own report also shows an **Evidence Integrity** block when a read was refused in that run.

No hash, signature or key reaches the page: the result file holds outcomes only.

**What the test does.** It reads the sample PDF, runs the real workflow on it, and publishes the real reports to a
disposable database. It then attacks independent copies of that database: repair amount changed (hash and signature
kept), payload changed, signature forged or replaced, another claim's report substituted, old reports replayed,
unauthorised readers, update and delete, malformed reports, payload and hash changed together, and a clean control. Whole
investigations also run with the report altered in the middle of the run, by `TamperingManager`, which is the real
manager and only chooses the moment.

| Real | Test double | Attacker |
|---|---|---|
| Blackboard, SHA-256, HMAC, triggers, manager, ADK Runner, CrewAI A2A service process, MCP, adjudicator, PDF parser | The agents' reasoning (offline test doubles); no model request | `mutate()` and `TamperingManager` |

**The test's claims.** `CLM-002` was the name in the specification; no file uses it. It is taken to mean the second of
the four samples, WC-2026-0412. That claim escalates anyway (prompt injection in its repair report), so its decision
does not change when it is tampered with. WC-2026-0411, which the rules approve, is attacked as well: there the tamper
turns an approval with a payout into an escalation with none.

**Safety.** `assert_disposable` refuses any database outside a marked folder in the system's temporary directory. The test
uses a temporary request ledger, removes API keys from its environment, and compares the hashes of the production
blackboard, ledger, `.env`, claim database and sample PDFs before and after.

## Security & Trust: Prompt Injection Protection

Under Evidence Integrity in the same section, built the same way, with the attempt tables expandable: it reads only what the
backend stored
(`GET /api/prompt-injection`), and keeps controlled tests apart from real investigations.

- **Controlled adversarial test:** the latest result file in `runtime/security/prompt_injection/`. **Run controlled test**
  (`POST /api/prompt-injection/run`) starts `dashboard/prompt_injection_lab.py` as its own process, one test at a time, and
  not during an investigation or an evaluation. It takes about five minutes and is always offline.
- **Real claim investigations:** `security.injection` entries from the stored run traces, without those marked `test_mode`.

**What the test does.** Each attempt checks the fingerprint of an original sample PDF, writes a labelled synthetic
instruction into a disposable copy, and runs the copy as a whole claim. Most attempts run with an **obedient double**
(`obedient_double.py`), which does what the document says: it recommends approval, hides conflicts, repeats its instructions
and passes the order on. The safeguards around an agent are therefore tested with the agent already manipulated. One attempt
of each category also runs with the well-behaved double, to show that extraction and policy checks still work.

| Real | Test double | Simulated | Not tested |
|---|---|---|---|
| PDF parser, input screen, `<untrusted>` delimiters, manager, ADK Runner, CrewAI A2A service process, MCP, blackboard, report validation, adjudicator | The agents' reasoning; no model request | The attacks: synthetic instructions written for this test | Whether a real model obeys or ignores any of this |

**How an attempt is reported.**

| Outcome | Meaning |
|---|---|
| Prevented | An agent's output was unsafe and a control stopped it, and the claim ended no better for the attacker than the clean claim |
| Detected | The input screen or an output check recorded it, and the agents' output was not unsafe (the well-behaved double) |
| Not detected, no effect | Nothing recorded it and the outcome stayed that of the clean claim. This is not called prevented |
| Not prevented | The outcome moved in the attacker's favour: an approval, more money, human review lost, or a secret stored. A detected instruction that did not end in escalation counts too. The test fails |
| Test failed | The run itself failed: it says nothing about the attack, and the test fails |

Some attempts are written to be missed by the patterns (a polite paraphrase, base64), so the "not detected" column is real.
A planted line that only looks like an instruction is covered too: repair-shop wording close to the patterns must leave the
outcome, the payout and the events exactly as for the clean claim.

**Safety.** As for Evidence Integrity: disposable folder, temporary ledger, no API key in the environment, and hashes of the
protected files compared before and after. The result file holds names, categories and outcomes only, never the planted text.

## Backend behind the page

- **Investigation:** `integration.run.run_claim_async`, used unchanged. It gained an optional `progress` callback,
  default `None`. Apart from notifications, nothing changes: a failing observer is ignored.
- **Agents:** ADK (Evidence + Investigator agents, test-double), CrewAI over A2A, MCP (both tools, with the
  investigation-scope rule).
- **Decision:** the deterministic adjudicator.
- **Endpoints:**
  - `/api/status`, `/api/samples`, `/api/upload`, `/api/runs`, `/api/jobs/<id>`, `/api/runs/<id>`;
  - `/api/runs/<id>/review`, `/api/runs/<id>/blackboard`, `/api/runs/<id>/trace`;
  - `/api/monitoring/agents`, `/api/reliability`, `/api/evidence-integrity`, `/api/prompt-injection`, `/api/evaluation`.

## Limitations

- **Models:** offline mode uses labelled test doubles, unless `ollama-local` is chosen for the CrewAI agent. Those agents
  mostly agree because their rules are similar, not because they are forced to. `--mode live` uses Gemini for all three.
- **Access control:** the reviewer name is typed in, not authenticated, and the review lock works within one process
  only. The application binds to 127.0.0.1 only.
  An MCP call without a claim ID (an operator script) is not scope-limited.
- **PDF layout:** uploaded PDFs must use the section headings and `Label: value` lines of the samples. Other layouts
  give "missing" fields, never guessed ones.
- **Concurrency:** one investigation runs at a time.
- **Restart after changes:** a running dashboard keeps the Python it started with. The reviewer name is typed in, so the review
  rules above record who said what, not who that person is.
- **Prompt injection:** the controlled test is offline, so it shows the controls around a manipulated agent, not whether a
  real model resists. The screen is a pattern list: a paraphrase, base64 or another language is not detected, and unusual
  legitimate wording can be escalated for human review. Text inside images and PDF annotations is not read. Runs stored
  before this section existed carry no injection events, and the panel covers the last 100 traces.

## Tests

```powershell
.venv\Scripts\python -m pytest                   # test_dashboard.py, test_dashboard_e2e.py, test_mcp_scope.py, ...
.venv-crew\Scripts\python -m pytest tests\crew  # includes test_guardrail_harness.py
```

Screenshots are in `docs/screenshots/`. They date from 1 November 2026, before the dashboard was simplified, so they
show the findings but not the current layout.
