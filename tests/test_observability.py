"""Observability: the unified run trace, its sources (ADK callbacks, the CrewAI event bus, the MCP server log, A2A
messages, blackboard entries and timed blackboard operations, the manager's plan), measured metrics, verified-only
pricing, isolation of concurrent runs and monitoring that can never change an investigation.

Everything runs offline: the MCP server, the ADK Runner, the CrewAI A2A service process and the blackboard are real;
the models are labelled test doubles (and, where token usage is tested, a scripted model that reports usage).
"""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from adk_test_double import ScriptedLlm, call
from claimsleuth.blackboard import ENV_VAR, RunController
from claimsleuth.datastore import load_claim
from claimsleuth.integration.run import run_claim, save_bundle
from claimsleuth.observability import pricing
from claimsleuth.observability import trace as trace_mod
from claimsleuth.observability.metrics import agent_metrics, stats
from claimsleuth.observability.trace import collect_trace
from claimsleuth.observability.trace_util import aware, ms_between, overlap_ms
from claimsleuth.orchestration import manager as manager_mod
from claimsleuth.schemas import Decision, ExecutionStatus, RunTrace, TaskStatus, TraceEvent
from test_adk_investigation import evidence_report, investigation_report
from test_hierarchical_orchestration import env, manage  # noqa: F401  (module fixture: MCP + CrewAI + blackboard)
from test_integration_e2e import CREW_PYTHON

AGENT_TASK = {"evidence_agent": "evidence", "investigator_agent": "investigation", "forensic_agent": "forensics"}
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
EVENT = dict(run_id="RUN-obs-1", claim_id="CS-001", event_id="a" * 32, correlation_id="trace-RUN-obs-1",
             component="adk", event_type="agent.start", timestamp=NOW, status="ok")


# =========================================================================== the trace model

def test_a_valid_trace_event_is_timezone_aware():
    event = TraceEvent(**EVENT, task_id="evidence", agent_name="evidence_agent", duration_ms=12.5,
                       metadata={"tool": "warranty_policy_search", "calls": 2, "usage_reported": False})
    assert event.timestamp.tzinfo is not None and event.timestamp.utcoffset() == timedelta(0)
    assert TraceEvent.model_validate_json(event.model_dump_json()) == event


@pytest.mark.parametrize("change", [
    {"timestamp": datetime(2026, 11, 4, 10, 0)},                              # naive timestamp
    {"event_type": "Agent Start"}, {"event_id": "not-hex"}, {"component": "llm-manager"},
    {"status": "done"}, {"duration_ms": -1}, {"task_id": "summary"},
    {"metadata": {"api_key": "x"}}, {"metadata": {"google_api_key_value": "x"}}, {"metadata": {"Authorization": "x"}},
    {"metadata": {"payload": {"nested": "dict"}}}, {"metadata": {"document": "x" * 301}},
    {"error_code": "AGENT_FAILED"},                                            # an error code on an ok event
    {"unexpected_field": 1}])
def test_invalid_trace_events_are_rejected(change):
    with pytest.raises(ValidationError):
        TraceEvent(**{**EVENT, **change})


def test_durations_overlap_and_statistics():
    assert ms_between(NOW, NOW + timedelta(milliseconds=1500)) == 1500.0
    assert ms_between("2026-11-04T10:00:00+00:00", "2026-11-04T10:00:02.250000+00:00") == 2250.0
    assert ms_between(NOW, None) is None
    assert aware(datetime(2026, 11, 4)).tzinfo == timezone.utc and aware(0).year == 1970
    a, b = (NOW, NOW + timedelta(seconds=4)), (NOW + timedelta(seconds=3), NOW + timedelta(seconds=9))
    assert overlap_ms(a, b) == 1000.0 and overlap_ms(a, (NOW + timedelta(seconds=5), NOW + timedelta(seconds=6))) == 0.0
    s = stats([10, 30, 20, None])
    assert (s.count, s.min_ms, s.max_ms, s.mean_ms, s.median_ms) == (3, 10, 30, 20, 20)
    assert stats([None]) is None


def test_cost_is_calculated_only_with_verified_pricing(tmp_path, monkeypatch):
    monkeypatch.delenv(pricing.ENV_VAR, raising=False)
    table, note = pricing.load_pricing()
    assert table is None and "no verified pricing" in note
    assert pricing.cost("gemini-3.5-flash", 1000, 200, None, None, note) == (None, None, note)
    good = tmp_path / "prices.json"
    good.write_text(json.dumps({"models": {"gemini-x": {
        "input_per_million": 1.0, "output_per_million": 4.0, "cached_input_per_million": 0.25, "currency": "USD",
        "source": "test fixture price list", "verified_on": "2026-11-04"}}}), encoding="utf-8")
    table, note = pricing.load_pricing(good)
    assert pricing.cost("gemini-x", 1_000_000, 500_000, 200_000, table, note)[:2] == (0.8 + 0.05 + 2.0, "USD")
    assert pricing.cost("gemini-x", None, 10, None, table, note)[0] is None                 # usage not reported
    assert pricing.cost("other-model", 10, 10, None, table, note)[0] is None                # no verified price
    assert pricing.cost("test-double", 10, 10, None, table, note)[0] is None                # offline: never billed
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"models": {"gemini-x": {"input_per_million": 1.0, "output_per_million": 4.0,
                                                        "currency": "USD"}}}), encoding="utf-8")   # no source / date
    table, note = pricing.load_pricing(bad)
    assert table is None and note.startswith("pricing file not used")


# =========================================================================== the trace of a real offline run

@pytest.fixture(scope="module")
def happy(env):
    return run_claim("CS-003", run_id="RUN-obs-happy", mcp_url=env.mcp.url, crew_card_url=env.crew.card,
                     data_dir=env.data_dir, db_path=env.db_path)


def events(bundle, **match):
    return [e for e in bundle.trace.events if all(getattr(e, k) == v for k, v in match.items())]


def test_the_run_has_one_correlated_trace_from_real_events(happy):
    tr = happy.trace
    assert tr.collection_errors == [] and len(tr.events) > 50
    assert {e.run_id for e in tr.events} == {happy.run_id} and {e.correlation_id for e in tr.events} == {f"trace-{happy.run_id}"}
    assert len({e.event_id for e in tr.events}) == len(tr.events)
    assert [e.timestamp for e in tr.events] == sorted(e.timestamp for e in tr.events)
    ids = {e.event_id for e in tr.events}
    assert all(e.parent_event_id in ids for e in tr.events if e.parent_event_id)            # parents exist
    assert tr.metrics.execution == "offline test double"
    assert {"manager", "baseline", "adk", "crewai", "mcp", "a2a", "blackboard", "adjudicator"} <= {e.component for e in tr.events}


def test_adk_agent_callbacks_are_traced(happy):
    runs = {r.agent: r for r in happy.adk.agent_runs}
    for agent in ("evidence_agent", "investigator_agent"):
        start, = events(happy, component="adk", event_type="agent.start", agent_name=agent)
        end, = events(happy, component="adk", event_type="agent.end", agent_name=agent)
        assert start.timestamp == runs[agent].started_at and end.duration_ms == runs[agent].latency_ms
        assert end.parent_event_id == start.event_id and start.task_id == AGENT_TASK[agent]
        root = next(e for e in happy.trace.events if e.event_id == start.parent_event_id)
        assert root.component == "manager" and root.task_id == AGENT_TASK[agent]
        model_calls = events(happy, component="adk", event_type="model.call", agent_name=agent)
        assert len(model_calls) == runs[agent].llm_calls and all(m.parent_event_id == start.event_id for m in model_calls)
        assert all(m.duration_ms is not None for m in model_calls)


def test_adk_tool_calls_are_traced_with_safe_metadata(happy):
    calls = events(happy, component="adk", event_type="tool.call")
    assert len(calls) == len(happy.adk.tool_calls)
    assert {e.metadata["tool"] for e in calls} >= {"warranty_policy_search", "claim_history_lookup"}
    lookup = next(e for e in calls if e.metadata["tool"] == "claim_history_lookup")
    assert lookup.duration_ms is not None and lookup.metadata["argument_keys"] in ("serial_number", "claimant_id")
    recorded = happy.trace.model_dump_json()
    mcp_calls = [c for c in happy.adk.tool_calls if c.protocol == "mcp"]
    assert mcp_calls
    for c in mcp_calls:                     # MCP argument values (serials, claimant ids) are never stored
        for key, value in c.arguments.items():
            if isinstance(value, str) and len(value) > 5 and value != happy.claim_id:   # the claim id is the run's key
                assert value not in recorded, key
    assert happy.adk.report.reasoning_summary not in recorded            # nor any report text
    assert happy.adk.evidence_report.reasoning_summary not in recorded


def test_crewai_crew_task_agent_guardrail_and_tool_events_are_traced(happy):
    kinds = {e.event_type: e for e in events(happy, component="crewai")}
    assert {"crew.kickoff_started", "crew.kickoff_completed", "task.started", "task.completed",
            "agent.execution_started", "agent.execution_completed", "guardrail.started", "guardrail.completed",
            "tool.started", "tool.call", "model.call", "agent.start", "agent.end"} <= set(kinds)
    assert kinds["guardrail.completed"].status == "ok" and kinds["guardrail.completed"].metadata["retry_count"] == 0
    assert all(e.metadata.get("framework_event_id") for e in events(happy, component="crewai")
               if e.event_type.startswith(("crew.", "task.", "agent.execution", "guardrail.")))
    started = next(e.timestamp for e in events(happy, component="crewai", event_type="crew.kickoff_started"))
    done = next(e.timestamp for e in events(happy, component="crewai", event_type="crew.kickoff_completed"))
    assert started < done
    assert len(events(happy, component="crewai", event_type="model.call")) == happy.crewai.agent_runs[0].llm_calls


def test_mcp_calls_are_traced_from_the_server_log(happy):
    calls = events(happy, component="mcp", event_type="mcp.call")
    assert len(calls) == len(happy.mcp.calls) == happy.trace.metrics.mcp_calls
    assert [e.duration_ms for e in calls] == [c.latency_ms for c in sorted(happy.mcp.calls, key=lambda c: c.started_at)]
    assert {(e.agent_name, e.task_id) for e in calls} <= {(a, t) for a, t in AGENT_TASK.items()}
    assert {e.metadata["caller"] for e in calls} == {"adk:evidence_agent", "adk:investigator_agent", "crewai:forensic_agent"}


def test_a2a_dispatch_and_response_are_traced(happy):
    dispatch, = events(happy, event_type="a2a.dispatch")
    response, = events(happy, event_type="a2a.response")
    request = happy.messages[0]
    assert dispatch.metadata["message_id"] == request.message_id and dispatch.context_ref == request.payload.context[0]
    assert response.metadata["in_reply_to"] == request.message_id and response.status == "ok"
    assert response.duration_ms == ms_between(request.timestamp, happy.messages[1].timestamp)
    assert happy.trace.metrics.a2a_latency.median_ms == response.duration_ms


def test_blackboard_publication_provenance(happy, env):
    from claimsleuth.blackboard import Blackboard
    posts = {e.section: e for e in Blackboard(env.db).entries(happy.run_id, "CS-003") if e.kind == "post"}
    published = events(happy, event_type="blackboard.publish")
    assert {e.metadata["section"] for e in published} == {"report:evidence_agent", "report:investigator_agent",
                                                           "report:forensic_agent"}
    for e in published:
        post = posts[e.metadata["section"]]
        assert (e.metadata["producer"], e.metadata["run_seq"], e.metadata["principal"]) == (post.author, post.run_seq, post.principal)
        assert e.metadata["sha256"] == post.payload_sha256[:16] and e.context_ref.sha256 == post.payload_sha256
    evidence = next(e for e in published if e.metadata["section"] == "report:evidence_agent")
    assert evidence.duration_ms is not None                    # measured publish operation (ADK side)
    assert happy.trace.metrics.blackboard_publish.count >= 1


def test_blackboard_consumption_provenance(happy):
    consumed = events(happy, event_type="blackboard.consume")
    pairs = sorted((e.metadata["consumer"], e.metadata["section"]) for e in consumed)
    assert pairs == [("adjudicator", "report:evidence_agent"), ("adjudicator", "report:forensic_agent"),
                     ("adjudicator", "report:investigator_agent"), ("forensic_agent", "report:evidence_agent"),
                     ("investigator_agent", "report:evidence_agent")]
    assert {e.metadata["receipt"] for e in consumed} == {"signed"} and all(e.duration_ms is not None for e in consumed)
    forensic = next(e for e in consumed if e.metadata["consumer"] == "forensic_agent")
    assert forensic.metadata["principal"] == "crewai" and forensic.metadata["producer"] == "evidence_agent"
    m = happy.trace.metrics
    assert m.blackboard_consume.count == 2 and m.blackboard_verify.count == 3 and m.blackboard_seal.count == 1


def test_concurrent_agents_have_separate_parents_and_overlap(happy):
    roots = {}
    for e in happy.trace.events:
        if e.component == "manager" and e.metadata.get("task_status") == "RUNNING" and e.task_id not in roots:
            roots[e.task_id] = e.event_id
    assert roots["investigation"] != roots["forensics"]
    by_id = {e.event_id: e for e in happy.trace.events}

    def root_of(e):
        while e.parent_event_id and by_id[e.parent_event_id].component != "manager":
            e = by_id[e.parent_event_id]
        return e.parent_event_id

    for e in happy.trace.events:
        if e.component in ("adk", "crewai") and e.agent_name in AGENT_TASK:
            assert e.task_id == AGENT_TASK[e.agent_name]
            assert root_of(e) == roots[AGENT_TASK[e.agent_name]]
    m = happy.trace.metrics
    assert m.parallel_overlap_ms > 0 and m.agents_elapsed_ms < m.agents_sequential_sum_ms   # not shown as sequential
    durations = {t.task_id: t for t in m.tasks}
    assert durations["investigation"].execution_ms == next(r.latency_ms for r in happy.adk.agent_runs
                                                           if r.agent == "investigator_agent")


def test_missing_model_usage_is_unavailable_not_zero(happy):
    for usage in happy.trace.metrics.models:
        assert usage.execution == "offline test double" and usage.usage_reported_calls == 0
        assert usage.prompt_tokens is usage.completion_tokens is usage.total_tokens is None
        assert usage.cost is None and "offline test double" in usage.cost_note
    assert all(e.metadata["usage_reported"] is False and e.metadata["prompt_tokens"] is None
               for e in events(happy, event_type="model.call"))


def test_no_documents_prompts_or_secrets_are_recorded(env, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test-only-not-a-real-key-123456")
    bundle = manage(env, "CS-001", "RUN-obs-secrets")
    recorded = bundle.trace.model_dump_json()
    _, docs = load_claim("CS-001", env.data_dir)
    lines = [text.strip() for d in docs for _, text in d.numbered_lines() if len(text.strip()) > 15]
    assert lines and not [t for t in lines if t in recorded]
    assert "AIza-test-only" not in recorded and "You are the Evidence Agent" not in recorded
    assert "<untrusted" not in recorded


# =========================================================================== token usage, isolation, failures

def test_actual_token_usage_is_captured_when_reported(env, monkeypatch):
    from claimsleuth.adk_service import test_double as adk_doubles
    real = adk_doubles.offline_models
    # Scripted models that report usage_metadata (100 prompt / 20 completion tokens per call), as a provider would.
    scripted = {"evidence_agent": ScriptedLlm(script=[call("warranty_policy_search", policy_id="POL-TECH-CARE-01"),
                                                      call("set_model_response", **evidence_report())]),
                "investigator_agent": ScriptedLlm(script=[call("claim_history_lookup", serial_number="NMB-X15-551230"),
                                                          call("claim_history_lookup", claimant_id="USR-7450"),
                                                          call("set_model_response", **investigation_report())])}
    monkeypatch.setattr(adk_doubles, "offline_models", lambda: {**real(), **scripted})
    bundle = manage(env, "CS-007", "RUN-obs-tokens")
    usage = {u.agent: u for u in bundle.trace.metrics.models}
    assert (usage["evidence_agent"].calls, usage["evidence_agent"].usage_reported_calls) == (2, 2)
    assert (usage["evidence_agent"].prompt_tokens, usage["evidence_agent"].completion_tokens,
            usage["evidence_agent"].total_tokens) == (200, 40, 240)
    assert usage["investigator_agent"].prompt_tokens == 300
    assert usage["forensic_agent"].prompt_tokens is None                     # its test double reports nothing
    calls = events(bundle, event_type="model.call", agent_name="evidence_agent")
    assert [c.metadata["prompt_tokens"] for c in calls] == [100, 100] and all(c.metadata["usage_reported"] for c in calls)


def test_concurrent_investigations_stay_isolated(env):
    async def both():
        return await asyncio.gather(*(manager_mod.InvestigationManager(
            claim, run_id=run_id, mcp_url=env.mcp.url, crew_card_url=env.crew.card, data_dir=env.data_dir,
            db_path=env.db_path).run() for claim, run_id in (("CS-001", "RUN-obs-par-a"), ("CS-006", "RUN-obs-par-b"))))

    first, second = asyncio.run(both())
    for bundle in (first, second):
        tr = bundle.trace
        assert {e.run_id for e in tr.events} == {bundle.run_id} and {e.claim_id for e in tr.events} == {bundle.claim_id}
        assert tr.collection_errors == []
        assert len(events(bundle, event_type="mcp.call")) == len(bundle.mcp.calls)
        assert all(c.run_id == bundle.run_id for c in bundle.mcp.calls)
        for e in tr.events:
            if e.agent_name in AGENT_TASK and e.component in ("adk", "crewai", "mcp"):
                assert e.task_id == AGENT_TASK[e.agent_name]
    assert not {e.event_id for e in first.trace.events} & {e.event_id for e in second.trace.events}


def test_adk_model_and_agent_errors_are_traced(env, monkeypatch):
    from claimsleuth.adk_service import test_double as adk_doubles
    real = adk_doubles.offline_models
    monkeypatch.setattr(adk_doubles, "offline_models", lambda: {**real(), "investigator_agent": ScriptedLlm(script=[])})
    bundle = manage(env, "CS-003", "RUN-obs-adkerror")              # the Investigator's model call raises
    model_error = [e for e in events(bundle, event_type="model.call", agent_name="investigator_agent") if e.status == "error"]
    assert model_error and model_error[0].error_code == "MODEL_ERROR" and model_error[0].metadata["model"]
    agent_error, = events(bundle, event_type="agent.error", agent_name="investigator_agent")   # ADK's on_agent_error
    assert (agent_error.error_code, agent_error.metadata["error_type"]) == ("AGENT_EXCEPTION", "RuntimeError")
    assert agent_error.task_id == "investigation" and agent_error.error_category == "agent"
    failed, = events(bundle, event_type="task.failed", task_id="investigation")
    assert failed.error_category == "agent"


def test_a_timeout_is_recorded(env, monkeypatch):
    async def never_answers(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(manager_mod, "request_forensics", never_answers)
    bundle = manage(env, "CS-003", "RUN-obs-timeout", deadlines={"forensics": 2})
    no_response, = events(bundle, event_type="a2a.no_response")
    assert (no_response.status, no_response.error_code, no_response.error_category) == ("timeout", "A2A_TIMEOUT", "integration")
    timed_out, = events(bundle, event_type="task.timed_out")
    assert timed_out.task_id == "forensics" and timed_out.status == "timeout"
    tasks = {t.task_id: t for t in bundle.trace.metrics.tasks}
    assert tasks["forensics"].status == TaskStatus.TIMED_OUT and tasks["forensics"].lifecycle_ms >= 2000
    incomplete, = events(bundle, event_type="investigation.incomplete")
    assert incomplete.error_category == "business"


def test_failures_are_recorded_by_category(env, monkeypatch):
    dead = "http://127.0.0.1:1/.well-known/agent-card.json"
    bundle = manage(env, "CS-001", "RUN-obs-noa2a", crew_card_url=dead, a2a_timeout=30)
    failed, = events(bundle, event_type="task.failed", task_id="forensics")
    assert (failed.error_code, failed.error_category) == ("A2A_FAILED", "integration")
    from claimsleuth.adk_service import test_double as adk_doubles
    real = adk_doubles.offline_models
    fabricated = evidence_report(evidence_citations=[{"doc_id": "invoice", "line": 7, "quote": "Purchase Date: 1999-01-01"}])
    monkeypatch.setattr(adk_doubles, "offline_models", lambda: {**real(), "evidence_agent": ScriptedLlm(script=[
        call("warranty_policy_search", policy_id="POL-TECH-CARE-01"), call("set_model_response", **fabricated)])})
    bad = manage(env, "CS-007", "RUN-obs-badevidence")
    evidence_failed, = events(bad, event_type="task.failed", task_id="evidence")
    assert (evidence_failed.error_code, evidence_failed.error_category) == ("AGENT_OUTPUT_INVALID", "agent")
    blocked = events(bad, event_type="task.blocked")
    assert {e.task_id for e in blocked} == {"investigation", "forensics"}
    assert {(e.error_code, e.error_category, e.status) for e in blocked} == {("BLOCKED_UPSTREAM", "business", "blocked")}
    assert agent_metrics([bad.trace]) and {r["task_id"]: r["blocked"] for r in agent_metrics([bad.trace])}["forensics"] == 1


def test_a_monitoring_failure_does_not_change_the_adjudication(env, monkeypatch, happy):
    def broken(self, name, payload):
        raise RuntimeError("monitoring store unavailable")

    monkeypatch.setattr(RunController, "record_manager", broken)
    monkeypatch.setattr(trace_mod.TraceBuilder, "a2a", lambda self, bundle: 1 / 0)       # trace assembly fails too
    bundle = manage(env, "CS-003", "RUN-obs-monfail")
    pick = lambda f: (f.final_decision, f.decided_by, f.review_status, f.requires_human_approval, f.payout)
    assert pick(bundle.final_report) == pick(happy.final_report)
    assert {t.status for t in bundle.execution_plan.tasks} == {TaskStatus.COMPLETED}
    assert bundle.trace.events == [] and len(bundle.trace.collection_errors) >= 2
    assert any("monitoring store unavailable" in e for e in bundle.trace.collection_errors)
    assert any("trace not assembled" in e for e in bundle.trace.collection_errors)


def test_a_monitoring_failure_cannot_bypass_fail_closed_verification(env, monkeypatch):
    monkeypatch.setattr(RunController, "record_manager", lambda self, name, payload: 1 / 0)
    real = manager_mod.request_forensics

    async def altered_in_transit(*args, **kwargs):
        exchange = await real(*args, **kwargs)
        report = exchange.result.report.model_copy(update={"recommended_outcome": Decision.REJECT})
        exchange.result = exchange.result.model_copy(update={"report": report})
        return exchange

    monkeypatch.setattr(manager_mod, "request_forensics", altered_in_transit)
    bundle = manage(env, "CS-003", "RUN-obs-monfail-verify")
    tasks = {t.task_id: t for t in bundle.execution_plan.tasks}
    assert tasks["forensics"].status == TaskStatus.FAILED                      # still not verified, still not used
    assert bundle.final_report.review_status == "PARTIAL" and bundle.final_report.requires_human_approval
    assert any(r.startswith("CREWAI review unavailable") for r in bundle.final_report.incomplete_reasons)
    assert bundle.trace.collection_errors                                      # the monitoring failure is visible
    assert {e.error_category for e in events(bundle, component="monitoring")} == {"monitoring"}


def test_collect_trace_never_raises(happy):
    broken = happy.model_copy(update={"execution_plan": None, "messages": "not a list"})
    trace = collect_trace(broken, [], [], ["monitoring: earlier problem"])
    assert isinstance(trace, RunTrace) and trace.events == [] and trace.collection_errors[0] == "monitoring: earlier problem"


# =========================================================================== dashboard endpoints and regression

def test_dashboard_monitoring_endpoints_return_the_stored_events(happy, env, tmp_path, dataset):
    from starlette.testclient import TestClient

    from claimsleuth.dashboard.app import create_app
    from claimsleuth.dashboard.services import ServiceConfig
    from claimsleuth.dashboard.workspace import Workspace
    save_bundle(happy, tmp_path / "rt")
    old = happy.model_copy(update={"run_id": "RUN-obs-old", "trace": None})     # a run stored before tracing
    save_bundle(old, tmp_path / "rt")
    ws = Workspace(tmp_path / "dash", source_data=dataset[0])
    with TestClient(create_app(ws, ServiceConfig(mcp_port=env.mcp.port, crew_port=env.crew.port), tmp_path / "rt")) as client:
        view = client.get(f"/api/runs/{happy.run_id}/trace").json()
        assert view["available"] and len(view["events"]) == len(happy.trace.events)
        assert [e["event_id"] for e in view["events"]] == [e.event_id for e in happy.trace.events]
        assert view["metrics"]["execution"] == "offline test double"
        assert client.get("/api/runs/RUN-obs-old/trace").json()["available"] is False
        assert client.get("/api/runs/RUN-nope-123/trace").status_code == 404
        agents = client.get("/api/monitoring/agents").json()
        assert agents["runs"] == 1 and {r["task_id"] for r in agents["agents"]} == {
            "baseline", "evidence", "investigation", "forensics", "adjudication"}
        page = client.get("/").text
        assert 'id="trace-body"' in page
    assert (tmp_path / "rt" / "runs" / happy.run_id / "trace.json").exists()


def test_the_offline_evaluation_is_unchanged_and_traced(evaluation):
    result, tmp = evaluation
    rows, eval_id = result["rows"], result["meta"]["evaluation_id"]
    assert result["meta"]["ledger_requests_added"] == 0
    assert {r["case_id"]: r["combined"]["final_decision"] for r in rows} == {r["case_id"]: r["expected_decision"] for r in rows}
    for row in rows:
        trace = RunTrace.model_validate_json((tmp / "evaluation" / eval_id / "runs" / row["run_id"] / "trace.json")
                                             .read_text(encoding="utf-8"))
        assert trace.collection_errors == [] and trace.metrics.execution == "offline test double", row["case_id"]
        assert {u.prompt_tokens for u in trace.metrics.models} == {None}
