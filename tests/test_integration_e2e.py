"""End to end: MCP server (.venv) + CrewAI A2A service (.venv-crew) as real processes, the orchestrator here.

ADK and CrewAI use their labelled offline test doubles; A2A, MCP, ADK Runner and Crew.kickoff are real.
"""
import asyncio
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from claimsleuth.adk_service.forensic_a2a import request_forensics
from claimsleuth.datastore import load_claim
from claimsleuth.integration.run import run_claim_async, save_bundle
from claimsleuth.schemas import ClaimRunBundle, Decision, ExecutionStatus
from test_mcp_server import RunningServer, _free_port

ROOT = Path(__file__).resolve().parents[1]
CREW_PYTHON = ROOT / ".venv-crew" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


class CrewService:
    def __init__(self, data_dir: Path, mcp_url: str, tmp: Path) -> None:
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.card = f"{self.base}/.well-known/agent-card.json"
        self.out = (tmp / f"crew_{self.port}.log").open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(CREW_PYTHON), "-m", "claimsleuth.crew_service.a2a_server", "--port", str(self.port), "--mcp-url",
             mcp_url, "--data-dir", str(data_dir), "--log-file", str(tmp / f"a2a_{self.port}.jsonl"),
             "--pid-file", str(tmp / f"crew_{self.port}.pid")],
            cwd=ROOT, stdout=self.out, stderr=subprocess.STDOUT, env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.base}/health", timeout=10) as resp:
                    if json.loads(resp.read())["status"] == "ok":
                        return
            except (urllib.error.URLError, OSError):
                if self.proc.poll() is not None:
                    raise RuntimeError(f"CrewAI service exited, see {self.out.name}")
                time.sleep(0.5)
        raise RuntimeError("CrewAI service did not become healthy")

    def messages(self, run_id: str) -> list[dict]:
        with urllib.request.urlopen(f"{self.base}/messages?run_id={run_id}", timeout=10) as resp:
            return json.loads(resp.read())["messages"]

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=15)
        self.out.close()


@pytest.fixture(scope="module")
def services(dataset, tmp_path_factory):
    if not CREW_PYTHON.exists():
        pytest.skip("needs .venv-crew for the CrewAI A2A service")
    tmp = tmp_path_factory.mktemp("e2e")
    mcp = RunningServer(*dataset, tmp)
    try:
        crew = CrewService(dataset[0], mcp.url, tmp)
    except Exception:
        mcp.stop()
        raise
    yield mcp, crew
    crew.stop()
    mcp.stop()


@pytest.fixture(scope="module")
def bundle(services, dataset) -> ClaimRunBundle:
    mcp, crew = services
    return asyncio.run(run_claim_async("CS-003", run_id="RUN-e2e-cs003", mcp_url=mcp.url, crew_card_url=crew.card,
                                       data_dir=dataset[0], db_path=dataset[1]))


def test_agent_card_is_served(services):
    _, crew = services
    with urllib.request.urlopen(crew.card, timeout=10) as resp:
        card = json.loads(resp.read())
    assert card["name"] == "ClaimSleuth CrewAI Forensic Agent"
    assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
    assert card["skills"][0]["id"] == "warranty_claim_forensics"


def test_all_four_outputs_exist_independently_with_one_run_id(bundle):
    assert bundle.baseline.decision == Decision.ESCALATE
    assert bundle.adk.status == bundle.crewai.status == ExecutionStatus.COMPLETED
    assert bundle.adk.report.recommended_decision == Decision.ESCALATE
    assert bundle.crewai.report.recommended_outcome == Decision.ESCALATE
    run_ids = {bundle.baseline.run_id, bundle.adk.run_id, bundle.crewai.run_id, bundle.mcp.run_id,
               bundle.final_report.run_id, *(m.run_id for m in bundle.messages), *(e.run_id for e in bundle.audit)}
    assert run_ids == {"RUN-e2e-cs003"}


def test_crewai_result_arrived_over_a2a_and_is_labelled(bundle, services):
    _, crew = services
    request, response = bundle.messages
    assert (request.message_type, response.message_type) == ("FORENSIC_REQUEST", "FORENSIC_RESPONSE")
    assert response.metadata.protocol.startswith("A2A 1.0 JSON-RPC")
    assert response.metadata.llm_mode == "test-double" and bundle.crewai.model == "test-double"
    service_log = crew.messages("RUN-e2e-cs003")
    assert [(m["direction"], m["message_id"]) for m in service_log] == [
        ("received", request.message_id), ("sent", response.message_id)]


def test_mcp_records_come_from_both_frameworks_for_this_claim(bundle):
    callers = {c.caller for c in bundle.mcp.calls}
    assert callers == {"adk:evidence_agent", "adk:investigator_agent", "crewai:forensic_agent"}
    assert all(c.run_id == "RUN-e2e-cs003" and c.claim_id == "CS-003" for c in bundle.mcp.calls)
    assert {t.name for t in bundle.mcp.discovered_tools} == {"warranty_policy_search", "claim_history_lookup"}
    assert bundle.mcp.errors == []


def test_final_report_and_audit_trail(bundle, tmp_path):
    final = bundle.final_report
    assert final.final_decision == Decision.ESCALATE and final.payout is None and final.requires_human_approval
    events = [e.event for e in bundle.audit]
    for needed in ("run_started", "workflow_started", "workflow_finished", "message_sent", "message_received",
                   "mcp_records_fetched", "adjudication", "run_finished"):
        assert needed in events
    assert [e.ts for e in bundle.audit] == sorted(e.ts for e in bundle.audit)
    out = save_bundle(bundle, tmp_path)
    for name in ("baseline.json", "adk.json", "crewai.json", "mcp.json", "final_report.json", "final_report.md",
                 "messages.jsonl", "audit.jsonl", "bundle.json"):
        assert (out / name).exists(), name
    assert ClaimRunBundle.model_validate_json((out / "bundle.json").read_text(encoding="utf-8")).run_id == "RUN-e2e-cs003"


def test_expired_claim_end_to_end(services, dataset):
    mcp, crew = services
    b = asyncio.run(run_claim_async("CS-002", run_id="RUN-e2e-cs002", mcp_url=mcp.url, crew_card_url=crew.card,
                                    data_dir=dataset[0], db_path=dataset[1]))
    assert b.crewai.report.recommended_outcome == Decision.APPROVE          # forensics finds no anomaly
    assert b.final_report.final_decision == Decision.REJECT                 # coverage rules decide
    assert b.final_report.deterministic_decision == Decision.REJECT


def test_evidence_mismatch_is_rejected_by_the_service_over_a2a(services, dataset):
    _, crew = services
    claim, docs = load_claim("CS-001", dataset[0])
    tampered = [docs[0].model_copy(update={"sha256": "f" * 64}), docs[1]]
    ex = asyncio.run(request_forensics(claim, tampered, "RUN-e2e-mismatch", crew.card))
    assert ex.response is not None and ex.response.message_type == "ERROR"
    assert ex.response.payload.code == "EVIDENCE_MISMATCH" and ex.result.status == ExecutionStatus.FAILED


def test_unreachable_crew_service_fails_cleanly(dataset):
    claim, docs = load_claim("CS-001", dataset[0])
    ex = asyncio.run(request_forensics(claim, docs, "RUN-e2e-down",
                                       f"http://127.0.0.1:{_free_port()}/.well-known/agent-card.json", timeout=30))
    assert ex.response is None and ex.result.status == ExecutionStatus.FAILED
    assert any("A2A" in e for e in ex.errors)
