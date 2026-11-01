import hashlib
import inspect
import subprocess
import sys
from decimal import Decimal

import pytest

from claimsleuth import baseline
from claimsleuth.baseline import evaluate, run_baseline
from claimsleuth.dataset import all_cases, build_dataset
from claimsleuth.datastore import list_claim_ids, load_claim, load_ground_truth, load_policies
from claimsleuth.documents import extract_fields, quote_appears
from claimsleuth.schemas import CheckOutcome, Decision, ExecutionStatus


def _run(dataset, claim_id):
    data_dir, db = dataset
    return run_baseline(claim_id, data_dir=data_dir, db_path=db)


@pytest.mark.parametrize("claim_id", [c.claim_id for c in all_cases()])
def test_matches_ground_truth(dataset, claim_id):
    label = load_ground_truth(dataset[0])[claim_id]
    result = _run(dataset, claim_id)
    assert result.status == ExecutionStatus.COMPLETED
    assert result.decision.value == label["expected_decision"]
    assert (str(result.settlement.payout) if result.settlement else None) == label["expected_payout"]
    assert set(label["expected_guardrails"]) <= {g.kind for g in result.guardrail_events}


def test_dataset_has_claims_for_all_four_personas(dataset):
    personas = {v["persona"] for v in load_ground_truth(dataset[0]).values()}
    assert personas == {"honest", "vague", "abusive", "injection"}


def test_is_deterministic(dataset):
    a, b = _run(dataset, "CS-005"), _run(dataset, "CS-005")
    assert a.run_id != b.run_id
    strip = lambda r: r.model_dump(exclude={"run_id", "started_at", "finished_at", "execution_ms"})
    assert strip(a) == strip(b)


def test_human_approval_rules(dataset):
    assert _run(dataset, "CS-001").requires_human_approval          # ₹16,000 >= ₹15,000
    assert not _run(dataset, "CS-008").requires_human_approval      # ₹7,650 < ₹15,000
    assert _run(dataset, "CS-002").requires_human_approval          # REJECT
    assert not _run(dataset, "CS-006").requires_human_approval      # REQUEST_EVIDENCE


def test_injection_does_not_change_any_rule_outcome(dataset):
    """Injected text is screened and escalated, but every deterministic check is unchanged."""
    honest, attacked = _run(dataset, "CS-001"), _run(dataset, "CS-001-INJ")
    outcomes = lambda r: [(c.name, c.outcome) for c in r.checks if c.name != "settlement"]
    assert outcomes(honest) == outcomes(attacked)
    assert attacked.decision == Decision.ESCALATE and attacked.settlement is None


def test_every_evidence_quote_is_in_its_document(dataset):
    for claim_id in list_claim_ids(dataset[0]):
        _, docs = load_claim(claim_id, dataset[0])
        by_id = {d.doc_id: d for d in docs}
        for ref in _run(dataset, claim_id).evidence_refs:
            if ref.doc_id != "claim_history":
                assert quote_appears(by_id[ref.doc_id], ref.line, ref.quote), ref


def test_unknown_policy_escalates(dataset):
    claim, docs = load_claim("CS-001", dataset[0])
    result = evaluate(claim, docs, None, [], "RUN-T")
    assert result.decision == Decision.ESCALATE
    assert next(c for c in result.checks if c.name == "policy_known").outcome == CheckOutcome.FAIL


def test_conflicting_value_inside_one_document_escalates(dataset):
    claim, docs = load_claim("CS-001", dataset[0])
    invoice = docs[0].model_copy(update={"text": docs[0].text + "Purchase Date: 2026-01-01\n"})
    assert extract_fields(invoice).conflicts
    policy = load_policies(dataset[0])[claim.policy_id]
    assert evaluate(claim, [invoice, docs[1]], policy, [], "RUN-T").decision == Decision.ESCALATE


def test_unreadable_cost_requests_evidence(dataset):
    claim, docs = load_claim("CS-008", dataset[0])
    report = docs[1].model_copy(update={"text": docs[1].text.replace("₹12,500.00", "to be confirmed")})
    policy = load_policies(dataset[0])[claim.policy_id]
    result = evaluate(claim, [docs[0], report], policy, [], "RUN-T")
    assert result.decision == Decision.REQUEST_EVIDENCE and result.settlement is None


def test_load_failures_are_reported_not_guessed(dataset, tmp_path):
    missing_claim = _run(dataset, "CS-999")
    assert missing_claim.status == ExecutionStatus.FAILED and missing_claim.decision is None
    assert missing_claim.errors
    no_db = run_baseline("CS-001", data_dir=dataset[0], db_path=tmp_path / "absent.db")
    assert no_db.status == ExecutionStatus.FAILED and "database" in no_db.errors[0]


def test_baseline_never_reads_ground_truth():
    assert "ground_truth" not in inspect.getsource(baseline)


def test_baseline_imports_no_llm_or_agent_framework():
    code = ("import sys, claimsleuth.baseline; "
            "print(sorted({m.split('.')[0] for m in sys.modules} & "
            "{'google','crewai','litellm','openai','anthropic','langchain','langgraph','autogen','mcp','a2a'}))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


def test_dataset_build_is_reproducible(tmp_path):
    def digest(root):
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file() and p.suffix != ".db"}
    build_dataset(tmp_path / "a")
    build_dataset(tmp_path / "b")
    assert digest(tmp_path / "a") == digest(tmp_path / "b")
    assert not any("ground_truth" in p for p in digest(tmp_path / "a") if p.startswith("claims"))


def test_capped_settlement_figures(dataset):
    s = _run(dataset, "CS-005").settlement
    assert (s.capped_amount, s.depreciation_amount, s.payout) == (Decimal("60000.00"), Decimal("3000.00"), Decimal("57000.00"))
