"""LIVE Gemini inference for the CrewAI Forensic Agent. Not run by default (costs API quota).

    .venv-crew\\Scripts\\python -m pytest -m live tests\\crew\\test_gemini_live.py -s

Needs GOOGLE_API_KEY in the environment or .env; skips without it.
"""
import pytest

pytest.importorskip("crewai", reason="CrewAI tests run in .venv-crew")

from claimsleuth import execution_mode as em  # noqa: E402
from claimsleuth.crew_service.runner import run_forensic_investigation  # noqa: E402
from claimsleuth.schemas import ExecutionStatus  # noqa: E402


@pytest.mark.live
def test_live_gemini_forensic_review(monkeypatch, mcp, dataset):
    em.load_env()
    if em.gemini_config_errors():
        pytest.skip("no Gemini credentials configured")
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.delenv("CLAIMSLEUTH_CREW_LLM", raising=False)
    result = run_forensic_investigation("CS-003", run_id="RUN-live-gemini-cs003", mcp_url=mcp.url,
                                        data_dir=dataset[0])
    print(result.model_dump_json(indent=2, exclude={"trace"})[:6000])
    assert result.status == ExecutionStatus.COMPLETED, result.errors
    assert result.report.provider == "google-gemini" and result.model.startswith("gemini-")
    model_calls = next(t["model_calls"] for t in result.trace if "model_calls" in t)
    assert model_calls and all(c["model"].startswith("gemini-") for c in model_calls)
    assert any(c.tool == "claim_history_lookup" and c.status == "ok" for c in result.tool_calls)
    assert any(r["run_id"] == "RUN-live-gemini-cs003" for r in mcp.log_records())   # MCP server saw the calls
    assert next(t for t in result.trace if "network_guard" in t)["blocked_hosts"] == []
