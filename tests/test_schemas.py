from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from claimsleuth.schemas import (AgentSystemResult, BaselineResult, ClaimInput, ComparisonRow, Decision,
                                 ExecutionStatus, FinalClaimReport, FinalComparison, ForensicsReport,
                                 InvestigationReport, MCPResult, Settlement, make_evidence_id, utc_now)

CLAIM = dict(claim_id="CS-100", policy_id="POL-TECH-CARE-01", claimant_id="USR-1", claimant_name="A B",
             device_model="Phone", incident_date="2026-01-01", reported_issue="Broken screen")


def test_claim_round_trip():
    claim = ClaimInput.model_validate(CLAIM)
    assert ClaimInput.model_validate_json(claim.model_dump_json()) == claim


@pytest.mark.parametrize("change", [
    {"claim_id": "cs 100; DROP TABLE"},
    {"incident_date": (date.today() + timedelta(days=1)).isoformat()},
    {"reported_issue": ""},
    {"unexpected_field": "x"},
    {"claimant_statement": "x" * 4001},
    {"documents": [{"doc_id": "a", "kind": "INVOICE", "filename": "a.txt"},
                   {"doc_id": "a", "kind": "DIAGNOSTIC", "filename": "b.txt"}]},
    {"documents": [{"doc_id": "a", "kind": "INVOICE", "filename": "../../etc/passwd"}]},
])
def test_claim_rejects_bad_input(change):
    with pytest.raises(ValidationError):
        ClaimInput.model_validate({**CLAIM, **change})


def test_evidence_id_is_stable():
    assert make_evidence_id("invoice", 7, "purchase_date") == make_evidence_id("invoice", 7, "purchase_date")
    assert make_evidence_id("invoice", 7, "purchase_date") != make_evidence_id("invoice", 8, "purchase_date")


def _settlement() -> Settlement:
    z = Decimal("0")
    return Settlement(repair_cost=z, deductible=z, eligible_amount=z, coverage_limit=z, capped_amount=z,
                      depreciation_rate=z, depreciation_amount=z, payout=z, explanation="")


def _baseline(**kw) -> BaselineResult:
    now = utc_now()
    return BaselineResult(run_id="RUN-1", claim_id="CS-100", started_at=now, finished_at=now, execution_ms=1, **kw)


def test_baseline_decision_rules():
    assert _baseline(status=ExecutionStatus.COMPLETED, decision=Decision.APPROVE, settlement=_settlement())
    with pytest.raises(ValidationError):
        _baseline(status=ExecutionStatus.COMPLETED)                                   # no decision
    with pytest.raises(ValidationError):
        _baseline(status=ExecutionStatus.FAILED, decision=Decision.REJECT)           # failed with decision
    with pytest.raises(ValidationError):
        _baseline(status=ExecutionStatus.COMPLETED, decision=Decision.REJECT, settlement=_settlement())


def test_agent_result_needs_report_only_when_completed():
    report = ForensicsReport(claim_id="C", findings=[], evidence_references=[], serial_number_checks=[],
                             chronology_checks=[], historical_pattern_findings=[], missing_information=[],
                             uncertainty=[], confidence=0.5, recommended_outcome=Decision.ESCALATE,
                             reasoning_summary="s", model="test-double", provider="offline-test-double",
                             execution_status=ExecutionStatus.COMPLETED)
    ok = AgentSystemResult(system="CREWAI", run_id="R", claim_id="C", status=ExecutionStatus.COMPLETED, report=report)
    assert AgentSystemResult.model_validate_json(ok.model_dump_json()).report == report
    with pytest.raises(ValidationError):
        AgentSystemResult(system="CREWAI", run_id="R", claim_id="C", status=ExecutionStatus.COMPLETED)
    with pytest.raises(ValidationError):
        AgentSystemResult(system="CREWAI", run_id="R", claim_id="C", status=ExecutionStatus.FAILED, report=report)


def test_llm_report_confidence_bounds():
    with pytest.raises(ValidationError):
        InvestigationReport(claim_id="CS-100", findings=[], evidence_citations=[], policy_eligibility="ELIGIBLE",
                            policy_eligibility_reason="", missing_information=[], confidence=1.5,
                            recommended_decision=Decision.APPROVE, reasoning_summary="")


def test_mcp_result_has_no_decision_field():
    assert "decision" not in MCPResult.model_fields


def _final(**kw) -> FinalClaimReport:
    comparison = FinalComparison(run_id="R", claim_id="C", decisions_agree=None, rows=[
        ComparisonRow(system="MCP_TOOLS", status=ExecutionStatus.COMPLETED, decision=None,
                      decision_label="Tool results, not a decision", confidence=None, confidence_label="N/A",
                      execution_ms=3.0, evidence_count=2, evidence_summary="2 tool calls")])
    base = dict(run_id="R", claim_id="C", decided_by="DETERMINISTIC_RULES", payout=None, agreements=[],
                disagreements=[], unsupported_claims=[], rationale=[], approval_status="PENDING", comparison=comparison)
    return FinalClaimReport(**{**base, **kw})


def test_final_report_rules():
    assert _final(final_decision=Decision.APPROVE, payout=_settlement(), requires_human_approval=False)
    with pytest.raises(ValidationError):
        _final(final_decision=Decision.ESCALATE, payout=_settlement(), requires_human_approval=True)
    with pytest.raises(ValidationError):
        _final(final_decision=Decision.REJECT, requires_human_approval=False)
