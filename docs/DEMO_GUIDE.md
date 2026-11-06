# ClaimSleuth demonstration guide

This is the sequence for presenting ClaimSleuth, with the commands checked against the repository. It uses offline test doubles,
so it needs no API key and sends no Gemini request. The real multi-agent evidence comes from a live run that is already stored.

All commands are for Windows PowerShell, from the project folder. Installation is in the README.

## What is real in the demonstration

| Part | What it is |
|---|---|
| Recorded run `RUN-dash-bcdbabaf97` | A genuine live run on Gemini, stored on 6 November 2026. Opening it sends no request |
| New investigation in offline mode | Real PDF extraction, ADK, CrewAI, MCP, A2A, Blackboard and adjudicator, with **test doubles instead of a model** |
| Security & Trust tests | Controlled tests on disposable copies, with a deterministic test double. They do not show how a real model behaves |
| 40-case evaluation | An offline benchmark on test doubles, not a measure of Gemini's accuracy |

The stored runs live in `runtime/`, which git does not carry. The recorded run can be opened only on the machine that holds it.

## Before you start

1. Plug the laptop in. Do not close the lid during the demonstration.
2. Stop anything already running. Ctrl+C in the dashboard's window is enough. For services left behind:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\stop_services.ps1
   ```

   Do this even if a dashboard is already open: a running dashboard keeps the Python code it started with.

3. Start the dashboard in offline mode:

   ```powershell
   .venv\Scripts\python -m claimsleuth.dashboard
   ```

   It starts the MCP server on port 8102 and the CrewAI A2A service on port 8103, then serves the page at
   <http://127.0.0.1:8110/>. The first start can take a minute. Ctrl+C stops everything it started.

4. Check that the mode badge says OFFLINE · test doubles.

## Part 1: a new claim, live on screen (about 3 minutes)

| Step | On screen | What to explain |
|---|---|---|
| 1. A claim enters the system | On the `WC-2026-0412` card, click **Use this claim**, then **Run Investigation** | One PDF holds a claim form, a retail invoice and a repair report. pypdf extracts every line with its page and line number |
| 2. Evidence Agent | The status strip marks Evidence Agent | It extracts facts and evidence, each citing its document line |
| 3. Evidence reaches the Blackboard | **Blackboard & Agent Coordination** | The validated report is published to the shared Blackboard as a signed entry |
| 4. Parallel agents | **Execution Overview** shows the overlap | The Investigator (ADK) and Forensic Agent (CrewAI) run at the same time, and each reads the report from the Blackboard with a signed receipt. Neither sees the other's report |
| 5. MCP and A2A | **Execution Overview**, then **Advanced Diagnostics**, **Execution Monitoring** | Both MCP tools are called on a separate server, which keeps its own log. The Forensic Agent is reached over A2A, in a separate Python environment |
| 6. Manager | Task progression in **Execution Overview** | The deterministic manager plans the run, starts each task when its dependencies are met, and passes on only verified reports |
| 7. Adjudicator | Results banner, then **Investigation Findings** | The decision comes from fixed rules, not from a model or a vote. Expect ESCALATE by the security guardrail: the hidden instruction on PDF page 3 is detected and not obeyed, and the serial mismatch is still reported |
| 8. Human review | **Human Approval** | An escalation needs a person. A review is recorded separately and never changes the automated decision |
| 9. Oversight | **Security & Trust**, **Advanced Diagnostics**, **Audit Trail** | Execution, security, monitoring and audit records are all on the page |

Running this adds one new run to `runtime/`. Existing runs are not changed.

To show other outcomes, use the same steps:

| Sample | Expected result |
|---|---|
| `WC-2026-0411` | APPROVE, ₹16,400.00, human sign-off required because it is ₹15,000 or more |
| `WC-2026-0413` | ESCALATE: an instruction asks for other customers' claim histories; every lookup stays within this claim |
| `WC-2026-0414` | REQUEST_EVIDENCE: the missing repair report is named |

## Part 2: the recorded live run (about 2 minutes)

Open this address in the same browser:

```text
http://127.0.0.1:8110/#investigation?run=RUN-dash-bcdbabaf97
```

**Do not click Approve, Reject or Request More Information on this run.** Its human review is deliberately still pending, and a
review is a permanent record.

| Fact, from the stored run and the request ledger | Value |
|---|---|
| Claim | WC-2026-0412 |
| Models | `gemini-3.5-flash` for all three agents |
| Decision | ESCALATE, by GUARDRAIL_ESCALATION |
| Run status, review status | ESCALATED, COMPLETE |
| Human approval | PENDING; no review recorded |
| Parallel start | Investigator and Forensic Agent both began at 08:53:32 UTC |
| Gemini requests | 7: Evidence 2, Investigator 2, Forensic 3 |
| MCP server log | 6 calls |
| Provider failure | Forensic Agent, HTTP 503. One retry after 15 seconds, recorded as recovered |

Compare this with Part 1: the same claim gets the same decision on a real model and on the test doubles, because
the decision comes from the rules. Then show **Advanced Diagnostics**, **Execution Monitoring**, where the original 503 stays in
the record even though the retry recovered.

A live claim on screen is possible, but it is not needed and can fail on Google's side:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_live.ps1
```

It uses about 6 to 9 of the free tier's 20 daily requests. If the provider fails, the claim goes to a person, and that is the
designed behaviour.

## Part 3: optional extras

- **Security & Trust:** run **Evidence Integrity** and **Prompt Injection Protection**. They use disposable copies and are offline.
- **Evaluation:** the **Agent Evaluation & Performance Analytics** tab shows stored results if `runtime/evaluation/` exists. A new
  offline run takes several minutes, so start it before the session if you want one:

  ```powershell
  .venv\Scripts\python -m claimsleuth.evaluation run
  ```

- **Blackboard from the command line:**

  ```powershell
  .venv\Scripts\python -m claimsleuth.blackboard RUN-dash-bcdbabaf97
  ```

## Saying what it does not show

If asked, the short answers are:
- The test doubles are not AI reasoning, and the page labels them.
- Four complete live runs show the system works on a real model. They do not measure accuracy.
- A partial loss of MCP records is not flagged, so a run can still settle with unverified concerns and no person.
- The reviewer's name is typed in, not authenticated, and the application runs locally only.

`docs/EVIDENCE.md` has the full list.

## Common problems

| Symptom | Likely cause | What to do |
|---|---|---|
| `LIVE START REFUSED: port ... is already in use` | Services from an earlier start | Run `scripts\stop_services.ps1`, then start again |
| `LIVE START REFUSED: ... environment not found` | A virtual environment is missing | Follow the README's installation steps |
| `LIVE START REFUSED: Gemini is not configured` or `ERROR: LIVE mode needs Gemini credentials` | No usable `GOOGLE_API_KEY` | Offline mode needs no key. For live mode, set it in `.env` |
| The log says a service is `already listening ... using it as is` | An old MCP or CrewAI service is still running, possibly in the wrong mode or on old code | Stop it with `scripts\stop_services.ps1`, then start again |
| LIVE runs refused with a CrewAI mode mismatch | The CrewAI service was started in another mode | Same as above |
| `running scripts is disabled on this system` | PowerShell execution policy | Use the `powershell -ExecutionPolicy Bypass -File ...` form shown here |
| A run shows `429 RESOURCE_EXHAUSTED`, or requests are refused | The daily quota or local budget is used up | Use offline mode. The budget resets at midnight Pacific |
| A run shows HTTP 503 | Gemini is overloaded | One retry is automatic. After that the claim goes to a person |
| The page shows old behaviour | The dashboard started before the last code change | Stop and start it again |
| Other ports are needed | Something else uses 8110, 8102 or 8103 | Add `--port 8111 --mcp-port 8202 --crew-port 8203` |
| The console prints `Failed to configure mTLS` or `Skipping missing token usage metadata` | Messages from the Google libraries in offline mode | Ignore them. Offline mode uses neither |
