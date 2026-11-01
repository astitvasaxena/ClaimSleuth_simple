"""The ADK <-> CrewAI WorkflowMessage contract (pure validation; transport is tested end to end elsewhere)."""
import json

import pytest

from claimsleuth import contract
from claimsleuth.datastore import load_claim
from claimsleuth.schemas import AgentSystemResult, ExecutionStatus, utc_now


@pytest.fixture
def request_msg(dataset):
    claim, docs = load_claim("CS-003", dataset[0])
    return contract.forensic_request("RUN-contract-01", "CS-003", docs), docs


def crew_result(run_id="RUN-contract-01", claim_id="CS-003"):
    now = utc_now()
    return AgentSystemResult(system="CREWAI", run_id=run_id, claim_id=claim_id, status=ExecutionStatus.FAILED,
                             started_at=now, finished_at=now, errors=["x"])


def test_valid_request_round_trips_and_matches_evidence(request_msg):
    msg, docs = request_msg
    parsed = contract.parse(msg.model_dump_json())
    assert parsed == msg and parsed.sender == "adk_orchestrator" and parsed.recipient == "crewai_forensic_service"
    contract.check_request(parsed, docs)                       # does not raise
    assert {d.doc_id for d in parsed.payload.documents} == {"invoice", "repair_report"}


def test_valid_response_and_error(request_msg):
    msg, _ = request_msg
    response = contract.forensic_response(msg, crew_result(), llm_mode="test-double")
    contract.check_response(contract.parse(response.model_dump_json()), msg)
    error = contract.error_message(msg.run_id, msg.claim_id, "EXECUTION_FAILED", "boom")
    contract.check_response(error, msg)
    assert error.payload.code == "EXECUTION_FAILED"


@pytest.mark.parametrize("mutate, fragment", [
    (lambda d: d.pop("run_id"), "run_id"),
    (lambda d: d.update(message_type="FORENSIC_RESPONSE"), "cannot carry"),
    (lambda d: d.update(recipient="adk_orchestrator"), "must differ"),
    (lambda d: d["payload"].update(claim_id="CS-001"), "payload claim_id differs"),
    (lambda d: d.update(claim_id="cs 003; drop"), "claim_id"),
    (lambda d: d.update(extra="x"), "extra"),
    (lambda d: d["payload"]["documents"][0].update(sha256="not-a-hash"), "sha256"),
])
def test_invalid_messages_are_rejected(request_msg, mutate, fragment):
    data = json.loads(request_msg[0].model_dump_json())
    mutate(data)
    with pytest.raises(contract.ContractError) as exc:
        contract.parse(json.dumps(data))
    assert exc.value.code == "INVALID_MESSAGE" and fragment.lower() in str(exc.value).lower()


def test_malformed_json_is_rejected():
    with pytest.raises(contract.ContractError) as exc:
        contract.parse("{not json")
    assert exc.value.code == "INVALID_MESSAGE"


def test_response_result_must_match_the_message(request_msg):
    msg, _ = request_msg
    with pytest.raises(ValueError, match="result claim_id/run_id differ"):
        contract.forensic_response(msg, crew_result(claim_id="CS-001"), llm_mode="test-double")


def test_wrong_run_id_and_wrong_claim_id_in_reply(request_msg):
    msg, _ = request_msg
    other_run = contract.error_message("RUN-other-99", msg.claim_id, "EXECUTION_FAILED", "x")
    other_claim = contract.error_message(msg.run_id, "CS-001", "EXECUTION_FAILED", "x")
    with pytest.raises(contract.ContractError) as a:
        contract.check_response(other_run, msg)
    with pytest.raises(contract.ContractError) as b:
        contract.check_response(other_claim, msg)
    assert (a.value.code, b.value.code) == ("RUN_MISMATCH", "CLAIM_MISMATCH")


def test_reply_in_the_wrong_direction_is_rejected(request_msg):
    msg, _ = request_msg
    with pytest.raises(contract.ContractError):
        contract.check_response(msg, msg)


def test_evidence_mismatch_is_detected(request_msg):
    msg, docs = request_msg
    tampered = [docs[0], docs[1].model_copy(update={"sha256": "0" * 64})]
    with pytest.raises(contract.ContractError) as exc:
        contract.check_request(msg, tampered)
    assert exc.value.code == "EVIDENCE_MISMATCH"
