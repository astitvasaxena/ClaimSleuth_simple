"""Every request the real CrewAI Gemini path sends through the Google SDK, recorded at the SDK's last step before the network.

The real BudgetedGeminiLLM, CrewAI's tool loop, the guardrail, the MCP server and the request ledger run. Only the HTTP call
is replaced, and it answers like the model in the stored live runs: both history lookups, then the report. The key is fake
and nothing leaves this machine. This proves which endpoints this code calls and that each is counted, not how Google answers.
"""
import json
import re
import socket

import pytest

pytest.importorskip("crewai", reason="CrewAI tests run in .venv-crew")

from forensic_provider_double import ProviderDouble  # noqa: E402
from google.genai import _api_client as transport  # noqa: E402

from claimsleuth import execution_mode as em, gemini_rate  # noqa: E402
from claimsleuth.blackboard import ENV_VAR, AgentParticipant, RunController, provision  # noqa: E402
from claimsleuth.crew_service.runner import run_forensic_investigation  # noqa: E402
from claimsleuth.crew_service.test_double import forensic_analysis  # noqa: E402
from claimsleuth.datastore import load_claim  # noqa: E402
from claimsleuth.packet import Packet, json_objects  # noqa: E402
from claimsleuth.schemas import ExecutionStatus  # noqa: E402
from test_forensic_performance import evidence  # noqa: E402


def body(parts: list[dict]) -> dict:
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}}


class Google:
    """Stands in for the HTTP call. Answers from the request it was given, as the provider double does."""

    def __init__(self) -> None:
        self.seen, self.answers = [], 0

    def __call__(self, client, http_request, http_options=None, stream=False):
        self.seen.append((http_request.method.upper(), http_request.url))
        request = http_request.data if isinstance(http_request.data, dict) else json.loads(http_request.data)
        text = "\n".join(p["text"] for c in request["contents"] for p in c["parts"] if "text" in p)
        results = [json.dumps(p["functionResponse"]) for c in request["contents"] for p in c["parts"] if "functionResponse" in p]
        names = re.findall(r"[\w-]*claim_history_lookup", json.dumps(request.get("tools", [])))
        self.answers += 1
        if names and not results:
            plan = ProviderDouble()._lookups(text)
            answer = body([{"functionCall": {"name": names[0], "args": args}} for args in plan])
        else:
            history = [o for r in results for o in json_objects(r.replace('\\"', '"').replace("\\n", "\n"))
                       if "records" in o and "total_matches" in o]
            answer = body([{"text": json.dumps(forensic_analysis(Packet(text), history, history_checked=True))}])
        return transport.HttpResponse({"content-type": "application/json"}, [json.dumps(answer)])


@pytest.fixture
def census(mcp, dataset, tmp_path, monkeypatch):
    google, outside, uploads = Google(), [], []
    real_connect = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple) and not str(address[0]).startswith(("127.", "::1", "localhost")):
            outside.append(str(address[0]))
            raise ConnectionRefusedError("refused in this test")
        return real_connect(sock, address)

    def upload(self, *args, **kwargs):
        uploads.append("upload")
        raise AssertionError("no file upload is expected")

    db = tmp_path / "blackboard.sqlite3"
    provision(db)
    monkeypatch.setenv(ENV_VAR, str(db))
    monkeypatch.setattr(transport.BaseApiClient, "_request", lambda client, *args, **kwargs: google(client, *args, **kwargs))
    for name in ("upload_file", "async_upload_file", "download_file"):
        monkeypatch.setattr(transport.BaseApiClient, name, upload)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(em, "load_env", lambda: None)
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-not-real")
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_LEDGER", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_RPM", "1000")
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_RPD", "0")
    monkeypatch.delenv("CLAIMSLEUTH_CREW_LLM", raising=False)
    run_id, claim = "RUN-census-crew", "CS-003"
    _, documents = load_claim(claim, dataset[0])
    RunController(run_id, claim, path=db).register(documents)
    adk = AgentParticipant(run_id, claim, "adk", ("evidence_agent", "investigator_agent"), path=db)
    adk.running(documents, mark_running=False)
    adk.agent_started("evidence_agent")
    ref = adk.publish_report("evidence_agent", evidence(claim))
    result = run_forensic_investigation(claim, run_id=run_id, mcp_url=mcp.url, data_dir=dataset[0], context=[ref])
    return result, google, outside, uploads


def test_every_crewai_request_is_generate_content_and_counted(census):
    result, google, outside, uploads = census
    assert result.status == ExecutionStatus.COMPLETED, result.errors
    assert len(google.seen) == 2                                     # the lookups, then the report
    assert {method for method, _ in google.seen} == {"POST"}
    assert all(url.split("?")[0].endswith(":generateContent") for _, url in google.seen), google.seen
    assert all("generativelanguage.googleapis.com" in url for _, url in google.seen)
    assert gemini_rate.run_requests(result.run_id) == {"crewai:forensic_agent": 2}
    assert result.agent_runs[0].llm_calls == 2


def test_nothing_else_leaves_this_machine(census):
    _, _, outside, uploads = census
    assert outside == [] and uploads == []
