"""Deterministic adjudicator rules, with hand-built agent results (no services needed)."""
from decimal import Decimal

import pytest

from claimsleuth.baseline import run_baseline
from claimsleuth.datastore import load_claim, load_policies
from claimsleuth.history import lookup_history
from claimsleuth.integration.adjudicator import adjudicate
from claimsleuth.integration.mcp_records import validate_records
from claimsleuth.schemas import (AgentSystemResult, AgentToolCallRecord, Decision, ExecutionStatus, ForensicsReport,
                                 InvestigationReport, MCPResult, MCPToolCall, utc_now)

RUN = "RUN-adj-0001"
SERIAL_FINDING = {"category": "SERIAL_INCONSISTENCY", "severity": "HIGH", "description": "serials differ",
                  "citations": [{"doc_id": "invoice", "line": 10, "quote": "Serial Number: NMB-X15-998821"}]}
FAKE_FINDING = {"category": "SERIAL_INCONSISTENCY", "severity": "HIGH", "description": "made-up mismatch",
                "citations": [{"doc_id": "invoice", "line": 10, "quote": "Serial Number: FAKE-0000"}]}


def adk(claim_id, decision="APPROVE", findings=(), history_refs=(), status=ExecutionStatus.COMPLETED, tool_calls=()):
    report = InvestigationReport(claim_id=claim_id, findings=list(findings), evidence_citations=[],
                                 history_references=list(history_refs), policy_eligibility="ELIGIBLE",
                                 policy_eligibility_reason="r", missing_information=[], confidence=0.7,
                                 recommended_decision=decision, reasoning_summary="s")
    return AgentSystemResult(system="GOOGLE_ADK", run_id=RUN, claim_id=claim_id, status=status, model="test-double",
                             report=report if status == ExecutionStatus.COMPLETED else None,
                             tool_calls=list(tool_calls))


def crew(claim_id, decision="APPROVE", findings=(), patterns=(), tool_calls=()):
    report = ForensicsReport(claim_id=claim_id, findings=list(findings), evidence_references=[], serial_number_checks=[],
                             chronology_checks=[], historical_pattern_findings=list(patterns), missing_information=[],
                             uncertainty=[], confidence=0.8, recommended_outcome=decision, reasoning_summary="s",
                             model="test-double", provider="offline-test-double",
                             execution_status=ExecutionStatus.COMPLETED)
    return AgentSystemResult(system="CREWAI", run_id=RUN, claim_id=claim_id, status=ExecutionStatus.COMPLETED,
                             model="test-double", report=report, tool_calls=list(tool_calls))


def mcp(claim_id, calls=()):
    return MCPResult(run_id=RUN, claim_id=claim_id, status=ExecutionStatus.COMPLETED, calls=list(calls))


def judge(dataset, claim_id, adk_result="default", crew_result="default", mcp_result=None, baseline="default"):
    data_dir, db = dataset
    claim, docs = load_claim(claim_id, data_dir)
    policy = load_policies(data_dir).get(claim.policy_id)
    serial = {"CS-007": "NMB-X15-551230", "CS-005": "FRL-WM-302118"}.get(claim_id)
    history = lookup_history(serial, claim.claimant_id, claim_id, db).records if serial else []
    base = run_baseline(claim_id, run_id=RUN, data_dir=data_dir, db_path=db) if baseline == "default" else baseline
    return adjudicate(RUN, claim, docs, policy, history, base,
                      adk(claim_id) if adk_result == "default" else adk_result,
                      crew(claim_id) if crew_result == "default" else crew_result,
                      mcp_result or mcp(claim_id))


def test_expired_policy_is_rejected_even_if_agents_approve(dataset):
    final = judge(dataset, "CS-002")
    assert final.final_decision == Decision.REJECT and final.payout is None and final.requires_human_approval
    assert any("not coverage approvals" in r for r in final.rationale)
    assert any(c.name == "coverage_window" and c.outcome.value == "FAIL" for c in final.rule_checks)


def test_excluded_cause_is_rejected_with_policy_reference(dataset):
    final = judge(dataset, "CS-004")
    assert final.final_decision == Decision.REJECT
    assert "policies.json#/POL-LAPTOP-STD-02/exclusions/0" in final.policy_references


@pytest.mark.parametrize("claim_id, payout, human", [("CS-001", "16000.00", True), ("CS-005", "57000.00", True),
                                                     ("CS-008", "7650.00", False)])
def test_payouts_are_decimal_and_rule_based(dataset, claim_id, payout, human):
    final = judge(dataset, claim_id)
    assert final.final_decision == Decision.APPROVE and final.decided_by == "DETERMINISTIC_RULES"
    assert isinstance(final.payout.payout, Decimal) and final.payout.payout == Decimal(payout)
    assert final.requires_human_approval is human


def test_verified_agent_conflict_escalates_an_approvable_claim(dataset):
    final = judge(dataset, "CS-001", crew_result=crew("CS-001", "ESCALATE", findings=[SERIAL_FINDING]))
    assert final.deterministic_decision == Decision.APPROVE
    assert final.final_decision == Decision.ESCALATE and final.decided_by == "CONFLICT_ESCALATION"
    assert final.payout is None and any("serials differ" in r for r in final.rationale)


def test_invalid_citation_is_ignored_and_listed(dataset):
    final = judge(dataset, "CS-001", crew_result=crew("CS-001", "ESCALATE", findings=[FAKE_FINDING]))
    assert final.final_decision == Decision.APPROVE
    assert any("FAKE-0000" in u or "made-up" in u for u in final.unsupported_claims)
    assert any("CREWAI recommends ESCALATE" in d for d in final.disagreements)


def test_agents_cannot_lift_an_escalation_or_rejection(dataset):
    assert judge(dataset, "CS-003").final_decision == Decision.ESCALATE        # agents say APPROVE
    assert judge(dataset, "CS-002").final_decision == Decision.REJECT


def test_missing_agent_results_block_approval(dataset):
    final = judge(dataset, "CS-001", adk_result=None, crew_result=None)
    assert final.final_decision == Decision.ESCALATE and final.decided_by == "INSUFFICIENT_REVIEW"
    failed_adk = adk("CS-001", status=ExecutionStatus.FAILED)
    one = judge(dataset, "CS-001", adk_result=failed_adk)
    assert one.final_decision == Decision.APPROVE and any("GOOGLE_ADK result unavailable" in r for r in one.rationale)


def test_missing_baseline_result_escalates(dataset):
    final = judge(dataset, "CS-001", baseline=None)
    assert final.final_decision == Decision.ESCALATE and final.decided_by == "BASELINE_UNAVAILABLE"


def test_tampered_baseline_result_escalates(dataset):
    data_dir, db = dataset
    base = run_baseline("CS-002", run_id=RUN, data_dir=data_dir, db_path=db)
    lie = base.model_copy(update={"decision": Decision.APPROVE})
    final = judge(dataset, "CS-002", baseline=lie)
    assert final.final_decision == Decision.ESCALATE and final.decided_by == "INTEGRITY_ESCALATION"


def test_insufficient_evidence_requests_evidence_or_escalates_on_verified_concern(dataset):
    assert judge(dataset, "CS-006").final_decision == Decision.REQUEST_EVIDENCE
    gap_concern = {"category": "SUSPICIOUS_DETAIL", "severity": "HIGH", "description": "odd store",
                   "citations": [{"doc_id": "invoice", "line": 4, "quote": "Store: Metro Digital Store #402"}]}
    final = judge(dataset, "CS-006", crew_result=crew("CS-006", "ESCALATE", findings=[gap_concern]))
    assert final.final_decision == Decision.ESCALATE and final.decided_by == "CONFLICT_ESCALATION"


def _history_call(claim_id):
    source = "sqlite:claimsleuth.db#claim_history/claim_id=HST-2025-1102"
    record = AgentToolCallRecord(run_id=RUN, agent="forensic_agent", tool="claim_history_lookup", protocol="mcp",
                                 arguments={"serial_number": "NMB-X15-551230"}, status="ok",
                                 result={"record_sources": [source]}, started_at=utc_now(), latency_ms=1)
    server = MCPToolCall(tool="claim_history_lookup", arguments={"serial_number": "NMB-X15-551230", "limit": 10},
                         status="ok", started_at=utc_now(), latency_ms=1, run_id=RUN, claim_id=claim_id)
    return record, source, server


def test_history_findings_count_only_when_confirmed_by_mcp_records(dataset):
    """On an approvable claim, a HIGH history finding blocks approval only if the MCP server's log confirms
    the call that returned the cited record."""
    record, source, server_call = _history_call("CS-001")
    pattern = {"pattern": "SAME_SERIAL_OTHER_CLAIMANT", "detail": "d", "references": [{"source": source, "value": "v"}]}
    finding = {"category": "CLAIM_HISTORY_PATTERN", "severity": "HIGH", "description": "serial used by USR-3305",
               "citations": []}
    result = crew("CS-001", "ESCALATE", [finding], [pattern], [record])
    confirmed = judge(dataset, "CS-001", crew_result=result, mcp_result=mcp("CS-001", [server_call]))
    assert confirmed.final_decision == Decision.ESCALATE and confirmed.decided_by == "CONFLICT_ESCALATION"
    assert confirmed.unsupported_claims == []
    unconfirmed = judge(dataset, "CS-001", crew_result=result, mcp_result=mcp("CS-001", []))
    assert unconfirmed.final_decision == Decision.APPROVE
    assert any("not confirmed by MCP records" in u for u in unconfirmed.unsupported_claims)


def test_final_outcome_is_deterministic(dataset):
    a = judge(dataset, "CS-005").model_dump(exclude={"generated_at"})
    b = judge(dataset, "CS-005").model_dump(exclude={"generated_at"})
    a["comparison"]["rows"][0].pop("execution_ms"), b["comparison"]["rows"][0].pop("execution_ms")
    assert a == b


def test_comparison_table_keeps_mcp_without_a_decision(dataset):
    rows = {r.system: r for r in judge(dataset, "CS-001").comparison.rows}
    assert list(rows) == ["DETERMINISTIC_BASELINE", "GOOGLE_ADK", "CREWAI", "MCP_TOOLS"]
    assert rows["MCP_TOOLS"].decision is None and rows["MCP_TOOLS"].decision_label == "Tool results, not a decision"
    assert rows["GOOGLE_ADK"].confidence_label == "0.70 (test-double)"


def test_mcp_records_from_another_run_or_claim_are_rejected():
    base = {"tool": "claim_history_lookup", "arguments": {}, "status": "ok", "started_at": "2026-10-31T10:00:00+00:00",
            "duration_ms": 2.0, "caller": "adk:investigator_agent"}
    records = [{**base, "call_id": "a", "run_id": RUN, "claim_id": "CS-007"},
               {**base, "call_id": "b", "run_id": "RUN-other", "claim_id": "CS-007"},
               {**base, "call_id": "c", "run_id": RUN, "claim_id": "CS-001"}]
    accepted, rejected = validate_records(records, RUN, "CS-007")
    assert [c.call_id for c in accepted] == ["a"] and accepted[0].caller == "adk:investigator_agent"
    assert any("belongs to run 'RUN-other'" in r for r in rejected) and any("claim 'CS-001'" in r for r in rejected)
