"""The unified execution trace of one run, assembled from what each component recorded while the run happened.

Sources (all existing, nothing invented):
  manager      the ExecutionPlan's task events (orchestration/manager.py)
  baseline     its own start time and execution time
  adk          ExecutionRecorder callbacks: agents, every model call, every tool call, agent errors
  crewai       CrewRecorder (event bus): agent run, crew / task / agent / guardrail / tool events, model calls, tools
  mcp          the MCP server's own tool-call log for the run
  a2a          the FORENSIC_REQUEST / response WorkflowMessages
  blackboard   the run's signed entries (publications, consumption receipts, seal, verification, decision) and the
               measured latency of each publish / consume / verify / seal operation
  adjudicator  the final report
  failures     the classified failures each agent system recorded (observability/provider_errors.py), one
               provider.failure event each; a failed task and its model and agent events carry the same error code
  security     the refused reads of shared context (schemas.SecurityEvent), one security.event each
  injection    instructions found in the input and refused agent output, one security.injection each

correlation_id is "trace-<run_id>" for every event; parent_event_id links each event to its task's dispatch (the
manager event), and tool / model events to their agent's start - so the concurrent Investigator and Forensic Agent
never share a parent. Metadata is reduced to short scalars (ids, counts, names, states): no document text, no
prompts, no tool arguments' values, no payloads, no keys.
"""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError

from claimsleuth.blackboard.models import RECEIPT_PREFIX, BoardEntry
from claimsleuth.observability.metrics import run_metrics, trace_entries
from claimsleuth.observability.provider_errors import AGENT_TASK, CODES, trace_labels
from claimsleuth.observability.trace_util import aware, ms_between
from claimsleuth.schemas import ClaimRunBundle, ContextRef, RunTrace, TaskStatus, TraceEvent, utc_now

NAMESPACE = uuid.UUID("6b1d2f7e-5c43-4d0a-9e61-0c9a8f3b2d17")
TASK_STATUS = {TaskStatus.COMPLETED: "ok", TaskStatus.FAILED: "error", TaskStatus.TIMED_OUT: "timeout",
               TaskStatus.BLOCKED: "blocked"}


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple, set)):
        value = ", ".join(str(v) for v in value)
    return str(value)[:300]


def _counts(result: Any) -> dict[str, int]:
    """Sizes of an MCP result (match / record counts), never its content."""
    if not isinstance(result, dict):
        return {}
    out = {}
    for key in ("total_matches", "returned", "count"):
        if isinstance(result.get(key), int):
            out[key] = result[key]
    for key in ("matches", "records"):
        if isinstance(result.get(key), list):
            out[f"{key}_count"] = len(result[key])
    return out


class TraceBuilder:
    def __init__(self, run_id: str, claim_id: str) -> None:
        self.run_id, self.claim_id, self.correlation_id = run_id, claim_id, f"trace-{run_id}"
        self.events: list[TraceEvent] = []
        self.errors: list[str] = []
        self.roots: dict[str, str] = {}              # task_id -> the event that dispatched / started it
        self.agent_start: dict[str, str] = {}        # agent -> its agent.start event
        self.failures: dict[str, Any] = {}           # agent -> its first classified failure
        self.task_status: dict[str, TaskStatus] = {}
        self._n = 0

    def add(self, component: str, event_type: str, ts: Any, status: str, *, task_id: str | None = None,
            agent: str | None = None, duration_ms: float | None = None, parent: str | None = None,
            ref: ContextRef | None = None, error_code: str | None = None, error_category: str | None = None,
            **meta: Any) -> str | None:
        when = aware(ts) or utc_now()
        self._n += 1
        event_id = uuid.uuid5(NAMESPACE, f"{self.run_id}|{self._n}|{component}|{event_type}|{when.isoformat()}").hex
        try:
            self.events.append(TraceEvent(
                run_id=self.run_id, claim_id=self.claim_id, event_id=event_id, correlation_id=self.correlation_id,
                parent_event_id=parent, task_id=task_id, agent_name=agent, component=component, event_type=event_type,
                timestamp=when, status=status, duration_ms=duration_ms, context_ref=ref, error_code=error_code,
                error_category=error_category, metadata={k: _scalar(v) for k, v in meta.items()}))
        except ValidationError as exc:          # a monitoring problem: recorded, the run is not affected
            self.errors.append(f"monitoring: {component}.{event_type} not recorded: {str(exc).splitlines()[0][:200]}")
            return None
        return event_id

    def parent_for(self, agent: str | None, task_id: str | None) -> str | None:
        return self.agent_start.get(agent or "") or self.roots.get(task_id or "")

    def labels(self, agent: str | None, default_code: str, default_category: str) -> tuple[str, str]:
        """The (error_code, error_category) of an agent's failure event: its classification, else the default."""
        failure = self.failures.get(agent or "")
        return trace_labels(failure.error_code if failure else None, default_code, default_category)

    # ------------------------------------------------------------------ sources
    def provider_failures(self, result: Any, component: str) -> None:
        for f in result.provider_failures if result is not None else []:
            self.failures.setdefault(f.agent or "", f)
            self.add(component, "provider.failure", f.occurred_at, "timeout" if f.category == "timeout" else "error",
                     task_id=f.task_id, agent=f.agent, error_code=f.error_code, error_category=CODES[f.error_code][2],
                     provider=f.provider,
                     model=f.model, http_status=f.http_status, provider_status=f.provider_status,
                     provider_reason=f.provider_reason, exception_type=f.exception_type, failure_category=f.category,
                     retryable=f.retryable, retried=f.retried, detail=f.summary)

    def provider_retries(self, result: Any, component: str) -> None:
        for r in result.provider_retries if result is not None else []:
            self.add(component, "provider.retry", r.occurred_at, "info", task_id=r.task_id, agent=r.agent,
                     first_error=r.error_code, http_status=r.http_status, delay_s=r.delay_s, outcome=r.outcome)

    def manager(self, bundle: ClaimRunBundle) -> None:
        plan = bundle.execution_plan
        if plan is None:
            return
        plan_id = self.add("manager", "plan.created", plan.created_at, "info", tasks=len(plan.tasks),
                           context_sharing=plan.context_sharing)
        for task in plan.tasks:
            self.task_status[task.task_id] = task.status
            for ev in task.events:
                status = TASK_STATUS.get(ev.status, "info") if ev.status in TASK_STATUS and ev.event not in (
                    "context_consumed", "tools_executed", "report_returned", "response_received", "agent_finished",
                    "agent_started", "escalated_to_human", "blackboard_sealed", "seal_failed", "not_adjudicated") else "info"
                code = category = None
                if ev.status in (TaskStatus.FAILED, TaskStatus.TIMED_OUT, TaskStatus.BLOCKED) and status != "info":
                    code, category = self._classify(task.task_id, ev.event, ev.status, ev.detail)
                    if ev.status == TaskStatus.FAILED:      # a timeout keeps its task-level code (A2A or agent timeout)
                        code, category = self.labels(task.agent, code, category)
                event_id = self.add("adjudicator" if task.task_id == "adjudication" and ev.event == "decided" else "manager",
                                    f"task.{ev.event}", ev.ts, status, task_id=task.task_id, agent=task.agent,
                                    parent=self.roots.get(task.task_id, plan_id), ref=ev.ref, error_code=code,
                                    error_category=category, task_status=ev.status.value, detail=ev.detail[:200])
                if ev.status == TaskStatus.RUNNING and task.task_id not in self.roots and event_id:
                    self.roots[task.task_id] = event_id

    @staticmethod
    def _classify(task_id: str, event: str, status: TaskStatus, detail: str) -> tuple[str, str]:
        text = detail.lower()
        if status == TaskStatus.BLOCKED:
            return "BLOCKED_UPSTREAM", "business"
        if event == "verification_failed":
            return "VERIFICATION_FAILED", "integration"
        if "not published to the blackboard:" in text:      # the agent's own output failed validation
            return "AGENT_OUTPUT_INVALID", "agent"
        if task_id == "forensics" and ("a2a" in text or "deadline" in text):
            return ("A2A_TIMEOUT" if status == TaskStatus.TIMED_OUT else "A2A_FAILED"), "integration"
        if "mcp server" in text or "blackboard" in text:
            return "INTEGRATION_FAILED", "integration"
        if status == TaskStatus.TIMED_OUT:
            return "AGENT_TIMEOUT", "agent"
        if "manager failure" in text:
            return "MANAGER_FAILED", "business"
        return "AGENT_FAILED", "agent"

    def baseline(self, bundle: ClaimRunBundle) -> None:
        b = bundle.baseline
        if b is not None:
            ok = b.status.value == "COMPLETED"
            self.add("baseline", "baseline.run", b.started_at, "ok" if ok else "error", task_id="baseline",
                     agent="deterministic_baseline", duration_ms=b.execution_ms, parent=self.roots.get("baseline"),
                     error_code=None if ok else "BASELINE_FAILED", error_category=None if ok else "business",
                     decision=b.decision.value if b.decision else None)

    def agents(self, result: Any, component: str) -> None:
        if result is None:
            return
        for run in result.agent_runs:
            task_id = AGENT_TASK.get(run.agent)
            start = self.add(component, "agent.start", run.started_at, "info", task_id=task_id, agent=run.agent,
                             parent=self.roots.get(task_id or ""), framework=run.framework)
            if start:
                self.agent_start[run.agent] = start
            if run.finished_at:
                # "ok" means the task succeeded: an agent that ended without an accepted report did not.
                failed = bool(run.errors) or self.task_status.get(task_id or "") in (
                    TaskStatus.FAILED, TaskStatus.TIMED_OUT, TaskStatus.BLOCKED)
                code, category = self.labels(run.agent, "AGENT_ERRORS" if run.errors else "REPORT_NOT_ACCEPTED",
                                             "agent") if failed else (None, None)
                self.add(component, "agent.end", run.finished_at, "error" if failed else "ok", task_id=task_id,
                         agent=run.agent, duration_ms=run.latency_ms, parent=start, error_code=code,
                         error_category=category, llm_calls=run.llm_calls, tool_calls=run.tool_calls,
                         errors=len(run.errors), outcome=run.outcome)
        for call in trace_entries(result, "model_calls"):
            agent = call.get("agent") or (result.agent_runs[0].agent if result.agent_runs else None)
            failed = call.get("status") == "error"
            code, category = trace_labels(call.get("error_code"), "MODEL_ERROR", "agent") if failed else (None, None)
            self.add(component, "model.call", call.get("started_at") or call.get("finished_at"),
                     "error" if failed else "ok", task_id=AGENT_TASK.get(agent or ""), agent=agent,
                     duration_ms=call.get("latency_ms"), parent=self.parent_for(agent, AGENT_TASK.get(agent or "")),
                     error_code=code, error_category=category, http_status=call.get("http_status"),
                     model=call.get("model") or result.model,
                     usage_reported=bool(call.get("usage_reported") or call.get("prompt_tokens") is not None
                                         or call.get("completion_tokens") is not None),
                     prompt_tokens=call.get("prompt_tokens"), completion_tokens=call.get("completion_tokens"),
                     cached_tokens=call.get("cached_tokens"), total_tokens=call.get("total_tokens"),
                     finish_reason=call.get("finish_reason"))
        for err in trace_entries(result, "agent_errors"):
            agent = err.get("agent")
            code, category = trace_labels(err.get("error_code"), "AGENT_EXCEPTION", "agent")
            self.add(component, "agent.error", err.get("ts"), "error", task_id=AGENT_TASK.get(agent or ""), agent=agent,
                     parent=self.parent_for(agent, AGENT_TASK.get(agent or "")), error_code=code,
                     error_category=category, error_type=err.get("error_type"), propagated=err.get("propagated"))
        for ev in trace_entries(result, "crew_events"):
            status = {"ok": "ok", "error": "error", "rejected": "rejected"}.get(ev.get("status"), "info")
            code, category = None, None
            if status == "rejected":
                code, category = "GUARDRAIL_REJECTED", "agent"
            elif status == "error":
                code, category = self.labels("forensic_agent", "CREWAI_ERROR", "agent")
            self.add(component, ev["event"], ev.get("ts"), status, task_id="forensics", agent="forensic_agent",
                     parent=self.parent_for("forensic_agent", "forensics"), error_code=code,
                     error_category=category, framework_event_id=ev.get("event_id"),
                     framework_parent_event_id=ev.get("parent_event_id"), retry_count=ev.get("retry_count"),
                     tool=ev.get("tool"), error=(ev.get("error") or None) and str(ev["error"])[:200])
        for call in result.tool_calls:
            failed = call.status != "ok"
            self.add(component, "tool.call", call.started_at, "error" if failed else "ok",
                     task_id=AGENT_TASK.get(call.agent), agent=call.agent, duration_ms=call.latency_ms,
                     parent=self.parent_for(call.agent, AGENT_TASK.get(call.agent)),
                     error_code="TOOL_ERROR" if failed else None,
                     error_category=("integration" if call.protocol == "mcp" else "agent") if failed else None,
                     tool=call.tool, protocol=call.protocol, argument_keys=sorted(call.arguments),
                     **_counts(call.result))

    def mcp(self, bundle: ClaimRunBundle) -> None:
        if bundle.mcp is None:
            return
        if bundle.mcp.discovered_tools:
            self.add("mcp", "mcp.discovery", bundle.mcp.calls[0].started_at if bundle.mcp.calls else utc_now(), "info",
                     tools=[t.name for t in bundle.mcp.discovered_tools])
        for call in bundle.mcp.calls:
            agent = (call.caller or "").split(":", 1)[-1] or None
            failed = call.status != "ok"
            self.add("mcp", "mcp.call", call.started_at, "error" if failed else "ok", task_id=AGENT_TASK.get(agent or ""),
                     agent=agent, duration_ms=call.latency_ms, parent=self.parent_for(agent, AGENT_TASK.get(agent or "")),
                     error_code="MCP_ERROR" if failed else None, error_category="integration" if failed else None,
                     tool=call.tool, caller=call.caller, call_id=call.call_id, **_counts(call.result))

    def a2a(self, bundle: ClaimRunBundle) -> None:
        request = next((m for m in bundle.messages if m.message_type == "FORENSIC_REQUEST"), None)
        response = next((m for m in bundle.messages if m.message_type != "FORENSIC_REQUEST"), None)
        forensics = next((t for t in bundle.execution_plan.tasks if t.task_id == "forensics"), None) \
            if bundle.execution_plan else None
        if request is not None:
            refs = request.payload.context
            self.add("a2a", "a2a.dispatch", request.timestamp, "ok", task_id="forensics", agent="forensic_agent",
                     parent=self.roots.get("forensics"), ref=refs[0] if refs else None, message_id=request.message_id,
                     sender=request.sender, recipient=request.recipient, context_refs=len(refs))
            if response is not None:
                ok = response.message_type == "FORENSIC_RESPONSE"
                # "ok" is the A2A exchange only: a delivered response can carry a failed Forensic result.
                self.add("a2a", "a2a.response", response.timestamp, "ok" if ok else "error", task_id="forensics",
                         agent="forensic_agent", duration_ms=ms_between(request.timestamp, response.timestamp),
                         parent=self.roots.get("forensics"), error_code=None if ok else "A2A_ERROR",
                         error_category=None if ok else "integration", message_id=response.message_id,
                         in_reply_to=request.message_id, message_type=response.message_type,
                         sender=response.sender, recipient=response.recipient,
                         forensic_status=bundle.crewai.status.value if bundle.crewai else None)
        if response is None and forensics is not None and forensics.status in (TaskStatus.FAILED, TaskStatus.TIMED_OUT):
            timed_out = forensics.status == TaskStatus.TIMED_OUT
            self.add("a2a", "a2a.no_response", forensics.finished_at, "timeout" if timed_out else "error",
                     task_id="forensics", agent="forensic_agent", parent=self.roots.get("forensics"),
                     error_code="A2A_TIMEOUT" if timed_out else "A2A_FAILED", error_category="integration",
                     request_sent=request is not None, reason=(forensics.failure or "")[:200])

    def blackboard(self, entries: list[BoardEntry], operations: list[dict[str, Any]]) -> None:
        ops = {(o.get("op"), o.get("run_seq")): o for o in operations if o.get("run_seq") is not None}
        for e in entries:
            producer_task = AGENT_TASK.get(e.author)
            auth_ok = e.auth == "valid"
            common = dict(principal=e.principal, run_seq=e.run_seq, auth=e.auth, sha256=e.payload_sha256[:16])
            if e.kind == "post" and e.section.startswith("report:"):
                ref = ContextRef(run_id=e.run_id, claim_id=e.claim_id, section=e.section, run_seq=e.run_seq,
                                 sha256=e.payload_sha256, fingerprint=e.fingerprint) if e.fingerprint else None
                op = ops.get(("publish", e.run_seq), {})
                self.add("blackboard", "blackboard.publish", e.ts, "ok" if auth_ok else "error", task_id=producer_task,
                         agent=e.author, duration_ms=op.get("latency_ms"), parent=self.parent_for(e.author, producer_task),
                         ref=ref, error_code=None if auth_ok else "ENTRY_UNAUTHENTICATED",
                         error_category=None if auth_ok else "integration", producer=e.author, section=e.section,
                         late=e.late, **common)
            elif e.kind == "monitor" and e.section.startswith(RECEIPT_PREFIX):
                consumer = e.payload.get("consumer")
                task_id = AGENT_TASK.get(consumer or "") or ("adjudication" if consumer == "adjudicator" else None)
                op = ops.get(("consume", e.run_seq)) or ops.get(("verify", e.run_seq)) or {}
                self.add("blackboard", "blackboard.consume", e.ts, "ok" if auth_ok else "error", task_id=task_id,
                         agent=consumer, duration_ms=op.get("latency_ms"), parent=self.roots.get(task_id or ""),
                         error_code=None if auth_ok else "ENTRY_UNAUTHENTICATED",
                         error_category=None if auth_ok else "integration", consumer=consumer,
                         producer=e.payload.get("source_author"), section=e.payload.get("source_section"),
                         source_run_seq=e.payload.get("source_run_seq"),
                         source_sha256=(e.payload.get("source_sha256") or "")[:16], receipt="signed" if auth_ok else e.auth,
                         **common)
            elif e.kind == "monitor" and e.section == "controller:seal":
                self.add("blackboard", "blackboard.seal", e.ts, "ok", task_id="adjudication", agent="controller",
                         duration_ms=ops.get(("seal", e.run_seq), {}).get("latency_ms"), **common)
            elif e.kind == "monitor" and e.section == "controller:verification":
                agents = e.payload.get("agents") or {}
                self.add("blackboard", "blackboard.verification", e.ts, "ok", task_id="adjudication", agent="adjudicator",
                         verified=sum(1 for a in agents.values() if a.get("verified")),
                         not_verified=sum(1 for a in agents.values() if not a.get("verified")), **common)
            elif e.kind == "retract":
                self.add("blackboard", "blackboard.retract", e.ts, "info", agent=e.author, section=e.section,
                         reason=str(e.payload.get("reason", ""))[:200], **common)
            elif e.kind == "post" and e.section == "decision:final":
                self.add("blackboard", "blackboard.decision", e.ts, "ok" if auth_ok else "error", task_id="adjudication",
                         agent="adjudicator", final_decision=e.payload.get("final_decision"), **common)
            elif not auth_ok:
                self.add("blackboard", "blackboard.entry", e.ts, "error", error_code="ENTRY_UNAUTHENTICATED",
                         error_category="integration", section=e.section, **common)
        for op in operations:                   # rejected reads and writes leave no entry: record them from the op
            if not op.get("ok"):
                actor = op.get("actor")
                task_id = AGENT_TASK.get(actor or "") or ("adjudication" if actor == "adjudicator" else None)
                self.add("blackboard", f"blackboard.{op.get('op')}_rejected", op.get("started_at"), "rejected",
                         task_id=task_id, agent=actor, duration_ms=op.get("latency_ms"),
                         parent=self.roots.get(task_id or ""), error_code="BLACKBOARD_REJECTED",
                         error_category="integration", section=op.get("section"), reason=(op.get("error") or "")[:200])

    def security(self, bundle: ClaimRunBundle) -> None:
        for e in bundle.security_events:
            task_id = AGENT_TASK.get(e.consumer or "") or ("adjudication" if e.consumer == "adjudicator" else None)
            self.add("blackboard", "security.event", e.timestamp, "rejected", task_id=task_id, agent=e.consumer,
                     parent=self.roots.get(task_id or ""), error_code=e.attack_type, error_category="security",
                     event_type_label=e.event_type, severity=e.severity, section=e.affected_entry_reference,
                     payload_integrity=e.verification_result.payload_integrity,
                     signature_check=e.verification_result.signature, downstream_blocked=e.downstream_blocked,
                     escalation_status=e.escalation_status, test_mode=e.test_mode, event_ref=e.event_id)

    def injection(self, bundle: ClaimRunBundle) -> None:
        for e in bundle.injection_events:
            task_id = AGENT_TASK.get(e.consumer or "") or "adjudication"
            self.add("adjudicator", "security.injection", e.timestamp, "rejected", task_id=task_id, agent=e.consumer,
                     parent=self.roots.get(task_id), error_code=e.attack_type, error_category="security",
                     severity=e.severity, source=e.affected_entry_reference,
                     detection_status=e.detection_status, rejection_status=e.rejection_status,
                     rules=", ".join(e.detection_rules)[:200], downstream_impact=e.downstream_impact,
                     escalation_status=e.escalation_status, test_mode=e.test_mode, event_ref=e.event_id)

    def adjudication(self, bundle: ClaimRunBundle) -> None:
        final = bundle.final_report
        plan = bundle.execution_plan
        if final is None or plan is None:
            return
        task = next(t for t in plan.tasks if t.task_id == "adjudication")
        if final.review_status != "COMPLETE":
            self.add("adjudicator", "investigation.incomplete", task.finished_at, "error", task_id="adjudication",
                     agent="adjudicator", parent=self.roots.get("adjudication"), error_code="INCOMPLETE_INVESTIGATION",
                     error_category="business", review_status=final.review_status,
                     requires_human_approval=final.requires_human_approval,
                     reasons=len(final.incomplete_reasons))


def build_trace(bundle: ClaimRunBundle, entries: list[BoardEntry], operations: list[dict[str, Any]],
                monitoring_errors: list[str] | None = None) -> RunTrace:
    b = TraceBuilder(bundle.run_id, bundle.claim_id)
    b.provider_failures(bundle.adk, "adk")
    b.provider_failures(bundle.crewai, "crewai")
    b.provider_retries(bundle.adk, "adk")
    b.provider_retries(bundle.crewai, "crewai")
    b.manager(bundle)
    b.baseline(bundle)
    b.agents(bundle.adk, "adk")
    b.agents(bundle.crewai, "crewai")
    b.mcp(bundle)
    b.a2a(bundle)
    all_ops = list(operations) + trace_entries(bundle.adk, "blackboard_ops") + trace_entries(bundle.crewai, "blackboard_ops")
    b.blackboard(entries, all_ops)
    b.security(bundle)
    b.injection(bundle)
    b.adjudication(bundle)
    errors = list(monitoring_errors or []) + b.errors
    for error in errors:
        b.add("monitoring", "monitoring.collection_error", utc_now(), "error", error_code="MONITORING_FAILED",
              error_category="monitoring", detail=error[:300])
    order = {e.event_id: i for i, e in enumerate(b.events)}
    events = sorted(b.events, key=lambda e: (e.timestamp, order[e.event_id]))
    metrics = None
    try:
        metrics = run_metrics(bundle, list(operations))   # run_metrics adds the agents' own operations itself
    except Exception as exc:                    # metrics are observational: a failure is recorded, nothing else
        errors.append(f"monitoring: metrics not computed: {type(exc).__name__}: {str(exc)[:200]}")
    return RunTrace(run_id=bundle.run_id, claim_id=bundle.claim_id, correlation_id=b.correlation_id,
                    collected_at=utc_now(), events=events, metrics=metrics, collection_errors=errors)


def collect_trace(bundle: ClaimRunBundle, entries: list[BoardEntry], operations: list[dict[str, Any]],
                  monitoring_errors: list[str] | None = None) -> RunTrace:
    """build_trace that never raises: a collection failure gives an empty trace that says why."""
    try:
        return build_trace(bundle, entries, operations, monitoring_errors)
    except Exception as exc:
        return RunTrace(run_id=bundle.run_id, claim_id=bundle.claim_id, correlation_id=f"trace-{bundle.run_id}",
                        collected_at=utc_now(), collection_errors=list(monitoring_errors or []) + [
                            f"monitoring: trace not assembled: {type(exc).__name__}: {str(exc)[:200]}"])
