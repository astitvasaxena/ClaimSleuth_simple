"""The outcome of one run, derived only from what the run recorded. The same derivation serves the manager, the API
and the dashboard, and works for runs stored before it existed.

    BLOCKED     no required agent review could begin
    INCOMPLETE  a required agent review began but did not finish with a verified report
    ESCALATED   every required review finished and a human must review the result
    COMPLETED   every required review finished and no human review is required

This describes the run. It decides nothing: the decision and human approval stay the adjudicator's.
"""
from __future__ import annotations

from claimsleuth.observability.provider_errors import explanation
from claimsleuth.schemas import (MCP_LOG_EMPTY, MCP_RECORDS_UNAVAILABLE, ClaimRunBundle, ExecutionStatus, FinalClaimReport, ProviderFailure,
                                 RunOutcome, RunStatus, TaskStatus)

AGENT_TASKS = ("evidence", "investigation", "forensics")
USED_EVENTS = ("report_verified", "report_validated")       # the report went on to the adjudicator


def records_only(final: FinalClaimReport) -> bool:
    """Both agent reviews finished; what is missing is the MCP server's confirmation of their tool calls."""
    return final.incomplete_reasons == [MCP_RECORDS_UNAVAILABLE]


def records_missing(bundle: ClaimRunBundle) -> bool:
    """The MCP server reported no fault, yet its log holds none of the tool calls the agents recorded. The manager now treats those
    records as unavailable; a run stored before it did keeps the server's COMPLETED, and no person was asked. This decides nothing."""
    mcp = bundle.mcp
    recorded = [c for result in (bundle.adk, bundle.crewai) if result is not None for c in result.tool_calls if c.protocol == "mcp"]
    return (mcp is not None and not mcp.calls and bool(recorded)
            and (mcp.status == ExecutionStatus.COMPLETED or MCP_LOG_EMPTY in mcp.errors))


def records_unconfirmed(bundle: ClaimRunBundle) -> bool:
    """Every review finished, and the MCP server's records could not confirm the tool calls, whether or not the server said so."""
    final = bundle.final_report
    return final is not None and (records_only(final) or (records_missing(bundle) and run_status(bundle) != RunStatus.INCOMPLETE))


def incomplete_text(final: FinalClaimReport) -> str:
    """What is actually incomplete, in words. `review_status` is PARTIAL in both cases, so it cannot say this itself."""
    if records_only(final):
        return "SUPPORTING RECORDS UNAVAILABLE (both agent reviews finished)"
    return f"INCOMPLETE INVESTIGATION ({final.review_status} agent review)" if final.review_status else "INCOMPLETE INVESTIGATION"


def _failures(bundle: ClaimRunBundle) -> list[ProviderFailure]:
    found = [f for result in (bundle.adk, bundle.crewai) if result is not None for f in result.provider_failures]
    return sorted(found, key=lambda f: f.occurred_at)


def run_status(bundle: ClaimRunBundle) -> RunStatus:
    final = bundle.final_report
    plan = bundle.execution_plan
    if plan is not None:
        tasks = [t for t in plan.tasks if t.task_id in AGENT_TASKS]
        started = any(t.started_at is not None for t in tasks)
        finished = all(t.status == TaskStatus.COMPLETED for t in tasks)
    else:                                       # stored before the manager existed
        results = [r for r in (bundle.adk, bundle.crewai) if r is not None]
        started = any(r.agent_runs for r in results)
        finished = len(results) == 2 and all(r.status == ExecutionStatus.COMPLETED for r in results)
    if not started:
        return RunStatus.BLOCKED
    # A report stored before `review_status` existed leaves it empty: the agents' own statuses above are then the record.
    if not finished or final is None or final.review_status not in ("COMPLETE", None):
        return RunStatus.INCOMPLETE
    return RunStatus.ESCALATED if final.requires_human_approval else RunStatus.COMPLETED


def _reports(bundle: ClaimRunBundle) -> tuple[list[str], list[str]]:
    """(agents whose valid report is kept, agents whose report went on to the adjudicator)."""
    adk, crewai = bundle.adk, bundle.crewai
    plan = bundle.execution_plan
    if plan is None:
        done = ([] if adk is None or adk.status != ExecutionStatus.COMPLETED else ["evidence_agent", "investigator_agent"]) \
            + ([] if crewai is None or crewai.status != ExecutionStatus.COMPLETED else ["forensic_agent"])
        return done, done
    tasks = [t for t in plan.tasks if t.task_id in AGENT_TASKS and t.status == TaskStatus.COMPLETED]
    # Earlier runs retracted an Evidence report when only the Investigator failed: that report was not kept.
    kept = [t for t in tasks if not (t.task_id == "evidence" and adk is not None and adk.evidence_report is None)]
    return ([t.agent for t in kept],
            [t.agent for t in kept if any(e.event in USED_EVENTS for e in t.events)])


def integrity_explanation(used: list[str], human: bool) -> str:
    """What the refused reads meant for this run, from what actually went on to the adjudicator."""
    reports = f"only {', '.join(used)} went on to the adjudicator" if used else "no agent report went on to the adjudicator"
    return f"Shared context was refused for a security reason: {reports}. " + (
        "A human must review the claim." if human else "No human review was required.")


def build_outcome(bundle: ClaimRunBundle, failures_recorded: bool = True) -> RunOutcome:
    final = bundle.final_report
    plan = bundle.execution_plan
    tasks = [t for t in plan.tasks if t.task_id in AGENT_TASKS] if plan else []
    failures = _failures(bundle)
    preserved, used = _reports(bundle)
    human = final.requires_human_approval if final else None
    return RunOutcome(
        run_status=run_status(bundle), requires_human_approval=human, final_decision=final.final_decision if final else None,
        review_status=final.review_status if final else None,
        failed_tasks=[t.task_id for t in tasks if t.status in (TaskStatus.FAILED, TaskStatus.TIMED_OUT)],
        blocked_tasks=[t.task_id for t in tasks if t.status == TaskStatus.BLOCKED],
        preserved_reports=preserved, adjudicated_reports=used, failures=failures, failures_recorded=failures_recorded,
        retries=[r for result in (bundle.adk, bundle.crewai) if result is not None for r in result.provider_retries],
        security_events=len(bundle.security_events),
        explanation=(integrity_explanation(used, bool(human)) if bundle.security_events
                     else explanation(failures[0], preserved, bool(human)) if failures else None))


def outcome_of(bundle: ClaimRunBundle) -> RunOutcome:
    """The outcome recorded with the run, or (a run stored earlier) derived from its stored records."""
    return bundle.outcome or build_outcome(bundle, failures_recorded=False)
