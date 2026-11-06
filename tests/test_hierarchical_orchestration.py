"""Hierarchical orchestration: the deterministic manager (Level 1) over the three specialist agents (Level 2).

The manager plans the run, enforces task dependencies (Evidence -> Investigator and Forensic), releases the
Investigator inside the ADK workflow, dispatches the Forensic Agent over A2A, monitors and collects their real
execution, seals the blackboard and hands only verified reports to the deterministic adjudicator.

Everything is real except the language models (labelled offline test doubles; no Gemini request): the manager, the
MCP server process, the ADK Runner and plugin, the CrewAI A2A service process in .venv-crew, the SQLite blackboard.
"""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from adk_test_double import ScriptedLlm, call
from claimsleuth.blackboard import ENV_VAR, Blackboard, run_monitor
from claimsleuth.blackboard.controller import report_sha256
from claimsleuth.blackboard.models import RECEIPT_PREFIX
from claimsleuth.datastore import load_claim
from claimsleuth.integration.run import run_claim
from claimsleuth.orchestration import manager as manager_mod
from claimsleuth.orchestration import plan as P
from claimsleuth.orchestration.manager import InvestigationManager, ManagerError
from claimsleuth.schemas import Decision, ExecutionPlan, ExecutionStatus, TaskStatus
from test_adk_investigation import evidence_report
from test_integration_e2e import CREW_PYTHON, CrewService
from test_mcp_server import RunningServer, _free_port

S = TaskStatus
FP = "a" * 64


# =========================================================================== the plan and its state machine

def test_the_plan_is_typed_with_explicit_dependencies():
    plan = P.build_plan("RUN-h-1", "CS-001", FP, shared=True)
    assert [t.task_id for t in plan.tasks] == ["baseline", "evidence", "investigation", "forensics", "adjudication"]
    deps = {t.task_id: [(d.task_id, d.requires) for d in t.depends_on] for t in plan.tasks}
    assert deps == {"baseline": [], "evidence": [],
                    "investigation": [("evidence", "completed")], "forensics": [("evidence", "completed")],
                    "adjudication": [(t, "finished") for t in ("baseline", "evidence", "investigation", "forensics")]}
    assert {t.status for t in plan.tasks} == {S.PENDING}
    assert ("investigation", "completed") not in deps["forensics"] and ("forensics", "completed") not in deps["investigation"]
    assert P.build_plan("RUN-h-1", "CS-001", FP, shared=False).tasks[3].depends_on == []   # direct mode
    with pytest.raises(ValidationError):                         # strict: bad fingerprint, unknown task, extra field
        ExecutionPlan.model_validate({**plan.model_dump(), "fingerprint": "not-a-hash"})
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate({**plan.model_dump(), "tasks": [{**plan.tasks[0].model_dump(), "task_id": "x"}]})
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate({**plan.model_dump(), "documents": []})


def test_invalid_task_transitions_are_rejected():
    plan = P.build_plan("RUN-h-2", "CS-001", FP, shared=True)
    with pytest.raises(P.InvalidTransition):
        P.transition(plan, "evidence", S.RUNNING, "x")                 # PENDING -> RUNNING skips READY
    with pytest.raises(P.InvalidTransition):
        P.transition(plan, "evidence", S.COMPLETED, "x")               # never COMPLETED without running
    with pytest.raises(P.InvalidTransition, match="dependencies not met"):
        P.transition(plan, "investigation", S.READY, "x")              # Evidence has not completed
    P.transition(plan, "evidence", S.READY, "x")
    P.transition(plan, "evidence", S.RUNNING, "x")
    P.transition(plan, "evidence", S.FAILED, "x", "invalid report")
    with pytest.raises(P.InvalidTransition):
        P.transition(plan, "evidence", S.RUNNING, "x")                 # terminal states never change
    assert P.blocking_reason(plan, "investigation") == "upstream task evidence is FAILED: invalid report"
    assert not P.dependencies_met(plan, "forensics")
    P.transition(plan, "investigation", S.BLOCKED, "blocked", P.blocking_reason(plan, "investigation"))
    with pytest.raises(P.InvalidTransition):
        P.transition(plan, "investigation", S.READY, "x")
    assert not P.dependencies_met(plan, "adjudication")                # forensics has not finished yet


def test_the_manager_releases_the_investigator_only_after_the_evidence_is_published(monkeypatch):
    from claimsleuth.blackboard import ContextRef, RunController
    monkeypatch.setenv(ENV_VAR, "off")
    m = InvestigationManager("CS-001", run_id="RUN-h-3", mcp_url="http://127.0.0.1:1/mcp", crew_card_url="x")
    m.board, m.plan = RunController("RUN-h-3", "CS-001"), P.build_plan("RUN-h-3", "CS-001", FP, shared=True)
    m._set("evidence", S.READY, "dependencies_met")
    m.on_agent("evidence_agent", "agent_started", {})
    assert m.status("evidence") == S.RUNNING
    assert m.authorise("forensic_agent") is not None                # only the Investigator is released this way
    ref = ContextRef(run_id="RUN-h-3", claim_id="CS-001", section="report:evidence_agent", run_seq=7,
                     sha256="b" * 64, fingerprint=FP)
    m.on_evidence(ref, None)
    assert (m.status("evidence"), m.status("investigation"), m.status("forensics")) == (S.COMPLETED, S.READY, S.READY)
    assert m.authorise("investigator_agent") is None and m.status("investigation") == S.RUNNING
    assert m.authorise("investigator_agent") is not None            # released once; a second start is refused
    # A failed Evidence task: the Investigator is refused and BLOCKED, never started.
    m2 = InvestigationManager("CS-001", run_id="RUN-h-4", mcp_url="http://127.0.0.1:1/mcp", crew_card_url="x")
    m2.board, m2.plan = RunController("RUN-h-4", "CS-001"), P.build_plan("RUN-h-4", "CS-001", FP, shared=True)
    m2._set("evidence", S.READY, "dependencies_met")
    m2.on_agent("evidence_agent", "agent_started", {})
    m2.on_evidence(None, "citation invoice#L7 not found in the document")
    assert m2.status("evidence") == S.FAILED
    assert m2.status("investigation") == m2.status("forensics") == S.BLOCKED
    assert "upstream task evidence is FAILED" in m2.authorise("investigator_agent")


# =========================================================================== the manager in real runs

@pytest.fixture(scope="module")
def env(dataset, tmp_path_factory):
    if not CREW_PYTHON.exists():
        pytest.skip("needs .venv-crew for the CrewAI A2A service")
    tmp = tmp_path_factory.mktemp("hier")
    db = tmp / "blackboard.sqlite3"
    previous = os.environ.get(ENV_VAR)
    os.environ[ENV_VAR] = str(db)                        # this process and the CrewAI service (inherits it)
    mcp = RunningServer(dataset[0], dataset[1], tmp)
    try:
        crew = CrewService(dataset[0], mcp.url, tmp)
    except Exception:
        mcp.stop()
        raise
    try:
        yield SimpleNamespace(db=db, mcp=mcp, crew=crew, data_dir=dataset[0], db_path=dataset[1])
    finally:
        crew.stop()
        mcp.stop()
        if previous is None:
            os.environ.pop(ENV_VAR, None)
        else:
            os.environ[ENV_VAR] = previous


def manage(env, claim_id, run_id, **kw):
    """Run one investigation through the manager directly (the entry point run_claim does the same)."""
    return asyncio.run(InvestigationManager(claim_id, run_id=run_id, mcp_url=env.mcp.url,
                                            crew_card_url=kw.pop("crew_card_url", env.crew.card),
                                            data_dir=env.data_dir, db_path=env.db_path, **kw).run())


def statuses(task):
    """The task's sequence of states (state changes only)."""
    out = []
    for e in task.events:
        if not out or out[-1] != e.status:
            out.append(e.status)
    return out


def tasks(bundle):
    return {t.task_id: t for t in bundle.execution_plan.tasks}


def manager_entries(env, bundle):
    return [e for e in Blackboard(env.db).entries(bundle.run_id, bundle.claim_id) if e.section.startswith("manager:")]


@pytest.fixture(scope="module")
def happy(env):
    return run_claim("CS-003", run_id="RUN-hier-happy", mcp_url=env.mcp.url, crew_card_url=env.crew.card,
                     data_dir=env.data_dir, db_path=env.db_path)


def test_the_investigation_entry_point_runs_through_the_manager(env, happy):
    assert happy.execution_plan is not None and happy.context_sharing == "blackboard"
    assert next(a for a in happy.audit if a.event == "run_started").data["orchestration"] == "hierarchical manager"
    assert {t.task_id: t.status for t in happy.execution_plan.tasks} == {
        "baseline": S.COMPLETED, "evidence": S.COMPLETED, "investigation": S.COMPLETED, "forensics": S.COMPLETED,
        "adjudication": S.COMPLETED}
    entries = manager_entries(env, happy)
    plan_entry = next(e for e in entries if e.section == "manager:plan")
    assert [t["task_id"] for t in plan_entry.payload["tasks"]] == ["baseline", "evidence", "investigation",
                                                                     "forensics", "adjudication"]
    assert {(e.principal, e.auth, e.framework) for e in entries} == {("orchestrator", "valid", "manager")}
    assert happy.final_report.review_status == "COMPLETE"
    # The plan (and every manager entry) holds identifiers, states and summaries - no document text.
    _, docs = load_claim("CS-003", env.data_dir)
    lines = [text.strip() for d in docs for _, text in d.numbered_lines() if len(text.strip()) > 15]
    recorded = happy.execution_plan.model_dump_json() + json.dumps([e.payload for e in entries])
    assert lines and not [text for text in lines if text in recorded]


def test_evidence_runs_first_and_both_downstream_tasks_wait_for_its_publication(env, happy):
    t = tasks(happy)
    ev_done = next(e for e in t["evidence"].events if e.status == S.COMPLETED)
    assert ev_done.event == "report_published" and t["evidence"].result_ref is not None
    for task_id in ("investigation", "forensics"):
        assert statuses(t[task_id])[:2] == [S.READY, S.RUNNING]
        ready = next(e for e in t[task_id].events if e.status == S.READY)
        running = next(e for e in t[task_id].events if e.status == S.RUNNING)
        assert ev_done.ts <= ready.ts <= running.ts
    assert next(e for e in t["investigation"].events if e.status == S.RUNNING).detail.startswith(
        "the manager released the Investigator")
    # On the blackboard: Evidence COMPLETED, then the Investigator's release, then its context receipt.
    entries = Blackboard(env.db).entries(happy.run_id, "CS-003")
    seq = lambda pred: next(e.run_seq for e in entries if pred(e))
    published = seq(lambda e: e.section == "report:evidence_agent")
    released = seq(lambda e: e.section == "manager:task:investigation" and e.payload["status"] == "RUNNING")
    consumed = seq(lambda e: e.section == RECEIPT_PREFIX + "investigator_agent")
    dispatched = seq(lambda e: e.section == "manager:task:forensics" and e.payload["status"] == "RUNNING")
    assert published < released < consumed and published < dispatched
    assert happy.messages[0].timestamp >= ev_done.ts                   # the A2A request was sent after publication


def test_investigator_and_forensic_execute_concurrently_after_evidence(happy):
    inv = next(r for r in happy.adk.agent_runs if r.agent == "investigator_agent")
    request, response = happy.messages[0].timestamp, happy.messages[1].timestamp
    evidence_done = next(e.ts for e in tasks(happy)["evidence"].events if e.status == S.COMPLETED)
    assert evidence_done <= inv.started_at and evidence_done <= request
    assert max(inv.started_at, request) < min(inv.finished_at, response)     # their executions overlap


def test_both_receive_the_authorised_context_and_neither_reads_the_other(env, happy):
    t = tasks(happy)
    ref = t["evidence"].result_ref
    for task_id in ("investigation", "forensics"):
        consumed = [e for e in t[task_id].events if e.event == "context_consumed"]
        assert consumed and all(e.ref == ref for e in consumed)
    reads = [(e.author, e.payload["source_section"]) for e in Blackboard(env.db).entries(happy.run_id, "CS-003")
             if e.section.startswith(RECEIPT_PREFIX) and e.author != "adjudicator"]
    assert sorted(reads) == [("forensic_agent", "report:evidence_agent"), ("investigator_agent", "report:evidence_agent")]


def test_the_manager_tracks_each_tasks_real_lifecycle(env, happy):
    t = tasks(happy)
    assert statuses(t["evidence"]) == [S.READY, S.RUNNING, S.COMPLETED]
    assert statuses(t["investigation"]) == statuses(t["forensics"]) == [S.READY, S.RUNNING, S.COMPLETED]
    events = {task_id: [e.event for e in task.events] for task_id, task in t.items()}
    assert {"agent_started", "agent_finished", "report_published", "tools_executed", "report_verified"} <= set(events["evidence"])
    assert {"dispatched", "context_consumed", "agent_started", "agent_finished", "tools_executed",
            "report_verified"} <= set(events["investigation"])
    assert {"dispatched", "context_consumed", "tools_executed", "report_returned", "report_verified"} <= set(events["forensics"])
    assert events["adjudication"][:4] == ["blackboard_sealed", "dependencies_met", "dispatched", "decided"]
    for task_id in ("investigation", "forensics"):              # COMPLETED only once verified on the sealed board
        done = next(e for e in t[task_id].events if e.status == S.COMPLETED)
        assert done.event == "report_verified"
    assert t["forensics"].result["report_sha256"] == report_sha256(happy.crewai.report)
    assert t["investigation"].result["report_sha256"] == report_sha256(happy.adk.report)
    view = run_monitor(happy.run_id, path=env.db)               # the dashboard's monitoring view
    assert {x["task_id"]: x["status"] for x in view["tasks"]} == {k: "COMPLETED" for k in t}


def test_valid_reports_are_collected_and_sealed_before_adjudication(env, happy):
    state = Blackboard(env.db).state(happy.run_id, "CS-003")
    entries = Blackboard(env.db).entries(happy.run_id, "CS-003")
    adjudicator_reads = [e for e in entries if e.section == RECEIPT_PREFIX + "adjudicator"]
    adjudication_ready = next(e for e in entries if e.section == "manager:task:adjudication" and e.payload["status"] == "READY")
    assert state.sealed_at < adjudicator_reads[0].run_seq < adjudication_ready.run_seq
    decided = next(e for e in entries if e.section == "manager:task:adjudication" and e.payload["status"] == "COMPLETED")
    decision = next(e for e in entries if e.section == "decision:final")
    assert decided.run_seq < decision.run_seq
    assert tasks(happy)["adjudication"].result["final_decision"] == happy.final_report.final_decision.value


def test_invalid_evidence_blocks_every_downstream_task(env, monkeypatch):
    from claimsleuth.adk_service import test_double as adk_doubles
    real = adk_doubles.offline_models
    fabricated = evidence_report(evidence_citations=[{"doc_id": "invoice", "line": 7, "quote": "Purchase Date: 1999-01-01"}])
    investigator = ScriptedLlm(script=[])
    monkeypatch.setattr(adk_doubles, "offline_models", lambda: {
        **real(), "evidence_agent": ScriptedLlm(script=[call("warranty_policy_search", policy_id="POL-TECH-CARE-01"),
                                                        call("set_model_response", **fabricated)]),
        "investigator_agent": investigator})
    bundle = manage(env, "CS-007", "RUN-hier-badevidence")
    t = tasks(bundle)
    assert statuses(t["evidence"]) == [S.READY, S.RUNNING, S.FAILED]
    assert "not found in the document" in t["evidence"].failure
    assert t["investigation"].status == t["forensics"].status == S.BLOCKED
    assert "upstream task evidence is FAILED" in t["investigation"].failure
    assert investigator.requests == [] and bundle.messages == []      # nothing was delegated downstream
    final = bundle.final_report
    assert final.review_status == "NONE" and final.requires_human_approval
    assert "escalated_to_human" in [e.event for e in t["adjudication"].events]


def test_an_a2a_failure_fails_the_forensic_task_and_keeps_the_adk_results(env):
    dead = f"http://127.0.0.1:{_free_port()}/.well-known/agent-card.json"
    bundle = manage(env, "CS-001", "RUN-hier-noa2a", crew_card_url=dead, a2a_timeout=30)
    t = tasks(bundle)
    assert statuses(t["forensics"]) == [S.READY, S.RUNNING, S.FAILED] and "A2A" in t["forensics"].failure
    assert t["investigation"].status == t["evidence"].status == S.COMPLETED
    final = bundle.final_report
    assert final.review_status == "PARTIAL" and final.requires_human_approval and final.approval_status == "PENDING"
    assert "escalated_to_human" in [e.event for e in t["adjudication"].events]


def test_a_forensic_timeout_is_tracked_as_timed_out(env, monkeypatch):
    async def never_answers(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(manager_mod, "request_forensics", never_answers)
    bundle = manage(env, "CS-003", "RUN-hier-crewtimeout", deadlines={"forensics": 2})
    t = tasks(bundle)
    assert statuses(t["forensics"]) == [S.READY, S.RUNNING, S.TIMED_OUT]
    assert "deadline of 2 s" in t["forensics"].failure and bundle.crewai.status == ExecutionStatus.FAILED
    assert t["investigation"].status == S.COMPLETED and bundle.final_report.review_status == "PARTIAL"


class BlockedLlm(ScriptedLlm):
    """A model call that never returns (to provoke a timeout)."""

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        await asyncio.sleep(3600)
        yield  # pragma: no cover


def test_an_investigator_timeout_is_tracked_as_timed_out(env, monkeypatch):
    from claimsleuth.adk_service import test_double as adk_doubles
    real = adk_doubles.offline_models
    monkeypatch.setattr(adk_doubles, "offline_models", lambda: {**real(), "investigator_agent": BlockedLlm()})
    bundle = manage(env, "CS-003", "RUN-hier-adktimeout", deadlines={"adk": 15})
    t = tasks(bundle)
    assert t["evidence"].status == S.COMPLETED                     # it had published before the deadline
    assert statuses(t["investigation"]) == [S.READY, S.RUNNING, S.TIMED_OUT]
    assert "deadline of 15 s" in t["investigation"].failure
    assert t["forensics"].status == S.COMPLETED                    # the parallel task was not affected
    final = bundle.final_report
    assert final.review_status == "PARTIAL" and final.requires_human_approval


def test_a_blackboard_verification_failure_is_never_adjudicated(env, monkeypatch):
    real = manager_mod.request_forensics

    async def altered_in_transit(*args, **kwargs):
        exchange = await real(*args, **kwargs)
        report = exchange.result.report.model_copy(update={"recommended_outcome": Decision.REJECT})
        exchange.result = exchange.result.model_copy(update={"report": report})
        return exchange

    monkeypatch.setattr(manager_mod, "request_forensics", altered_in_transit)
    bundle = manage(env, "CS-003", "RUN-hier-unverified")
    t = tasks(bundle)
    assert statuses(t["forensics"]) == [S.READY, S.RUNNING, S.FAILED]
    assert next(e for e in t["forensics"].events if e.status == S.FAILED).event == "verification_failed"
    final = bundle.final_report
    assert final.review_status == "PARTIAL" and final.requires_human_approval
    assert any(r.startswith("CREWAI review unavailable") for r in final.incomplete_reasons)   # the altered report
    assert not [a for a in final.agreements + final.disagreements if a.startswith("CREWAI")]  # was never used


def test_a_manager_failure_never_produces_an_approval(env, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("MCP record service exploded")

    monkeypatch.setattr(manager_mod, "fetch_mcp_records", broken)
    with pytest.raises(ManagerError, match="no decision was produced"):
        manage(env, "CS-001", "RUN-hier-managerfail")              # CS-001: the rules would approve it
    entries = Blackboard(env.db).entries("RUN-hier-managerfail", "CS-001")
    assert not [e for e in entries if e.section == "decision:final"]
    failure = next(e for e in entries if e.section == "manager:failure")
    assert "MCP record service exploded" in failure.payload["detail"] and failure.payload["decision"] == "none produced"
    adjudication = [e for e in entries if e.section == "manager:task:adjudication"]
    assert adjudication[-1].payload["status"] == "FAILED"


def test_direct_mode_still_runs_through_the_manager(env, monkeypatch, happy):
    monkeypatch.setenv(ENV_VAR, "off")
    bundle = manage(env, "CS-003", "RUN-hier-direct")
    t = tasks(bundle)
    assert bundle.context_sharing == "direct" and bundle.execution_plan.context_sharing == "direct"
    assert t["forensics"].depends_on == [] and {x.status for x in t.values()} == {S.COMPLETED}
    assert bundle.messages[0].payload.context == []
    pick = lambda f: (f.final_decision, f.decided_by, f.review_status, f.requires_human_approval)
    assert pick(bundle.final_report) == pick(happy.final_report)
    assert Blackboard(env.db).registration("RUN-hier-direct") is None


def test_the_offline_evaluation_runs_through_the_manager_with_unchanged_decisions(evaluation):
    result, tmp = evaluation
    rows, eval_id = result["rows"], result["meta"]["evaluation_id"]
    assert {r["case_id"]: r["combined"]["final_decision"] for r in rows} == {r["case_id"]: r["expected_decision"] for r in rows}
    for row in rows:
        plan = ExecutionPlan.model_validate_json(
            (tmp / "evaluation" / eval_id / "runs" / row["run_id"] / "execution_plan.json").read_text(encoding="utf-8"))
        assert {t.task_id: t.status for t in plan.tasks} == {
            "baseline": S.COMPLETED, "evidence": S.COMPLETED, "investigation": S.COMPLETED,
            "forensics": S.COMPLETED, "adjudication": S.COMPLETED}, row["case_id"]
