"""CrewAI Forensic Agent tests (offline). Run with:  .venv-crew\\Scripts\\python -m pytest tests\\crew

The LLM is the labelled ForensicTestDoubleLLM; Agent, Task, Crew.kickoff(), CrewAI's native MCP client,
callbacks and the MCP server process are all real. The main .venv has no crewai, so it skips this module.
"""
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

pytest.importorskip("crewai", reason="CrewAI tests run in .venv-crew")

import claimsleuth.crew_service  # noqa: E402,F401
from crewai import Agent, Crew, Process, Task  # noqa: E402

from claimsleuth.crew_service.crew import build_crew  # noqa: E402
from claimsleuth.crew_service.network_guard import NetworkBlockedError, loopback_only  # noqa: E402
from claimsleuth.crew_service.runner import run_forensic_investigation, save_result  # noqa: E402
from claimsleuth.crew_service.test_double import MODEL_NAME, PROVIDER, ForensicTestDoubleLLM  # noqa: E402
from claimsleuth.datastore import load_claim  # noqa: E402
from claimsleuth.render import crewai_markdown  # noqa: E402
from claimsleuth.schemas import AgentSystemResult, ExecutionStatus, ForensicsReport  # noqa: E402
from conftest import free_port  # noqa: E402

_cache: dict = {}


def run(mcp, dataset, claim_id, **kw):
    key = (claim_id, tuple(sorted(kw.items())))
    if not kw and key in _cache:
        return _cache[key]
    result = run_forensic_investigation(claim_id, run_id=f"RUN-crewtest-{claim_id}-{len(_cache)}",
                                        mcp_url=mcp.url, data_dir=dataset[0], **kw)
    if not kw:
        _cache[key] = result
    return result


def double(mode="forensic"):
    return ForensicTestDoubleLLM(model=MODEL_NAME, mode=mode)


# --------------------------------------------------------------------------- objects

def test_agent_task_and_crew_are_real_crewai_objects(dataset):
    claim, docs = load_claim("CS-003", dataset[0])
    agent, task, crew = build_crew(double(), "http://127.0.0.1:1/mcp", "RUN-x", "CS-003", docs, set,
                                   lambda s: None, lambda o: None)
    assert isinstance(agent, Agent) and isinstance(task, Task) and isinstance(crew, Crew)
    assert agent.llm.model == "test-double" and isinstance(agent.llm, ForensicTestDoubleLLM)
    assert agent.mcps[0].url == "http://127.0.0.1:1/mcp" and agent.mcps[0].headers == {"X-ClaimSleuth-Run-Id": "RUN-x", "X-ClaimSleuth-Claim-Id": "CS-003",
                                     "X-ClaimSleuth-Caller": "crewai:forensic_agent"}
    assert task.output_pydantic.__name__ == "ForensicsAnalysis" and task.guardrail is not None
    assert task.agent is agent and crew.process == Process.sequential
    assert (len(crew.agents), len(crew.tasks)) == (1, 1)


# --------------------------------------------------------------------------- real kickoff

def test_kickoff_completes_and_finds_serial_mismatch(mcp, dataset):
    result = run(mcp, dataset, "CS-003")
    assert result.status == ExecutionStatus.COMPLETED, result.errors
    rep = result.report
    serial = next(c for c in rep.serial_number_checks if c.check.startswith("invoice serial"))
    assert serial.result == "INCONSISTENT"
    assert [(c.doc_id, c.line) for c in serial.citations] == [("invoice", 10), ("repair_report", 10)]
    assert rep.recommended_outcome.value == "ESCALATE"


def test_kickoff_execution_is_recorded(mcp, dataset):
    result = run(mcp, dataset, "CS-003")
    agent = result.agent_runs[0]
    assert (agent.agent, agent.framework, agent.llm_calls, agent.tool_calls) == ("forensic_agent", "crewai", 3, 2)
    assert agent.latency_ms > 0 and agent.finished_at >= agent.started_at
    steps = [t.get("step") for t in result.trace if "step" in t]
    assert "AgentAction" in steps and "task_completed" in steps
    crew_info = next(t["crew"] for t in result.trace if "crew" in t)
    assert crew_info == {"agents": ["Warranty claim forensic analyst"], "tasks": 1, "process": "sequential",
                         "llm": "ForensicTestDoubleLLM"}


def test_mcp_calls_are_real_and_correlated_by_run_id(mcp, dataset):
    result = run(mcp, dataset, "CS-003")
    calls = result.tool_calls
    assert [(c.tool, c.protocol, c.status) for c in calls] == [("claim_history_lookup", "mcp", "ok")] * 2
    assert calls[0].arguments == {"serial_number": "FRL-RF-77100-A"} and calls[1].arguments == {"claimant_id": "USR-8182"}
    assert all(c.run_id == result.run_id and c.latency_ms > 0 for c in calls)
    logged = [r for r in mcp.log_records() if r["run_id"] == result.run_id]
    assert [(r["tool"], r["run_id_source"], r["status"]) for r in logged] == [("claim_history_lookup", "http_header", "ok")] * 2


def test_result_is_labelled_test_double(mcp, dataset):
    result = run(mcp, dataset, "CS-003")
    assert result.model == "test-double" and result.report.model == "test-double"
    assert result.report.provider == PROVIDER == "offline-test-double"
    assert result.report.execution_status == ExecutionStatus.COMPLETED
    assert "TEST DOUBLE, not a language model" in crewai_markdown(result)


def test_no_real_llm_network_access(mcp, dataset):
    result = run(mcp, dataset, "CS-003")
    guard = next(t for t in result.trace if "network_guard" in t)
    assert guard == {"ts": guard["ts"], "network_guard": "loopback_only", "blocked_hosts": []}


def test_network_guard_blocks_llm_providers_but_not_localhost(mcp):
    with loopback_only() as blocked:
        for host in ("generativelanguage.googleapis.com", "openrouter.ai", "api.openai.com"):
            with pytest.raises((NetworkBlockedError, urllib.error.URLError), match="is blocked"):
                urllib.request.urlopen(f"https://{host}/", timeout=3)
        with urllib.request.urlopen(mcp.url.replace("/mcp", "/health"), timeout=3) as resp:
            assert resp.status == 200
    assert {"generativelanguage.googleapis.com", "openrouter.ai", "api.openai.com"} <= set(blocked)


# --------------------------------------------------------------------------- structured output

def test_report_validates_and_has_no_payout_field(mcp, dataset, tmp_path):
    result = run(mcp, dataset, "CS-003")
    assert ForensicsReport.model_validate_json(result.report.model_dump_json()) == result.report
    assert not {"payout", "amount", "settlement", "approved"} & set(ForensicsReport.model_fields)
    out = save_result(result, tmp_path)
    assert AgentSystemResult.model_validate_json((out / "crewai.json").read_text(encoding="utf-8")).report == result.report
    assert (out / "crewai.md").exists()


def test_unparseable_output_is_rejected_by_crewai_output_conversion(mcp, dataset):
    """Not-JSON output fails in CrewAI's output_pydantic converter, before the guardrail runs."""
    result = run(mcp, dataset, "CS-003", llm=double("invalid_json"))
    assert result.status == ExecutionStatus.FAILED and result.report is None
    assert any("ConverterError" in e for e in result.errors), result.errors


@pytest.mark.parametrize("mode, message", [
    ("fabricated_citation", "do not match the documents"),
    ("wrong_claim", "claim_id must be CS-003"),
])
def test_invalid_output_is_rejected_by_the_task_guardrail(mcp, dataset, mode, message):
    llm = double(mode)
    result = run(mcp, dataset, "CS-003", llm=llm)
    assert result.status == ExecutionStatus.FAILED and result.report is None
    assert any("guardrail validation after 2 retries" in e and message in e for e in result.errors), result.errors
    assert llm.calls >= 5          # 3 calls for the first attempt + one per guardrail retry


# --------------------------------------------------------------------------- forensic coverage

def test_missing_documents(mcp, dataset):
    rep = run(mcp, dataset, "CS-006").report
    assert "diagnostic / repair report" in rep.missing_information
    assert {f.category for f in rep.findings} == {"EVIDENCE_GAP"} and rep.recommended_outcome.value == "REQUEST_EVIDENCE"
    assert all(c.result == "NOT_EVALUABLE" for c in rep.serial_number_checks if c.check.startswith("invoice serial"))


def test_chronology_mismatch(mcp, dataset):
    rep = run(mcp, dataset, "CS-009").report
    bad = [c for c in rep.chronology_checks if c.result == "INCONSISTENT"]
    assert [c.check for c in bad] == ["incident date before inspection date"]
    assert bad[0].citations[0].doc_id == "repair_report" and bad[0].citations[0].line == 6
    assert any(f.category == "DATE_CONFLICT" for f in rep.findings)


def test_conflicting_invoice_and_repair_report(mcp, dataset, tmp_path):
    data = tmp_path / "data"
    shutil.copytree(dataset[0], data)
    report = data / "claims" / "CS-001" / "repair_report.txt"
    report.write_text(report.read_text(encoding="utf-8").replace("Client: Elena Rostova", "Client: Viktor Hale"),
                      encoding="utf-8", newline="\n")
    rep = run_forensic_investigation("CS-001", run_id="RUN-crewtest-conflict", mcp_url=mcp.url, data_dir=data).report
    names = [f for f in rep.findings if f.category == "SUSPICIOUS_DETAIL" and "Viktor Hale" in f.description]
    assert names and [(c.doc_id, c.line) for c in names[0].citations] == [("invoice", 5), ("repair_report", 7)]


def test_historical_claim_patterns_come_from_mcp(mcp, dataset):
    other = run(mcp, dataset, "CS-007").report
    pattern = other.historical_pattern_findings[0]
    assert pattern.pattern == "SAME_SERIAL_OTHER_CLAIMANT"
    assert [r.source for r in pattern.references] == ["sqlite:claimsleuth.db#claim_history/claim_id=HST-2025-1102"]
    assert other.recommended_outcome.value == "ESCALATE"
    same = run(mcp, dataset, "CS-005").report
    assert same.historical_pattern_findings[0].pattern == "PRIOR_CLAIM_SAME_CLAIMANT"


def test_injection_in_a_document_is_flagged_not_obeyed(mcp, dataset):
    rep = run(mcp, dataset, "CS-004-INJ").report
    flagged = [f for f in rep.findings if f.category == "SUSPICIOUS_DETAIL"]
    assert flagged and flagged[0].citations[0].doc_id == "repair_report"
    assert rep.recommended_outcome.value == "ESCALATE"


# --------------------------------------------------------------------------- configuration and failure

def test_unknown_llm_mode_is_not_configured(monkeypatch, dataset):
    monkeypatch.setenv("CLAIMSLEUTH_CREW_LLM", "live")
    result = run_forensic_investigation("CS-003", data_dir=dataset[0])
    assert result.status == ExecutionStatus.NOT_CONFIGURED and "is not a known mode" in result.errors[0]
    assert result.tool_calls == [] and result.agent_runs == []


def test_missing_mcp_server_fails_before_kickoff(dataset):
    llm = double()
    result = run_forensic_investigation("CS-003", llm=llm, mcp_url=f"http://127.0.0.1:{free_port()}/mcp",
                                        data_dir=dataset[0])
    assert result.status == ExecutionStatus.FAILED and "MCP server not available" in result.errors[0]
    assert llm.calls == 0


def test_crewai_result_is_independent_of_baseline_and_adk():
    code = ("import sys, claimsleuth.crew_service.runner; "
            "print(sorted(m for m in sys.modules if m.startswith(('claimsleuth.baseline', 'claimsleuth.adk_service', "
            "'google.adk'))))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"
