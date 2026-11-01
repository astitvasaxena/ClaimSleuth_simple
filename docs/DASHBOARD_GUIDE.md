# ClaimSleuth application guide

The application has one page: **Claim Investigation**. All the agent, protocol and tool machinery (ADK, CrewAI, A2A,
MCP, PDF parsing, the deterministic adjudicator) runs behind it. Security is shown inside the investigation itself:
the report's **Security & Document Integrity** section. There is no separate security testing page.

**Design reference:** the original minimal prototype,
`claim_sleuth/backups/dashboard_before_cinematic_redesign/index.html`, read only and never modified. This version keeps
its single-page layout and component order:
1. sample claim cards;
2. a toolbar with the claim chips and **Run Investigation**;
3. the four-card results banner;
4. two columns: the investigation timeline, verification and settlement on the left; documents, extracted evidence,
   **Human Approval** and **Audit Trail** on the right;
5. one collapsed details section.

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
   - **Results banner:** automated decision, coverage, settlement payout, human approval.
   - **Left column:** claim summary; **Security & Document Integrity** (below); warranty policy (retrieved through MCP) with its rule checks; claim history (real
     SQLite records through MCP, or "no earlier claims"); suspicious inconsistencies; missing evidence; why this
     decision; settlement breakdown.
   - **Right column:** source documents with line numbers and PDF pages; extracted evidence with its source line;
     Human Approval; Audit Trail.
5. **Human Approval:** Approve & Close / Reject Claim / Request More Information, with the reviewer's name and notes.
   - Allowed only when the automated decision requires human approval.
   - Each action is appended to `runtime/runs/<run_id>/human_review.jsonl`, with a timestamp, the reviewer and a
     copy of the automated decision it was made against.
   - `final_report.json`, `bundle.json` and `audit.jsonl` are never written to.
   - The page shows *Automated decision: ESCALATE* next to *Human review: Approved – <reviewer>*.
6. **Investigation Details** (collapsed): the three agents' reports, each in its own tab and never merged: Evidence
   Agent (Google ADK), Investigator Agent (Google ADK) and Forensic Agent (CrewAI). It also states the execution mode
   and the result of the latest genuine local-model attempt (`RUN-live-cs003-01`: FAILED, 4,096-token context).

**Currency:** all money is Indian Rupees.
- **Backend:** the settlement carries `currency: "INR"`, the rules write ₹ with Indian digit grouping
  (`₹1,25,000.00`), and the human-approval threshold is ₹15,000 (`config.HUMAN_APPROVAL_THRESHOLD`).
- **Page:** shows the same through `/api/status` (`currency`, `human_approval_threshold`).
- **Other currencies:** an amount marked in another currency (`$`, `USD`, `€`, `£`) is treated as unreadable, which
  leads to REQUEST_EVIDENCE. It is never taken as rupees.

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

## Backend behind the page

- **Investigation:** `integration.run.run_claim_async`, used unchanged. It gained an optional `progress` callback,
  default `None`. Apart from notifications, nothing changes: a failing observer is ignored.
- **Agents:** ADK (Evidence + Investigator agents, test-double), CrewAI over A2A, MCP (both tools, with the
  investigation-scope rule).
- **Decision:** the deterministic adjudicator.
- **Endpoints:**
  - `/api/status`, `/api/samples`, `/api/upload`, `/api/runs`, `/api/jobs/<id>`, `/api/runs/<id>`;
  - `/api/runs/<id>/review`.

## Limitations

- **Models:** the ADK agents always use their labelled test double. The CrewAI agent does too unless `ollama-local`
  is chosen. The agents mostly agree because their rules are similar, not because they are forced to.
- **Access control:** the reviewer name is typed in, not authenticated. The application binds to 127.0.0.1 only.
  An MCP call without a claim ID (an operator script) is not scope-limited.
- **PDF layout:** uploaded PDFs must use the section headings and `Label: value` lines of the samples. Other layouts
  give "missing" fields, never guessed ones.
- **Concurrency:** one investigation runs at a time.

## Tests

```powershell
.venv\Scripts\python -m pytest                   # test_dashboard.py, test_dashboard_e2e.py, test_mcp_scope.py, ...
.venv-crew\Scripts\python -m pytest tests\crew  # includes test_guardrail_harness.py
```

Screenshots are in `docs/screenshots/`.
