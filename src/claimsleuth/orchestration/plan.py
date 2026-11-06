"""The manager's execution plan: typed tasks, explicit dependencies and a validated task state machine.

Task graph (blackboard mode):

    baseline ──────────────────────────────────────────────┐
    evidence ──(completed)──► investigation ──(finished)──┤
        └──────(completed)──► forensics ─────(finished)──┴──► adjudication

- evidence: no upstream task; COMPLETED only when its validated report is published to the blackboard.
- investigation and forensics: each requires a COMPLETED evidence task (otherwise BLOCKED); they do not depend on
  each other, so they run concurrently and neither receives the other's report.
- adjudication: requires every other task to have finished (in any state) and, in blackboard mode, the sealed
  blackboard; it receives only verified reports and escalates an incomplete investigation to a human.

Direct mode (blackboard disabled) has no shared context: forensics has no upstream task and starts with the ADK
workflow, as the earlier design did.
"""
from __future__ import annotations

from datetime import datetime

from claimsleuth.schemas import (ContextRef, ExecutionPlan, PlanTask, TaskDependency, TaskEvent, TaskStatus, utc_now)

S = TaskStatus
TERMINAL: frozenset[TaskStatus] = frozenset({S.COMPLETED, S.FAILED, S.TIMED_OUT, S.BLOCKED})
TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    S.PENDING: frozenset({S.READY, S.BLOCKED}),
    S.READY: frozenset({S.RUNNING, S.BLOCKED, S.FAILED}),       # FAILED: dispatched but could not start
    S.RUNNING: frozenset({S.COMPLETED, S.FAILED, S.TIMED_OUT}),
    S.COMPLETED: frozenset(), S.FAILED: frozenset(), S.TIMED_OUT: frozenset(), S.BLOCKED: frozenset(),
}


class InvalidTransition(ValueError):
    """A task state change the plan does not allow (for example COMPLETED -> RUNNING, or PENDING -> COMPLETED)."""


def build_plan(run_id: str, claim_id: str, fingerprint: str, shared: bool) -> ExecutionPlan:
    evidence_dep = [TaskDependency(task_id="evidence", requires="completed")]
    tasks = [
        PlanTask(task_id="baseline", agent="deterministic_baseline", framework="deterministic",
                 channel="in-process", description="Deterministic rule checks (no LLM)."),
        PlanTask(task_id="evidence", agent="evidence_agent", framework="adk", channel="ADK SequentialAgent, step 1",
                 description="Extract the facts with line provenance, list missing documents; publish the validated "
                             "EvidenceReport to the blackboard." if shared else
                             "Extract the facts with line provenance and list missing documents (EvidenceReport)."),
        PlanTask(task_id="investigation", agent="investigator_agent", framework="adk",
                 channel="ADK SequentialAgent, step 2, released by the manager" if shared else
                 "ADK SequentialAgent, step 2",
                 description="Policy eligibility, claim history and anomalies from the Evidence context; "
                             "InvestigationReport.", depends_on=evidence_dep),
        PlanTask(task_id="forensics", agent="forensic_agent", framework="crewai",
                 channel="A2A request to the CrewAI service" + (" with a ContextRef" if shared else ""),
                 description="Serial, chronology and history-pattern forensics" +
                             (" from the authorised Evidence context; ForensicsReport." if shared else
                              "; ForensicsReport."),
                 depends_on=evidence_dep if shared else []),
        PlanTask(task_id="adjudication", agent="adjudicator", framework="deterministic", channel="in-process",
                 description="Deterministic decision from the baseline and the verified agent reports"
                             + (" on the sealed blackboard." if shared else "."),
                 depends_on=[TaskDependency(task_id=t, requires="finished")
                             for t in ("baseline", "evidence", "investigation", "forensics")]),
    ]
    return ExecutionPlan(run_id=run_id, claim_id=claim_id, fingerprint=fingerprint,
                         context_sharing="blackboard" if shared else "direct", created_at=utc_now(), tasks=tasks)


def task(plan: ExecutionPlan, task_id: str) -> PlanTask:
    found = next((t for t in plan.tasks if t.task_id == task_id), None)
    if found is None:
        raise KeyError(f"no task {task_id!r} in the plan")
    return found


def blocking_reason(plan: ExecutionPlan, task_id: str) -> str | None:
    """Why the task can never run: a dependency that must be COMPLETED ended otherwise. None if it still may."""
    for dep in task(plan, task_id).depends_on:
        upstream = task(plan, dep.task_id)
        if dep.requires == "completed" and upstream.status in TERMINAL and upstream.status != S.COMPLETED:
            return f"upstream task {dep.task_id} is {upstream.status.value}" + (
                f": {upstream.failure}" if upstream.failure else "")
    return None


def dependencies_met(plan: ExecutionPlan, task_id: str) -> bool:
    for dep in task(plan, task_id).depends_on:
        upstream = task(plan, dep.task_id)
        if dep.requires == "completed" and upstream.status != S.COMPLETED:
            return False
        if dep.requires == "finished" and upstream.status not in TERMINAL:
            return False
    return True


def transition(plan: ExecutionPlan, task_id: str, status: TaskStatus, event: str, detail: str = "",
               ref: ContextRef | None = None, at: datetime | None = None) -> TaskEvent:
    """Move one task to a new state, enforcing the state machine and its dependencies.

    READY needs every dependency met; RUNNING needs READY (so a task can never run before its dependencies);
    COMPLETED needs RUNNING. Raises InvalidTransition otherwise."""
    t = task(plan, task_id)
    if status not in TRANSITIONS[t.status]:
        raise InvalidTransition(f"task {task_id}: {t.status.value} -> {status.value} is not allowed")
    if status == S.READY and not dependencies_met(plan, task_id):
        raise InvalidTransition(f"task {task_id}: dependencies not met, it cannot become READY")
    now = at or utc_now()
    if status == S.RUNNING:
        t.started_at = now
    if status in TERMINAL:
        t.finished_at = now
    if status in (S.FAILED, S.TIMED_OUT, S.BLOCKED):
        t.failure = (detail or status.value)[:2000]
    if ref is not None and status == S.COMPLETED:     # a dispatch's ref is the task's input, never its result
        t.result_ref = ref
    t.status = status
    ev = TaskEvent(ts=now, event=event, status=status, detail=detail[:500], ref=ref)
    t.events.append(ev)
    return ev


def record(plan: ExecutionPlan, task_id: str, event: str, detail: str = "", ref: ContextRef | None = None,
           at: datetime | None = None) -> TaskEvent:
    """A monitoring event that does not change the task's state (tool use, context consumed, report returned)."""
    t = task(plan, task_id)
    ev = TaskEvent(ts=at or utc_now(), event=event, status=t.status, detail=detail[:500], ref=ref)
    t.events.append(ev)
    return ev
