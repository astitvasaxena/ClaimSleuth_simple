"""Provider failures on the CrewAI side, inside .venv-crew with the real CrewAI Crew and a real MCP server. The model is
a labelled test double that raises real google-genai exceptions; no Gemini request is made."""
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("crewai", reason="CrewAI tests run in .venv-crew")

from crewai.llms.base_llm import llm_call_context  # noqa: E402
from google.genai import errors  # noqa: E402

from claimsleuth import contract, gemini_rate  # noqa: E402
from claimsleuth.blackboard import ENV_VAR  # noqa: E402
from claimsleuth.crew_service.runner import run_forensic_investigation  # noqa: E402
from claimsleuth.crew_service.test_double import MODEL_NAME, ForensicTestDoubleLLM  # noqa: E402
from claimsleuth.crew_service import gemini as gm  # noqa: E402
from claimsleuth.datastore import load_claim  # noqa: E402
from claimsleuth.observability.provider_errors import retry_record  # noqa: E402
from claimsleuth.schemas import AgentSystemResult, ExecutionStatus  # noqa: E402

CLAIM = "CS-003"
FAKE_KEY = "AIza" + "FAKE-not-a-real-key-0123456789abcdef"
HIGH_DEMAND = "This model is currently experiencing high demand. Spikes in demand are usually temporary."


class FailingLLM(ForensicTestDoubleLLM):
    """Answers normally, then its `fail_on`-th model call raises `error`, as CrewAI's Gemini provider does."""
    error: object = None
    fail_on: int = 2
    calls_seen: int = 0

    def call(self, messages, tools=None, callbacks=None, available_functions=None, from_task=None, from_agent=None,
             response_model=None):
        self.calls_seen += 1
        if self.calls_seen < self.fail_on:
            return super().call(messages, tools, callbacks, available_functions, from_task, from_agent, response_model)
        with llm_call_context():                      # as CrewAI's Gemini provider: one call id for start and failure
            self._emit_call_started_event(messages=messages, tools=tools, callbacks=callbacks,
                                          available_functions=available_functions, from_task=from_task, from_agent=from_agent)
            self._emit_call_failed_event(error=f"Google Gemini API error: {getattr(self.error, 'code', '')} - {self.error}",
                                         from_task=from_task, from_agent=from_agent)
            raise self.error


class RecoveringLLM(ForensicTestDoubleLLM):
    """Its second request fails with a 503 and the retry succeeds, as BudgetedGeminiLLM reports it."""
    calls_seen: int = 0
    provider_retries: list = []

    def call(self, messages, tools=None, callbacks=None, available_functions=None, from_task=None, from_agent=None,
             response_model=None):
        self.calls_seen += 1
        if self.calls_seen == 2:
            with llm_call_context():
                self._emit_call_started_event(messages=messages, tools=tools, callbacks=callbacks,
                                              available_functions=available_functions, from_task=from_task,
                                              from_agent=from_agent)
                self._emit_call_failed_event(error="Google Gemini API error: 503 - busy", from_task=from_task,
                                             from_agent=from_agent)
            self.provider_retries = [retry_record(unavailable(), "forensic_agent", "recovered", 15.0)]
        return super().call(messages, tools, callbacks, available_functions, from_task, from_agent, response_model)


def unavailable(message=HIGH_DEMAND):
    return errors.ServerError(503, {"error": {"code": 503, "message": message, "status": "UNAVAILABLE"}})


def run(mcp, dataset, monkeypatch, run_id, llm):
    monkeypatch.setenv(ENV_VAR, "off")
    return run_forensic_investigation(CLAIM, run_id=run_id, llm=llm, mcp_url=mcp.url, data_dir=dataset[0])


def calls(result):
    return [c for t in result.trace for c in t.get("model_calls", [])]


def crew_events(result):
    return [e for t in result.trace for e in t.get("crew_events", [])]


def test_a_503_is_classified_and_the_agent_failed(mcp, dataset, monkeypatch):
    llm = FailingLLM(model=MODEL_NAME, error=unavailable())
    result = run(mcp, dataset, monkeypatch, "RUN-crewrel-503", llm)
    assert result.status == ExecutionStatus.FAILED and result.report is None
    failure, = result.provider_failures
    assert (failure.error_code, failure.http_status, failure.provider_status, failure.retryable) == (
        "PROVIDER_UNAVAILABLE", 503, "UNAVAILABLE", True)
    assert (failure.agent, failure.task_id, failure.exception_type) == ("forensic_agent", "forensics", "ServerError")
    assert (failure.provider, failure.model) == ("offline-test-double", MODEL_NAME)
    assert failure.correlation_id == "trace-RUN-crewrel-503" and failure.provider_reason == "high demand"
    assert result.errors == ["CrewAI run failed: ServerError: 503 UNAVAILABLE"]
    assert llm.calls_seen == 2                                       # the failed request was not repeated
    assert result.model == MODEL_NAME                                # and no other model was used


def test_the_failed_call_and_the_agent_have_durations(mcp, dataset, monkeypatch):
    result = run(mcp, dataset, monkeypatch, "RUN-crewrel-time", FailingLLM(model=MODEL_NAME, error=unavailable()))
    run_record, = result.agent_runs
    assert run_record.outcome == "provider_error" and run_record.latency_ms > 0 and run_record.finished_at is not None
    failed = [c for c in calls(result) if c.get("status") == "error"]
    assert len(failed) == 1 and failed[0]["started_at"] and failed[0]["latency_ms"] is not None
    assert (failed[0]["error_code"], failed[0]["http_status"]) == ("PROVIDER_UNAVAILABLE", 503)


def test_a_failed_result_survives_the_a2a_response(mcp, dataset, monkeypatch):
    result = run(mcp, dataset, monkeypatch, "RUN-crewrel-a2a", FailingLLM(model=MODEL_NAME, error=unavailable()))
    _, documents = load_claim(CLAIM, dataset[0])
    request = contract.forensic_request("RUN-crewrel-a2a", CLAIM, documents)
    response = contract.parse(contract.forensic_response(request, result, llm_mode="test-double").model_dump_json())
    assert response.message_type == "FORENSIC_RESPONSE"              # the exchange worked; the agent did not
    carried = response.payload.result
    assert carried.status == ExecutionStatus.FAILED and carried.provider_failures == result.provider_failures
    assert AgentSystemResult.model_validate_json(result.model_dump_json()) == result


def test_a_provider_body_reaches_no_record(mcp, dataset, monkeypatch):
    marker = "CONFIDENTIAL-PROMPT-MARKER"
    result = run(mcp, dataset, monkeypatch, "RUN-crewrel-body",
                 FailingLLM(model=MODEL_NAME, error=unavailable(f"{marker} key={FAKE_KEY}")))
    stored = result.model_dump_json()
    assert marker not in stored and FAKE_KEY not in stored and "503" in stored
    assert all("error" not in e or marker not in e["error"] for e in crew_events(result))


def test_a_rate_limit_is_not_an_outage(mcp, dataset, monkeypatch):
    error = errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    result = run(mcp, dataset, monkeypatch, "RUN-crewrel-429", FailingLLM(model=MODEL_NAME, fail_on=1, error=error))
    failure, = result.provider_failures
    assert (failure.error_code, failure.http_status, failure.category) == ("PROVIDER_RATE_LIMITED", 429, "quota")


def test_guardrail_exhaustion_is_an_output_failure(mcp, dataset, monkeypatch):
    llm = ForensicTestDoubleLLM(model=MODEL_NAME, mode="fabricated_citation")
    result = run(mcp, dataset, monkeypatch, "RUN-crewrel-guard", llm)
    assert result.status == ExecutionStatus.FAILED
    failure, = result.provider_failures
    assert (failure.error_code, failure.category, failure.http_status) == ("STRUCTURED_OUTPUT_INVALID", "output", None)
    assert any("guardrail validation" in e for e in result.errors)


def test_an_invalid_final_answer_is_an_output_failure(mcp, dataset, monkeypatch):
    result = run(mcp, dataset, monkeypatch, "RUN-crewrel-json", ForensicTestDoubleLLM(model=MODEL_NAME, mode="invalid_json"))
    assert result.status == ExecutionStatus.FAILED
    assert result.provider_failures[0].error_code == "STRUCTURED_OUTPUT_INVALID"


def test_a_recovered_503_leaves_a_completed_agent(mcp, dataset, monkeypatch):
    result = run(mcp, dataset, monkeypatch, "RUN-crewrel-recover", RecoveringLLM(model=MODEL_NAME))
    assert result.status == ExecutionStatus.COMPLETED and result.report is not None
    assert result.errors == [] and result.provider_failures == []
    assert result.agent_runs[0].errors == [] and result.agent_runs[0].outcome == "finished"
    assert [r.outcome for r in result.provider_retries] == ["recovered"]
    failed, = [c for c in calls(result) if c.get("status") == "error"]
    assert (failed["error_code"], failed["http_status"]) == ("PROVIDER_UNAVAILABLE", 503)


# --------------------------------------------------------------------------- the retry itself (BudgetedGeminiLLM)

DUMMY_KEY = "dummy-key-not-real"


@pytest.fixture
def provider(monkeypatch, tmp_path):
    """The real BudgetedGeminiLLM over a stubbed provider call (no request is made), with its own ledger."""
    from crewai.llms.providers.gemini.completion import GeminiCompletion
    from claimsleuth import execution_mode as em
    monkeypatch.setattr(em, "load_env", lambda: None)
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_LEDGER", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.delenv("CLAIMSLEUTH_GEMINI_RPD", raising=False)
    waits = []
    monkeypatch.setattr(gm, "time", SimpleNamespace(sleep=waits.append, monotonic=time.monotonic))

    def build(*behaviour):
        steps, attempts = list(behaviour), []

        def stub(self, *a, **k):
            attempts.append(1)
            step = steps.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        monkeypatch.setattr(GeminiCompletion.call, "__wrapped__", stub)
        llm = gm.build_llm(gm.settings_from_env({"GOOGLE_API_KEY": DUMMY_KEY}))
        return llm, attempts

    build.waits = waits
    return build


def test_a_503_is_retried_once_through_the_ledger(provider):
    llm, attempts = provider(unavailable(), "the second answer")
    llm.start_budget(60, 8, "RUN-crewrel-retry")
    assert llm.call("x") == "the second answer"
    assert len(attempts) == 2 and llm.calls_made == 2
    assert gemini_rate.run_requests("RUN-crewrel-retry") == {"crewai:forensic_agent": 2}
    assert [r.outcome for r in llm.provider_retries] == ["recovered"]
    assert provider.waits == [gemini_rate.UNAVAILABLE_RETRY_DELAY_S] and llm.provider_retries[0].delay_s == 15.0


def test_a_failed_retry_raises_and_is_not_repeated(provider):
    llm, attempts = provider(unavailable(), unavailable(), "never reached")
    llm.start_budget(60, 8, "RUN-crewrel-twice")
    with pytest.raises(errors.ServerError):
        llm.call("x")
    assert len(attempts) == 2 and [r.outcome for r in llm.provider_retries] == ["failed"]


def test_only_one_retry_per_run(provider):
    llm, attempts = provider(unavailable(), "ok", unavailable(), "never reached")
    llm.start_budget(60, 8, "RUN-crewrel-one")
    assert llm.call("a") == "ok"
    with pytest.raises(errors.ServerError):
        llm.call("b")
    assert len(attempts) == 3 and [r.outcome for r in llm.provider_retries] == ["recovered"]


@pytest.mark.parametrize("error", [
    errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}),
    errors.ClientError(401, {"error": {"code": 401, "message": "key", "status": "UNAUTHENTICATED"}}),
    errors.ServerError(500, {"error": {"code": 500, "message": "boom", "status": "INTERNAL"}}),
    RuntimeError("503 UNAVAILABLE in the text only")], ids=["429", "401", "500", "text"])
def test_nothing_but_a_503_is_retried(provider, error):
    llm, attempts = provider(error, "never reached")
    llm.start_budget(60, 8, "RUN-crewrel-no")
    with pytest.raises(type(error)):
        llm.call("x")
    assert len(attempts) == 1 and llm.provider_retries == [] and provider.waits == []


def test_a_retry_over_the_call_budget_is_not_sent(provider):
    llm, attempts = provider(unavailable(), "never reached")
    llm.start_budget(60, 1, "RUN-crewrel-budget")
    with pytest.raises(errors.ServerError):               # the 503 stands, not a budget error
        llm.call("x")
    assert len(attempts) == 1 and llm.calls_made == 1
    assert [r.outcome for r in llm.provider_retries] == ["refused"]


def test_a_retry_the_daily_budget_refuses_is_not_sent(provider, monkeypatch):
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_RPD", "1")
    llm, attempts = provider(unavailable(), "never reached")
    llm.start_budget(60, 8, "RUN-crewrel-daily")
    with pytest.raises(errors.ServerError):
        llm.call("x")
    assert len(attempts) == 1 and gemini_rate.run_requests("RUN-crewrel-daily") == {"crewai:forensic_agent": 1}
    assert [r.outcome for r in llm.provider_retries] == ["refused"]


def test_the_http_layer_stays_at_one_attempt(provider):
    llm, _ = provider("ok")
    assert llm.client_params["http_options"].retry_options.attempts == 1


def test_tool_times_are_real_utc(mcp, dataset, monkeypatch):
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    result = run(mcp, dataset, monkeypatch, "RUN-crewrel-utc", ForensicTestDoubleLLM(model=MODEL_NAME))
    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert result.status == ExecutionStatus.COMPLETED and result.provider_failures == []
    assert result.agent_runs[0].outcome == "finished"
    for call_ in result.tool_calls:
        assert call_.started_at.utcoffset() == timedelta(0) and before <= call_.started_at <= after
