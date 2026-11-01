# Shared coordination blackboard

One persistent blackboard per installation, shared by the two processes that run agents:
- the **ADK side**: the Evidence and Investigator Agents, which run in the dashboard or integration process
  (`.venv`);
- the **CrewAI side**: the Forensic Agent, which runs in the A2A service process (`.venv-crew`).

Both open the same SQLite file independently.

## What it is, and what it is not

It is a **coordination board**:
- the orchestrator registers each investigation run;
- each side reads that registration, checks that it is investigating the same claim and the same documents, and
  publishes its own agents' lifecycle, status and validated reports;
- the orchestrator then seals the run, tracks completion and failures, reconciles what was published with what the
  adjudicator received, and records the adjudicator's decision.

It is **not** a collaborative workspace:
- no agent reads another agent's findings, so the three agents stay independent;
- agent prompts, outputs and the deterministic adjudication are exactly as they are without the board;
- the adjudicator still receives its inputs directly, as before, and still makes the decision.

## Turning it on and off

| Setting | Effect |
|---|---|
| `CLAIMSLEUTH_BLACKBOARD_DB` unset, or `off` / `0` / `false` / `none` / `disabled` / empty | No blackboard; nothing changes |
| `CLAIMSLEUTH_BLACKBOARD_DB=<path>` | Both sides use that SQLite file. A service started from this environment inherits the setting. |
| `python -m claimsleuth.dashboard` | Uses an explicitly set value. If none is set, it uses `runtime/blackboard.sqlite3`. |
| `python -m claimsleuth.dashboard --no-blackboard` | Disables the board, for the dashboard and the services it starts |

`python -m claimsleuth.integration` uses the board only when `CLAIMSLEUTH_BLACKBOARD_DB` is set. The CrewAI
service must have the same setting; services started by the dashboard get it automatically.

## A run on the board

Example: `RUN-dash-8b1b95a293`, claim WC-2026-0412, a live dashboard run.

| # | Kind | Section | Writer (process) | Event |
|---|---|---|---|---|
| 1–4 | register, status | run, status:\<agent\> | controller (`.venv`) | run bound to the claim and its documents; all three agents PENDING |
| 5 | status | status:evidence_agent | ADK (`.venv`) | RUNNING, from the ADK plugin's `before_agent_callback` |
| 6–8 | status, post, status | status / report:forensic_agent | CrewAI (`.venv-crew`, another pid) | RUNNING, report, COMPLETED, while the ADK workflow is still running |
| 9 | monitor | lifecycle:evidence_agent | ADK | `agent_finished`, from `after_agent_callback` (report awaiting validation) |
| 10–11 | status, monitor | investigator_agent | ADK | RUNNING, then `agent_finished` (it starts only after the Evidence Agent finished) |
| 12–15 | post, status | report / status for both ADK agents | ADK | validated reports, then COMPLETED, published when the ADK workflow ends |
| 16 | monitor | controller:seal | controller | adjudicator inputs fixed |
| 17 | monitor | controller:completion | controller | completed / failed / unfinished agents, reconciliation, authentication, monitoring failures |
| 18 | post | decision:final | adjudicator, recorded by the controller | ESCALATE by GUARDRAIL_ESCALATION, plus the final report's SHA-256 |

To look at a run:

```powershell
.venv\Scripts\python -m claimsleuth.blackboard --list
.venv\Scripts\python -m claimsleuth.blackboard RUN-dash-8b1b95a293 [--json] [--db PATH]
```

**On the dashboard:** the collapsed **Blackboard Monitoring** section under Investigation Details is filled from
`GET /api/runs/<run_id>/blackboard`. It is read-only and shows:
- the run and claim IDs, the database and the seal point;
- whether every writer was authenticated;
- each agent's state, with late states marked;
- the recorded decision, and whether it matches the stored report;
- monitoring failures and findings;
- the most recent events, with the writing process.

## ADK agent lifecycle

Each ADK agent's own start and end are published **while the ADK workflow runs**. This uses ADK's supported plugin
mechanism, `App(plugins=[BlackboardLifecyclePlugin])` (`blackboard/adk_plugin.py`):
- `before_agent_callback` marks the agent RUNNING;
- `after_agent_callback` records `agent_finished`;
- both return `None`, so ADK continues normally;
- the agents, their instructions and the existing callbacks (`adk_service/callbacks.py`) are unchanged;
- the plugin is attached only when the board is enabled.

An agent's **validated report**, and its COMPLETED or FAILED status, still follow when the workflow ends. Reports
are validated only after the whole ADK workflow has finished, so the board never shows an unvalidated report as a
result.

## Guarantees

- **Append-only history.** Database triggers refuse every `UPDATE` and `DELETE`. A retraction is a new entry
  pointing at the retracted post and giving a reason; the post itself stays.
- **Isolation.** Each run is bound to one claim and one document fingerprint when it is registered. Reading or
  writing a run under another claim is refused, and so is posting with other documents.
- **Validation before acceptance:**
  - the writer must authenticate (below);
  - the run must be registered, the claim must match, and the document fingerprint must match the registration;
  - only an agent failure reported before its documents were loaded may omit the fingerprint.
- **Status transitions:**
  - an agent moves PENDING → RUNNING → COMPLETED / FAILED / NOT_CONFIGURED;
  - final states are terminal;
  - an agent that failed cannot publish a result;
  - invalid moves are refused.
- **Idempotency.** Repeating a status, an identical post, a retraction, the seal, or a registration with the same
  details returns the existing entry and writes nothing.
- **Concurrency:**
  - WAL journal, a 5-second busy timeout, and `BEGIN IMMEDIATE` transactions;
  - the check and the append happen in one transaction, so concurrent writers and retries cannot both succeed with
    contradictory final states;
  - a process that dies mid-write leaves nothing behind.
- **Reconciliation:**
  - a published report that the adjudicator did not receive (for example after an A2A time-out or a failed
    contract check) is retracted, with the reason;
  - a report the adjudicator received but that was never published is recorded as a finding.

## Writer authentication (local)

There are three writer principals, each with its own 256-bit key in `<database>.keys/<principal>.key`. The
orchestrator creates the keys when it registers a run, using exclusive create, so existing keys are never
replaced.

| Principal | May write as | Process |
|---|---|---|
| `orchestrator` | controller, adjudicator | dashboard / integration |
| `adk` | evidence_agent, investigator_agent | dashboard / integration (ADK runner) |
| `crewai` | forensic_agent | CrewAI A2A service |

- **On every write:**
  - the writer's key is compared, in constant time, with its principal's key file;
  - the author must belong to that principal;
  - only the orchestrator may seal a run.
- **Every entry is signed** with HMAC-SHA256 over its run, sequence, claim, time, kind, section, author, framework,
  process, fingerprint, payload hash, reference and principal.
- **On every read, each entry is verified:**
  - `valid`: correctly signed;
  - `unsigned`: inserted without a key, for example by raw SQL, or written before authentication existed;
  - `invalid`: content or signature altered;
  - `identity`: author not allowed for the principal;
  - `unknown-key`: no key for that principal.
- **Visibility:** the controller records any entry that isn't `valid` as a finding, and the dashboard shows it.
- **Older databases:** one created before authentication gets the two new columns automatically. Its old entries
  are kept and shown as `unsigned`.

**Limitation:** the keys are protected only by the operating-system account. This separates and authenticates the
ClaimSleuth writers and makes tampering evident. It does not stop a malicious program running as the same OS user,
which could read the key files; that would need separate OS accounts.

## Late CrewAI reports

The controller seals a run (`controller:seal`) at the moment the adjudicator's inputs are fixed, before it
reconciles anything. An agent entry written after the seal:
- is **accepted**, so it stays traceable;
- is **flagged `late`** on every read;
- is **never** a live result;
- leaves the agent's `state_at_seal` showing what the controller actually saw;
- is reported as a monitoring failure (`late ...`).

The recorded decision comes before any late entry, and nothing can change it.

The A2A time-out is between 30 s and 3,600 s, and the CrewAI test double answers in about 2.5 s. So a real time-out
does not occur in test-double mode. The late path is exercised in two ways:
- **Tests:** `tests/crew/test_blackboard_crew.py` runs the real CrewAI runner after the seal.
- **Live demonstration:** the controller seals the run, then a real A2A request goes to the running CrewAI
  service; that service's three entries arrive flagged LATE.

## Blackboard monitoring failures

A blackboard problem never changes an investigation. Examples: the database is unusable or locked too long, a
write is rejected, a run is unregistered, or an entry is late. Each one is reported explicitly as a **Blackboard
monitoring failure**:
- on stderr (`BLACKBOARD MONITORING FAILURE [where] run/claim: error`);
- in `<database>.monitoring.jsonl`, with the process and environment that hit it;
- in the controller's `controller:completion` entry for the run, when the database is usable;
- in the dashboard's Blackboard Monitoring section. This works even when the database cannot be opened, because the
  monitoring log is a separate file.

## Limitations

- **Agents do not exchange findings.** This is by design, to keep their independence.
- **ADK report timing:** the validated ADK reports appear when the ADK workflow ends; only the lifecycle events
  appear while it runs.
- **Same OS user:** writer authentication does not protect against a malicious process running as the same user
  (see above).
- **Machine scope:** SQLite in WAL mode needs all writers on one machine and a local disk, which fits the
  127.0.0.1-only services.
- **A real A2A time-out is not reproducible with the test double.** The late path is tested with the real CrewAI
  runner instead.

## Code and tests

- `src/claimsleuth/blackboard/`:
  - `models.py`: entries and states;
  - `store.py`: SQLite store, migration, seal and late flags;
  - `auth.py`: keys, signatures, verification;
  - `controller.py`: `AgentParticipant`, `RunController`, monitoring failures;
  - `adk_plugin.py`: ADK lifecycle plugin;
  - `monitor.py`: dashboard and viewer summary;
  - `__main__.py`: the viewer.
- **Integration points:**
  - `adk_service/runner.py`: registration check, the plugin, publishing in `finish()`;
  - `crew_service/runner.py`: the same, without the plugin;
  - `integration/run.py`: register, collect (seal), publish the decision;
  - `dashboard/__main__.py`: default path and `--no-blackboard`, through `blackboard_setting()`;
  - `dashboard/app.py`: the `/api/runs/<id>/blackboard` route;
  - `dashboard/static/`: the collapsed Blackboard Monitoring section.
- **Tests:**
  - `tests/test_blackboard.py` (44): store, authentication and tampering, migration, late entries, real
    multi-process concurrency including a `.venv-crew` writer, crash mid-write, contention, the controller, the ADK
    plugin, the monitoring view, the dashboard route and the launcher's board settings;
  - `tests/test_blackboard_e2e.py` (11): real MCP and CrewAI A2A service processes, per-agent ADK lifecycle,
    authentication, an unusable board with an unchanged adjudication, and the dashboard view of a real run;
  - `tests/crew/test_blackboard_crew.py` (3): the CrewAI side, including a late report from the real CrewAI runner.
