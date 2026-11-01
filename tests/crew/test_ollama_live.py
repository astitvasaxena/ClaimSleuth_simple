"""LIVE local inference (qwen2.5:3b through Ollama on 127.0.0.1). Not run by default.

    .venv-crew\\Scripts\\python -m pytest -m live tests\\crew\\test_ollama_live.py -s

Takes several minutes on a CPU-only laptop. Skips when Ollama or the model is not available locally.
"""
import pytest

pytest.importorskip("crewai", reason="CrewAI tests run in .venv-crew")

from claimsleuth.crew_service import ollama as om  # noqa: E402
from claimsleuth.crew_service.runner import run_forensic_investigation  # noqa: E402
from claimsleuth.schemas import ExecutionStatus  # noqa: E402


@pytest.mark.live
def test_live_cs003_forensic_review(monkeypatch, mcp, dataset):
    try:
        om.preflight(om.settings_from_env({}))
    except (om.OllamaConfigError, om.OllamaUnavailableError) as exc:
        pytest.skip(f"local Ollama model not available: {exc}")
    monkeypatch.setenv("CLAIMSLEUTH_CREW_LLM", "ollama-local")
    result = run_forensic_investigation("CS-003", run_id="RUN-live-pytest-cs003", mcp_url=mcp.url,
                                        data_dir=dataset[0])
    print(result.model_dump_json(indent=2, exclude={"trace"})[:6000])
    model_calls = next((t["model_calls"] for t in result.trace if "model_calls" in t), [])
    assert result.model == "qwen2.5:3b" and result.model != "test-double"
    assert model_calls and all(c["completion_tokens"] for c in model_calls)          # tokens reported by Ollama
    assert any(r["run_id"] == "RUN-live-pytest-cs003" for r in mcp.log_records())   # MCP server saw the calls
    assert next(t for t in result.trace if "network_guard" in t)["blocked_hosts"] == []
    assert result.status == ExecutionStatus.COMPLETED, result.errors
    assert result.report.provider == "ollama-local" and result.report.model == "qwen2.5:3b"
    assert result.report.findings or result.report.serial_number_checks
