"""The CrewAI side of the execution trace, inside .venv-crew against a real MCP server: lifecycle events from the
installed CrewAI event bus (crew, task, agent, guardrail, tool), guardrail rejections, model calls, timed blackboard
operations - and a result that still serialises for its A2A response."""
import pytest

pytest.importorskip("crewai", reason="CrewAI tests run in .venv-crew")

from claimsleuth.blackboard import ENV_VAR, AgentParticipant, RunController, provision  # noqa: E402
from claimsleuth.crew_service.runner import run_forensic_investigation  # noqa: E402
from claimsleuth.crew_service.test_double import MODEL_NAME, ForensicTestDoubleLLM  # noqa: E402
from claimsleuth.datastore import load_claim  # noqa: E402
from claimsleuth.schemas import AgentSystemResult, EvidenceReport, ExecutionStatus  # noqa: E402

CLAIM = "CS-003"


def recorded(result, key):
    return [item for t in result.trace if isinstance(t.get(key), list) for item in t[key]]


def test_crewai_lifecycle_events_are_recorded_and_the_result_serialises(mcp, dataset, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "off")
    result = run_forensic_investigation(CLAIM, run_id="RUN-obscrew-1", mcp_url=mcp.url, data_dir=dataset[0])
    assert result.status == ExecutionStatus.COMPLETED
    AgentSystemResult.model_validate_json(result.model_dump_json())     # the A2A response can carry it
    events = recorded(result, "crew_events")
    kinds = [e["event"] for e in events]
    for kind in ("crew.kickoff_started", "task.started", "agent.execution_started", "guardrail.started",
                 "guardrail.completed", "agent.execution_completed", "task.completed", "crew.kickoff_completed"):
        assert kind in kinds, kind
    assert kinds.index("crew.kickoff_started") < kinds.index("task.started") < kinds.index("task.completed") \
        < kinds.index("crew.kickoff_completed")
    assert all(isinstance(e["event"], str) and e["ts"] and e["event_id"] for e in events)
    assert [e["tool"] for e in events if e["event"] == "tool.started"] == ["claim_history_lookup"] * 2
    calls = recorded(result, "model_calls")
    assert len(calls) == result.agent_runs[0].llm_calls and all(c["latency_ms"] is not None for c in calls)
    assert all(c["prompt_tokens"] is None for c in calls)                # the test double reports no usage


def test_a_guardrail_rejection_is_recorded(mcp, dataset, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "off")
    llm = ForensicTestDoubleLLM(model=MODEL_NAME, mode="fabricated_citation")
    result = run_forensic_investigation(CLAIM, run_id="RUN-obscrew-2", llm=llm, mcp_url=mcp.url, data_dir=dataset[0])
    assert result.status == ExecutionStatus.FAILED
    checks = [e for e in recorded(result, "crew_events") if e["event"] == "guardrail.completed"]
    assert checks and all(e["status"] == "rejected" and e["success"] is False for e in checks)
    assert [e["retry_count"] for e in checks] == list(range(len(checks)))
    assert "do not match the documents" in checks[0]["error"]


def test_blackboard_operations_are_timed_on_the_crewai_side(mcp, dataset, tmp_path, monkeypatch):
    db = tmp_path / "blackboard.sqlite3"
    monkeypatch.setenv(ENV_VAR, str(db))
    provision(db)
    _, documents = load_claim(CLAIM, dataset[0])
    RunController("RUN-obscrew-3", CLAIM, path=db).register(documents)
    adk = AgentParticipant("RUN-obscrew-3", CLAIM, "adk", ("evidence_agent", "investigator_agent"), path=db)
    adk.running(documents, mark_running=False)
    adk.agent_started("evidence_agent")
    ref = adk.publish_report("evidence_agent", EvidenceReport.model_validate({
        "claim_id": CLAIM, "facts": [], "findings": [], "evidence_citations": [], "policy_references": [],
        "missing_information": [], "confidence": 0.8, "recommended_decision": "ESCALATE", "reasoning_summary": "x"}))
    result = run_forensic_investigation(CLAIM, run_id="RUN-obscrew-3", mcp_url=mcp.url, data_dir=dataset[0], context=[ref])
    ops = recorded(result, "blackboard_ops")
    # The consumption is timed. The Forensic report itself is posted when the runner finishes, after this execution
    # record was closed, so its publication appears in the trace from the blackboard entry, without a latency.
    assert [(o["op"], o["section"], o["actor"], o["ok"]) for o in ops] == [
        ("consume", "report:evidence_agent", "forensic_agent", True)]
    assert all(o["latency_ms"] >= 0 and o["run_seq"] for o in ops)
    bad = run_forensic_investigation(CLAIM, run_id="RUN-obscrew-3", mcp_url=mcp.url, data_dir=dataset[0],
                                     context=[ref.model_copy(update={"sha256": "0" * 64})])
    rejected, = recorded(bad, "blackboard_ops")
    assert rejected["ok"] is False and "not the expected" in rejected["error"]
