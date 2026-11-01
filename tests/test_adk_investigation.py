"""ADK investigation tests.

Offline tests use the labelled ScriptedLlm test double (tests/adk_test_double.py) for the model only;
the ADK Runner, sessions, callbacks, McpToolset and a real MCP server subprocess are all genuine.
The live test (pytest -m live) needs GOOGLE_API_KEY and a running MCP server on 127.0.0.1:8102.
"""
import asyncio
import json
import os

import pytest
from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from adk_test_double import ScriptedLlm, call, text
from claimsleuth.adk_service import config as adk_config
from claimsleuth.adk_service.agents import EVIDENCE_AGENT, INVESTIGATOR_AGENT, build_agents
from claimsleuth.adk_service.callbacks import ExecutionRecorder
from claimsleuth.adk_service.runner import run_adk_investigation_async, save_result
from claimsleuth.schemas import AgentSystemResult, ExecutionStatus
from test_mcp_server import RunningServer, _free_port

CLAIM = "CS-007"     # serial NMB-X15-551230, previously claimed by another claimant (HST-2025-1102)


@pytest.fixture(scope="module")
def mcp(dataset, tmp_path_factory):
    srv = RunningServer(*dataset, tmp_path_factory.mktemp("adk_mcp"))
    yield srv
    srv.stop()


def evidence_report(**over):
    report = {
        "claim_id": CLAIM,
        "facts": [
            {"field": "serial_number", "value": "NMB-X15-551230", "status": "VERIFIED",
             "citation": {"doc_id": "invoice", "line": 10, "quote": "Serial Number: NMB-X15-551230"}},
            {"field": "repair_cost", "value": "₹6,500.00", "status": "VERIFIED",
             "citation": {"doc_id": "repair_report", "line": 12, "quote": "Estimated Cost: ₹6,500.00"}},
        ],
        "findings": [],
        "evidence_citations": [{"doc_id": "invoice", "line": 7, "quote": "Purchase Date: 2025-05-05"}],
        "policy_references": [{"source": "policies.json#/POL-TECH-CARE-01/requires_diagnostic_report", "value": "True"}],
        "missing_information": [],
        "confidence": 0.8,
        "recommended_decision": "APPROVE",
        "reasoning_summary": "Invoice and repair report are present and consistent.",
    }
    return {**report, **over}


def investigation_report(**over):
    report = {
        "claim_id": CLAIM,
        "findings": [{"category": "CLAIM_HISTORY_PATTERN", "severity": "HIGH",
                      "description": "Serial NMB-X15-551230 was claimed before by claimant USR-3305.",
                      "citations": [{"doc_id": "invoice", "line": 10, "quote": "Serial Number: NMB-X15-551230"}]}],
        "evidence_citations": [{"doc_id": "repair_report", "line": 10, "quote": "Serial Number: NMB-X15-551230"}],
        "policy_references": [],
        "history_references": [{"source": "sqlite:claimsleuth.db#claim_history/claim_id=HST-2025-1102",
                                "value": "HST-2025-1102 USR-3305 APPROVED"}],
        "policy_eligibility": "UNDETERMINED",
        "policy_eligibility_reason": "Coverage window looks fine, but the serial history is unresolved.",
        "missing_information": [],
        "confidence": 0.7,
        "recommended_decision": "ESCALATE",
        "reasoning_summary": "Serial previously claimed by a different claimant; needs a human reviewer.",
    }
    return {**report, **over}


def models(evidence_script, investigator_script):
    return {EVIDENCE_AGENT: ScriptedLlm(script=list(evidence_script)),
            INVESTIGATOR_AGENT: ScriptedLlm(script=list(investigator_script))}


def happy_models():
    return models(
        [call("warranty_policy_search", policy_id="POL-TECH-CARE-01"), call("set_model_response", **evidence_report())],
        [call("claim_history_lookup", serial_number="NMB-X15-551230"),
         call("claim_history_lookup", claimant_id="USR-7450"),
         call("set_model_response", **investigation_report())])


def run(mcp, dataset, scripted, run_id, claim_id=CLAIM, mcp_url=None):
    return asyncio.run(run_adk_investigation_async(claim_id, run_id=run_id, model=scripted,
                                                   mcp_url=mcp_url or mcp.url, data_dir=dataset[0]))


# --------------------------------------------------------------------------- construction

def test_agents_are_real_adk_objects_with_schemas_tools_and_callbacks():
    pipeline = build_agents("gemini-3.5-flash", "http://127.0.0.1:1/mcp", ExecutionRecorder("RUN-x"))
    assert isinstance(pipeline, SequentialAgent)
    evidence, investigator = pipeline.sub_agents
    assert all(isinstance(a, LlmAgent) for a in (evidence, investigator))
    assert (evidence.name, investigator.name) == (EVIDENCE_AGENT, INVESTIGATOR_AGENT)
    assert (evidence.output_key, investigator.output_key) == ("evidence_report", "investigation_report")
    assert evidence.output_schema.__name__ == "EvidenceReport"
    assert investigator.output_schema.__name__ == "InvestigationReport"
    assert "{evidence_report?}" in investigator.instruction          # consumes the upstream output
    for agent in (evidence, investigator):
        assert isinstance(agent.tools[0], McpToolset)
        assert agent.before_agent_callback and agent.after_tool_callback and agent.on_tool_error_callback


# --------------------------------------------------------------------------- full offline run

@pytest.fixture(scope="module")
def happy(mcp, dataset):
    scripted = happy_models()
    return run(mcp, dataset, scripted, "RUN-adk-happy-01"), scripted


def test_runner_completes_with_two_validated_reports(happy):
    result, _ = happy
    assert result.status == ExecutionStatus.COMPLETED, result.errors
    assert result.model == "test-double"
    assert result.evidence_report.facts[0].value == "NMB-X15-551230"
    assert result.report.recommended_decision.value == "ESCALATE"
    assert result.unsupported_references == []


def test_session_lifecycle_is_recorded(happy):
    result, _ = happy
    lifecycle = [t["lifecycle"] for t in result.trace if "lifecycle" in t]
    assert lifecycle == ["session_created", "runner_closed", "session_deleted"]
    assert result.session_id and any(t.get("final") for t in result.trace)


def test_mcp_tools_discovered_by_adk(happy):
    tools = next(t["tools_offered_to_model"] for t in happy[0].trace if "tools_offered_to_model" in t)
    assert tools[EVIDENCE_AGENT] == ["set_model_response", "warranty_policy_search"]
    assert tools[INVESTIGATOR_AGENT] == ["claim_history_lookup", "set_model_response", "warranty_policy_search"]


def test_mcp_tools_invoked_by_adk_with_real_results(happy):
    calls = [c for c in happy[0].tool_calls if c.protocol == "mcp"]
    assert [(c.agent, c.tool) for c in calls] == [(EVIDENCE_AGENT, "warranty_policy_search"),
                                                  (INVESTIGATOR_AGENT, "claim_history_lookup"),
                                                  (INVESTIGATOR_AGENT, "claim_history_lookup")]
    by_serial = calls[1].result
    assert [r["claim_id"] for r in by_serial["records"]] == ["HST-2025-1102"]
    assert calls[0].result["matches"][0]["terms"]["deductible"] == "2500.00"
    assert all(c.status == "ok" and c.latency_ms > 0 for c in calls)


def test_run_id_correlates_adk_records_and_mcp_server_log(happy, mcp):
    result, _ = happy
    assert all(c.run_id == "RUN-adk-happy-01" for c in result.tool_calls)
    logged = [r for r in mcp.log_records() if r["run_id"] == "RUN-adk-happy-01"]
    assert [r["tool"] for r in logged] == ["warranty_policy_search", "claim_history_lookup", "claim_history_lookup"]
    assert all(r["run_id_source"] == "http_header" and r["status"] == "ok" for r in logged)


def test_agent_execution_records(happy):
    runs = {r.agent: r for r in happy[0].agent_runs}
    assert set(runs) == {EVIDENCE_AGENT, INVESTIGATOR_AGENT}
    assert (runs[EVIDENCE_AGENT].llm_calls, runs[INVESTIGATOR_AGENT].llm_calls) == (2, 3)
    assert (runs[EVIDENCE_AGENT].tool_calls, runs[INVESTIGATOR_AGENT].tool_calls) == (2, 3)  # incl. set_model_response
    assert runs[EVIDENCE_AGENT].prompt_tokens == 200 and happy[0].usage.total_tokens == 600  # test-double counts
    assert runs[EVIDENCE_AGENT].finished_at <= runs[INVESTIGATOR_AGENT].started_at       # sequential


def test_investigator_received_the_evidence_report(happy):
    _, scripted = happy
    instruction = scripted[INVESTIGATOR_AGENT].system_instructions()[0]
    assert "Invoice and repair report are present and consistent." in instruction


def test_documents_reach_the_model_as_untrusted_numbered_lines(happy):
    _, scripted = happy
    first = scripted[EVIDENCE_AGENT].requests[0]
    user_text = " ".join(p.text or "" for c in first.contents for p in (c.parts or []))
    assert '<untrusted source="doc:invoice">' in user_text and "invoice#L10: Serial Number: NMB-X15-551230" in user_text


def test_result_is_saved_as_json_and_markdown(happy, tmp_path):
    out = save_result(happy[0], tmp_path)
    loaded = AgentSystemResult.model_validate_json((out / "adk.json").read_text(encoding="utf-8"))
    assert loaded.report == happy[0].report
    assert "# GOOGLE ADK RESULT" in (out / "adk.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- failure handling

def test_mcp_tool_error_is_recorded_and_run_continues(mcp, dataset):
    scripted = models(
        [call("warranty_policy_search", policy_id="POL-TECH-CARE-01"), call("set_model_response", **evidence_report())],
        [call("claim_history_lookup", claimant_id="USR-7450", limit=0),
         call("set_model_response", **investigation_report(history_references=[]))])
    result = run(mcp, dataset, scripted, "RUN-adk-toolerr-01")
    failed = [c for c in result.tool_calls if c.status == "error"]
    assert result.status == ExecutionStatus.COMPLETED
    assert len(failed) == 1 and failed[0].tool == "claim_history_lookup" and "greater than or equal to 1" in failed[0].error
    assert any("claim_history_lookup" in e for r in result.agent_runs for e in r.errors)


def test_missing_mcp_server_fails_before_running(dataset):
    scripted = happy_models()
    result = asyncio.run(run_adk_investigation_async(CLAIM, run_id="RUN-adk-nomcp-01", model=scripted,
                                                     mcp_url=f"http://127.0.0.1:{_free_port()}/mcp",
                                                     data_dir=dataset[0]))
    assert result.status == ExecutionStatus.FAILED and result.report is None
    assert "MCP server not available" in result.errors[0]
    assert scripted[EVIDENCE_AGENT].requests == []       # no model call was made


def test_invalid_agent_output_fails_the_run(mcp, dataset):
    scripted = models(
        [call("set_model_response", **evidence_report())],
        [call("set_model_response", **investigation_report(confidence=5)),     # rejected by ADK validation
         text("I could not produce the report.")])
    result = run(mcp, dataset, scripted, "RUN-adk-invalid-01")
    assert result.status == ExecutionStatus.FAILED and result.report is None
    assert any(INVESTIGATOR_AGENT in e for e in result.errors)
    rejected = [c for c in result.tool_calls if c.tool == "set_model_response" and c.status == "error"]
    assert rejected and "Validation Error" in rejected[0].error


def test_wrong_claim_id_fails_the_run(mcp, dataset):
    scripted = models([call("set_model_response", **evidence_report(claim_id="CS-001"))],
                      [call("set_model_response", **investigation_report())])
    result = run(mcp, dataset, scripted, "RUN-adk-claimid-01")
    assert result.status == ExecutionStatus.FAILED
    assert any("expected 'CS-007'" in e for e in result.errors)


def test_fabricated_citations_and_references_are_flagged(mcp, dataset):
    fake_fact = {"field": "serial_number", "value": "FAKE-1", "status": "VERIFIED",
                 "citation": {"doc_id": "invoice", "line": 10, "quote": "Serial Number: FAKE-1"}}
    scripted = models(
        [call("warranty_policy_search", policy_id="POL-TECH-CARE-01"),
         call("set_model_response", **evidence_report(
             facts=[fake_fact],
             policy_references=[{"source": "policies.json#/POL-TECH-CARE-01/deductible", "value": "0.00"}]))],
        [call("set_model_response", **investigation_report())])          # never called claim_history_lookup
    result = run(mcp, dataset, scripted, "RUN-adk-fabricated-01")
    problems = "\n".join(result.unsupported_references)
    assert "invoice#L10 'Serial Number: FAKE-1' not found" in problems
    assert "has value '2500.00' in the MCP result" in problems
    assert "claim_id=HST-2025-1102' was not returned by any MCP tool" in problems


def test_not_configured_without_credentials(monkeypatch, dataset):
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(adk_config, "load_env", lambda: None)
    result = asyncio.run(run_adk_investigation_async(CLAIM, data_dir=dataset[0]))
    assert result.status == ExecutionStatus.NOT_CONFIGURED and "GOOGLE_API_KEY" in result.errors[0]
    assert result.tool_calls == [] and result.agent_runs == []


# --------------------------------------------------------------------------- live (real Gemini)

@pytest.mark.live
def test_live_gemini_investigation():
    adk_config.load_env()
    if adk_config.llm_config_errors():
        pytest.skip("no Gemini credentials configured")
    result = asyncio.run(run_adk_investigation_async("CS-007", run_id=f"RUN-live-{os.getpid()}"))
    print(json.dumps(result.model_dump(mode="json", exclude={"trace"}), indent=2)[:4000])
    assert result.status == ExecutionStatus.COMPLETED, result.errors
    assert result.model and result.model != "test-double"
    assert any(c.protocol == "mcp" and c.status == "ok" for c in result.tool_calls)
    assert result.usage.prompt_tokens and result.usage.prompt_tokens > 0
