"""Offline tests for the ollama-local mode (run with .venv-crew). No inference happens here: the endpoint is
either unreachable or the FakeOllama fixture (tests/crew/fake_ollama.py). Live tests: test_ollama_live.py."""
import socket

import pytest

pytest.importorskip("crewai", reason="CrewAI tests run in .venv-crew")

from starlette.testclient import TestClient  # noqa: E402

from claimsleuth.crew_service import ollama as om  # noqa: E402
from claimsleuth.crew_service.a2a_server import build_app  # noqa: E402
from claimsleuth.crew_service.runner import run_forensic_investigation  # noqa: E402
from claimsleuth.crew_service.test_double import ForensicTestDoubleLLM  # noqa: E402
from claimsleuth.schemas import ExecutionStatus  # noqa: E402
from conftest import free_port  # noqa: E402
from fake_ollama import FakeOllama, local_model  # noqa: E402


@pytest.fixture
def fake():
    server = FakeOllama()
    yield server
    server.stop()


def env_for(monkeypatch, url: str, **extra) -> None:
    monkeypatch.setenv("CLAIMSLEUTH_CREW_LLM", "ollama-local")
    monkeypatch.setenv("CLAIMSLEUTH_OLLAMA_URL", url)
    for key, value in extra.items():
        monkeypatch.setenv(key, str(value))


# --------------------------------------------------------------------------- configuration

def test_defaults_are_conservative_and_local():
    s = om.settings_from_env({})
    assert (s.model, s.base_url, s.temperature) == ("qwen2.5:3b", "http://127.0.0.1:11434/v1", 0.0)
    assert (s.call_timeout_s, s.run_budget_s, s.max_tokens, s.max_iter, s.guardrail_retries) == (360, 900, 1200, 4, 1)


@pytest.mark.parametrize("url", ["http://10.0.0.5:11434/v1", "http://ollama.example.com:11434/v1",
                                 "https://127.0.0.1:11434/v1", "http://127.0.0.1:11434/api",
                                 "http://user@127.0.0.1:11434/v1", "http://0.0.0.0:11434/v1"])
def test_non_loopback_or_wrong_endpoint_is_rejected(url):
    with pytest.raises(om.OllamaConfigError):
        om.settings_from_env({"CLAIMSLEUTH_OLLAMA_URL": url})


@pytest.mark.parametrize("model", ["qwen3:4b-cloud", "gpt-oss:120b-cloud", "registry.example.com/qwen:3b",
                                   "Qwen2.5:3B", "qwen2.5:3b; rm -rf", ""])
def test_cloud_or_malformed_model_names_are_rejected(model):
    with pytest.raises(om.OllamaConfigError):
        om.settings_from_env({"CLAIMSLEUTH_OLLAMA_MODEL": model})


@pytest.mark.parametrize("var, value", [("CLAIMSLEUTH_OLLAMA_CALL_TIMEOUT", "abc"),
                                        ("CLAIMSLEUTH_OLLAMA_CALL_TIMEOUT", "5"),
                                        ("CLAIMSLEUTH_OLLAMA_RUN_BUDGET", "99999"),
                                        ("CLAIMSLEUTH_OLLAMA_MAX_ITER", "50"),
                                        ("CLAIMSLEUTH_OLLAMA_GUARDRAIL_RETRIES", "9")])
def test_invalid_numeric_settings_are_rejected(var, value):
    with pytest.raises(om.OllamaConfigError):
        om.settings_from_env({var: value})


# --------------------------------------------------------------------------- preflight

def test_preflight_ollama_unavailable():
    s = om.OllamaSettings(base_url=f"http://127.0.0.1:{free_port()}/v1")
    with pytest.raises(om.OllamaUnavailableError, match="not reachable"):
        om.preflight(s)


def test_preflight_model_missing_is_not_downloaded():
    server = FakeOllama(models=[local_model("llama3.2:3b")])
    try:
        with pytest.raises(om.OllamaUnavailableError, match="not in the local Ollama list.*will not be downloaded"):
            om.preflight(om.OllamaSettings(base_url=server.base_url))
        assert server.chat_requests == []
    finally:
        server.stop()


@pytest.mark.parametrize("entry, message", [
    (local_model("qwen2.5:3b", remote_host="https://ollama.com:443"), "not a local model"),
    (local_model("qwen2.5:3b", size=0), "not a local model"),
    (local_model("qwen2.5:3b", capabilities=["completion"]), "does not support tool calling"),
])
def test_preflight_rejects_remote_empty_or_toolless_models(entry, message):
    server = FakeOllama(models=[entry])
    try:
        with pytest.raises(om.OllamaUnavailableError, match=message):
            om.preflight(om.OllamaSettings(base_url=server.base_url))
    finally:
        server.stop()


def test_preflight_success_reports_local_model(fake):
    info = om.preflight(om.OllamaSettings(base_url=fake.base_url))
    assert info["model"] == "qwen2.5:3b" and info["ollama_version"] == "fake-0.0" and "tools" in info["capabilities"]


def test_run_budget_is_checked_before_the_model_is_called(fake):
    llm = om.build_llm(om.OllamaSettings(base_url=fake.base_url))
    llm.start_budget(-1)
    with pytest.raises(TimeoutError, match="run budget exhausted"):
        llm.call([{"role": "user", "content": "hi"}])
    assert fake.chat_requests == []


# --------------------------------------------------------------------------- runner behaviour

def test_invalid_configuration_is_not_configured(monkeypatch, dataset):
    env_for(monkeypatch, "http://192.168.1.10:11434/v1")
    result = run_forensic_investigation("CS-003", data_dir=dataset[0])
    assert result.status == ExecutionStatus.NOT_CONFIGURED and "loopback" in result.errors[0]
    assert result.report is None and result.agent_runs == []


def test_ollama_unavailable_fails_without_fallback(monkeypatch, dataset):
    calls = []
    monkeypatch.setattr(ForensicTestDoubleLLM, "call", lambda *a, **k: calls.append(1))
    env_for(monkeypatch, f"http://127.0.0.1:{free_port()}/v1")
    result = run_forensic_investigation("CS-003", data_dir=dataset[0])
    assert result.status == ExecutionStatus.FAILED and "ollama-local unavailable" in result.errors[0]
    assert result.model == "qwen2.5:3b" and result.model != "test-double" and calls == []


def test_unknown_mode_does_not_fall_back(monkeypatch, dataset):
    monkeypatch.setenv("CLAIMSLEUTH_CREW_LLM", "gemini")
    result = run_forensic_investigation("CS-003", data_dir=dataset[0])
    assert result.status == ExecutionStatus.NOT_CONFIGURED and "no fallback" in result.errors[0]
    assert result.model is None


def test_native_tool_loop_is_bounded_and_uses_mcp(monkeypatch, mcp, dataset, fake):
    """The fake model asks for a tool on every turn: CrewAI's real native-tool path must still stop."""
    env_for(monkeypatch, fake.base_url, CLAIMSLEUTH_OLLAMA_MAX_ITER=3, CLAIMSLEUTH_OLLAMA_GUARDRAIL_RETRIES=0)
    result = run_forensic_investigation("CS-003", run_id="RUN-fake-loop", mcp_url=mcp.url, data_dir=dataset[0])
    assert result.status == ExecutionStatus.FAILED and result.report is None
    assert 1 <= len(fake.chat_requests) <= 8                       # bounded, no infinite loop
    assert fake.chat_requests[0]["model"] == "qwen2.5:3b" and fake.chat_requests[0]["tools"]
    mcp_calls = [c for c in result.tool_calls if c.protocol == "mcp"]
    assert mcp_calls and all(c.tool == "claim_history_lookup" for c in mcp_calls)
    logged = [r for r in mcp.log_records() if r["run_id"] == "RUN-fake-loop"]
    assert logged and all(r["caller"] == "crewai:forensic_agent" and r["claim_id"] == "CS-003" for r in logged)
    llm_info = next(t["llm"] for t in result.trace if "llm" in t)
    assert (llm_info["mode"], llm_info["model"], llm_info["base_url"]) == ("ollama-local", "qwen2.5:3b", fake.base_url)
    assert result.model == "qwen2.5:3b"
    assert next(t for t in result.trace if "network_guard" in t)["blocked_hosts"] == []
    run = result.agent_runs[0]                                   # tokens recorded although the run failed
    assert (run.prompt_tokens, run.completion_tokens) == (11 * len(fake.chat_requests), 3 * len(fake.chat_requests))


def test_model_call_timeout_is_a_failed_result(mcp, dataset):
    slow = FakeOllama(behaviour="slow", delay=6)
    try:
        llm = om.LocalOllamaLLM(model="qwen2.5:3b", provider="ollama", base_url=slow.base_url, timeout=2,
                                max_retries=0, max_tokens=256)
        result = run_forensic_investigation("CS-003", run_id="RUN-fake-timeout", llm=llm, mcp_url=mcp.url,
                                            data_dir=dataset[0])
        assert result.status == ExecutionStatus.FAILED and result.report is None
        assert any("timed out" in e.lower() or "timeout" in e.lower() for e in result.errors), result.errors
        assert result.execution_ms < 60_000
    finally:
        slow.stop()


# --------------------------------------------------------------------------- A2A service health

def test_service_health_reflects_ollama_readiness(monkeypatch, mcp, dataset, tmp_path, fake):
    env_for(monkeypatch, fake.base_url)
    with TestClient(build_app("http://127.0.0.1:9999", mcp.url, dataset[0], tmp_path / "a.jsonl")) as client:
        body = client.get("/health").json()
        assert body["status"] == "ok" and body["checks"]["llm"]["model"] == "qwen2.5:3b"
        assert "local Ollama inference" in client.get("/.well-known/agent-card.json").json()["description"]
    env_for(monkeypatch, f"http://127.0.0.1:{free_port()}/v1")
    with TestClient(build_app("http://127.0.0.1:9999", mcp.url, dataset[0], tmp_path / "b.jsonl")) as client:
        response = client.get("/health")
        assert response.status_code == 503 and not response.json()["checks"]["llm"]["ok"]
