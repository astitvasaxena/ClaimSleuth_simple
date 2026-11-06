"""Every request the real ADK Gemini path sends through the Google SDK, recorded at the SDK's last step before the network.

The real Runner, the real `Gemini` model class, the real google-genai client and the request ledger run; only the HTTP call
is replaced, and it answers with a scripted model turn. The key is fake and nothing leaves this machine. This proves which
endpoints this code calls and that each call is counted. It does not prove how Google's service answers.
"""
import asyncio
import json
import socket

import pytest
from google.genai import _api_client as transport

from claimsleuth import execution_mode as em
from claimsleuth import gemini_rate
from claimsleuth.adk_service import runner as adk_runner
from claimsleuth.adk_service.runner import run_adk_investigation_async
from test_adk_investigation import CLAIM, happy_models, mcp  # noqa: F401  (fixture)


def turn(part) -> str:
    content = {"role": "model", "parts": [part.model_dump(mode="json", by_alias=True, exclude_none=True)]}
    return json.dumps({"candidates": [{"content": content, "finishReason": "STOP"}],
                       "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}})


@pytest.fixture
def census(monkeypatch, tmp_path):
    """Records every SDK transport route, and every connection that does not stay on this machine."""
    seen, outside = [], []
    answers = [turn(part) for llm in happy_models().values() for part in llm.script]

    def record(http_request):
        seen.append((http_request.method.upper(), http_request.url))

    def answer():
        return transport.HttpResponse({"content-type": "application/json"}, [answers.pop(0)])

    def sync_request(self, http_request, http_options=None, stream=False):
        record(http_request)
        return answer()

    async def async_request(self, http_request, http_options=None, stream=False):
        record(http_request)
        return answer()

    def upload(self, *args, **kwargs):
        seen.append(("UPLOAD", "file upload"))
        raise AssertionError("no file upload is expected")

    real_connect = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple) and not str(address[0]).startswith(("127.", "::1", "localhost")):
            outside.append(str(address[0]))
            raise ConnectionRefusedError("refused in this test")
        return real_connect(sock, address)

    monkeypatch.setattr(transport.BaseApiClient, "_request", sync_request)
    monkeypatch.setattr(transport.BaseApiClient, "_async_request", async_request)
    for name in ("upload_file", "async_upload_file", "download_file"):
        monkeypatch.setattr(transport.BaseApiClient, name, upload)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(em, "load_env", lambda: None)                 # never the real .env
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-not-real")
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_LEDGER", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_RPM", "1000")
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_RPD", "0")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(adk_runner, "_mcp_reachable", lambda *a, **k: True, raising=False)
    return seen, outside


def test_every_adk_request_is_generate_content_and_counted(census, mcp, dataset):  # noqa: F811
    seen, outside = census
    result = asyncio.run(run_adk_investigation_async(CLAIM, run_id="RUN-census-adk", mcp_url=mcp.url, data_dir=dataset[0],
                                                     llm_mode="gemini"))
    assert result.status.value == "COMPLETED", result.errors
    assert len(seen) == 5                                             # Evidence 2 turns, Investigator 3
    assert {method for method, _ in seen} == {"POST"}
    assert all(url.split("?")[0].endswith(":generateContent") for _, url in seen), seen
    assert all("generativelanguage.googleapis.com" in url for _, url in seen)
    requests = gemini_rate.run_requests("RUN-census-adk")
    assert sum(requests.values()) == len(seen) == sum(r.llm_calls for r in result.agent_runs)
    assert set(requests) == {"adk:evidence_agent", "adk:investigator_agent"}
    assert not [address for address in outside if "169.254" not in address]    # only ADK's local metadata probe
