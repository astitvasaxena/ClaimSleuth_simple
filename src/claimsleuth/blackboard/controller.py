"""How the investigation uses the shared blackboard: shared state and intermediate context between agents.

AgentParticipant  used by one framework's runner (ADK in the main process, CrewAI in the A2A service process):
                  reads the run's registration, publishes its own agents' lifecycle, status and validated reports
                  (publish_report: during execution, returning a ContextRef), and consumes another agent's report as
                  shared context (consume_context: verified, receipted), authenticated as the "adk" or "crewai" writer.
RunController     used by the orchestrator ("orchestrator" writer): provisions the writer keys, registers the run,
                  seals it when the adjudicator's inputs are fixed, tracks completion and failures, reconciles what
                  was published with what was received, hands the adjudicator only reports it verified on the
                  blackboard (verified_inputs), and publishes the adjudicator's decision.

Context flow (blackboard mode): Evidence Agent -> report:evidence_agent -> read by the Investigator Agent (ADK) and
by the Forensic Agent (CrewAI, via a ContextRef in the A2A request); the Investigator and Forensic reports are read
only by the adjudicator. The Forensic Agent never reads the Investigator's report (models.CONTEXT_READERS).

Security events: a read refused for a security reason (store.BlackboardSecurityError) becomes a SecurityEvent, kept by
the participant that refused it and added to the monitoring log; a refused read still writes nothing on the blackboard.
If any entry of the run fails verification, verified_inputs withholds every report of the run: a run with a compromised
entry is not trusted in part, so the adjudicator receives no agent review and a human decides.

Blackboard problems never raise into the caller: each is reported as a "Blackboard monitoring failure" on stderr and
in <database>.monitoring.jsonl. Where context or adjudicator inputs depend on the blackboard, a failure is fail
closed: the report is not consumed or not used, so the investigation becomes incomplete and needs a human.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from claimsleuth.blackboard.auth import BlackboardAuthError, Writer, load_writer
from claimsleuth.blackboard.models import (CONTEXT_READERS, EXPECTED_AGENTS, SECURITY_RESPONSES, TERMINAL, BoardEntry,
                                           ConsumedContext, ContextRef, document_fingerprint, payload_hash)
from claimsleuth.blackboard.store import (SEAL_SECTION, Blackboard, BlackboardError, BlackboardSecurityError,
                                          configured_path, provision)
from claimsleuth.schemas import (AgentSystemResult, EvidenceDocument, EvidenceReport, ExecutionStatus,
                                 FinalClaimReport, ForensicsReport, InvestigationReport, SecurityEvent,
                                 SecurityVerification, utc_now)
from claimsleuth.security import TEST_MODE_ENV, wrap_untrusted

# The schema of the report each section holds; a consumed report is validated against it before it is used.
SECTION_SCHEMAS: dict[str, type[BaseModel]] = {"report:evidence_agent": EvidenceReport,
                                               "report:investigator_agent": InvestigationReport,
                                               "report:forensic_agent": ForensicsReport}


def monitoring_log(path: Path) -> Path:
    return Path(f"{path}.monitoring.jsonl")


def report_monitoring_failure(path: Path | None, run_id: str, claim_id: str, where: str, error: str,
                              security: SecurityEvent | None = None) -> None:
    """Make a blackboard problem visible without touching the investigation: stderr plus the monitoring log."""
    record = {"ts": utc_now().isoformat(), "run_id": run_id, "claim_id": claim_id, "where": where,
              "error": error[:500], "pid": os.getpid(), "environment": Path(sys.prefix).name}
    if security is not None:
        record["security_event"] = security.model_dump(mode="json")
    print(f"BLACKBOARD MONITORING FAILURE [{where}] {run_id}/{claim_id}: {error[:500]}", file=sys.stderr, flush=True)
    if path is not None:
        try:
            with monitoring_log(path).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError as exc:
            print(f"BLACKBOARD MONITORING FAILURE [monitoring log] {exc}", file=sys.stderr, flush=True)


def read_monitoring_failures(path: Path, run_id: str) -> list[dict[str, Any]]:
    log = monitoring_log(path)
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("run_id") == run_id:
            out.append(record)
    return out


def security_event(run_id: str, claim_id: str, error: BlackboardSecurityError, consumer: str | None) -> SecurityEvent:
    """The structured record of a refused read: fixed labels and references, nothing taken from the entry's content."""
    event_type, severity, reason = SECURITY_RESPONSES[error.attack_type]
    return SecurityEvent(
        event_id=f"SEC-{uuid.uuid4().hex[:12]}", run_id=run_id, claim_id=claim_id, event_type=event_type,
        attack_type=error.attack_type, severity=severity,
        affected_entry_reference=f"{error.section}@{error.run_seq}" if error.section else None,
        verification_result=SecurityVerification(payload_integrity=error.payload_check, signature=error.signature_check),
        rejection_reason=reason, consumer=consumer, test_mode=os.environ.get(TEST_MODE_ENV) == "1")


def report_sha256(report: Any) -> str:
    return payload_hash(report.model_dump_json())


def context_block(report: BaseModel, ref: ContextRef) -> str:
    """A consumed report as it is given to a model: delimited as untrusted data, labelled with its blackboard
    version. The report quotes claim documents, so its text must never be treated as instructions."""
    return wrap_untrusted(report.model_dump_json(indent=2), f"blackboard:{ref.section}@{ref.run_seq}")


def context_facts(report: EvidenceReport, ref: ContextRef) -> str:
    """The Forensic Agent's view of a consumed Evidence report: what the documents state and what is missing, as
    untrusted data. The report's findings, confidence, recommendation and reasoning are left out, so the Forensic Agent
    reaches its own conclusions. The whole report was verified and receipted when it was consumed."""
    facts = [{"field": f.field, "value": f.value, "status": f.status, "source": f"{f.citation.doc_id}#L{f.citation.line}"}
             for f in report.facts]
    return wrap_untrusted(json.dumps({"facts": facts, "missing_information": report.missing_information},
                                     separators=(",", ":")), f"blackboard:{ref.section}@{ref.run_seq}")


class _Participant:
    principal = ""

    def __init__(self, run_id: str, claim_id: str, path: Path | None) -> None:
        self.run_id, self.claim_id = run_id, claim_id
        self.path = configured_path() if path is None else path
        self.requested = self.path is not None    # blackboard mode, even if the database later proves unusable
        self._board: Blackboard | None = None
        self._writer: Writer | None = None
        self.failures: list[str] = []
        self.security_events: list[SecurityEvent] = []    # reads this participant refused
        # Timed blackboard operations of this participant (publish / consume / verify / seal), for observability:
        # what, which entry, how long, and whether it succeeded. Never report content.
        self.operations: list[dict[str, Any]] = []

    def _timed(self, op: str, section: str, actor: str, action):
        started, t0 = utc_now(), time.perf_counter()
        result = action()
        entry = result[1] if isinstance(result, tuple) else result
        self.operations.append({"op": op, "section": section, "actor": actor, "started_at": started.isoformat(),
                                "latency_ms": round((time.perf_counter() - t0) * 1000, 3), "ok": result is not None,
                                "run_seq": getattr(entry, "run_seq", None),
                                "error": None if result is not None else self.last_failure[:300]})
        return result

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def board(self) -> Blackboard | None:
        if self._board is None and self.path is not None:
            try:
                self._board = Blackboard(self.path)
            except BlackboardError as exc:
                self.fail("open", str(exc))
                self.path = None              # one report is enough; the run continues without the blackboard
        return self._board

    def writer(self) -> Writer:
        if self._writer is None:
            self._writer = load_writer(self.path, self.principal)
        return self._writer

    def fail(self, where: str, error: str, security: SecurityEvent | None = None) -> None:
        self.failures.append(f"{where}: {error}")
        report_monitoring_failure(self.path, self.run_id, self.claim_id, where, error, security)

    @property
    def last_failure(self) -> str:
        return self.failures[-1] if self.failures else "blackboard not enabled"

    def refused(self, where: str, error: BlackboardSecurityError, consumer: str | None = None) -> None:
        """A security refusal: reported like any blackboard problem, with its structured event."""
        event = security_event(self.run_id, self.claim_id, error, consumer)
        self.security_events.append(event)
        self.fail(where, str(error), event)

    def guarded(self, where: str, action, consumer: str | None = None) -> Any:
        board = self.board()
        if board is None:
            return None
        try:
            result = action(board)
        except BlackboardSecurityError as exc:
            self.refused(where, exc, consumer)
            return None
        except (BlackboardError, BlackboardAuthError) as exc:
            self.fail(where, str(exc))
            return None
        except Exception as exc:              # never let monitoring break an investigation
            self.fail(where, f"{type(exc).__name__}: {exc}")
            return None
        if isinstance(result, BoardEntry) and result.late:
            self.fail(f"late {where}", f"entry {result.run_seq} ({result.section}) was written after the "
                                       "adjudicator's inputs were fixed; kept for traceability, not used")
        return result


class AgentParticipant(_Participant):
    """One framework's agents on the blackboard. A no-op when the blackboard is not configured."""

    def __init__(self, run_id: str, claim_id: str, framework: str, agents: tuple[str, ...],
                 path: Path | None = None) -> None:
        super().__init__(run_id, claim_id, path)
        self.framework, self.agents, self.principal = framework, agents, framework
        self.fingerprint: str | None = None
        self.published: dict[str, ContextRef] = {}        # reports published during execution
        self.consumed: list[ConsumedContext] = []          # shared context this side actually received

    def running(self, documents: list[EvidenceDocument], mark_running: bool = True) -> None:
        """Read the registration (same claim, same documents?). With mark_running, mark every agent RUNNING now;
        otherwise each agent is marked when it actually starts (agent_started, e.g. from an ADK plugin)."""
        if not self.enabled:
            return
        self.fingerprint = document_fingerprint(documents)
        registration = self.guarded("read registration", lambda b: b.registration(self.run_id))
        if registration is None:
            if self.board() is not None:
                self.fail("read registration", f"run {self.run_id} is not registered; this side's posts will be "
                                               "rejected")
            return
        if (registration["claim_id"], registration["fingerprint"]) != (self.claim_id, self.fingerprint):
            self.fail("read registration", f"registered for claim {registration['claim_id']} / documents "
                                            f"{registration['fingerprint'][:12]}..., this side loaded claim "
                                            f"{self.claim_id} / documents {self.fingerprint[:12]}...")
        if mark_running:
            for agent in self.agents:
                self.agent_started(agent)

    def agent_started(self, agent: str, **detail: Any) -> None:
        if self.enabled and self.fingerprint is not None and agent in self.agents:
            self.guarded(f"status {agent}", lambda b: b.set_status(
                self.run_id, self.claim_id, agent, "RUNNING", agent, self.framework, self.fingerprint,
                {"event": "agent_started", **detail}, writer=self.writer()))

    def agent_finished(self, agent: str, **detail: Any) -> None:
        """The agent's own execution ended. Its report is validated and published later, in finished()."""
        if self.enabled and self.fingerprint is not None and agent in self.agents:
            self.guarded(f"lifecycle {agent}", lambda b: b.monitor(
                self.run_id, self.claim_id, agent, self.framework, f"lifecycle:{agent}",
                {"event": "agent_finished", "report": "awaiting validation", **detail}, writer=self.writer()))

    def publish_report(self, agent: str, report: BaseModel, agent_run: dict[str, Any] | None = None) -> ContextRef | None:
        """Publish one agent's validated report during execution and mark the agent COMPLETED. Returns the
        ContextRef other agents use to consume it, or None (reason in last_failure) when it was not published.
        The caller validates the report's content (schema, citations, MCP references) before calling."""
        where = f"publish report:{agent}"
        if not self.enabled:
            if self.requested:
                self.fail(where, "the blackboard is not usable")
            return None
        if agent not in self.agents:
            self.fail(where, f"{agent} is not one of this {self.framework} participant's agents")
            return None
        if self.fingerprint is None:
            self.fail(where, "the run's registration and documents were not checked (running() was not called)")
            return None
        if getattr(report, "claim_id", None) != self.claim_id or type(report) is not SECTION_SCHEMAS[f"report:{agent}"]:
            self.fail(where, f"not a {SECTION_SCHEMAS[f'report:{agent}'].__name__} for claim {self.claim_id}")
            return None
        sha = report_sha256(report)
        posted = self._timed("publish", f"report:{agent}", agent, lambda: self.guarded(where, lambda b: b.post(
            self.run_id, self.claim_id, agent, self.framework, f"report:{agent}",
            {"report": report.model_dump(mode="json"), "report_sha256": sha, "agent_run": agent_run or {},
             "published": "during execution"}, self.fingerprint, writer=self.writer())))
        if posted is None or posted.auth != "valid" or posted.late:
            if posted is not None:
                self.fail(where, f"entry {posted.run_seq} is not usable as context (auth {posted.auth}, late {posted.late})")
            return None
        status = self.guarded(f"status {agent}", lambda b: b.set_status(
            self.run_id, self.claim_id, agent, "COMPLETED", agent, self.framework, self.fingerprint,
            {"event": "report_published", "report_sha256": sha, "report_run_seq": posted.run_seq,
             "agent_run": agent_run or {}}, writer=self.writer()))
        if status is None:
            return None
        ref = ContextRef(run_id=self.run_id, claim_id=self.claim_id, section=f"report:{agent}", run_seq=posted.run_seq,
                         sha256=posted.payload_sha256, fingerprint=self.fingerprint)
        self.published[agent] = ref
        return ref

    def consume_context(self, consumer: str, ref: ContextRef | dict[str, Any]) -> BaseModel | None:
        """Read a report as shared context through the blackboard (Blackboard.consume: permission, version, hash,
        signature, retraction, staleness and seal checked atomically, signed receipt recorded). Returns the verified,
        schema-validated report, or None (reason in last_failure): an unverifiable report is never returned."""
        where = f"consume context by {consumer}"
        if not self.enabled:
            self.fail(where, "the blackboard is not enabled or not usable; shared context cannot be consumed")
            return None
        try:
            ref = ContextRef.model_validate(ref.model_dump() if isinstance(ref, BaseModel) else ref)
        except ValidationError as exc:
            self.fail(where, f"invalid context reference: {str(exc).splitlines()[0]}")
            return None
        where = f"consume {ref.section}@{ref.run_seq} by {consumer}"
        problem, attack = None, None
        if consumer not in self.agents:
            problem, attack = f"{consumer} is not one of this {self.framework} participant's agents", "UNAUTHORISED_ACCESS"
        elif (ref.run_id, ref.claim_id) != (self.run_id, self.claim_id):
            problem = f"the reference belongs to run {ref.run_id} / claim {ref.claim_id}, not {self.run_id} / {self.claim_id}"
            attack = "REPLAY" if ref.claim_id == self.claim_id else "CROSS_CLAIM_SUBSTITUTION"
        elif self.fingerprint is None or ref.fingerprint != self.fingerprint:
            problem = "the reference was made for other claim documents than the ones this side loaded"
            attack = None if self.fingerprint is None else "CROSS_CLAIM_SUBSTITUTION"
        elif consumer not in CONTEXT_READERS.get(ref.section, frozenset()):
            problem, attack = f"{consumer} is not authorised to read {ref.section}", "UNAUTHORISED_ACCESS"
        if attack:
            self.refused(where, BlackboardSecurityError(problem, attack, ref.section, ref.run_seq), consumer)
            return None
        if problem:
            self.fail(where, problem)
            return None
        schema, holder = SECTION_SCHEMAS[ref.section], {}

        def validate(entry: BoardEntry) -> None:
            report = schema.model_validate(entry.payload.get("report"))
            if report.claim_id != self.claim_id:
                raise BlackboardSecurityError(f"report claim_id {report.claim_id} != {self.claim_id}",
                                              "CROSS_CLAIM_SUBSTITUTION")
            if report_sha256(report) != entry.payload.get("report_sha256"):
                raise ValueError("the report does not match its recorded report_sha256")
            holder["report"] = report

        got = self._timed("consume", ref.section, consumer, lambda: self.guarded(where, lambda b: b.consume(
            ref.run_id, ref.claim_id, ref.fingerprint, consumer, ref.section, ref.run_seq, ref.sha256,
            writer=self.writer(), framework=self.framework, validate=validate), consumer))
        if got is None:
            return None
        self.consumed.append(ConsumedContext(consumer=consumer, ref=ref, receipt_run_seq=got[1].run_seq))
        return holder["report"]

    def finished(self, result: AgentSystemResult) -> None:
        """Publish each agent's validated report and its final status, exactly as the result says. An agent whose
        report was already published during execution (publish_report) keeps that report and its COMPLETED status:
        nothing is posted twice, and its real history stays as it happened even if a later agent failed."""
        if not self.enabled:
            return
        runs = {r.agent: r for r in result.agent_runs}
        for agent in self.agents:
            if agent in self.published:
                continue
            report = result.evidence_report if agent == "evidence_agent" else result.report
            run = runs.get(agent)
            timing = {"started_at": run.started_at.isoformat() if run and run.started_at else None,
                      "finished_at": run.finished_at.isoformat() if run and run.finished_at else None,
                      "latency_ms": run.latency_ms if run else None}
            if result.status == ExecutionStatus.COMPLETED and report is not None and self.fingerprint is not None:
                sha = report_sha256(report)
                posted = self.guarded(f"post report:{agent}", lambda b, a=agent, r=report, s=sha: b.post(
                    self.run_id, self.claim_id, a, self.framework, f"report:{a}",
                    {"report": r.model_dump(mode="json"), "report_sha256": s, "agent_run": timing}, self.fingerprint,
                    writer=self.writer()))
                if posted is not None:
                    self.guarded(f"status {agent}", lambda b, a=agent, s=sha: b.set_status(
                        self.run_id, self.claim_id, a, "COMPLETED", a, self.framework, self.fingerprint,
                        {"report_sha256": s, "agent_run": timing}, writer=self.writer()))
                continue
            state = result.status.value if result.status.value in TERMINAL - {"COMPLETED"} else "FAILED"
            self.guarded(f"status {agent}", lambda b, a=agent, st=state: b.set_status(
                self.run_id, self.claim_id, a, st, a, self.framework, self.fingerprint,
                {"errors": result.errors[:5], "agent_run": timing}, writer=self.writer()))


class RunController(_Participant):
    """The orchestrator's view of the blackboard for one run. A no-op when the blackboard is not configured."""
    principal = "orchestrator"

    def __init__(self, run_id: str, claim_id: str, path: Path | None = None) -> None:
        super().__init__(run_id, claim_id, path)
        self.fingerprint: str | None = None
        self.summary: dict[str, Any] | None = None

    def register(self, documents: list[EvidenceDocument]) -> None:
        if not self.enabled:
            return
        self.fingerprint = document_fingerprint(documents)
        try:
            provision(self.path)              # database and writer keys (existing keys are kept)
        except (BlackboardError, OSError) as exc:
            self.fail("provision", str(exc))
            self.path = None
            return
        self.guarded("register", lambda b: b.register(self.run_id, self.claim_id, self.fingerprint,
                                                      writer=self.writer()))

    def collect(self, adk: AgentSystemResult, crewai: AgentSystemResult, a2a_errors: list[str]) -> dict[str, Any] | None:
        """Seal the run (the adjudicator's inputs are now fixed), then track completion and failures and reconcile
        every published report with the report the adjudicator actually received. Agent entries written after the
        seal stay on the blackboard as "late" and are never counted as results."""
        if not self.enabled:
            return None
        seal = self._timed("seal", SEAL_SECTION, "controller", lambda: self.guarded("seal", lambda b: b.monitor(
            self.run_id, self.claim_id, "controller", "orchestrator", SEAL_SECTION,
            {"reason": "adjudicator inputs fixed", "adk_status": adk.status.value, "crewai_status": crewai.status.value},
            writer=self.writer())))
        state = self.guarded("collect", lambda b: b.state(self.run_id, self.claim_id))
        if seal is None or state is None:
            return None
        received = {"evidence_agent": (adk.status.value, adk.evidence_report),
                    "investigator_agent": (adk.status.value, adk.report),
                    "forensic_agent": (crewai.status.value, crewai.report)}
        posts = {p.section.split(":", 1)[1]: p for p in state.live_posts if p.section.startswith("report:")}
        rows, findings = [], []
        for agent, framework in EXPECTED_AGENTS.items():
            status = state.agents.get(agent)
            board_state = (status.state_at_seal or status.state) if status else None
            got_status, report = received[agent]
            post = posts.get(agent)
            sha = report_sha256(report) if report is not None else None
            if board_state not in TERMINAL:
                findings.append(f"{agent}: no final status on the blackboard (state {board_state}); the {framework} "
                                "process did not finish or could not publish")
            if post is not None and report is None:
                reason = f"not received by the adjudicator (received status {got_status}" + (
                    f"; A2A: {a2a_errors[0][:200]}" if agent == "forensic_agent" and a2a_errors else "") + ")"
                self.guarded(f"retract report:{agent}", lambda b, p=post, r=reason: b.retract(
                    self.run_id, self.claim_id, p.run_seq, "controller", "orchestrator", r, writer=self.writer()))
                findings.append(f"{agent}: published report retracted - {reason}")
            elif post is not None and post.payload.get("report_sha256") != sha:
                reason = "differs from the report the adjudicator received"
                self.guarded(f"retract report:{agent}", lambda b, p=post: b.retract(
                    self.run_id, self.claim_id, p.run_seq, "controller", "orchestrator", reason, writer=self.writer()))
                findings.append(f"{agent}: published report retracted - {reason}")
            elif post is None and report is not None:
                findings.append(f"{agent}: the adjudicator received a report this agent did not publish on the "
                                "blackboard")
            rows.append({"agent": agent, "framework": framework, "blackboard_state": board_state,
                         "received_status": got_status, "published": post is not None,
                         "received_report_sha256": sha,
                         "matches_received": post is not None and post.payload.get("report_sha256") == sha})
        unauthenticated = {k: v for k, v in state.authentication.items() if k != "valid"}
        if unauthenticated:
            findings.append(f"entries that failed authentication: {unauthenticated}")
        failures = read_monitoring_failures(self.path, self.run_id) if self.path else []
        self.summary = {"sealed_at": seal.run_seq, "agents": rows, "findings": findings,
                        "completed": [r["agent"] for r in rows if r["blackboard_state"] == "COMPLETED"],
                        "failed": [r["agent"] for r in rows if r["blackboard_state"] in ("FAILED", "NOT_CONFIGURED")],
                        "unfinished": [r["agent"] for r in rows if r["blackboard_state"] not in TERMINAL],
                        "authentication": state.authentication, "monitoring_failures": failures}
        self.guarded("monitor completion", lambda b: b.monitor(self.run_id, self.claim_id, "controller", "orchestrator",
                                                              "controller:completion", self.summary,
                                                              writer=self.writer()))
        return self.summary

    def record_manager(self, name: str, payload: dict[str, Any]) -> None:
        """The orchestration manager's plan and task lifecycle, as signed monitor entries `manager:<name>`
        (`manager:plan`, `manager:task:<task_id>`, `manager:failure`). Identifiers, states and references only -
        never document content."""
        if self.enabled:
            section = f"manager:{name}"
            self.guarded(f"record {section}", lambda b: b.monitor(
                self.run_id, self.claim_id, "controller", "manager", section, payload, writer=self.writer()))

    # Which reports of each system the adjudicator needs: agent -> (AgentSystemResult field, blackboard section).
    _INPUTS = {"GOOGLE_ADK": {"evidence_agent": "evidence_report", "investigator_agent": "report"},
               "CREWAI": {"forensic_agent": "report"}}

    def verified_inputs(self, adk: AgentSystemResult, crewai: AgentSystemResult
                        ) -> tuple[AgentSystemResult, AgentSystemResult, dict[str, dict[str, Any]]]:
        """The adjudicator's inputs, verified against the sealed blackboard (call after collect()).

        For every report a completed system delivered, the adjudicator consumes the live blackboard post of that
        agent (signature, version, hash, not retracted, not late, producer COMPLETED - Blackboard.consume, receipt
        recorded) and the post must hold exactly the delivered report. The returned results carry the blackboard
        copies. Fail closed: if any report of a system cannot be verified - or the blackboard is unusable - that
        system is handed to the adjudicator as FAILED without reports, with the reason in its errors, so its review
        is unavailable (PARTIAL / NONE review, human approval). The results as executed are not changed."""
        checks: dict[str, dict[str, Any]] = {}
        state = self.guarded("verify inputs", lambda b: b.state(self.run_id, self.claim_id)) if self.enabled else None
        out = []
        for result in (adk, crewai):
            wanted = self._INPUTS[result.system]
            if result.status != ExecutionStatus.COMPLETED:
                checks.update({a: {"verified": False, "reason": f"{result.system} status {result.status.value}"}
                               for a in wanted})
                out.append(result)
                continue
            copies, problems = {}, []
            for agent, field in wanted.items():
                received = getattr(result, field)
                section = f"report:{agent}"
                post = None if state is None else next(
                    (p for p in reversed(state.live_posts) if p.section == section), None)
                if received is None:
                    problems.append(f"{agent}: no report delivered")
                elif state is None:
                    problems.append(f"{agent}: the blackboard could not be read ({self.last_failure})")
                elif post is None:
                    problems.append(f"{agent}: no live report on the blackboard")
                else:
                    copy = self._consume_verified(post, received)
                    if copy is None:
                        problems.append(f"{agent}: {self.last_failure}")
                    else:
                        copies[field] = copy
                        checks[agent] = {"verified": True, "run_seq": post.run_seq,
                                         "report_sha256": report_sha256(copy)}
                        continue
                checks[agent] = {"verified": False, "reason": problems[-1]}
            if problems:
                for agent in wanted:
                    if checks[agent]["verified"]:
                        checks[agent] = {**checks[agent], "verified": False,
                                         "reason": "not used: another report of the same system failed verification"}
                out.append(result.model_copy(update={
                    "status": ExecutionStatus.FAILED, "report": None, "evidence_report": None,
                    "errors": list(result.errors) + [f"blackboard verification failed - {p}" for p in problems]}))
            else:
                out.append(result.model_copy(update=copies))
        if state is not None and state.unverified:
            out = self._withhold_all(state.unverified, out, checks)
        if self.enabled:
            self.guarded("monitor verification", lambda b: b.monitor(
                self.run_id, self.claim_id, "adjudicator", "deterministic", "controller:verification",
                {"event": "adjudicator_inputs_verified", "agents": checks}, writer=self.writer()))
        return out[0], out[1], checks

    def _withhold_all(self, unverified: list[str], results: list[AgentSystemResult],
                      checks: dict[str, dict[str, Any]]) -> list[AgentSystemResult]:
        """An entry of this run failed verification, so nothing the run produced is trusted: the adjudicator gets no
        agent review, whichever entry it was and whenever it was altered (an agent may have read a report before that).
        Entries not already reported as a refused read are reported here. What ran and what is stored do not change."""
        reported = {e.affected_entry_reference for e in self.security_events}
        for reference in unverified:
            if reference not in reported:
                run_seq = int(reference.rpartition("@")[2])
                error = self.guarded("verify inputs", lambda b: b.unverified_error(self.run_id, run_seq))
                if error is not None:
                    self.refused("verify inputs", error)
        reason = f"not used: {len(unverified)} entry(ies) of this run failed integrity verification"
        for agent, check in list(checks.items()):
            if check["verified"]:
                checks[agent] = {**check, "verified": False, "reason": reason}
        return [r if r.status != ExecutionStatus.COMPLETED else r.model_copy(update={
            "status": ExecutionStatus.FAILED, "report": None, "evidence_report": None,
            "errors": list(r.errors) + [f"blackboard verification failed - {reason}"]}) for r in results]

    def _consume_verified(self, post: BoardEntry, received: BaseModel) -> BaseModel | None:
        schema, holder, expected = SECTION_SCHEMAS[post.section], {}, report_sha256(received)

        def validate(entry: BoardEntry) -> None:
            report = schema.model_validate(entry.payload.get("report"))
            if report_sha256(report) != entry.payload.get("report_sha256") or report_sha256(report) != expected:
                raise BlackboardSecurityError("the blackboard report differs from the report that was delivered",
                                              "PAYLOAD_TAMPERING")
            holder["report"] = report

        got = self._timed("verify", post.section, "adjudicator", lambda: self.guarded(f"verify {post.section}", lambda b: b.consume(
            self.run_id, self.claim_id, self.fingerprint or "", "adjudicator", post.section, post.run_seq,
            post.payload_sha256, writer=self.writer(), framework="deterministic", validate=validate), "adjudicator"))
        return holder.get("report") if got is not None else None

    def publish_decision(self, final: FinalClaimReport) -> None:
        """Record the deterministic adjudicator's decision (the adjudicator itself is not involved)."""
        if not self.enabled or self.fingerprint is None:
            return
        payload = {"final_decision": final.final_decision.value, "decided_by": final.decided_by,
                   "deterministic_decision": final.deterministic_decision.value if final.deterministic_decision else None,
                   "requires_human_approval": final.requires_human_approval, "approval_status": final.approval_status,
                   "review_status": getattr(final, "review_status", None),
                   "payout": str(final.payout.payout) if final.payout else None,
                   "currency": final.payout.currency if final.payout else None,
                   "final_report_sha256": report_sha256(final)}
        self.guarded("publish decision", lambda b: b.post(self.run_id, self.claim_id, "adjudicator", "deterministic",
                                                          "decision:final", payload, self.fingerprint,
                                                          writer=self.writer()))
