"""Live reliability: model-provider failures are classified, attributed and shown truthfully, and they never change
what an investigation may conclude.

Everything runs offline with deterministic mocks: the failures are real google-genai / httpx exception objects raised
by a scripted model, inside the real manager, ADK Runner, MCP server, CrewAI service and blackboard. No Gemini request
is made (the one test that goes through the google-genai client uses a mock transport and a loopback base URL).
"""
import asyncio
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
from google import genai
from google.adk.models.llm_request import LlmRequest
from google.genai import errors, types
from pydantic import BaseModel, ValidationError
from starlette.testclient import TestClient

import claimsleuth.observability.provider_errors as pe
from adk_test_double import ScriptedLlm, call, text
from claimsleuth import contract, gemini_rate
from claimsleuth.adk_service import config as adk_config
from claimsleuth.adk_service.callbacks import ExecutionRecorder
from claimsleuth.adk_service.forensic_a2a import ForensicExchange
from claimsleuth.blackboard import Blackboard
from claimsleuth.dashboard.app import create_app
from claimsleuth.dashboard.services import ServiceConfig
from claimsleuth.dashboard.views import run_view
from claimsleuth.dashboard.workspace import Workspace
from claimsleuth.datastore import load_claim
from claimsleuth.evaluation.runner import _stop_reason
from claimsleuth.integration.run import save_bundle
from claimsleuth.observability.outcome import build_outcome, outcome_of
from claimsleuth.orchestration import manager as manager_mod
from claimsleuth.orchestration.manager import InvestigationManager
from claimsleuth.schemas import (AgentRunRecord, AgentSystemResult, ClaimRunBundle, Decision, ExecutionStatus, RunStatus,
                                 TaskStatus, utc_now)
from test_hierarchical_orchestration import env, manage  # noqa: F401  (module fixture: MCP + CrewAI + blackboard)

S = TaskStatus
HIGH_DEMAND = "This model is currently experiencing high demand. Spikes in demand are usually temporary."
POLICY_CALL = ("warranty_policy_search", {"policy_id": "POL-TECH-CARE-01"})     # CS-007's policy
FAKE_KEY = "AIza" + "FAKE-not-a-real-key-0123456789abcdef"
PROMPT_TEXT = "CONFIDENTIAL-PROMPT-MARKER what the claimant wrote"


@pytest.fixture(scope="module", autouse=True)
def no_retry_wait():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gemini_rate, "UNAVAILABLE_RETRY_DELAY_S", 0)
        yield


def server_error(code=503, status="UNAVAILABLE", message=HIGH_DEMAND):
    return errors.ServerError(code, {"error": {"code": code, "message": message, "status": status}})


def client_error(code, status, message="refused"):
    return errors.ClientError(code, {"error": {"code": code, "message": message, "status": status}})


def use_models(monkeypatch, **models):
    from claimsleuth.adk_service import test_double as adk_doubles
    real = adk_doubles.offline_models
    monkeypatch.setattr(adk_doubles, "offline_models", lambda: {**real(), **models})


def evidence_fails(monkeypatch, error):
    """The Evidence Agent's first request succeeds (it calls its MCP tool) and its second raises `error`. A 503 is
    retried, so it is scripted twice: the retry fails too."""
    name, args = POLICY_CALL
    llm = ScriptedLlm(script=[call(name, **args), error] + ([error] if pe.should_retry(error) else []))
    use_models(monkeypatch, evidence_agent=llm)
    return llm


def forensic_fails(monkeypatch, error):
    """The CrewAI service answers with a real FORENSIC_RESPONSE that carries a FAILED result (the A2A exchange works)."""
    async def answer(claim, documents, run_id, card_url, timeout=None, context=None):
        request = contract.forensic_request(run_id, claim.claim_id, documents, context or ())
        now = utc_now()
        run = AgentRunRecord(agent="forensic_agent", framework="crewai", started_at=now, finished_at=now, latency_ms=54615.5,
                             llm_calls=2, tool_calls=2, outcome="provider_error", errors=[pe.provider_text(error)])
        failure = pe.classify(error, run_id=run_id, agent="forensic_agent", model="test-double", provider="offline-test-double")
        result = AgentSystemResult(
            system="CREWAI", run_id=run_id, claim_id=claim.claim_id, status=ExecutionStatus.FAILED, model="test-double",
            started_at=now, finished_at=now, execution_ms=54700.0, agent_runs=[run], provider_failures=[failure],
            errors=[f"CrewAI run failed: {pe.failure_text(error)}"],
            trace=[{"ts": 0, "model_calls": [{"agent": "forensic_agent", "model": "test-double", "status": "ok", "latency_ms": 20157.4},
                                             {"agent": "forensic_agent", "model": "test-double", "status": "error", "latency_ms": 34000.0,
                                              "error_code": failure.error_code, "http_status": failure.http_status,
                                              "started_at": now.isoformat(), "finished_at": now.isoformat()}]}])
        response = contract.parse(contract.forensic_response(request, result, llm_mode="test-double").model_dump_json())
        return ForensicExchange(request, response, response.payload.result)

    monkeypatch.setattr(manager_mod, "request_forensics", answer)


def tasks(bundle):
    return {t.task_id: t for t in bundle.execution_plan.tasks}


def events(bundle, **match):
    return [e for e in bundle.trace.events if all(getattr(e, k) == v for k, v in match.items())]


def metrics(bundle):
    return {t.task_id: t for t in bundle.trace.metrics.tasks}


# =========================================================================== the classification

class Report(BaseModel):
    n: int


def validation_error():
    try:
        Report.model_validate({"n": "x"})
    except ValidationError as exc:
        return exc


@pytest.mark.parametrize("error, code, http, category, retryable", [
    (server_error(), pe.PROVIDER_UNAVAILABLE, 503, "availability", True),
    (client_error(429, "RESOURCE_EXHAUSTED"), pe.PROVIDER_RATE_LIMITED, 429, "quota", True),
    (server_error(500, "INTERNAL", "boom"), pe.PROVIDER_SERVER_ERROR, 500, "server", True),
    (server_error(502, "BAD_GATEWAY", "boom"), pe.PROVIDER_SERVER_ERROR, 502, "server", True),
    (server_error(504, "DEADLINE_EXCEEDED", "boom"), pe.PROVIDER_SERVER_ERROR, 504, "server", True),
    (client_error(401, "UNAUTHENTICATED"), pe.AUTHENTICATION_FAILED, 401, "authentication", False),
    (client_error(403, "PERMISSION_DENIED"), pe.AUTHENTICATION_FAILED, 403, "authentication", False),
    (client_error(400, "INVALID_ARGUMENT"), pe.INVALID_REQUEST, 400, "invalid_request", False),
    (client_error(400, "INVALID_ARGUMENT", "reason: API_KEY_INVALID"), pe.AUTHENTICATION_FAILED, 400, "authentication", False),
    (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), pe.NETWORK_ERROR, None, "network", True),
    (httpx.ReadError(""), pe.NETWORK_ERROR, None, "network", True),
    (socket.gaierror(11001, "getaddrinfo failed"), pe.NETWORK_ERROR, None, "network", True),
    (ConnectionResetError("reset"), pe.NETWORK_ERROR, None, "network", True),
    (httpx.ReadTimeout("slow"), pe.APPLICATION_TIMEOUT, None, "timeout", True),
    (TimeoutError("limit"), pe.APPLICATION_TIMEOUT, None, "timeout", True),
    (validation_error(), pe.STRUCTURED_OUTPUT_INVALID, None, "output", False),
    (json.JSONDecodeError("bad", "x", 0), pe.STRUCTURED_OUTPUT_INVALID, None, "output", False),
    (gemini_rate.GeminiQuotaGuard("daily budget used"), pe.LOCAL_BUDGET_EXCEEDED, None, "local_budget", False),
    (RuntimeError("something else"), pe.UNCLASSIFIED, None, "unknown", None),
])
def test_failures_are_classified(error, code, http, category, retryable):
    failure = pe.classify(error, run_id="RUN-class", agent="forensic_agent", model="gemini-x", provider="google-gemini")
    assert (failure.error_code, failure.http_status, failure.category, failure.retryable) == (code, http, category, retryable)
    assert (failure.provider, failure.model, failure.agent, failure.task_id) == ("google-gemini", "gemini-x", "forensic_agent", "forensics")
    assert failure.exception_type == type(error).__name__ and failure.correlation_id == "trace-RUN-class"
    assert failure.occurred_at.utcoffset() is not None


def test_an_http_status_is_never_inferred_from_text():
    for error in (RuntimeError("HTTP 503 UNAVAILABLE high demand"), ValueError("503"), httpx.ConnectError("429 from proxy")):
        assert pe.classify(error, run_id="RUN-class").http_status is None
    assert pe.classify(RuntimeError("503 UNAVAILABLE"), run_id="RUN-class").error_code == pe.UNCLASSIFIED


def test_a_provider_error_wrapped_by_a_framework_keeps_its_status():
    try:
        try:
            raise server_error()
        except errors.ServerError as inner:
            raise RuntimeError("the crew failed") from inner
    except RuntimeError as outer:
        failure = pe.classify(outer, run_id="RUN-class")
    assert (failure.error_code, failure.http_status, failure.provider_status) == (pe.PROVIDER_UNAVAILABLE, 503, "UNAVAILABLE")


def test_the_503_reason_comes_from_a_fixed_vocabulary():
    assert pe.classify(server_error(), run_id="RUN-class").provider_reason == "high demand"
    assert pe.classify(server_error(message="maintenance"), run_id="RUN-class").provider_reason is None


def test_the_evaluation_still_stops_on_a_provider_failure():
    for error in (server_error(), client_error(429, "RESOURCE_EXHAUSTED")):
        text_ = f"evidence_agent: model call failed: {pe.failure_text(error)}"
        row = {"evidence": {"errors": [text_]}, "investigator": {}, "forensic": {}, "combined": {}}
        assert _stop_reason(row) == text_


@pytest.mark.parametrize("error_text", [
    f"key {FAKE_KEY} refused", f"https://x.test/v1?key={FAKE_KEY}", f"api_key: '{FAKE_KEY}'", f"Authorization: Bearer {FAKE_KEY}"])
def test_credentials_are_redacted(error_text):
    assert FAKE_KEY not in pe.redact(error_text) and "[redacted]" in pe.redact(error_text)


def test_the_configured_key_is_redacted_whatever_its_form(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "plain-test-value-1234")
    assert "plain-test-value-1234" not in pe.redact("failed with plain-test-value-1234")


def test_a_provider_body_is_dropped():
    error = server_error(message=f"{PROMPT_TEXT} {FAKE_KEY}")
    for safe in (pe.failure_text(error), pe.provider_text(error), pe.classify(error, run_id="RUN-class").model_dump_json()):
        assert PROMPT_TEXT not in safe and FAKE_KEY not in safe and "503" in safe
    assert pe.failure_text(error) == "ServerError: 503 UNAVAILABLE"
    assert pe.provider_text(f"Google Gemini API error: 503 - {PROMPT_TEXT}") == "Google Gemini API error: 503"
    assert pe.provider_text("503 UNAVAILABLE. {'error': {'code': 503}}") == "503 UNAVAILABLE"


def test_the_503_explanation():
    failure = pe.classify(server_error(), run_id="RUN-class", agent="forensic_agent", provider="google-gemini")
    assert pe.explanation(failure, ["evidence_agent"], True) == (
        "Gemini is temporarily unavailable due to high demand. The affected agent could not complete. Existing verified "
        "results have been preserved, and the investigation requires human review.")
    assert "No verified report existed yet" in pe.explanation(failure, [], True)   # never claims what is not true
    plain = pe.classify(server_error(message="maintenance"), run_id="RUN-class", provider="google-gemini")
    assert pe.explanation(plain, [], True).startswith("Gemini is temporarily unavailable. ")
    assert "available again" not in pe.explanation(failure, ["evidence_agent"], True)


def test_an_exception_is_classified_once_and_a_duration_measured_once():
    recorder = ExecutionRecorder("RUN-once")
    recorder.agent_runs["evidence_agent"] = AgentRunRecord(agent="evidence_agent", framework="adk", started_at=utc_now())
    recorder._agent_t0["evidence_agent"] = 0.0
    error = server_error()
    assert recorder.record_failure(error, "evidence_agent") is recorder.record_failure(error, "evidence_agent")
    assert len(recorder.failures) == 1
    recorder.close_agent("evidence_agent", "provider_error")
    first = recorder.agent_runs["evidence_agent"].model_copy()
    recorder.close_agent("evidence_agent", "finished")
    recorder.close_open("timeout", TimeoutError("late"))
    assert recorder.agent_runs["evidence_agent"] == first and first.outcome == "provider_error"
    assert len(recorder.failures) == 1                                       # nothing was open any more


# =========================================================================== provider failures in real runs

@pytest.fixture(scope="module")
def evidence_503(env):
    with pytest.MonkeyPatch.context() as mp:
        llm = evidence_fails(mp, server_error())
        return manage(env, "CS-007", "RUN-rel-ev503"), llm


def test_a_503_during_evidence_is_a_provider_failure_not_an_application_bug(evidence_503):
    bundle, _ = evidence_503
    t = tasks(bundle)
    assert (t["evidence"].status, t["investigation"].status, t["forensics"].status) == (S.FAILED, S.BLOCKED, S.BLOCKED)
    failure, = bundle.adk.provider_failures
    assert (failure.error_code, failure.http_status, failure.provider_status, failure.retryable) == (
        "PROVIDER_UNAVAILABLE", 503, "UNAVAILABLE", True)
    assert (failure.agent, failure.task_id, failure.correlation_id) == ("evidence_agent", "evidence", "trace-RUN-rel-ev503")
    assert failure.provider_reason == "high demand" and failure.retried is True
    assert bundle.adk.provider_failures == bundle.outcome.failures
    assert [r.outcome for r in bundle.outcome.retries] == ["failed"]
    event, = events(bundle, event_type="provider.failure")
    assert (event.error_code, event.error_category, event.metadata["http_status"]) == ("PROVIDER_UNAVAILABLE", "provider", 503)
    failed_call = next(e for e in events(bundle, event_type="model.call") if e.status == "error")
    assert (failed_call.error_code, failed_call.error_category) == ("PROVIDER_UNAVAILABLE", "provider")
    for kind in ("task.failed", "agent.end"):
        event, = events(bundle, event_type=kind, task_id="evidence")
        assert (event.error_code, event.error_category) == ("PROVIDER_UNAVAILABLE", "provider")
    assert {e.error_code for e in events(bundle, event_type="agent.error")} == {"PROVIDER_UNAVAILABLE"}
    assert [e.metadata["propagated"] for e in events(bundle, event_type="agent.error")] == [False, True]
    assert {(e.error_code, e.error_category) for e in events(bundle, event_type="task.blocked")} == {("BLOCKED_UPSTREAM", "business")}


def test_failed_agent_durations_are_measured_and_kept_apart(evidence_503):
    bundle, _ = evidence_503
    run, = bundle.adk.agent_runs
    assert run.finished_at is not None and run.latency_ms > 0 and run.outcome == "provider_error"
    m = metrics(bundle)["evidence"]
    assert m.execution_ms == run.latency_ms == bundle.trace.metrics.evidence_ms and m.duration_source == "agent record"
    assert m.model_calls == 3 and 0 <= m.model_request_ms <= m.execution_ms
    assert m.lifecycle_ms is not None and m.error_code == "PROVIDER_UNAVAILABLE"
    assert len(events(bundle, event_type="agent.end", agent_name="evidence_agent")) == 1
    assert metrics(bundle)["investigation"].execution_ms is None and metrics(bundle)["investigation"].duration_source is None


def test_blocked_tasks_never_report_success(evidence_503):
    bundle, _ = evidence_503
    for task_id in ("investigation", "forensics"):
        task = tasks(bundle)[task_id]
        assert task.status == S.BLOCKED and task.started_at is None and task.result_ref is None
        assert [e.event for e in task.events] == ["blocked"]
        assert not [e for e in bundle.trace.events if e.task_id == task_id and e.status == "ok"]
        assert metrics(bundle)[task_id].status == S.BLOCKED
    assert bundle.messages == []                                             # no A2A request for a blocked task


def test_an_incomplete_investigation_is_escalated_and_labelled(evidence_503):
    bundle, _ = evidence_503
    final, outcome = bundle.final_report, bundle.outcome
    assert outcome.run_status == RunStatus.INCOMPLETE and outcome.requires_human_approval is True
    assert final.requires_human_approval and final.approval_status == "PENDING" and final.review_status == "NONE"
    assert outcome.failed_tasks == ["evidence"] and outcome.blocked_tasks == ["investigation", "forensics"]
    assert outcome.preserved_reports == [] and outcome.adjudicated_reports == []
    assert "No verified report existed yet" in outcome.explanation and "human review" in outcome.explanation
    assert "The automatic retry also failed." in outcome.explanation


def test_one_retry_after_a_503_and_no_model_fallback(evidence_503):
    bundle, llm = evidence_503
    assert len(llm.requests) == 3                                            # the 503 and its one retry
    assert bundle.adk.agent_runs[0].llm_calls == 3 and bundle.adk.model == "test-double"
    assert llm.requests[1] is llm.requests[2]                                # the same request
    assert {e.metadata["model"] for e in events(bundle, event_type="model.call")} == {llm.model}   # that model only
    assert adk_config.GEMINI_RETRY_ATTEMPTS == 1 and adk_config.gemini_llm("gemini-x").retry_options.attempts == 1
    assert adk_config.gemini_llm("gemini-x").model == "gemini-x"


class Flaky(ScriptedLlm):
    """The real offline double, except that the numbered requests raise."""
    inner: object = None
    fail_on: set = set()
    error: object = None
    seen: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.seen += 1
        if self.seen in self.fail_on:
            raise self.error
        async for response in self.inner.generate_content_async(llm_request, stream):
            yield response


def flaky(monkeypatch, agent, fail_on, error=None):
    from claimsleuth.adk_service import test_double as adk_doubles
    real = adk_doubles.offline_models
    llm = Flaky(fail_on=fail_on, error=error or server_error())

    def models():
        doubles = real()
        llm.inner = doubles[agent]
        return {**doubles, agent: llm}

    monkeypatch.setattr(adk_doubles, "offline_models", models)
    return llm


@pytest.mark.parametrize("agent, task_id", [("evidence_agent", "evidence"), ("investigator_agent", "investigation")])
def test_a_503_the_retry_recovers_is_not_a_failure(env, monkeypatch, agent, task_id):
    llm = flaky(monkeypatch, agent, {2})
    bundle = manage(env, "CS-003", f"RUN-rel-recover-{task_id}")
    assert all(t.status == S.COMPLETED for t in bundle.execution_plan.tasks) and llm.seen >= 3
    assert bundle.adk.status == ExecutionStatus.COMPLETED and bundle.adk.errors == []
    run = next(r for r in bundle.adk.agent_runs if r.agent == agent)
    assert run.errors == [] and run.outcome == "finished" and run.llm_calls == llm.seen
    outcome = bundle.outcome
    assert outcome.failures == [] and outcome.explanation is None and outcome.failed_tasks == []
    assert outcome.run_status in (RunStatus.COMPLETED, RunStatus.ESCALATED)
    retry, = outcome.retries
    assert (retry.outcome, retry.agent, retry.task_id, retry.http_status) == ("recovered", agent, task_id, 503)
    calls = events(bundle, event_type="model.call", agent_name=agent)
    assert len(calls) == llm.seen and [c.error_code for c in calls if c.status == "error"] == ["PROVIDER_UNAVAILABLE"]
    event, = events(bundle, event_type="provider.retry")
    assert event.status == "info" and event.metadata["outcome"] == "recovered"
    assert not events(bundle, event_type="provider.failure")
    end, = events(bundle, event_type="agent.end", agent_name=agent)
    assert end.status == "ok"
    assert bundle.final_report.review_status == "COMPLETE"


def view_of(env, bundle):
    return run_view(bundle, *load_claim(bundle.claim_id, env.data_dir))


def test_a_recovered_503_is_shown_as_recovered_and_keeps_its_history(env, monkeypatch):
    flaky(monkeypatch, "evidence_agent", {2})
    bundle = manage(env, "CS-003", "RUN-rel-recoveredview")
    view = view_of(env, bundle)
    retry, = view["provider_retries"]
    assert (retry["result"], retry["outcome"], retry["agent"]) == ("RECOVERED AFTER RETRY", "recovered", "evidence_agent")
    assert (retry["error_code"], retry["http_status"]) == ("PROVIDER_UNAVAILABLE", 503)
    assert {c["key"]: (c["status"], c["retry"]) for c in view["agents"]} == {
        "evidence_agent": ("COMPLETED", "RECOVERED AFTER RETRY"), "investigator_agent": ("COMPLETED", None), "forensic_agent": ("COMPLETED", None)}
    assert view["outcome"]["failures"] == [] and view["outcome"]["failed_tasks"] == []
    assert [c.error_code for c in events(bundle, event_type="model.call", agent_name="evidence_agent") if c.status == "error"] == [
        "PROVIDER_UNAVAILABLE"]                                                # the original 503 stays on the record


def test_an_unrecovered_503_stays_a_failure(evidence_503, env):
    bundle, _ = evidence_503
    view = view_of(env, bundle)
    retry, = view["provider_retries"]
    assert (retry["result"], retry["outcome"]) == ("RETRY FAILED", "failed")
    assert len(view["outcome"]["failures"]) == 1 and view["outcome"]["failed_tasks"] == ["evidence"]
    assert {c["key"]: (c["status"], c["retry"]) for c in view["agents"]}["evidence_agent"] == ("FAILED", "RETRY FAILED")


def test_a_recovered_run_decides_like_an_undisturbed_one(env, monkeypatch):
    flaky(monkeypatch, "evidence_agent", {1})
    recovered = manage(env, "CS-001", "RUN-rel-recover-same")
    monkeypatch.undo()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gemini_rate, "UNAVAILABLE_RETRY_DELAY_S", 0)
        plain = manage(env, "CS-001", "RUN-rel-recover-plain")
    pick = lambda f: (f.final_decision, f.decided_by, f.review_status, f.requires_human_approval, f.payout)
    assert pick(recovered.final_report) == pick(plain.final_report)


def test_only_one_retry_per_agent(env, monkeypatch):
    llm = flaky(monkeypatch, "evidence_agent", {1, 3})        # 1 fails, 2 is its retry, 3 fails and gets none
    bundle = manage(env, "CS-003", "RUN-rel-once")
    assert llm.seen == 3 and [r.outcome for r in bundle.outcome.retries] == ["recovered"]
    failure, = bundle.outcome.failures
    assert failure.error_code == "PROVIDER_UNAVAILABLE" and failure.retried is False
    assert tasks(bundle)["evidence"].status == S.FAILED


def test_a_different_error_on_the_retry_is_the_one_reported(env, monkeypatch):
    use_models(monkeypatch, evidence_agent=ScriptedLlm(script=[server_error(), client_error(429, "RESOURCE_EXHAUSTED")]))
    bundle = manage(env, "CS-007", "RUN-rel-retry429")
    failure, = bundle.outcome.failures
    assert (failure.error_code, failure.http_status, failure.retried) == ("PROVIDER_RATE_LIMITED", 429, True)
    assert [r.outcome for r in bundle.outcome.retries] == ["failed"]
    assert [e.error_code for e in events(bundle, event_type="model.call") if e.status == "error"] == [
        "PROVIDER_UNAVAILABLE", "PROVIDER_RATE_LIMITED"]


def model_error_context(agent="evidence_agent"):
    from claimsleuth.schemas import AgentRunRecord as Run
    recorder_run = Run(agent=agent, framework="adk", started_at=utc_now())
    return SimpleNamespace(agent_name=agent), recorder_run


def unit_recorder(run_id, retry_llm):
    recorder = ExecutionRecorder(run_id, before_request=lambda a: gemini_rate.acquire("gemini-x", f"adk:{a}", run_id),
                                 model="gemini-x", provider="google-gemini", retry_model=lambda a: retry_llm)
    context, record = model_error_context()
    recorder.agent_runs["evidence_agent"] = record
    recorder._agent_t0["evidence_agent"] = 0.0
    return recorder, context


def test_the_retry_takes_a_ledger_slot(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_LEDGER", str(tmp_path / "ledger.sqlite3"))
    llm = ScriptedLlm(script=[text("the second answer")])
    recorder, context = unit_recorder("RUN-rel-ledger", llm)
    request = LlmRequest()

    async def go():
        await recorder.before_model(context, request)
        return await recorder.on_model_error(context, request, server_error())

    response = asyncio.run(go())
    assert response.content.parts[0].text == "the second answer"
    assert gemini_rate.run_requests("RUN-rel-ledger") == {"adk:evidence_agent": 2}
    assert recorder.agent_runs["evidence_agent"].llm_calls == 2 and recorder.failures == []


def test_a_retry_the_budget_refuses_is_not_sent(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_LEDGER", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_RPD", "1")
    llm = ScriptedLlm(script=[text("never sent")])
    recorder, context = unit_recorder("RUN-rel-refused", llm)
    request = LlmRequest()

    async def go():
        await recorder.before_model(context, request)
        return await recorder.on_model_error(context, request, server_error())

    assert asyncio.run(go()) is None                                         # ADK re-raises the 503
    assert llm.requests == [] and gemini_rate.run_requests("RUN-rel-refused") == {"adk:evidence_agent": 1}
    assert [r.outcome for r in recorder.retries] == ["refused"]
    assert recorder.failures[0].error_code == "PROVIDER_UNAVAILABLE" and recorder.failures[0].retried is False
    assert recorder.agent_runs["evidence_agent"].llm_calls == 1


def test_the_real_gemini_class_recovers_through_the_retry():
    sent = []

    def handler(request):
        sent.append(request.url.host)
        if len(sent) == 1:
            return httpx.Response(503, json={"error": {"code": 503, "message": HIGH_DEMAND, "status": "UNAVAILABLE"}})
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "recovered"}]}}]})

    llm = adk_config.gemini_llm("gemini-x")
    llm.client = genai.Client(api_key="not-a-real-key", http_options=types.HttpOptions(
        base_url="http://127.0.0.1:9/", retry_options=llm.retry_options,
        async_client_args={"transport": httpx.MockTransport(handler)}))
    recorder = ExecutionRecorder("RUN-rel-real", model="gemini-x", provider="google-gemini", retry_model=lambda a: llm)
    context, record = model_error_context()
    recorder.agent_runs["evidence_agent"] = record
    request = LlmRequest(model="gemini-x", contents=[types.Content(role="user", parts=[types.Part(text="x")])])

    async def go():
        try:
            async for _ in llm.generate_content_async(request):
                pass
        except errors.ServerError as error:
            return await recorder.on_model_error(context, request, error)

    response = asyncio.run(go())
    assert sent == ["127.0.0.1", "127.0.0.1"]                                # the mock transport, twice
    assert response.content.parts[0].text == "recovered"
    assert [r.outcome for r in recorder.retries] == ["recovered"]


def test_the_real_client_sends_a_503_request_exactly_once():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(503, json={"error": {"code": 503, "message": HIGH_DEMAND, "status": "UNAVAILABLE"}})

    options = types.HttpOptions(base_url="http://127.0.0.1:9/", retry_options=adk_config.gemini_llm("gemini-x").retry_options,
                                client_args={"transport": httpx.MockTransport(handler)})
    client = genai.Client(api_key="not-a-real-key", http_options=options)
    with pytest.raises(errors.ServerError) as caught:
        client.models.generate_content(model="gemini-x", contents="x")
    assert calls == ["127.0.0.1"]                                            # the mock transport, once
    failure = pe.classify(caught.value, run_id="RUN-class")
    assert (failure.error_code, failure.http_status, failure.provider_status) == (pe.PROVIDER_UNAVAILABLE, 503, "UNAVAILABLE")


@pytest.mark.parametrize("error, code, http, retryable", [
    (client_error(429, "RESOURCE_EXHAUSTED", "quota exceeded"), "PROVIDER_RATE_LIMITED", 429, True),
    (client_error(401, "UNAUTHENTICATED", "API key not valid"), "AUTHENTICATION_FAILED", 401, False),
    (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), "NETWORK_ERROR", None, True),
], ids=["quota-429", "auth-401", "network"])
def test_other_provider_failures_end_the_same_safe_way(env, monkeypatch, error, code, http, retryable):
    evidence_fails(monkeypatch, error)
    bundle = manage(env, "CS-007", f"RUN-rel-{code.lower().replace('_', '-')[:12]}")
    failure, = bundle.outcome.failures
    assert (failure.error_code, failure.http_status, failure.retryable) == (code, http, retryable)
    assert bundle.outcome.retries == [] and failure.retried is False         # only a 503 is retried
    assert bundle.adk.agent_runs[0].llm_calls == 2
    assert bundle.outcome.run_status == RunStatus.INCOMPLETE and bundle.final_report.requires_human_approval
    assert tasks(bundle)["forensics"].status == S.BLOCKED
    assert events(bundle, event_type="task.failed", task_id="evidence")[0].error_code == code
    assert bundle.adk.agent_runs[0].latency_ms is not None


def test_the_local_budget_guard_is_not_a_provider_429(env, monkeypatch):
    evidence_fails(monkeypatch, gemini_rate.GeminiQuotaGuard("daily Gemini request budget used: 20 of 20; no request was sent"))
    bundle = manage(env, "CS-007", "RUN-rel-budget")
    failure, = bundle.outcome.failures
    assert (failure.error_code, failure.http_status, failure.category) == ("LOCAL_BUDGET_EXCEEDED", None, "local_budget")
    assert "nothing was sent to Gemini" in bundle.outcome.explanation
    assert any("GeminiQuotaGuard" in e for e in bundle.adk.errors)           # the evaluation's stop marker


def test_an_unknown_error_stays_an_agent_failure(env, monkeypatch):
    evidence_fails(monkeypatch, RuntimeError("a bug in the application"))
    bundle = manage(env, "CS-007", "RUN-rel-bug")
    failure, = bundle.outcome.failures
    assert (failure.error_code, failure.retryable, failure.http_status) == ("UNCLASSIFIED", None, None)
    event, = events(bundle, event_type="task.failed", task_id="evidence")
    assert (event.error_code, event.error_category) == ("AGENT_FAILED", "agent")     # not presented as the provider


class HangingLlm(ScriptedLlm):
    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        await asyncio.sleep(3600)
        yield None


def test_the_managers_deadline_keeps_the_agents_record(env, monkeypatch):
    use_models(monkeypatch, evidence_agent=HangingLlm())
    bundle = manage(env, "CS-007", "RUN-rel-deadline", deadlines={"adk": 8})   # past MCP setup: the request is in flight
    t = tasks(bundle)
    assert (t["evidence"].status, t["investigation"].status, t["forensics"].status) == (S.TIMED_OUT, S.BLOCKED, S.BLOCKED)
    run, = bundle.adk.agent_runs                                             # survives the cancelled run
    assert run.outcome == "cancelled" and run.latency_ms >= 5000 and run.finished_at is not None
    failure, = bundle.outcome.failures
    assert (failure.error_code, failure.http_status, failure.agent) == ("APPLICATION_TIMEOUT", None, "evidence_agent")
    call_ = next(e for e in events(bundle, event_type="model.call") if e.status == "error")
    assert call_.duration_ms >= 1000 and call_.error_code == "APPLICATION_TIMEOUT"
    m = metrics(bundle)["evidence"]
    assert m.execution_ms == run.latency_ms and m.model_calls == 1 and m.lifecycle_ms >= 5000
    assert m.model_request_ms == call_.duration_ms < m.execution_ms


def test_a_run_cancelled_before_any_model_request_records_none(env, monkeypatch):
    llm = HangingLlm()
    use_models(monkeypatch, evidence_agent=llm)
    bundle = manage(env, "CS-007", "RUN-rel-earlydeadline", deadlines={"adk": 0.5})
    assert tasks(bundle)["evidence"].status == S.TIMED_OUT and not llm.requests
    assert not events(bundle, event_type="model.call") and metrics(bundle)["evidence"].model_calls == 0
    assert bundle.outcome.run_status == RunStatus.INCOMPLETE and bundle.final_report.requires_human_approval


def test_the_adk_runs_own_timeout_keeps_the_agents_record(env, monkeypatch):
    use_models(monkeypatch, evidence_agent=HangingLlm())
    monkeypatch.setattr(adk_config, "RUN_TIMEOUT_SECONDS", 2)
    bundle = manage(env, "CS-007", "RUN-rel-timeout", deadlines={"adk": 30})
    assert tasks(bundle)["evidence"].status == S.TIMED_OUT
    run, = bundle.adk.agent_runs
    assert run.outcome == "timeout" and run.latency_ms >= 1900
    assert bundle.outcome.failures[0].error_code == "APPLICATION_TIMEOUT"
    event, = events(bundle, event_type="task.timed_out", task_id="evidence")
    assert event.status == "timeout" and event.error_code == "AGENT_TIMEOUT"


def test_structured_output_failure(env, monkeypatch):
    name, args = POLICY_CALL
    use_models(monkeypatch, evidence_agent=ScriptedLlm(script=[call(name, **args), text("this is not a report")]))
    bundle = manage(env, "CS-007", "RUN-rel-output")
    failure, = bundle.outcome.failures
    assert (failure.error_code, failure.category, failure.retryable) == ("STRUCTURED_OUTPUT_INVALID", "output", False)
    assert failure.agent == "evidence_agent" and bundle.adk.agent_runs[0].outcome == "exception"   # ADK itself refused it
    assert bundle.adk.agent_runs[0].latency_ms is not None
    event, = events(bundle, event_type="task.failed", task_id="evidence")
    assert (event.error_code, event.error_category) == ("STRUCTURED_OUTPUT_INVALID", "agent")
    end, = events(bundle, event_type="agent.end", agent_name="evidence_agent")
    assert end.status == "error" and end.error_code == "STRUCTURED_OUTPUT_INVALID"


# =========================================================================== later agents failing: what is preserved

@pytest.fixture(scope="module")
def forensic_503(env):
    with pytest.MonkeyPatch.context() as mp:
        forensic_fails(mp, server_error())
        return manage(env, "CS-003", "RUN-rel-fo503")


def test_a_503_during_forensics_preserves_the_upstream_reports(forensic_503, env):
    bundle = forensic_503
    t = tasks(bundle)
    assert (t["evidence"].status, t["investigation"].status, t["forensics"].status) == (S.COMPLETED, S.COMPLETED, S.FAILED)
    outcome = bundle.outcome
    assert outcome.run_status == RunStatus.INCOMPLETE and outcome.failed_tasks == ["forensics"] and outcome.blocked_tasks == []
    assert outcome.preserved_reports == ["evidence_agent", "investigator_agent"] == outcome.adjudicated_reports
    assert outcome.failures[0].error_code == "PROVIDER_UNAVAILABLE" and outcome.failures[0].agent == "forensic_agent"
    assert "Existing verified results have been preserved" in outcome.explanation
    assert bundle.adk.status == ExecutionStatus.COMPLETED and bundle.adk.report is not None
    assert bundle.final_report.requires_human_approval and bundle.final_report.review_status == "PARTIAL"
    assert metrics(bundle)["forensics"].execution_ms == 54615.5 and metrics(bundle)["forensics"].model_calls == 2


def test_an_a2a_response_does_not_mean_the_forensic_agent_succeeded(forensic_503):
    bundle = forensic_503
    assert [m.message_type for m in bundle.messages] == ["FORENSIC_REQUEST", "FORENSIC_RESPONSE"]
    names = [e.event for e in tasks(bundle)["forensics"].events]
    assert "response_received" in names and "report_returned" not in names and names[-1] == "failed"
    assert tasks(bundle)["forensics"].status == S.FAILED and tasks(bundle)["forensics"].result_ref is None
    response, = events(bundle, event_type="a2a.response")
    assert response.status == "ok" and response.metadata["forensic_status"] == "FAILED"
    failed, = events(bundle, event_type="task.failed", task_id="forensics")
    assert (failed.error_code, failed.error_category) == ("PROVIDER_UNAVAILABLE", "provider")
    assert not events(bundle, event_type="task.report_verified", task_id="forensics")


def test_blackboard_records_stay_signed_and_verified(forensic_503, env):
    board = Blackboard(env.db)
    state = board.state(forensic_503.run_id, forensic_503.claim_id)
    assert set(state.authentication) == {"valid"} and state.retracted == [] and state.late == []
    assert {"report:evidence_agent", "report:investigator_agent"} <= {p.section for p in state.live_posts}
    assert forensic_503.trace.collection_errors == []
    assert all(e.auth == "valid" for e in board.entries(forensic_503.run_id, forensic_503.claim_id))


def test_a_valid_evidence_report_survives_an_investigator_503(env, monkeypatch):
    use_models(monkeypatch, investigator_agent=ScriptedLlm(script=[server_error(), server_error()]))
    bundle = manage(env, "CS-003", "RUN-rel-inv503")
    t = tasks(bundle)
    assert (t["evidence"].status, t["investigation"].status, t["forensics"].status) == (S.COMPLETED, S.FAILED, S.COMPLETED)
    assert not [e for e in t["investigation"].events if e.event == "report_returned"]
    state = Blackboard(env.db).state(bundle.run_id, bundle.claim_id)
    assert state.retracted == [] and "report:evidence_agent" in {p.section for p in state.live_posts}
    assert bundle.adk.evidence_report is not None and bundle.adk.report is None
    assert bundle.outcome.preserved_reports == ["evidence_agent", "forensic_agent"]
    assert bundle.outcome.adjudicated_reports == ["forensic_agent"]          # a FAILED ADK result is never adjudicated
    assert any("GOOGLE_ADK review unavailable" in r for r in bundle.final_report.incomplete_reasons)
    assert any("evidence_agent report not used" in a.detail for a in bundle.audit)


def test_no_approval_without_a_review(env, monkeypatch):
    use_models(monkeypatch, evidence_agent=ScriptedLlm(script=[server_error(), server_error()]))
    bundle = manage(env, "CS-001", "RUN-rel-noapprove")
    assert bundle.baseline.decision == Decision.APPROVE                      # the rules alone would approve
    final = bundle.final_report
    assert (final.final_decision, final.decided_by, final.payout) == (Decision.ESCALATE, "INSUFFICIENT_REVIEW", None)
    assert final.requires_human_approval and final.approval_status == "PENDING"


def test_a_partial_review_never_skips_the_human(env, monkeypatch):
    forensic_fails(monkeypatch, server_error())
    bundle = manage(env, "CS-001", "RUN-rel-partial")
    final = bundle.final_report
    assert final.review_status == "PARTIAL" and final.requires_human_approval and final.approval_status == "PENDING"
    assert bundle.outcome.run_status == RunStatus.INCOMPLETE


def test_run_status_of_a_complete_investigation(env):
    bundle = manage(env, "CS-001", "RUN-rel-complete")
    assert bundle.outcome.failures == [] and bundle.outcome.explanation is None
    expected = RunStatus.ESCALATED if bundle.final_report.requires_human_approval else RunStatus.COMPLETED
    assert bundle.outcome.run_status == expected
    assert bundle.outcome.preserved_reports == ["evidence_agent", "investigator_agent", "forensic_agent"]
    names = {t.task_id: [e.event for e in t.events] for t in bundle.execution_plan.tasks}
    assert "report_returned" in names["forensics"] and "response_received" not in names["forensics"]
    assert all(t.status == S.COMPLETED for t in bundle.execution_plan.tasks)


def test_run_status_blocked_when_no_review_could_begin(env):
    bundle = manage(env, "CS-003", "RUN-rel-blocked")
    dead = bundle.execution_plan.model_copy(deep=True)
    for t in dead.tasks:
        if t.task_id in ("evidence", "investigation", "forensics"):
            t.started_at, t.status = None, S.BLOCKED
    assert build_outcome(bundle.model_copy(update={"execution_plan": dead})).run_status == RunStatus.BLOCKED


def test_concurrent_failures_stay_in_their_own_run(env, monkeypatch):
    class FailsForOneRun(ScriptedLlm):
        inner: object = None

        async def generate_content_async(self, llm_request, stream=False):
            if "RUN-rel-iso-bad" in str(llm_request.contents):
                raise server_error()
            async for response in self.inner.generate_content_async(llm_request, stream):
                yield response

    from claimsleuth.adk_service import test_double as adk_doubles
    real = adk_doubles.offline_models

    def models():
        doubles = real()
        return {**doubles, "evidence_agent": FailsForOneRun(inner=doubles["evidence_agent"])}

    monkeypatch.setattr(adk_doubles, "offline_models", models)

    async def both():
        return await asyncio.gather(*(InvestigationManager(
            claim, run_id=run_id, mcp_url=env.mcp.url, crew_card_url=env.crew.card, data_dir=env.data_dir,
            db_path=env.db_path).run() for claim, run_id in (("CS-003", "RUN-rel-iso-bad"), ("CS-006", "RUN-rel-iso-ok"))))

    bad, good = asyncio.run(both())
    assert bad.outcome.run_status == RunStatus.INCOMPLETE and bad.outcome.failures[0].correlation_id == "trace-RUN-rel-iso-bad"
    assert good.outcome.failures == [] and good.adk.provider_failures == [] and tasks(good)["forensics"].status == S.COMPLETED
    for bundle in (bad, good):
        assert {e.run_id for e in bundle.trace.events} == {bundle.run_id}
        assert {e.correlation_id for e in bundle.trace.events} == {f"trace-{bundle.run_id}"}
    assert not {e.event_id for e in bad.trace.events} & {e.event_id for e in good.trace.events}


# =========================================================================== nothing sensitive is stored

def test_a_provider_body_reaches_no_record(env, monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_API_KEY", FAKE_KEY)
    evidence_fails(monkeypatch, server_error(message=f"{PROMPT_TEXT} key={FAKE_KEY}"))
    bundle = manage(env, "CS-007", "RUN-rel-secret")
    out = save_bundle(bundle, tmp_path)
    stored = "".join(p.read_text(encoding="utf-8") for p in out.iterdir() if p.is_file())
    entries = Blackboard(env.db).entries(bundle.run_id, bundle.claim_id)
    for record in (stored, bundle.model_dump_json(), json.dumps([e.payload for e in entries], default=str)):
        assert FAKE_KEY not in record and PROMPT_TEXT not in record
    assert "ServerError: 503 UNAVAILABLE" in stored                          # what is needed is still there


# =========================================================================== the dashboard

@pytest.fixture(scope="module")
def dashboard(env, evidence_503, forensic_503, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("reldash")
    ws = Workspace(tmp / "dash", source_data=env.data_dir)
    ws.prepare()
    save_bundle(evidence_503[0], tmp)
    save_bundle(forensic_503, tmp)
    client = TestClient(create_app(ws, ServiceConfig(mcp_port=1, crew_port=1), runtime_dir=tmp, live=False))
    return client, tmp


def test_the_dashboard_shows_the_safe_failure(dashboard):
    client, _ = dashboard
    view = client.get("/api/runs/RUN-rel-fo503").json()
    assert view["run_status"] == "INCOMPLETE"
    assert {a["key"]: a["status"] for a in view["agents"]} == {
        "evidence_agent": "COMPLETED", "investigator_agent": "COMPLETED", "forensic_agent": "FAILED"}
    trace = client.get("/api/runs/RUN-rel-fo503/trace").json()
    outcome = trace["outcome"]
    failure = outcome["failures"][0]
    assert (failure["error_code"], failure["http_status"], failure["retryable"], failure["agent"], failure["category"]) == (
        "PROVIDER_UNAVAILABLE", 503, True, "forensic_agent", "availability")
    assert outcome["preserved_reports"] == ["evidence_agent", "investigator_agent"] and outcome["blocked_tasks"] == []
    assert outcome["requires_human_approval"] is True and outcome["explanation"]
    task = next(t for t in trace["metrics"]["tasks"] if t["task_id"] == "forensics")
    assert task["execution_ms"] == 54615.5 and task["model_request_ms"] == 54157.4 and task["lifecycle_ms"] is not None


def test_the_dashboard_shows_blocked_agents_as_blocked(dashboard):
    client, _ = dashboard
    view = client.get("/api/runs/RUN-rel-ev503").json()
    assert {a["key"]: a["status"] for a in view["agents"]} == {
        "evidence_agent": "FAILED", "investigator_agent": "BLOCKED", "forensic_agent": "BLOCKED"}
    assert view["run_status"] == "INCOMPLETE"


def test_runs_stored_earlier_still_load(dashboard):
    client, tmp = dashboard
    path = tmp / "runs" / "RUN-rel-ev503" / "bundle.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("outcome")
    raw["adk"].pop("provider_failures")
    raw["crewai"].pop("provider_failures")
    for run in raw["adk"]["agent_runs"]:
        run.pop("outcome")
    raw["run_id"] = "RUN-rel-old"
    old = tmp / "runs" / "RUN-rel-old"
    old.mkdir()
    (old / "bundle.json").write_text(json.dumps(raw), encoding="utf-8")
    trace = client.get("/api/runs/RUN-rel-old/trace").json()
    assert trace["outcome"]["run_status"] == "INCOMPLETE" and trace["outcome"]["failures"] == []
    assert trace["outcome"]["failures_recorded"] is False and trace["outcome"]["explanation"] is None
    assert client.get("/api/runs/RUN-rel-old").json()["run_status"] == "INCOMPLETE"


def test_stored_live_runs_get_a_truthful_status():
    """Read only, when this machine has the two runs that prompted this work (no Gemini, no writes)."""
    from claimsleuth import config
    for run_id, preserved in (("RUN-dash-5d4466fee6", ["evidence_agent", "investigator_agent"]), ("RUN-dash-f5a39f4d36", [])):
        path = config.RUNTIME_DIR / "runs" / run_id / "bundle.json"
        if not path.exists():
            continue
        outcome = outcome_of(ClaimRunBundle.model_validate_json(path.read_text(encoding="utf-8")))
        assert outcome.run_status == RunStatus.INCOMPLETE and outcome.preserved_reports == preserved
        assert outcome.failures_recorded is False                            # nothing is inferred from old text


def test_the_page_renders_the_outcome_with_escaping():
    from claimsleuth.dashboard import app as app_module
    from pathlib import Path
    script = (Path(app_module.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "function outcomeBlock(o)" in script and "HTTP status" in script
    assert "esc(f.summary)" in script and "esc(o.explanation)" in script and "esc(v.outcome.explanation)" in script
    assert 'setStatus(f ? "COMPLETED"' not in script
    assert "only an HTTP 503 is retried automatically" in script and "o.retries" in script


# =========================================================================== the offline regression

def test_the_40_case_runs_are_unaffected(evaluation):
    result, tmp = evaluation
    assert gemini_rate.total_requests(tmp / "ledger.sqlite3") == 0
    work = tmp / "evaluation" / result["meta"]["evaluation_id"]
    for row in result["rows"]:
        bundle = ClaimRunBundle.model_validate_json((work / "runs" / row["run_id"] / "bundle.json").read_text(encoding="utf-8"))
        assert bundle.outcome.failures == [] and bundle.outcome.run_status in (RunStatus.COMPLETED, RunStatus.ESCALATED)
        assert (bundle.outcome.run_status == RunStatus.ESCALATED) == bundle.final_report.requires_human_approval
        assert bundle.final_report.final_decision.value == row["combined"]["final_decision"]
