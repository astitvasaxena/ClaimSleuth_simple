"""How the investigation uses the shared coordination blackboard.

AgentParticipant  used by one framework's runner (ADK in the main process, CrewAI in the A2A service process):
                  reads the run's registration, then publishes its own agents' lifecycle, status and validated
                  reports, authenticated as the "adk" or "crewai" writer.
RunController     used by the orchestrator ("orchestrator" writer): provisions the writer keys, registers the run,
                  seals it when the adjudicator's inputs are fixed, tracks completion and failures, reconciles what
                  was published with what the adjudicator received, and publishes the adjudicator's decision.

The agents stay independent: no agent reads another agent's findings from the blackboard.
Nothing here can change an investigation result. Every blackboard problem is caught and reported explicitly as a
"Blackboard monitoring failure" on stderr and in <database>.monitoring.jsonl, which the controller then records.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from claimsleuth.blackboard.auth import BlackboardAuthError, Writer, load_writer
from claimsleuth.blackboard.models import EXPECTED_AGENTS, TERMINAL, BoardEntry, document_fingerprint, payload_hash
from claimsleuth.blackboard.store import SEAL_SECTION, Blackboard, BlackboardError, configured_path, provision
from claimsleuth.schemas import (AgentSystemResult, EvidenceDocument, ExecutionStatus, FinalClaimReport,
                                 utc_now)


def monitoring_log(path: Path) -> Path:
    return Path(f"{path}.monitoring.jsonl")


def report_monitoring_failure(path: Path | None, run_id: str, claim_id: str, where: str, error: str) -> None:
    """Make a blackboard problem visible without touching the investigation: stderr plus the monitoring log."""
    record = {"ts": utc_now().isoformat(), "run_id": run_id, "claim_id": claim_id, "where": where,
              "error": error[:500], "pid": os.getpid(), "environment": Path(sys.prefix).name}
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


def report_sha256(report: Any) -> str:
    return payload_hash(report.model_dump_json())


class _Participant:
    principal = ""

    def __init__(self, run_id: str, claim_id: str, path: Path | None) -> None:
        self.run_id, self.claim_id = run_id, claim_id
        self.path = configured_path() if path is None else path
        self._board: Blackboard | None = None
        self._writer: Writer | None = None
        self.failures: list[str] = []

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

    def fail(self, where: str, error: str) -> None:
        self.failures.append(f"{where}: {error}")
        report_monitoring_failure(self.path, self.run_id, self.claim_id, where, error)

    def guarded(self, where: str, action) -> Any:
        board = self.board()
        if board is None:
            return None
        try:
            result = action(board)
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

    def finished(self, result: AgentSystemResult) -> None:
        """Publish each agent's validated report and its final status, exactly as the result says."""
        if not self.enabled:
            return
        runs = {r.agent: r for r in result.agent_runs}
        for agent in self.agents:
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
        seal = self.guarded("seal", lambda b: b.monitor(
            self.run_id, self.claim_id, "controller", "orchestrator", SEAL_SECTION,
            {"reason": "adjudicator inputs fixed", "adk_status": adk.status.value, "crewai_status": crewai.status.value},
            writer=self.writer()))
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

    def publish_decision(self, final: FinalClaimReport) -> None:
        """Record the deterministic adjudicator's decision (the adjudicator itself is not involved)."""
        if not self.enabled or self.fingerprint is None:
            return
        payload = {"final_decision": final.final_decision.value, "decided_by": final.decided_by,
                   "deterministic_decision": final.deterministic_decision.value if final.deterministic_decision else None,
                   "requires_human_approval": final.requires_human_approval, "approval_status": final.approval_status,
                   "payout": str(final.payout.payout) if final.payout else None,
                   "currency": final.payout.currency if final.payout else None,
                   "final_report_sha256": report_sha256(final)}
        self.guarded("publish decision", lambda b: b.post(self.run_id, self.claim_id, "adjudicator", "deterministic",
                                                          "decision:final", payload, self.fingerprint,
                                                          writer=self.writer()))
