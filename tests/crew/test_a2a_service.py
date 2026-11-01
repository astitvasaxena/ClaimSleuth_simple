"""CrewAI A2A service: request validation in the executor, Agent Card and health (run with .venv-crew)."""
import asyncio
import json

import pytest

pytest.importorskip("crewai", reason="CrewAI tests run in .venv-crew")

from starlette.testclient import TestClient  # noqa: E402

from claimsleuth import contract  # noqa: E402
from claimsleuth.crew_service.a2a_server import ForensicExecutor, MessageLog, build_app  # noqa: E402
from claimsleuth.datastore import load_claim  # noqa: E402
from claimsleuth.schemas import ExecutionStatus  # noqa: E402
from conftest import free_port  # noqa: E402


@pytest.fixture
def executor(mcp, dataset, tmp_path):
    return ForensicExecutor(mcp.url, dataset[0], MessageLog(tmp_path / "msgs.jsonl"))


def handle(executor, text):
    return asyncio.run(executor.handle(text))


def request_json(dataset, claim_id="CS-003", run_id="RUN-svc-0001"):
    _, docs = load_claim(claim_id, dataset[0])
    return contract.forensic_request(run_id, claim_id, docs)


def test_valid_request_runs_the_crew_and_answers(executor, dataset):
    request = request_json(dataset)
    reply = handle(executor, request.model_dump_json())
    assert reply.message_type == "FORENSIC_RESPONSE" and (reply.run_id, reply.claim_id) == (request.run_id, "CS-003")
    result = reply.payload.result
    assert result.status == ExecutionStatus.COMPLETED and result.run_id == request.run_id
    assert result.report.model == "test-double" and reply.metadata.llm_mode == "test-double"
    contract.check_response(contract.parse(reply.model_dump_json()), request)


def test_malformed_message_gets_an_error_reply(executor):
    reply = handle(executor, '{"run_id": "RUN-svc-bad", "claim_id": "CS-003", "payload": 1}')
    assert reply.message_type == "ERROR" and reply.payload.code == "INVALID_MESSAGE"
    assert (reply.run_id, reply.claim_id) == ("RUN-svc-bad", "CS-003")
    garbage = handle(executor, "not json at all")
    assert garbage.payload.code == "INVALID_MESSAGE" and garbage.run_id == "unknown-run"


def test_unknown_claim_is_rejected(executor, dataset):
    request = request_json(dataset)
    data = json.loads(request.model_dump_json())
    data["claim_id"] = data["payload"]["claim_id"] = "CS-404"
    reply = handle(executor, json.dumps(data))
    assert reply.payload.code == "CLAIM_MISMATCH" and reply.claim_id == "CS-404"


def test_evidence_mismatch_is_rejected(executor, dataset):
    data = json.loads(request_json(dataset).model_dump_json())
    data["payload"]["documents"][0]["sha256"] = "a" * 64
    assert handle(executor, json.dumps(data)).payload.code == "EVIDENCE_MISMATCH"


def test_message_sent_to_the_wrong_side_is_rejected(executor, dataset):
    request = request_json(dataset)
    wrong = contract.error_message(request.run_id, request.claim_id, "EXECUTION_FAILED", "x")   # crew -> adk
    reply = handle(executor, wrong.model_dump_json())
    assert reply.payload.code == "INVALID_MESSAGE"


def test_agent_card_and_health(mcp, dataset, tmp_path):
    app = build_app("http://127.0.0.1:9999", mcp.url, dataset[0], tmp_path / "m.jsonl")
    with TestClient(app) as client:
        card = client.get("/.well-known/agent-card.json").json()
        assert card["supportedInterfaces"][0]["url"] == "http://127.0.0.1:9999/a2a"
        health = client.get("/health")
        assert health.status_code == 200 and health.json()["checks"]["mcp_server"]["ok"]
    down = build_app("http://127.0.0.1:9999", f"http://127.0.0.1:{free_port()}/mcp", dataset[0], tmp_path / "n.jsonl")
    with TestClient(down) as client:
        assert client.get("/health").status_code == 503
