# Shared blackboard: shared state and context between agents

One persistent blackboard per installation, shared by the two processes that run agents:
- the **ADK side**: the Evidence and Investigator Agents, which run in the dashboard or integration process
  (`.venv`);
- the **CrewAI side**: the Forensic Agent, which runs in the A2A service process (`.venv-crew`).

Both open the same SQLite file independently.

## What it is

The blackboard is the run's **shared state** and the channel through which **intermediate output of one agent feeds
another**:
- the orchestrator registers each investigation run (one claim, one document fingerprint);
- the **Evidence Agent's validated report is published to the blackboard as soon as the agent finishes**;
- the **Investigator Agent** (ADK) and the **Forensic Agent** (CrewAI) each **consume that report from the
  blackboard**: read access is checked, the exact version and hash are verified, and a signed receipt is recorded;
- both publish their own validated reports; the orchestrator **seals** the run;
- the **deterministic adjudicator consumes only reports verified on the sealed blackboard**, then the decision is
  recorded there.

What it does not do:
- it never decides anything: the deterministic adjudicator remains the only final decision authority;
- it never hands the Investigator's report to the Forensic Agent, so ADK and CrewAI stay two independent opinions
  (no anchoring); the Investigator and Forensic reports are read only by the adjudicator;
- it does not replace the other channels (next section).

### The four ways information moves

| Mechanism | What it carries | Between |
|---|---|---|
| **Blackboard** (SQLite, this guide) | Shared state and intermediate context: registrations, statuses, agents' reports, consumption receipts, seal, decision | All agents, the orchestrator and the adjudicator |
| **A2A** (A2A 1.0 JSON-RPC) | The FORENSIC_REQUEST / FORENSIC_RESPONSE messages; the request carries a **ContextRef** (a reference, never the report content) | The ADK side and the CrewAI service |
| **MCP** (Streamable HTTP) | Tool calls: `warranty_policy_search`, `claim_history_lookup` | Each agent and the MCP server |
| **ADK session state** | ADK's own workflow state inside one `SequentialAgent` run | The two ADK agents' execution; in blackboard mode the `evidence_report` key is filled from the verified blackboard copy |

### Read permissions (`models.CONTEXT_READERS`)

| Section | Writer | Authorised readers |
|---|---|---|
| `report:evidence_agent` | ADK Evidence Agent (`adk`) | Investigator Agent, Forensic Agent, adjudicator |
| `report:investigator_agent` | ADK Investigator Agent (`adk`) | adjudicator |
| `report:forensic_agent` | CrewAI Forensic Agent (`crewai`) | adjudicator |

These permissions are enforced by `Blackboard.consume`, the only way one participant receives another's report.
They are application-level rules: any program running as the same OS user could still open the database file (see
Limitations).

## Turning it on and off

| Setting | Effect |
|---|---|
| `CLAIMSLEUTH_BLACKBOARD_DB` unset, or `off` / `0` / `false` / `none` / `disabled` / empty | **Direct mode**: no blackboard. The Investigator reads the Evidence report from ADK session state, ADK and the A2A request start together, and the adjudicator receives the results as returned. Bundles say `context_sharing: "direct"`. |
| `CLAIMSLEUTH_BLACKBOARD_DB=<path>` | **Blackboard mode** (`context_sharing: "blackboard"`). Both sides use that SQLite file. A service started from this environment inherits the setting. |
| `python -m claimsleuth.dashboard` | Uses an explicitly set value. If none is set, it uses `runtime/blackboard.sqlite3`. |
| `python -m claimsleuth.dashboard --no-blackboard` | Direct mode, for the dashboard and the services it starts |
| Agent evaluation | Always blackboard mode, with its own file `runtime/evaluation/<id>/blackboard.sqlite3`, shared with its own CrewAI service |

`python -m claimsleuth.integration` uses the board only when `CLAIMSLEUTH_BLACKBOARD_DB` is set. The CrewAI
service must have the same setting; services started by the dashboard get it automatically. A CrewAI service without
the blackboard refuses to run on context references (fail closed).

## A run on the board

The entries of one offline run of CS-003 in blackboard mode, in order (asserted by `tests/test_blackboard_e2e.py`
and `tests/test_blackboard_context.py`). The CrewAI rows interleave with the Investigator's, because the two run
concurrently:

| Kind | Section | Writer (process) | Event |
|---|---|---|---|
| register, status | run, status:\<agent\> | controller (`.venv`) | run bound to the claim and its documents; all three agents PENDING |
| status | status:evidence_agent | ADK (`.venv`) | RUNNING, from the ADK plugin's `before_agent_callback` |
| monitor | lifecycle:evidence_agent | ADK | `agent_finished`, from `after_agent_callback` |
| post | report:evidence_agent | ADK | the validated Evidence report, **published during execution**; the orchestrator gets its ContextRef |
| status | status:evidence_agent | ADK | COMPLETED (`report_published`) |
| monitor | context:consumed:investigator_agent | ADK | **receipt**: the Investigator read that report from the blackboard |
| status, monitor, post, status | investigator_agent | ADK | RUNNING, `agent_finished`, its report, COMPLETED |
| status | status:forensic_agent | CrewAI (`.venv-crew`, another pid) | RUNNING, after the A2A request with the ContextRef arrived |
| monitor | context:consumed:forensic_agent | CrewAI | **receipt**: the Forensic Agent read the Evidence report with the `crewai` key |
| post, status | report / status:forensic_agent | CrewAI | its report, COMPLETED |
| monitor | controller:seal | controller | adjudicator inputs fixed |
| monitor | controller:completion | controller | completed / failed / unfinished agents, reconciliation, authentication, monitoring failures |
| monitor ×3 | context:consumed:adjudicator | adjudicator (`orchestrator` key) | **receipts**: each report the adjudicator verified and used |
| monitor | controller:verification | adjudicator | which reports were verified, by entry and hash |
| post | decision:final | adjudicator, recorded by the controller | the decision, plus the final report's SHA-256 |

Interleaved with these, the **orchestration manager** (`orchestration/manager.py`) records its plan and every task
state change as signed `monitor` entries written with the `orchestrator` key (framework `manager`):
- `manager:plan`: the five tasks, their agents, channels and dependencies, right after registration;
- `manager:task:<task_id>` (`baseline`, `evidence`, `investigation`, `forensics`, `adjudication`): one entry per
  event - for example `dependencies_met` (READY), `dispatched` (RUNNING; for the Investigator, the manager's release
  just before its context receipt), `context_consumed`, `report_published`, `report_verified` (COMPLETED),
  `blocked`, `failed`, `timed_out`, `blackboard_sealed`, `decided`, `escalated_to_human`;
- `manager:failure`: only if the manager itself failed (then no `decision:final` is ever written).

They carry identifiers, states and references only, never document text. The dashboard's Blackboard Monitoring
section shows each task's latest state ("Manager tasks").

The run's **execution trace** (see "Monitoring and observability" in `ARCHITECTURE_GUIDE.md`) turns these entries into
provenance events: each publication and consumption receipt with producer, consumer, section, `run_seq`, SHA-256 and
receipt status, plus the seal, verification and decision. Each participant also times its own publish / consume /
verify / seal operations (`_Participant.operations`), and rejected reads appear as `blackboard.consume_rejected`.
The Investigator's and Forensic Agent's reports are posted when their runner finishes, after their execution record
is closed, so their publication is traced from the entry alone, without a latency.

To look at a run:

```powershell
.venv\Scripts\python -m claimsleuth.blackboard --list
.venv\Scripts\python -m claimsleuth.blackboard RUN-dash-8b1b95a293 [--json] [--db PATH]
```

**On the dashboard:** **Blackboard & Agent Coordination** summarises the run, and the **Blackboard Monitoring** section under
Advanced Diagnostics is filled from `GET /api/runs/<run_id>/blackboard`. It is read-only and shows:
- the run and claim IDs, the database and the seal point;
- whether every writer was authenticated;
- each agent's state, with late states marked;
- the recorded decision, and whether it matches the stored report;
- monitoring failures and findings;
- the most recent events, with the writing process.

## Shared context: how one agent's output reaches another

**Evidence → blackboard → Investigator (ADK, `blackboard/adk_plugin.py`).** This uses ADK's supported plugin
mechanism, `App(plugins=[BlackboardLifecyclePlugin])`:
- `after_agent_callback` of the Evidence Agent: the raw output is validated (`adk_service/runner.check_evidence`:
  Pydantic schema, claim id, in live mode its required MCP call, every citation against the documents and every
  policy reference against this run's MCP results). Only a fully valid report is published (`publish_report`), and
  the orchestrator is notified at once with its **ContextRef** (run, claim, section, `run_seq`, payload SHA-256,
  document fingerprint).
- `before_agent_callback` of the Investigator: the report is **consumed from the blackboard** (`consume_context`).
  The verified copy, delimited with `wrap_untrusted` as `<untrusted source="blackboard:report:evidence_agent@<run_seq>">`,
  is written to the session-state key the Investigator's instruction already reads (`{evidence_report?}`). The
  instruction text is unchanged. Whatever the Evidence Agent left in session state is overwritten and never used;
  the ADK result's `evidence_report` is the published report.
- Fail closed: if the report cannot be validated, published or consumed, the plugin returns content from
  `before_agent_callback`, which ends the ADK invocation. The Investigator does not run and the ADK result is FAILED.

**Evidence → blackboard → A2A reference → Forensic (CrewAI).** The orchestrator (`integration/run.py`) starts the
A2A request only when the Evidence report is published, so the Investigator and the Forensic Agent run concurrently
after it. The FORENSIC_REQUEST carries the ContextRef, not the report. The CrewAI service:
- checks the reference in the contract (`contract.check_request`: same run and claim, same documents, a section the
  Forensic Agent may read; otherwise `ERROR CONTEXT_REJECTED`);
- consumes the report from the blackboard with its **own `crewai` key** (`crew_service/runner.py`), leaving a signed
  receipt;
- appends the verified copy to the `claim_packet` input as an untrusted block under a one-line header
  (`claim_text.shared_context`); the CrewAI task description is unchanged;
- refuses to run the crew if any reference cannot be consumed (FAILED, no model call).

If no verified Evidence report exists, the orchestrator sends no FORENSIC_REQUEST at all (`forensic_a2a.not_sent`).

**Consumption (`Blackboard.consume`).** Everything is checked and recorded in **one** `BEGIN IMMEDIATE`
transaction, so nothing can change between the check and the recorded read:
1. the run is registered, the claim matches and the document fingerprint matches;
2. the consumer belongs to the writer's principal (`auth.PRINCIPALS`) and may read the section (`CONTEXT_READERS`);
3. the entry at the expected `run_seq` is a post of that section, and its stored payload has the expected SHA-256;
4. its HMAC signature verifies;
5. it is not retracted, not superseded by a newer post (stale), and not late;
6. its producer's status (before any seal) is COMPLETED;
7. agents read only **before** the seal; the adjudicator reads only **after** it;
8. the report validates against its schema, belongs to the claim and matches its recorded `report_sha256`;
9. a signed receipt is appended: a `monitor` entry `context:consumed:<consumer>` whose `ref_seq` points at the post,
   with the source section, version, author, principal and hashes.

Any failure writes nothing and is reported as a monitoring failure. Receipts reuse the existing `monitor` kind, so
**no table or schema change** was made; existing databases and records are read unchanged.

**Adjudicator inputs (`RunController.verified_inputs`).** After the seal, for each report a completed system
delivered, the adjudicator consumes the live post of that agent, and the post must hold exactly the delivered
report. The adjudicator receives the blackboard copies. If any report of a system cannot be verified (mismatch,
retracted, late, unsigned, missing, or the board is unusable), that system is handed to the adjudicator as FAILED
without reports. Its review is then unavailable, so the investigation is PARTIAL or NONE and needs a human, by the
adjudicator's existing rules. The results as executed are stored unchanged in the bundle.

## ADK agent lifecycle

Each ADK agent's own start and end are published **while the ADK workflow runs**, by the same plugin:
- `before_agent_callback` marks the agent RUNNING (for the Investigator, after it consumed its context);
- `after_agent_callback` records `agent_finished`;
- the agents, their instructions and the existing callbacks (`adk_service/callbacks.py`) are unchanged;
- the plugin is attached only in blackboard mode.

The Evidence Agent's report and COMPLETED status are published during execution; the Investigator's follow when the
ADK workflow ends. An agent published during execution keeps that status even if a later agent fails, so the history
shows what really happened.

## Guarantees

- **Append-only history.** Database triggers refuse every `UPDATE` and `DELETE`. A retraction is a new entry
  pointing at the retracted post and giving a reason; the post itself stays.
- **A later failure does not retract a valid report.** If the Investigator fails after the Evidence report was
  published, that report stays live and with the ADK result. The adjudicator still ignores it, because it uses nothing
  from a system that did not complete.
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

## Blackboard monitoring failures and fail-closed behaviour

Every blackboard problem is reported explicitly as a **Blackboard monitoring failure**. Examples: the database is
unusable or locked too long, a write or read is rejected, a run is unregistered, or an entry is late. It appears:
- on stderr (`BLACKBOARD MONITORING FAILURE [where] run/claim: error`);
- in `<database>.monitoring.jsonl`, with the process and environment that hit it;
- in the controller's `controller:completion` entry for the run, when the database is usable;
- in the dashboard's Blackboard Monitoring section. This works even when the database cannot be opened, because the
  monitoring log is a separate file.

In blackboard mode a problem is also **fail closed** wherever context or adjudicator inputs depend on it:

| Situation | Outcome |
|---|---|
| Evidence report invalid, unsupported or not publishable | Investigator not started; no FORENSIC_REQUEST; both reviews unavailable: review NONE, human approval |
| Investigator fails after the Evidence report was published | Evidence stays COMPLETED (and consumed by Forensic), Investigator FAILED; the Evidence post is retracted at the seal (the ADK result is not adjudicated); review PARTIAL, human approval |
| Forensic fails or is unreachable | ADK reports verified and used; review PARTIAL, human approval |
| A delivered report differs from, or is missing on, the blackboard | that system is not adjudicated; review PARTIAL or NONE, human approval |
| Any entry of the run fails verification (altered, forged, unsigned) | no report of the run is adjudicated, even one read before the change: review NONE, human approval (an approval by the rules becomes ESCALATE, INSUFFICIENT_REVIEW) |
| The blackboard cannot be opened at all | nothing is shared or verified: review NONE, human approval (an approval by the rules becomes ESCALATE, INSUFFICIENT_REVIEW) |

An incomplete investigation never becomes a silent approval.

## Evidence integrity

**How tampering is detected.** The HMAC signature covers the stored `payload_sha256`, not the payload text, so the two
checks do different jobs. Changing a payload and keeping its hash leaves the signature valid: the payload-hash check
catches it. Changing the hash too breaks the signature. `auth.diagnose` reports each check on its own.

| What was changed | Payload check | Signature check | `attack_type` |
|---|---|---|---|
| Payload only | MISMATCH | VALID | `PAYLOAD_TAMPERING` |
| Payload and its hash | MATCH | INVALID | `HASH_AND_PAYLOAD_TAMPERING` (or `SIGNATURE_FORGERY` if the ContextRef was changed too) |
| Signature | MATCH | INVALID | `SIGNATURE_FORGERY` |
| Nothing: the reference is wrong | MATCH | VALID | `REFERENCE_MISMATCH` |

**A refused read is a security event.** `consume()` raises `BlackboardSecurityError`, which names what was found. The
participant that refused the read turns it into a `SecurityEvent` (`schemas.py`). Its text comes from the fixed table
`models.SECURITY_RESPONSES`, never from the exception, so nothing from a stored entry reaches monitoring. A refused read
still writes nothing on the blackboard: the event goes to `<database>.monitoring.jsonl`, the agent system's result, the
run bundle, the trace (`security.event`, category `security`) and, once the run ends, one signed `manager:security` entry.

**One bad entry stops the whole run's agent reports.** `verified_inputs` looks at every entry of the sealed run. If any
failed verification, nothing is adjudicated, whichever entry it was and whenever it changed. An agent may have read a
report while it was still sound, but the run's record is no longer trustworthy, so it is not trusted in part. Without
this, a report altered after both agents read it left the other system's review standing, and a claim the rules approve
stayed APPROVE with a payout.

**Other rules:** a status row must verify before it can make a report final; a payload that is not a JSON object reads as
empty (and then fails verification) instead of stopping the run being read; the CrewAI service refuses an A2A request
without a ContextRef for a run registered on the blackboard (`contract.check_context_required`).

**Controlled test:** `python -m claimsleuth.dashboard.evidence_integrity_lab`. See `docs/DASHBOARD_GUIDE.md`.

## Limitations

- **Same OS user:** writer authentication and read permissions are enforced by the ClaimSleuth code. They do not
  stop a malicious program running as the same OS user, which could read the key files or the database itself;
  that would need separate OS accounts. Someone holding the keys can re-sign a changed entry. The agents' ContextRef
  and the adjudicator's comparison with the delivered report still refuse it, but only for that system's report.
- **Whole-file rollback:** restoring an older copy of the database and keys is not detected by signatures. A run already
  holding a ContextRef refuses it, because that entry is then missing. Nothing outside the file records its latest state.
- **Shared ADK key:** the Evidence and Investigator Agents run in one process and share the `adk` key; they are told
  apart by author binding, not cryptographically.
- **A forged row is fail closed, not ignored:** a raw-SQL row in a report section fails authentication, and it also
  makes the genuine report no longer the current one, so that report is no longer consumed either.
- **Strict Evidence validation:** one unsupported citation in the Evidence report stops the Investigator and the
  Forensic Agent for that run. A live model that cites carelessly gives an incomplete investigation, never a silent
  one.
- **Latency:** the Forensic Agent now starts after the Evidence Agent instead of at the same time.
- **Machine scope:** SQLite in WAL mode needs all writers on one machine and a local disk, which fits the
  127.0.0.1-only services.
- **A real A2A time-out is not reproducible with the test double.** The late path is tested with the real CrewAI
  runner instead.

## Code and tests

- `src/claimsleuth/blackboard/`:
  - `models.py`: entries and states, `CONTEXT_READERS`, `ContextRef` (defined in `schemas.py`);
  - `store.py`: SQLite store, migration, seal and late flags, `consume()`;
  - `auth.py`: keys, signatures, verification, `diagnose`;
  - `controller.py`: `AgentParticipant` (`publish_report`, `consume_context`), `RunController`
    (`verified_inputs`), `context_block`, monitoring failures;
  - `adk_plugin.py`: ADK lifecycle plugin and `EvidenceContext` (Evidence → Investigator);
  - `monitor.py`: dashboard and viewer summary, including the consumption receipts (`context`) and the
    manager's task states (`tasks`);
  - `__main__.py`: the viewer.
- **Integration points:**
  - `adk_service/runner.py`: `check_evidence`, the plugin, the published Evidence report as the run's report;
  - `adk_service/forensic_a2a.py`, `contract.py`: ContextRefs in the FORENSIC_REQUEST, `not_sent`;
  - `crew_service/a2a_server.py`, `crew_service/runner.py`: consumption with the `crewai` key, the claim packet;
  - `orchestration/manager.py`: register, the plan, release the Investigator, dispatch Forensic on the published
    Evidence report, collect (seal), `verified_inputs`, publish the decision, the `manager:*` entries;
  - `integration/run.py`: the entry point, which hands the run to the manager;
  - `evaluation/runner.py`: one blackboard per evaluation;
  - `dashboard/__main__.py`: default path and `--no-blackboard`, through `blackboard_setting()`;
  - `dashboard/app.py`: the `/api/runs/<id>/blackboard` route;
  - `dashboard/static/`: the collapsed Blackboard Monitoring section (unchanged; it lists the receipts as events).
- **Tests:**
  - `tests/test_blackboard_context.py`: publication during execution, consumption and receipts, cross-run and
    cross-claim, permissions, forged, unsigned, mismatched, retracted, stale, unfinished and late context, the seal,
    real multi-process publication and consumption, adjudicator inputs, the Investigator's actual model instruction,
    a tampered raw session state, unsupported Evidence, the real A2A + CrewAI path with the ContextRef, Forensic
    independence, Forensic and Investigator failures, direct mode, the 40-case offline evaluation and the dashboard
    and evaluation endpoints;
  - `tests/crew/test_blackboard_context_crew.py`: the Forensic model's actual prompt, injected text in the shared
    report, unverifiable or unauthorised context and a service without the blackboard;
  - `tests/test_blackboard.py`: store, authentication and tampering, migration, late entries, real multi-process
    concurrency including a `.venv-crew` writer, crash mid-write, contention, the controller, the ADK plugin, the
    monitoring view, the dashboard route and the launcher's board settings;
  - `tests/test_blackboard_e2e.py`: real MCP and CrewAI A2A service processes, the per-agent ADK lifecycle with the
    context receipt, authentication, an unusable board (fail closed), and the dashboard view of a real run;
  - `tests/crew/test_blackboard_crew.py`: the CrewAI side, including a late report from the real CrewAI runner.
