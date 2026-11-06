"""Hierarchical orchestration: the deterministic investigation manager (Level 1) over the specialist agents (Level 2).

    Level 1  InvestigationManager (deterministic code, not an LLM)
               intake -> typed ExecutionPlan -> assigns, releases and dispatches tasks -> monitors them
               -> collects and verifies their reports -> seals the blackboard -> hands verified reports to the
               deterministic adjudicator -> escalates an incomplete investigation to a human
    Level 2  Evidence Agent (Google ADK)       extracts the facts; its report is published to the blackboard
             Investigator Agent (Google ADK)   policy and history; released by the manager once the Evidence
                                               report is published, reads it from the blackboard
             Forensic Agent (CrewAI, over A2A) forensics; dispatched by the manager once the Evidence report is
                                               published, reads it from the blackboard with its own key
             (the deterministic baseline and the adjudicator are plan tasks too; neither is an agent)

The manager never extracts evidence, judges policy or does forensics itself, and never decides the claim: the
adjudicator (integration/adjudicator.py, unchanged) remains the only decision authority.

Patterns: sequential (Evidence -> Investigator inside one ADK SequentialAgent), parallel (Investigator and Forensic
concurrently once the Evidence report is published) and hierarchical (this manager assigns, gates, monitors and
collects both).

Monitoring: every task state change and event is kept in the plan (returned in the bundle) and, in blackboard mode,
written as a signed `manager:task:<task_id>` monitor entry (the plan itself: `manager:plan`). Events come from the
real execution: the ADK plugin's callbacks (agent started / finished, context consumed, the Investigator's release),
the Evidence publication, the A2A dispatch and response, the agents' recorded tool calls and receipts, the seal and
the blackboard verification.

Failure handling (fail closed): a task that cannot run is BLOCKED, a failure is FAILED, a passed deadline is
TIMED_OUT, and nothing becomes COMPLETED without a validated (and, in blackboard mode, blackboard-verified) report.
If the manager itself fails, it records the failure and raises ManagerError: no decision is produced or recorded.

Security events: every read of shared context that any participant refused (the Investigator, the Forensic Agent, the
adjudicator's verification) is collected into the bundle with the escalation that followed, audited, and noted on the
adjudication task. They report what happened; the reports were already withheld where the read was refused.
Instructions found in the input and agent output refused for restricted content are collected the same way, apart from
them (injection_events): an escalated injection is not a refused read, and nothing was withheld because of it.
"""
from __future__ import annotations

import asyncio
from importlib.metadata import version
from pathlib import Path
from typing import Any

from claimsleuth import config, execution_mode, gemini_rate
from claimsleuth.adk_service import config as adk_config
from claimsleuth.adk_service.forensic_a2a import ForensicExchange, a2a_timeout, not_sent, request_forensics
from claimsleuth.adk_service.runner import run_adk_investigation_async
from claimsleuth.baseline import new_run_id, run_baseline
from claimsleuth.blackboard import Blackboard, ContextRef, RunController, document_fingerprint
from claimsleuth.datastore import load_claim, load_policies
from claimsleuth.documents import extract_fields
from claimsleuth.history import lookup_history
from claimsleuth.integration import run as integration
from claimsleuth.integration.adjudicator import adjudicate
from claimsleuth.integration.mcp_records import fetch_mcp_records
from claimsleuth.observability.provider_errors import AGENT_TASK, classify
from claimsleuth.orchestration import plan as P
from claimsleuth.rules import normalise_serial
from claimsleuth.security import injection_events
from claimsleuth.schemas import (MCP_LOG_EMPTY, AgentSystemResult, ClaimRunBundle, DocumentKind, ExecutionMetadata, ExecutionPlan,
                                 ExecutionStatus, MCPResult, SecurityEvent, TaskStatus, WorkflowMessage, utc_now)

S = TaskStatus
DEADLINE_MARGIN_S = 60.0          # the manager's own deadline: each component's timeout plus this margin
MCP_RECORDS_DEADLINE_S = 30.0     # the MCP server's records, read after the agents finish


class ManagerError(RuntimeError):
    """The manager could not complete the run. Nothing was adjudicated or recorded as a decision."""


def reconcile_records(mcp: MCPResult, *results: AgentSystemResult) -> MCPResult:
    """The server reported no fault, yet its log holds none of the MCP calls the agents recorded: the two accounts contradict each
    other. The records then count as unavailable, and the adjudicator's own check sends the run to a human. A run whose agents
    recorded no MCP call has nothing to contradict its empty log, and is left as it is."""
    recorded = any(c.protocol == "mcp" for result in results for c in result.tool_calls)
    if mcp.status != ExecutionStatus.COMPLETED or mcp.calls or not recorded:
        return mcp
    return mcp.model_copy(update={"status": ExecutionStatus.FAILED, "errors": [*mcp.errors, MCP_LOG_EMPTY]})


class InvestigationManager:
    def __init__(self, claim_id: str, *, run_id: str | None = None, mcp_url: str, crew_card_url: str,
                 data_dir: Path = config.DATA_DIR, db_path: Path = config.DB_PATH, adk_llm_mode: str | None = None,
                 a2a_timeout: float | None = None, progress: integration.ProgressCallback | None = None,
                 blackboard_path: Path | None = None, deadlines: dict[str, float] | None = None) -> None:
        """deadlines: the manager's own limits in seconds for the delegated "adk" workflow and "forensics" task
        (default: the component's own timeout plus DEADLINE_MARGIN_S); a passed deadline cancels the task and
        marks it TIMED_OUT."""
        self.claim_id, self.run_id, self.mcp_url, self.crew_card_url = claim_id, run_id, mcp_url, crew_card_url
        self.data_dir, self.db_path, self.adk_llm_mode, self.a2a_timeout = data_dir, db_path, adk_llm_mode, a2a_timeout
        self.progress, self.blackboard_path, self.deadlines = progress, blackboard_path, dict(deadlines or {})
        self.plan: ExecutionPlan | None = None
        self.board: RunController | None = None
        self._timed_out: set[str] = set()
        self.monitoring_errors: list[str] = []       # monitoring problems: recorded, never acted on
        self.security_events: list[SecurityEvent] = []
        self.injection_events: list[SecurityEvent] = []

    # ------------------------------------------------------------------ plan state and monitoring
    def _set(self, task_id: str, status: TaskStatus, event: str, detail: str = "", ref: ContextRef | None = None,
             at=None, **result: Any) -> None:
        ev = P.transition(self.plan, task_id, status, event, detail, ref, at)
        if result:
            P.task(self.plan, task_id).result.update(result)
        self._emit(task_id, ev)

    def _note(self, task_id: str, event: str, detail: str = "", ref: ContextRef | None = None, at=None) -> None:
        self._emit(task_id, P.record(self.plan, task_id, event, detail, ref, at))

    def _emit(self, task_id: str, ev) -> None:
        t = P.task(self.plan, task_id)
        self._record(f"task:{task_id}", {
            "run_id": self.plan.run_id, "task_id": task_id, "agent": t.agent, "event": ev.event,
            "status": ev.status.value, "ts": ev.ts.isoformat(), "detail": ev.detail,
            "ref": ev.ref.model_dump() if ev.ref else None})

    def _record(self, name: str, payload: dict[str, Any]) -> None:
        """Monitoring only: a failure to record is noted and never changes a task, a report or the run."""
        try:
            self.board.record_manager(name, payload)
        except Exception as exc:
            self.monitoring_errors.append(f"monitoring: manager:{name} ({payload.get('event', 'plan')}) not recorded "
                                          f"on the blackboard: {type(exc).__name__}: {str(exc)[:200]}")

    def status(self, task_id: str) -> TaskStatus:
        return P.task(self.plan, task_id).status

    def _block(self, task_id: str, fallback: str) -> None:
        if self.status(task_id) in (S.PENDING, S.READY):
            self._set(task_id, S.BLOCKED, "blocked", P.blocking_reason(self.plan, task_id) or fallback)

    def _end_unfinished(self, task_id: str, reason: str) -> None:
        """A task that cannot finish normally: RUNNING -> TIMED_OUT / FAILED, READY -> FAILED, PENDING -> BLOCKED."""
        st = self.status(task_id)
        if st == S.RUNNING:
            self._set(task_id, S.TIMED_OUT if task_id in self._timed_out else S.FAILED,
                      "timed_out" if task_id in self._timed_out else "failed", reason)
        elif st == S.READY:
            self._set(task_id, S.FAILED, "not_started", reason)
        elif st == S.PENDING:
            self._block(task_id, reason)

    def deadline(self, name: str, default: float) -> float:
        return self.deadlines.get(name, default + DEADLINE_MARGIN_S)

    # ------------------------------------------------------------------ callbacks from the ADK run (blackboard mode)
    def on_agent(self, agent: str, event: str, data: dict[str, Any]) -> None:
        task_id = AGENT_TASK[agent]
        if event == "agent_started" and task_id == "evidence" and self.status("evidence") == S.READY:
            self._set("evidence", S.RUNNING, "agent_started", "ADK Evidence Agent started")
        elif event == "timed_out":
            self._timed_out.add(task_id)
        elif event == "context_consumed":
            ref = data["ref"]
            self._note(task_id, "context_consumed", f"read {ref.section}@{ref.run_seq} from the blackboard "
                       f"(receipt {data['receipt_run_seq']})", ref=ref)
        else:
            self._note(task_id, event)

    def authorise(self, agent: str) -> str | None:
        """The Investigator may start only when the manager releases it: its task READY, the Evidence task
        COMPLETED (report published). Returns None when released, else the reason (the task is then BLOCKED)."""
        if agent != "investigator_agent":
            return f"{agent} is not released by the manager"
        if self.status("investigation") == S.READY and P.dependencies_met(self.plan, "investigation"):
            self._set("investigation", S.RUNNING, "dispatched", "the manager released the Investigator: the "
                      "Evidence report is published")
            return None
        reason = P.blocking_reason(self.plan, "investigation") or f"task investigation is {self.status('investigation').value}"
        self._block("investigation", reason)
        return reason

    def on_evidence(self, ref: ContextRef | None, error: str | None) -> None:
        if ref is not None and self.status("evidence") in (S.READY, S.RUNNING):
            if self.status("evidence") == S.READY:
                self._set("evidence", S.RUNNING, "agent_started")
            self._set("evidence", S.COMPLETED, "report_published", f"validated EvidenceReport published as "
                      f"{ref.section}@{ref.run_seq}", ref=ref)
            for task_id in ("investigation", "forensics"):
                self._set(task_id, S.READY, "dependencies_met", f"Evidence report {ref.section}@{ref.run_seq} published")
        elif ref is None:
            self._end_unfinished("evidence", error or "the Evidence report was not published")
            for task_id in ("investigation", "forensics"):
                self._block(task_id, "the Evidence report was not published")

    # ------------------------------------------------------------------ the run
    async def run(self) -> ClaimRunBundle:
        live = execution_mode.execution_mode() == execution_mode.LIVE
        self.gemini_model = integration.live_preflight(self.crew_card_url) if live else None
        if live and self.a2a_timeout is None:
            self.a2a_timeout = integration.LIVE_A2A_TIMEOUT
        if self.adk_llm_mode is None and not live:
            self.adk_llm_mode = "test-double"
        self.live = live
        run_id = self.run_id = self.run_id or new_run_id()
        self.audit = integration.Audit(run_id, self.claim_id)
        self.board = RunController(run_id, self.claim_id, path=self.blackboard_path)  # a no-op unless enabled
        self.shared = self.board.requested                                             # blackboard mode
        self.audit("run_started", "orchestrator", f"integrated run for {self.claim_id}", mcp_url=self.mcp_url,
                   crew_card=self.crew_card_url, execution_mode="live" if live else "offline",
                   gemini_model=self.gemini_model, context_sharing="blackboard" if self.shared else "direct",
                   orchestration="hierarchical manager")
        # Stage 1 - intake: a valid claim and its documents, the run registered, the plan created.
        self.claim, self.documents = load_claim(self.claim_id, self.data_dir)
        self.policy = load_policies(self.data_dir).get(self.claim.policy_id)
        self.board.register(self.documents)
        self.plan = P.build_plan(run_id, self.claim_id, document_fingerprint(self.documents), self.shared)
        self._record("plan", {
            "run_id": run_id, "context_sharing": self.plan.context_sharing, "orchestration": "hierarchical manager",
            "tasks": [{"task_id": t.task_id, "agent": t.agent, "framework": t.framework, "channel": t.channel,
                       "depends_on": [d.model_dump() for d in t.depends_on]} for t in self.plan.tasks]})
        try:
            return await self._execute()
        except Exception as exc:          # the manager itself failed: record it, never produce a decision
            reason = f"manager failure: {type(exc).__name__}: {str(exc)[:300]}"
            for t in self.plan.tasks:
                if t.status not in P.TERMINAL:
                    self._end_unfinished(t.task_id, reason)
            self._record("failure", {"run_id": run_id, "event": "manager_failed", "detail": reason,
                                     "decision": "none produced"})
            raise ManagerError(f"run {run_id}: {reason}; no decision was produced") from exc

    async def _execute(self) -> ClaimRunBundle:
        audit, claim, documents, run_id = self.audit, self.claim, self.documents, self.run_id

        # Baseline (deterministic, in-process).
        self._set("baseline", S.READY, "dependencies_met")
        self._set("baseline", S.RUNNING, "dispatched", "deterministic baseline")
        audit("workflow_started", "baseline", "deterministic baseline")
        baseline = run_baseline(self.claim_id, run_id=run_id, data_dir=self.data_dir, db_path=self.db_path)
        audit("workflow_finished", "baseline", f"{baseline.status.value} {baseline.decision.value if baseline.decision else ''}",
              execution_ms=baseline.execution_ms)
        self._set("baseline", S.COMPLETED if baseline.status == ExecutionStatus.COMPLETED else S.FAILED, "finished",
                  baseline.status.value, decision=baseline.decision.value if baseline.decision else None)
        integration._notify(self.progress, "baseline_finished", status=baseline.status)

        # Stage 2 - the Evidence task (ADK workflow; the Investigator inside it is released by the manager).
        self._set("evidence", S.READY, "dependencies_met")
        audit("workflow_started", "adk", f"Evidence + Investigator agents (llm_mode={self.adk_llm_mode or 'gemini'})")
        evidence_ready = asyncio.Event()

        def on_evidence(ref, error) -> None:
            self.on_evidence(ref, error)
            evidence_ready.set()

        if not self.shared:
            self._set("evidence", S.RUNNING, "dispatched", "ADK workflow dispatched (direct mode)")
        adk_deadline = self.deadline("adk", adk_config.LIVE_RUN_TIMEOUT_SECONDS if self.live
                                     else adk_config.RUN_TIMEOUT_SECONDS)
        started = utc_now()
        recorders: list[Any] = []

        async def adk_workflow() -> AgentSystemResult:
            work = run_adk_investigation_async(
                self.claim_id, run_id=run_id, mcp_url=self.mcp_url, data_dir=self.data_dir, llm_mode=self.adk_llm_mode,
                blackboard_path=self.blackboard_path, on_evidence=on_evidence if self.shared else None,
                authorise=self.authorise if self.shared else None, on_agent=self.on_agent if self.shared else None,
                on_recorder=recorders.append)
            try:
                result = await asyncio.wait_for(work, adk_deadline)
                if any("timed_out_after_s" in t for t in result.trace):      # the ADK run's own timeout
                    self._timed_out.update({"evidence", "investigation"})
                return result
            except TimeoutError:
                self._timed_out.update({"evidence", "investigation"})
                record = recorders[0] if recorders else None       # what the cancelled run measured before it ended
                return AgentSystemResult(system="GOOGLE_ADK", run_id=run_id, claim_id=self.claim_id,
                                         status=ExecutionStatus.FAILED, started_at=started, finished_at=utc_now(),
                                         model=record.model if record else None,
                                         agent_runs=list(record.agent_runs.values()) if record else [],
                                         tool_calls=record.tool_calls if record else [],
                                         trace=record.trace_records() if record else [],
                                         provider_failures=list(record.failures) if record else [],
                                         provider_retries=list(record.retries) if record else [],
                                         errors=[f"the manager's deadline of {adk_deadline:g} s for the ADK workflow "
                                                 "passed; the workflow was cancelled"])

        adk_coro = adk_workflow()
        adk_task = asyncio.ensure_future(integration._observed(adk_coro, self.progress, "adk_finished")
                                         if self.progress else adk_coro)

        # Stage 3 - parallel delegation: the Forensic task over A2A, once its dependency is met.
        async def forensic() -> ForensicExchange:
            context: list[ContextRef] = []
            if self.shared:
                ready = asyncio.ensure_future(evidence_ready.wait())
                await asyncio.wait({ready, adk_task}, return_when=asyncio.FIRST_COMPLETED)   # published, or ADK ended
                ready.cancel()
                if self.status("evidence") in (S.READY, S.RUNNING):      # the ADK workflow ended without publishing
                    self._end_unfinished("evidence", "the ADK workflow ended before the Evidence report was published")
                    self._block("investigation", "the Evidence report was not published")
                    self._block("forensics", "the Evidence report was not published")
                if self.status("forensics") != S.READY:
                    reason = P.blocking_reason(self.plan, "forensics") or f"task forensics is {self.status('forensics').value}"
                    self._block("forensics", reason)
                    audit("blackboard_context", "orchestrator", f"FORENSIC_REQUEST not sent: {reason}")
                    return not_sent(claim, documents, run_id, f"no verified Evidence report on the blackboard ({reason})")
                ref = P.task(self.plan, "evidence").result_ref
                context = [ref]
                audit("blackboard_context", "evidence_agent", f"Evidence report published as {ref.section}@{ref.run_seq}",
                      ref=ref.model_dump())
                audit("workflow_started", "crewai", f"forensic request over A2A via ADK RemoteA2aAgent, with ContextRef "
                                                    f"{ref.section}@{ref.run_seq}")
                self._set("forensics", S.RUNNING, "dispatched", f"FORENSIC_REQUEST over A2A with ContextRef "
                          f"{ref.section}@{ref.run_seq}", ref=ref)
            else:
                audit("workflow_started", "crewai", "forensic request over A2A via ADK RemoteA2aAgent")
                self._set("forensics", S.READY, "dependencies_met", "no upstream task in direct mode")
                self._set("forensics", S.RUNNING, "dispatched", "FORENSIC_REQUEST over A2A")
            deadline = self.deadline("forensics", a2a_timeout(self.a2a_timeout))
            try:
                return await asyncio.wait_for(request_forensics(claim, documents, run_id, self.crew_card_url,
                                                                timeout=self.a2a_timeout, context=context), deadline)
            except TimeoutError:
                self._timed_out.add("forensics")
                exchange = not_sent(claim, documents, run_id, f"the manager's deadline of {deadline:g} s passed")
                exchange.errors[0] = (f"the manager's deadline of {deadline:g} s for the Forensic task passed; the A2A "
                                      "request was cancelled and no result was accepted")
                exchange.result.errors[:] = exchange.errors
                exchange.timed_out = True
                return exchange

        crew_coro = integration._observed(forensic(), self.progress, "crewai_finished") if self.progress else forensic()
        adk, exchange = await asyncio.gather(adk_task, crew_coro)
        crewai = exchange.result
        if exchange.timed_out:
            self._timed_out.add("forensics")
            crewai.provider_failures.append(classify(
                TimeoutError("no Forensic result arrived within the time limit"), run_id=run_id, agent="forensic_agent",
                model=crewai.model or self.gemini_model, provider=execution_mode.PROVIDER_GEMINI if self.live else None))
        if self.live:
            adk = integration.require_genuine_inference(adk, execution_mode.PROVIDER_GEMINI
                                                        if adk.model != "test-double" else None)
            crewai = integration.require_genuine_inference(crewai, crewai.report.provider
                                                           if crewai.report is not None else None)
        self._audit_exchange(adk, crewai, exchange)

        # Stage 4 - monitoring from the agents' own execution records.
        self._observe_results(adk, crewai, exchange)
        self._settle_before_seal(adk, crewai)

        # Stage 5 - collection: seal, then verify every report on the sealed blackboard.
        summary = self.board.collect(adk, crewai, exchange.errors)
        adk_input, crewai_input = adk, crewai
        if self.shared:
            self._note("adjudication", "blackboard_sealed" if summary else "seal_failed",
                       f"sealed at entry {summary['sealed_at']}" if summary else "the blackboard could not be sealed; "
                       "no report can be verified")
            adk_input, crewai_input, checks = self.board.verified_inputs(adk, crewai)
            for agent, check in checks.items():
                if check["verified"]:
                    audit("blackboard_context", "adjudicator", f"{agent} report verified on the blackboard "
                          f"(entry {check['run_seq']})", **check)
                else:
                    audit("validation_failure", "adjudicator", f"{agent} report not used: {check['reason']}")
            self._settle_verified(checks)
        elif self.status("forensics") == S.RUNNING:
            self._set("forensics", S.COMPLETED, "report_validated",
                      "ForensicsReport validated by the CrewAI service and the A2A contract (direct mode: no "
                      "blackboard verification)")

        # Stage 6 - adjudication of the verified reports by the deterministic adjudicator.
        self._set("adjudication", S.READY, "dependencies_met", "all agent tasks finished" +
                  (" and the blackboard is sealed" if self.shared and summary else ""))
        self._set("adjudication", S.RUNNING, "dispatched", "deterministic adjudicator")
        mcp = reconcile_records(await self._mcp_records(), adk, crewai)
        audit("mcp_records_fetched", "orchestrator", f"{len(mcp.calls)} tool call(s) logged by the MCP server",
              callers=sorted({c.caller or "unknown" for c in mcp.calls}), discovered=[t.name for t in mcp.discovered_tools])
        for error in mcp.errors:
            audit("validation_failure", "orchestrator", error)
        serial = next((extract_fields(d).value("serial_number") for d in documents if d.kind == DocumentKind.INVOICE), None)
        serial = normalise_serial(serial or claim.serial_number)
        history = lookup_history(serial, claim.claimant_id, claim.claim_id, self.db_path).records if serial else []
        final = adjudicate(run_id, claim, documents, self.policy, history, baseline, adk_input, crewai_input, mcp)
        self._set("adjudication", S.COMPLETED, "decided", f"{final.final_decision.value} by {final.decided_by}; "
                  f"review {final.review_status}", final_decision=final.final_decision.value,
                  decided_by=final.decided_by, review_status=final.review_status,
                  requires_human_approval=final.requires_human_approval)
        if final.requires_human_approval:
            reasons = "; ".join(final.incomplete_reasons) or f"{final.final_decision.value} by {final.decided_by}"
            self._note("adjudication", "escalated_to_human", f"human approval required: {reasons}")
        self.security_events = self._settle_security(adk, crewai, final)
        self.injection_events = self._settle_injection(baseline, adk, crewai, final)
        self.board.publish_decision(final)
        audit("adjudication", "adjudicator", f"{final.final_decision.value} decided by {final.decided_by}",
              deterministic=final.deterministic_decision.value if final.deterministic_decision else None,
              human_approval=final.requires_human_approval)
        requests = gemini_rate.run_requests(run_id) if self.live else {}
        audit("run_finished", "orchestrator", "all outputs stored independently",
              gemini_requests=requests, gemini_requests_total=sum(requests.values()))
        integration._notify(self.progress, "adjudicated", decision=final.final_decision)

        messages: list[WorkflowMessage] = (([exchange.request] if exchange.sent else [])
                                           + ([exchange.response] if exchange.response else []))
        crew_meta = exchange.response.metadata if exchange.response else ExecutionMetadata(framework="crewai")
        bundle = ClaimRunBundle(
            run_id=run_id, claim_id=self.claim_id, baseline=baseline, adk=adk, crewai=crewai, mcp=mcp,
            messages=messages, final_report=final, audit=sorted(audit.events, key=lambda e: e.ts),
            context_sharing="blackboard" if self.shared else "direct", execution_plan=self.plan,
            security_events=self.security_events, injection_events=self.injection_events,
            workflow_metadata={
                "baseline": ExecutionMetadata(framework="deterministic", llm_mode="none"),
                "orchestrator": ExecutionMetadata(framework="orchestrator", model=self.gemini_model,
                                                  llm_mode="live" if self.live else "none",
                                                  provider=execution_mode.PROVIDER_GEMINI if self.live else None),
                "adk": ExecutionMetadata(framework="adk", model=adk.model,
                                         provider="offline-test-double" if adk.model == "test-double" else "google-gemini",
                                         llm_mode="test-double" if adk.model == "test-double" else "live"),
                "crewai": crew_meta,
                "mcp": ExecutionMetadata(framework="mcp", protocol=f"MCP streamable-http (mcp {version('mcp')})"),
            })
        bundle.outcome = self._outcome(bundle)        # after the decision: observing cannot change it
        bundle.trace = self._collect_trace(bundle)
        return bundle

    async def _mcp_records(self) -> MCPResult:
        """The MCP server's own log. A server that stops answering must not hold the run, and the single-job lock, open:
        the record comes back FAILED, and the adjudicator sends the run to a human."""
        limit = self.deadlines.get("mcp_records", MCP_RECORDS_DEADLINE_S)
        try:
            return await asyncio.wait_for(fetch_mcp_records(self.mcp_url, self.run_id, self.claim_id), limit)
        except TimeoutError:
            return MCPResult(run_id=self.run_id, claim_id=self.claim_id, status=ExecutionStatus.FAILED, server_url=self.mcp_url,
                             transport="streamable-http", errors=[f"the manager's deadline of {limit:g} s for the MCP "
                                                                  "server's records passed"])

    def _outcome(self, bundle: ClaimRunBundle):
        """How the run ended (observability/outcome.py). A problem here is a monitoring problem, nothing more."""
        from claimsleuth.observability.outcome import build_outcome
        try:
            return build_outcome(bundle)
        except Exception as exc:
            self.monitoring_errors.append(f"monitoring: run outcome not derived: {type(exc).__name__}: {str(exc)[:200]}")
            return None

    def _collect_trace(self, bundle: ClaimRunBundle):
        """The unified trace (observability/trace.py). Never raises; problems become collection errors."""
        from claimsleuth.observability.trace import collect_trace
        entries = []
        if self.shared and self.board.enabled:
            try:
                entries = Blackboard(self.board.path).entries(self.run_id, self.claim_id)
            except Exception as exc:
                self.monitoring_errors.append(f"monitoring: blackboard entries not read: {type(exc).__name__}: "
                                              f"{str(exc)[:200]}")
        return collect_trace(bundle, entries, list(self.board.operations), self.monitoring_errors)

    # ------------------------------------------------------------------ collection helpers
    def _settle_security(self, adk: AgentSystemResult, crewai: AgentSystemResult, final) -> list[SecurityEvent]:
        """Every refused read of this run, stamped with the escalation that followed. Decides nothing."""
        escalation = "HUMAN_REVIEW_REQUIRED" if final.requires_human_approval else "NOT_ESCALATED"
        events = [e.model_copy(update={"escalation_status": escalation})
                  for e in adk.security_events + crewai.security_events + self.board.security_events]
        for e in events:
            self.audit("validation_failure", e.consumer or "blackboard", f"{e.attack_type}: {e.rejection_reason}",
                       event_id=e.event_id, section=e.affected_entry_reference)
        if events:
            kinds = ", ".join(sorted({e.attack_type for e in events}))
            self._note("adjudication", "security_event", f"{len(events)} refused read(s) of shared context: {kinds}; "
                       f"{escalation}")
            self._record("security", {"run_id": self.run_id, "event": "security_events",
                                      "events": [e.model_dump(mode="json") for e in events]})
        return events

    def _settle_injection(self, baseline, adk: AgentSystemResult, crewai: AgentSystemResult, final) -> list[SecurityEvent]:
        """Instructions the screen found in this run's input and agent output that was refused, with what became of them.
        It decides nothing: the decision is already made, and a problem here is a monitoring problem."""
        escalation = "HUMAN_REVIEW_REQUIRED" if final.requires_human_approval else "NOT_ESCALATED"
        try:
            reports = {"evidence_agent": adk.evidence_report, "investigator_agent": adk.report,
                       "forensic_agent": crewai.report}
            advised = {agent: (r.recommended_outcome if agent == "forensic_agent" else r.recommended_decision).value
                       for agent, r in reports.items() if r is not None}
            events = [e.model_copy(update={"escalation_status": escalation})
                      for e in adk.injection_events + crewai.injection_events]
            events += injection_events(self.run_id, self.claim_id, baseline.guardrail_events if baseline else [], advised,
                                       final.final_decision.value, final.payout is not None, final.requires_human_approval)
        except Exception as exc:
            self.monitoring_errors.append(f"monitoring: injection events not recorded: {type(exc).__name__}: {str(exc)[:200]}")
            return []
        for e in events:
            self.audit("validation_failure", e.consumer or "guardrail", f"{e.attack_type}: {e.rejection_reason}",
                       event_id=e.event_id, source=e.affected_entry_reference)
        if events:
            kinds = ", ".join(sorted({e.attack_type for e in events}))
            self._note("adjudication", "injection_event", f"{len(events)} prompt injection record(s): {kinds}; {escalation}")
        return events

    def _audit_exchange(self, adk: AgentSystemResult, crewai: AgentSystemResult, exchange: ForensicExchange) -> None:
        audit = self.audit
        audit("workflow_finished", "adk", f"{adk.status.value}", ts=adk.finished_at, execution_ms=adk.execution_ms,
              model=adk.model, errors=adk.errors[:5])
        if exchange.sent:
            audit("message_sent", "adk_orchestrator", "FORENSIC_REQUEST over A2A", ts=exchange.request.timestamp,
                  message_id=exchange.request.message_id, recipient=exchange.request.recipient,
                  context=[r.model_dump() for r in exchange.request.payload.context])
        if exchange.response is not None:
            audit("message_received", "crewai_forensic_service", f"{exchange.response.message_type} over A2A",
                  ts=exchange.response.timestamp, message_id=exchange.response.message_id)
        for error in exchange.errors:
            audit("validation_failure", "adk_orchestrator", error)
        audit("workflow_finished", "crewai", f"{crewai.status.value}", ts=crewai.finished_at,
              execution_ms=crewai.execution_ms, model=crewai.model, errors=crewai.errors[:5])
        for consumed in adk.consumed_context + crewai.consumed_context:
            audit("blackboard_context", consumed.consumer, f"consumed {consumed.ref.section}@{consumed.ref.run_seq} "
                  f"from the blackboard (receipt {consumed.receipt_run_seq})", ref=consumed.ref.model_dump())

    def _observe_results(self, adk: AgentSystemResult, crewai: AgentSystemResult, exchange: ForensicExchange) -> None:
        """Tool executions, the Forensic Agent's context receipt and the returned reports, from each agent's own
        execution record (tool calls are not reported live; their recorded start times are used)."""
        for result, agents in ((adk, ("evidence_agent", "investigator_agent")), (crewai, ("forensic_agent",))):
            for agent in agents:
                calls = [c for c in result.tool_calls if c.agent == agent]
                if calls:
                    names = sorted({f"{c.tool} ({c.status})" for c in calls})
                    self._note(AGENT_TASK[agent], "tools_executed", f"{len(calls)} MCP call(s): {', '.join(names)}",
                               at=calls[-1].started_at)
        for consumed in crewai.consumed_context:
            self._note("forensics", "context_consumed", f"read {consumed.ref.section}@{consumed.ref.run_seq} from the "
                       f"blackboard with the crewai key (receipt {consumed.receipt_run_seq})", ref=consumed.ref)
        # A response is not a report: "report_returned" only when a report really came back. Whether it counts is
        # settled afterwards (_settle_before_seal, _settle_verified).
        if exchange.response is not None:
            returned = crewai.status == ExecutionStatus.COMPLETED and crewai.report is not None
            self._note("forensics", "report_returned" if returned else "response_received",
                       f"{exchange.response.message_type} over A2A: CrewAI {crewai.status.value}")
        task_id = "investigation" if self.shared else "evidence"
        if adk.status == ExecutionStatus.COMPLETED and adk.report is not None:
            self._note(task_id, "report_returned", f"ADK workflow {adk.status.value}")

    def _settle_before_seal(self, adk: AgentSystemResult, crewai: AgentSystemResult) -> None:
        """Tasks whose outcome is already final from execution alone (failures, timeouts, never started)."""
        adk_error = "; ".join(adk.errors[:3]) or f"ADK workflow {adk.status.value}"
        if not self.shared:                                     # direct mode: the ADK result is all or nothing
            runs = {r.agent: r for r in adk.agent_runs}
            if adk.status == ExecutionStatus.COMPLETED:
                ev, inv = runs.get("evidence_agent"), runs.get("investigator_agent")
                self._set("evidence", S.COMPLETED, "report_validated", "EvidenceReport validated with the ADK result",
                          at=ev.finished_at if ev else None)
                self._set("investigation", S.READY, "dependencies_met", at=ev.finished_at if ev else None)
                self._set("investigation", S.RUNNING, "agent_started", "recorded by the ADK callbacks",
                          at=inv.started_at if inv else None)
                self._set("investigation", S.COMPLETED, "report_validated", "InvestigationReport validated by its "
                          "runner (direct mode: no blackboard verification)", at=inv.finished_at if inv else None)
            else:
                self._end_unfinished("evidence", adk_error)
                self._block("investigation", "direct mode: the ADK workflow did not complete")
        else:
            if self.status("evidence") in (S.READY, S.RUNNING):
                self._end_unfinished("evidence", adk_error)
            if self.status("investigation") == S.RUNNING and adk.status != ExecutionStatus.COMPLETED:
                self._end_unfinished("investigation", adk_error)
            elif self.status("investigation") == S.READY:
                self._end_unfinished("investigation", f"the ADK workflow ended before the Investigator started: {adk_error}")
            else:
                self._block("investigation", "the Evidence report was not published")
        if self.status("forensics") == S.RUNNING and crewai.status != ExecutionStatus.COMPLETED:
            self._end_unfinished("forensics", "; ".join(crewai.errors[:3]) or f"CrewAI {crewai.status.value}")
        self._block("forensics", "the Evidence report was not published")

    def _settle_verified(self, checks: dict[str, dict[str, Any]]) -> None:
        """Blackboard mode: a returned report counts only once the adjudicator verified it on the sealed board."""
        for agent, task_id in (("investigator_agent", "investigation"), ("forensic_agent", "forensics")):
            if self.status(task_id) == S.RUNNING:
                check = checks.get(agent, {})
                if check.get("verified"):
                    self._set(task_id, S.COMPLETED, "report_verified", f"report verified on the sealed blackboard "
                              f"(entry {check['run_seq']})", report_sha256=check["report_sha256"])
                else:
                    self._set(task_id, S.FAILED, "verification_failed", check.get("reason", "not verified"))
        evidence = checks.get("evidence_agent", {})
        if self.status("evidence") == S.COMPLETED:
            self._note("evidence", "report_verified" if evidence.get("verified") else "not_adjudicated",
                       f"entry {evidence['run_seq']} verified for adjudication" if evidence.get("verified")
                       else evidence.get("reason", "not verified"))


async def run_investigation(claim_id: str, **kwargs) -> ClaimRunBundle:
    return await InvestigationManager(claim_id, **kwargs).run()

