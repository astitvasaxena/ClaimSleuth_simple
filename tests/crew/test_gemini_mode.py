"""The CrewAI Forensic Agent's Gemini mode (EXECUTION_MODE=live), offline: configuration, refusal of conflicting
settings, the network guard's single allowed host and the per-run call budget. No model call is made here; the
live run is in test_gemini_live.py (marked `live`)."""
import socket

import pytest

pytest.importorskip("crewai", reason="CrewAI tests run in .venv-crew")

from claimsleuth import execution_mode as em  # noqa: E402
from claimsleuth.crew_service import a2a_server  # noqa: E402
from claimsleuth.crew_service import gemini as gm  # noqa: E402
from claimsleuth.crew_service.network_guard import NetworkBlockedError, loopback_only  # noqa: E402
from claimsleuth.crew_service.runner import run_forensic_investigation  # noqa: E402
from claimsleuth.crew_service.test_double import ForensicTestDoubleLLM  # noqa: E402
from claimsleuth.schemas import ExecutionStatus  # noqa: E402

DUMMY_KEY = "dummy-key-for-tests-0123456789"


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in ("EXECUTION_MODE", "CLAIMSLEUTH_CREW_LLM", "GOOGLE_API_KEY", "GEMINI_API_KEY",
                 "GOOGLE_GENAI_USE_VERTEXAI", "CLAIMSLEUTH_GEMINI_MODEL", "GEMINI_MODEL",
                 "CLAIMSLEUTH_GEMINI_RPM", "CLAIMSLEUTH_GEMINI_RPD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(em, "load_env", lambda: None)          # never read the developer's real .env in tests
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_LEDGER", str(tmp_path / "ledger.sqlite3"))   # never the real ledger
    return monkeypatch


def stub_provider(monkeypatch, behaviour):
    """Replace the provider's unwrapped call (the one BudgetedGeminiLLM uses) so no request can leave the test."""
    from crewai.llms.providers.gemini.completion import GeminiCompletion
    monkeypatch.setattr(GeminiCompletion.call, "__wrapped__", behaviour)


def test_settings_need_a_key_and_stay_in_range(clean_env):
    with pytest.raises(gm.GeminiConfigError, match="GOOGLE_API_KEY"):
        gm.settings_from_env({})
    settings = gm.settings_from_env({"GOOGLE_API_KEY": DUMMY_KEY})
    assert settings.model == em.DEFAULT_GEMINI_MODEL and settings.max_calls == 4 and settings.guardrail_retries == 1
    assert DUMMY_KEY not in str(settings.public())
    with pytest.raises(gm.GeminiConfigError, match="outside the allowed range"):
        gm.settings_from_env({"GOOGLE_API_KEY": DUMMY_KEY, "CLAIMSLEUTH_GEMINI_MAX_CALLS": "500"})
    with pytest.raises(gm.GeminiConfigError, match="not a Gemini model"):
        gm.settings_from_env({"GOOGLE_API_KEY": DUMMY_KEY, "CLAIMSLEUTH_GEMINI_MODEL": "gpt-4o"})


def test_live_mode_without_key_is_not_configured_and_never_uses_the_test_double(clean_env, dataset):
    calls = []
    clean_env.setattr(ForensicTestDoubleLLM, "call", lambda *a, **k: calls.append(1))
    clean_env.setenv("EXECUTION_MODE", "live")
    result = run_forensic_investigation("CS-003", data_dir=dataset[0])
    assert result.status == ExecutionStatus.NOT_CONFIGURED and "GOOGLE_API_KEY" in result.errors[0]
    assert calls == [] and result.tool_calls == [] and result.model != "test-double"


def test_live_mode_refuses_a_conflicting_crew_setting(clean_env, dataset):
    clean_env.setenv("EXECUTION_MODE", "live")
    clean_env.setenv("CLAIMSLEUTH_CREW_LLM", "test-double")
    result = run_forensic_investigation("CS-003", data_dir=dataset[0])
    assert result.status == ExecutionStatus.NOT_CONFIGURED and "no fallback" in result.errors[0]


def test_health_reports_gemini_readiness_without_the_key_or_a_model_call(clean_env):
    clean_env.setenv("EXECUTION_MODE", "live")
    assert a2a_server.llm_check() == {"ok": False, "mode": "gemini", "error": em.gemini_config_errors({})[0]}
    clean_env.setenv("GOOGLE_API_KEY", DUMMY_KEY)
    check = a2a_server.llm_check()
    assert check["ok"] and check["mode"] == "gemini" and check["provider"] == "google-gemini"
    assert DUMMY_KEY not in str(check) and "Gemini" in a2a_server._model_description()
    clean_env.setenv("CLAIMSLEUTH_CREW_LLM", "test-double")
    assert a2a_server.llm_check()["ok"] is False                     # conflict is reported, not hidden


def test_guard_allows_only_the_named_host_and_its_resolved_addresses(monkeypatch):
    fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.9", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: fake)
    with loopback_only(("api.example.test",)) as blocked:
        assert socket.getaddrinfo("api.example.test", 443) == fake
        with pytest.raises(NetworkBlockedError):
            socket.getaddrinfo("other.example.test", 443)
        with pytest.raises(NetworkBlockedError):
            socket.socket().connect(("198.51.100.7", 443))           # not an address of the allowed host
    assert blocked == ["other.example.test", "198.51.100.7"]
    with loopback_only() as blocked:                                 # offline: nothing external at all
        with pytest.raises(NetworkBlockedError):
            socket.getaddrinfo("api.example.test", 443)


def test_call_budget_stops_further_model_calls(clean_env):
    stub_provider(clean_env, lambda self, *a, **k: "stub (no request made)")
    llm = gm.build_llm(gm.settings_from_env({"GOOGLE_API_KEY": DUMMY_KEY}))
    llm.start_budget(60, 2)
    assert llm.call("a") == llm.call("b") == "stub (no request made)" and llm.calls_made == 2
    with pytest.raises(gm.GeminiBudgetExceeded, match="call budget of 2"):
        llm.call("c")
    llm.start_budget(0, 5)
    with pytest.raises(gm.GeminiBudgetExceeded, match="time"):
        llm.call("d")


def test_a_rate_limited_request_is_sent_exactly_once(clean_env):
    """CrewAI wraps provider calls in a 3-attempt rate-limit retry; ClaimSleuth's Gemini LLM bypasses it."""
    from google.genai import errors
    attempts = []

    def throttled(self, *a, **k):
        attempts.append(1)
        raise errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                 "message": "Generate content requests per minute limit exceeded"}})
    stub_provider(clean_env, throttled)
    llm = gm.build_llm(gm.settings_from_env({"GOOGLE_API_KEY": DUMMY_KEY}))
    llm.start_budget(60, 8, "RUN-once")
    with pytest.raises(errors.ClientError):
        llm.call("x")
    assert attempts == [1] and llm.calls_made == 1
    assert llm.client_params["http_options"].retry_options.attempts == 1        # no HTTP-level retry either


def test_every_request_takes_one_slot_in_the_shared_ledger(clean_env):
    from claimsleuth import gemini_rate
    stub_provider(clean_env, lambda self, *a, **k: "ok")
    llm = gm.build_llm(gm.settings_from_env({"GOOGLE_API_KEY": DUMMY_KEY}))
    llm.start_budget(60, 8, "RUN-ledger")
    llm.call("a")
    llm.call("b")
    assert gemini_rate.run_requests("RUN-ledger") == {"crewai:forensic_agent": 2}
    clean_env.setenv("CLAIMSLEUTH_GEMINI_RPD", "2")                     # daily budget used: refused, not sent
    with pytest.raises(gemini_rate.GeminiQuotaGuard, match="no request was sent"):
        llm.call("c")
    assert llm.calls_made == 2
