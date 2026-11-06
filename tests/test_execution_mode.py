"""EXECUTION_MODE (live / offline): resolution, explicit refusal of conflicts, and no silent test-double fallback.

All offline: no Gemini key and no model call is needed (the key, when a test sets one, is a dummy value).
"""
import asyncio

import pytest
from starlette.testclient import TestClient

from claimsleuth import execution_mode as em
from claimsleuth.adk_service import config as adk_config
from claimsleuth.adk_service.runner import run_adk_investigation_async
from claimsleuth.integration import run as integration
from claimsleuth.schemas import AgentRunRecord, AgentSystemResult, ExecutionStatus, utc_now

DUMMY_KEY = "dummy-key-for-tests-0123456789"


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in ("EXECUTION_MODE", "CLAIMSLEUTH_ADK_LLM", "CLAIMSLEUTH_CREW_LLM", "GOOGLE_API_KEY", "GEMINI_API_KEY",
                 "GOOGLE_GENAI_USE_VERTEXAI", "CLAIMSLEUTH_GEMINI_MODEL", "GEMINI_MODEL",
                 "CLAIMSLEUTH_GEMINI_RPM", "CLAIMSLEUTH_GEMINI_RPD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_LEDGER", str(tmp_path / "ledger.sqlite3"))   # never the real ledger
    monkeypatch.setattr(em, "load_env", lambda: None)          # never read the developer's real .env in tests
    monkeypatch.setattr(adk_config, "load_env", lambda: None)
    return monkeypatch


def test_resolution_follows_execution_mode(clean_env):
    assert em.execution_mode({}) is None
    assert em.adk_llm_mode(env={}) == "gemini" and em.crew_llm_mode(env={}) == "test-double"   # unchanged defaults
    live, offline = {"EXECUTION_MODE": "live"}, {"EXECUTION_MODE": " Offline "}
    assert (em.adk_llm_mode(env=live), em.crew_llm_mode(env=live)) == ("gemini", "gemini")
    assert (em.adk_llm_mode(env=offline), em.crew_llm_mode(env=offline)) == ("test-double", "test-double")
    with pytest.raises(em.ExecutionModeError, match="'live' or 'offline'"):
        em.execution_mode({"EXECUTION_MODE": "simulated"})


@pytest.mark.parametrize("env", [
    {"EXECUTION_MODE": "live", "CLAIMSLEUTH_CREW_LLM": "test-double"},
    {"EXECUTION_MODE": "live", "CLAIMSLEUTH_CREW_LLM": "ollama-local"},
    {"EXECUTION_MODE": "offline", "CLAIMSLEUTH_CREW_LLM": "gemini"},
])
def test_conflicting_crew_setting_is_refused(env):
    with pytest.raises(em.ExecutionModeError, match="no fallback"):
        em.crew_llm_mode(env=env)


def test_conflicting_adk_setting_is_refused():
    with pytest.raises(em.ExecutionModeError):
        em.adk_llm_mode(env={"EXECUTION_MODE": "live", "CLAIMSLEUTH_ADK_LLM": "test-double"})
    with pytest.raises(em.ExecutionModeError):
        em.adk_llm_mode("test-double", env={"EXECUTION_MODE": "live"})


def test_credential_check_never_contains_the_key():
    assert em.gemini_config_errors({"GOOGLE_API_KEY": DUMMY_KEY}) == []
    assert em.gemini_config_errors({"GOOGLE_API_KEY": "   "})          # blank is not a key
    problems = em.gemini_config_errors({})
    assert problems and "GOOGLE_API_KEY" in problems[0] and DUMMY_KEY not in problems[0]


def test_live_adk_without_key_is_not_configured_and_runs_nothing(clean_env, dataset):
    clean_env.setenv("EXECUTION_MODE", "live")
    result = asyncio.run(run_adk_investigation_async("CS-003", data_dir=dataset[0]))
    assert result.status == ExecutionStatus.NOT_CONFIGURED and "GOOGLE_API_KEY" in result.errors[0]
    assert result.tool_calls == [] and result.agent_runs == [] and result.model is None


def test_live_adk_never_uses_the_test_double(clean_env, dataset):
    clean_env.setenv("EXECUTION_MODE", "live")
    result = asyncio.run(run_adk_investigation_async("CS-003", data_dir=dataset[0], llm_mode="test-double"))
    assert result.status == ExecutionStatus.NOT_CONFIGURED and "no fallback" in result.errors[0]
    assert result.model != "test-double" and result.tool_calls == []


def test_live_preflight_refuses_missing_key(clean_env):
    with pytest.raises(integration.LiveModeError, match="GOOGLE_API_KEY"):
        integration.live_preflight("http://127.0.0.1:1/.well-known/agent-card.json")


@pytest.mark.parametrize("llm, match", [
    ({}, "does not answer"),
    ({"ok": True, "mode": "test-double", "model": "test-double"}, "mode 'test-double'"),
    ({"ok": False, "mode": "gemini", "error": "No Gemini credentials"}, "No Gemini credentials"),
])
def test_live_preflight_refuses_a_crewai_service_that_is_not_live(clean_env, llm, match):
    clean_env.setenv("GOOGLE_API_KEY", DUMMY_KEY)
    clean_env.setenv("EXECUTION_MODE", "live")
    clean_env.setattr(integration, "crew_service_llm", lambda url: llm)
    with pytest.raises(integration.LiveModeError, match=match):
        integration.live_preflight("http://127.0.0.1:1/.well-known/agent-card.json")


def test_live_run_is_refused_before_anything_runs(clean_env, dataset):
    clean_env.setenv("EXECUTION_MODE", "live")
    with pytest.raises(integration.LiveModeError):
        asyncio.run(integration.run_claim_async("CS-003", data_dir=dataset[0], db_path=dataset[1],
                                                crew_card_url="http://127.0.0.1:1/.well-known/agent-card.json"))


def _result(model: str | None, llm_calls: int) -> AgentSystemResult:
    run = AgentRunRecord(agent="forensic_agent", framework="crewai", started_at=utc_now(), llm_calls=llm_calls)
    # model_construct: only the inference check is under test here, not the report contents
    return AgentSystemResult.model_construct(system="CREWAI", run_id="RUN-x", claim_id="CS-003",
                                             status=ExecutionStatus.COMPLETED, report=object(), errors=[],
                                             model=model, agent_runs=[run])


def test_a_completed_result_without_live_inference_is_marked_failed():
    failed = integration.require_genuine_inference(_result("test-double", 1), "offline-test-double")
    assert failed.status == ExecutionStatus.FAILED and failed.report is None
    assert any("no live model" in e for e in failed.errors) and any("not Gemini" in e for e in failed.errors)
    no_calls = integration.require_genuine_inference(_result("gemini-3.5-flash", 0), em.PROVIDER_GEMINI)
    assert no_calls.status == ExecutionStatus.FAILED and "recorded no model call" in no_calls.errors[-1]
    ok = _result("gemini-3.5-flash", 3)
    assert integration.require_genuine_inference(ok, em.PROVIDER_GEMINI) is ok


def test_dashboard_status_in_live_mode_shows_the_mode_but_never_the_key(clean_env, tmp_path):
    from claimsleuth.dashboard.app import create_app
    from claimsleuth.dashboard.services import ServiceConfig
    from claimsleuth.dashboard.workspace import Workspace
    clean_env.setenv("GOOGLE_API_KEY", DUMMY_KEY)
    ws = Workspace(tmp_path / "ws")
    with TestClient(create_app(ws, ServiceConfig(mcp_port=1, crew_port=2), tmp_path / "rt", live=True)) as client:
        resp = client.get("/api/status")
    status = resp.json()
    assert status["execution_mode"] == "LIVE" and status["adk_mode"] == "gemini"
    assert status["provider"] == "google-gemini" and status["gemini_model"] == em.DEFAULT_GEMINI_MODEL
    assert status["gemini_key_configured"] is True and DUMMY_KEY not in resp.text


def test_dashboard_refuses_a_live_run_when_crewai_is_not_live(clean_env, tmp_path):
    from claimsleuth.dashboard import app as app_module
    from claimsleuth.dashboard.services import ServiceConfig
    from claimsleuth.dashboard.workspace import Workspace
    clean_env.setattr(app_module, "check_mcp", lambda cfg, d: {"name": "MCP server", "status": "RUNNING"})
    clean_env.setattr(app_module, "check_crew", lambda cfg, d: {"name": "CrewAI A2A service", "status": "RUNNING",
                                                               "llm_mode": "test-double", "model": "test-double"})
    ws = Workspace(tmp_path / "ws")
    with TestClient(app_module.create_app(ws, ServiceConfig(mcp_port=1, crew_port=2), tmp_path / "rt",
                                          live=True)) as client:
        claim_id = ws.claims()[0]["claim_id"]
        resp = client.post("/api/runs", json={"claim_id": claim_id})
        status = client.get("/api/status").json()
    assert resp.status_code == 503 and "not Gemini" in resp.json()["error"] and "Nothing was run" in resp.json()["error"]
    assert status["crew_mode_mismatch"] is True


def test_dashboard_live_mode_without_key_exits_before_starting(clean_env, capsys):
    from claimsleuth.dashboard.__main__ import main
    assert main(["--mode", "live", "--no-services", "--no-blackboard"]) == 2
    assert "needs Gemini credentials" in capsys.readouterr().err


def test_dashboard_refuses_live_with_an_ollama_crew_mode(clean_env, capsys):
    from claimsleuth.dashboard.__main__ import main
    assert main(["--mode", "live", "--crew-mode", "ollama-local", "--no-services"]) == 2
    assert "cannot be combined" in capsys.readouterr().err
