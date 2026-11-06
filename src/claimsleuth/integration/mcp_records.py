"""MCP TOOL RESULTS for one run: real tool discovery plus the MCP server's own tool-call log.

MCP records are evidence about tool use, not decisions. Records are accepted only when the server logged
them with this run id AND this claim id; anything else is rejected and reported.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, urlsplit

from mcp import Client

from claimsleuth.schemas import ExecutionStatus, MCPResult, MCPToolCall, MCPToolInfo


def validate_records(records: list[dict[str, Any]], run_id: str, claim_id: str) -> tuple[list[MCPToolCall], list[str]]:
    accepted, rejected = [], []
    for r in records:
        if r.get("run_id") != run_id:
            rejected.append(f"call {r.get('call_id')} belongs to run {r.get('run_id')!r}, not {run_id}")
        elif r.get("claim_id") != claim_id:
            rejected.append(f"call {r.get('call_id')} belongs to claim {r.get('claim_id')!r}, not {claim_id}")
        else:
            accepted.append(MCPToolCall(
                tool=r["tool"], arguments=r.get("arguments") or {}, status=r["status"],
                result=r.get("result_summary") or None, error=r.get("error"), started_at=r["started_at"],
                latency_ms=r.get("duration_ms") or 0, run_id=r["run_id"], claim_id=r["claim_id"],
                caller=r.get("caller"), call_id=r.get("call_id")))
    return accepted, rejected


async def fetch_mcp_records(mcp_url: str, run_id: str, claim_id: str) -> MCPResult:
    t0, errors = time.perf_counter(), []
    tools: list[MCPToolInfo] = []
    try:
        async with Client(mcp_url) as client:          # real MCP discovery (tools/list), no tool is invoked here
            tools = [MCPToolInfo(name=t.name, description=t.description, input_schema=t.input_schema)
                     for t in (await client.list_tools()).tools]
    except Exception as exc:
        errors.append(f"MCP discovery failed: {type(exc).__name__}: {exc}")
    parts = urlsplit(mcp_url)
    log_url = f"{parts.scheme}://{parts.netloc}/tool-calls?run_id={quote(run_id)}&limit=500"
    records: list[dict[str, Any]] = []

    def read_log() -> list[dict[str, Any]]:
        with urllib.request.urlopen(log_url, timeout=10) as resp:
            return json.loads(resp.read()).get("calls", [])

    try:
        records = await asyncio.to_thread(read_log)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        errors.append(f"MCP tool-call log unavailable: {exc}")
    calls, rejected = validate_records(records, run_id, claim_id)
    errors += [f"rejected MCP record: {r}" for r in rejected]
    status = ExecutionStatus.COMPLETED if tools and not any(e.startswith("MCP tool-call log") for e in errors) \
        else ExecutionStatus.FAILED
    return MCPResult(run_id=run_id, claim_id=claim_id, status=status, server_url=mcp_url, transport="streamable-http",
                     discovered_tools=tools, calls=calls, execution_ms=round((time.perf_counter() - t0) * 1000, 3),
                     errors=errors)
