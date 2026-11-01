"""End-to-end MCP tests: a real server subprocess (Streamable HTTP) and the official MCP client.

Nothing here calls the tool functions directly; every assertion is about what crosses the protocol.
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from mcp import Client

from claimsleuth.schemas import ClaimHistoryLookupOutput, PolicySearchOutput

ROOT = Path(__file__).resolve().parents[1]
META = "claimsleuth/run_id"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class RunningServer:
    def __init__(self, data_dir: Path, db: Path, tmp: Path) -> None:
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.url = f"{self.base}/mcp"
        self.log_file = tmp / f"tool_calls_{self.port}.jsonl"
        self.stderr = (tmp / f"server_{self.port}.log").open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "claimsleuth.mcp_server", "--port", str(self.port), "--data-dir", str(data_dir),
             "--db", str(db), "--log-file", str(self.log_file), "--pid-file", str(tmp / f"{self.port}.pid")],
            cwd=ROOT, stdout=self.stderr, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early, see {self.stderr.name}")
            try:
                _get(f"{self.base}/health")
                return
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.2)
        raise RuntimeError("server did not start")

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.stderr.close()

    def log_records(self) -> list[dict]:
        return [json.loads(x) for x in self.log_file.read_text(encoding="utf-8").splitlines()] if self.log_file.exists() else []


@pytest.fixture(scope="module")
def server(dataset, tmp_path_factory):
    srv = RunningServer(*dataset, tmp_path_factory.mktemp("mcp"))
    yield srv
    srv.stop()


def _call(url, tool, args, run_id="RUN-pytest-0001"):
    async def go():
        async with Client(url) as client:
            return await client.call_tool(tool, args, meta={META: run_id})
    return asyncio.run(go())


def test_health_ok(server):
    status, body = _get(f"{server.base}/health")
    assert status == 200 and body["status"] == "ok"
    assert body["transport"] == "streamable-http" and body["mcp_sdk_version"] == "2.3.0"
    assert body["checks"]["claim_history_db"]["rows"] == 5 and body["checks"]["policy_register"]["policies"] == 3


def test_tool_discovery_lists_exactly_the_two_tools_with_schemas(server):
    async def go():
        async with Client(server.url) as client:
            return (await client.list_tools()).tools
    tools = {t.name: t for t in asyncio.run(go())}
    assert set(tools) == {"warranty_policy_search", "claim_history_lookup"}
    assert set(tools["warranty_policy_search"].input_schema["properties"]) == {"policy_id", "search_query", "limit"}
    assert set(tools["claim_history_lookup"].input_schema["properties"]) == {"claim_id", "claimant_id", "serial_number", "limit"}
    assert all(t.output_schema and t.description for t in tools.values())


def test_policy_search_over_mcp(server):
    result = _call(server.url, "warranty_policy_search", {"search_query": "water damage laptop"})
    out = PolicySearchOutput.model_validate(result.structured_content)
    assert not result.is_error and out.matches[0].policy_id == "POL-LAPTOP-STD-02"


def test_policy_search_no_match_over_mcp(server):
    result = _call(server.url, "warranty_policy_search", {"search_query": "spaceship"})
    assert not result.is_error and result.structured_content["total_matches"] == 0


def test_history_lookup_over_mcp(server):
    result = _call(server.url, "claim_history_lookup", {"serial_number": "NMB-X15-551230"})
    out = ClaimHistoryLookupOutput.model_validate(result.structured_content)
    assert [(r.claim_id, r.claimant_id) for r in out.records] == [("HST-2025-1102", "USR-3305")]


def test_history_lookup_no_records_over_mcp(server):
    result = _call(server.url, "claim_history_lookup", {"claimant_id": "USR-0000"})
    assert not result.is_error and result.structured_content["records"] == []


@pytest.mark.parametrize("tool, args", [
    ("warranty_policy_search", {}),
    ("warranty_policy_search", {"policy_id": "bad id"}),
    ("warranty_policy_search", {"search_query": "water", "limit": 99}),
    ("claim_history_lookup", {"serial_number": "x' OR '1'='1"}),
    ("claim_history_lookup", {"claimant_id": 42}),
    ("claim_history_lookup", {}),
    ("no_such_tool", {}),
])
def test_invalid_input_is_a_tool_error(server, tool, args):
    result = _call(server.url, tool, args)
    assert result.is_error and not result.structured_content and result.content[0].text


def test_every_call_is_logged_with_its_run_id(server):
    run_id = "RUN-pytest-logcheck"
    _call(server.url, "warranty_policy_search", {"policy_id": "POL-TECH-CARE-01"}, run_id)
    _call(server.url, "claim_history_lookup", {"limit": 0, "claimant_id": "USR-1"}, run_id)
    mine = [r for r in server.log_records() if r["run_id"] == run_id]
    assert [(r["tool"], r["status"]) for r in mine] == [("warranty_policy_search", "ok"), ("claim_history_lookup", "error")]
    assert all(r["run_id_source"] == "mcp_meta" and r["started_at"] and r["duration_ms"] >= 0 for r in mine)
    assert mine[0]["arguments"] == {"policy_id": "POL-TECH-CARE-01"} and mine[1]["error"]
    _, body = _get(f"{server.base}/tool-calls?run_id={run_id}")
    assert [c["call_id"] for c in body["calls"]] == [r["call_id"] for r in mine]


def test_run_id_from_http_header_and_secrets_redacted(server):
    import httpx2

    async def go():
        http = httpx2.AsyncClient(headers={"X-ClaimSleuth-Run-Id": "RUN-pytest-header"})
        from mcp.client.streamable_http import streamable_http_client
        async with Client(streamable_http_client(server.url, http_client=http)) as client:
            return await client.call_tool("claim_history_lookup", {"claimant_id": "USR-6610", "api_key": "s3cr3t"})
    asyncio.run(go())
    record = [r for r in server.log_records() if r["run_id"] == "RUN-pytest-header"][-1]
    assert record["run_id_source"] == "http_header"
    assert record["arguments"]["api_key"] == "[REDACTED]" and "s3cr3t" not in json.dumps(server.log_records())


def test_missing_database_is_reported_not_hidden(dataset, tmp_path):
    srv = RunningServer(dataset[0], tmp_path / "absent.db", tmp_path)
    try:
        status, body = _get(f"{srv.base}/health")
        assert status == 503 and body["status"] == "unhealthy" and not body["checks"]["claim_history_db"]["ok"]
        result = _call(srv.url, "claim_history_lookup", {"claimant_id": "USR-6610"})
        assert result.is_error and "database unavailable" in result.content[0].text
        assert not (tmp_path / "absent.db").exists()
    finally:
        srv.stop()


def test_independent_client_program_passes(server):
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "mcp_client_check.py"), "--url", server.url, "--quiet"],
                          capture_output=True, text=True, encoding="utf-8", timeout=120,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "0 failed" in proc.stdout
