"""The guardrail harness (controlled test, scripted output) against a real MCP server and genuine Crew.kickoff()."""
import pytest

pytest.importorskip("crewai", reason="CrewAI tests run in .venv-crew")

from claimsleuth.crew_service.guardrail_harness import SCENARIOS, corrupt, run_scenario  # noqa: E402


def test_corruptions_are_what_they_claim():
    base = {"claim_id": "CS-003", "findings": [], "evidence_references": []}
    assert corrupt(base, "wrong_claim")["claim_id"] == "CS-999"
    assert corrupt(base, "invalid_category")["findings"][0]["category"] == "FRAUD_CONFIRMED"
    assert corrupt(base, "fabricated_citation")["evidence_references"][0]["doc_id"] == "warranty_card"
    assert corrupt(base, "valid") == base and base["findings"] == []


def test_guardrail_rejects_then_accepts_corrected_output(mcp, dataset):
    out = run_scenario("fabricated_citation_then_valid", "CS-003", mcp.url, dataset[0])
    assert [v["accepted"] for v in out["guardrail_verdicts"]] == [False, True]
    assert "do not match the documents" in out["guardrail_verdicts"][0]["message"]
    assert out["status"] == "COMPLETED" and not out["fabricated_citation_in_report"]
    assert "claim_history_lookup" in out["mcp_calls"]


@pytest.mark.parametrize("name", ["mismatched_claim_id", "fabricated_citation_every_attempt", "invalid_finding_category"])
def test_persistent_invalid_output_fails_the_run(mcp, dataset, name):
    out = run_scenario(name, "CS-003", mcp.url, dataset[0])
    assert out["status"] == "FAILED" and out["report_claim_id"] is None and out["errors"]
    assert out["guardrail_verdicts"] and not out["guardrail_verdicts"][0]["accepted"]
    assert set(SCENARIOS) >= {name}
