"""ClaimSleuth MCP server, built on the official MCP Python SDK 2.x (`mcp.server.MCPServer`).

Exposes exactly two tools over the Streamable HTTP transport at /mcp:
  - warranty_policy_search: keyword search over data/policies.json
  - claim_history_lookup:   records from the SQLite claim_history table
Plus two plain HTTP routes for operations (not MCP): GET /health and GET /tool-calls.

Every tools/call request, including ones rejected during argument validation, is logged by a
server middleware as one JSON line (run id, tool, arguments, start time, duration, status, error).
The run id comes from the request's MCP `_meta` ("claimsleuth/run_id") or, failing that, the
X-ClaimSleuth-Run-Id HTTP header; claim id and caller are read the same way (X-ClaimSleuth-Claim-Id,
X-ClaimSleuth-Caller) when present.

Investigation scope: a claim_history_lookup call that carries a claim id (every ADK and CrewAI agent call
does; the frameworks attach it from code, not from model output) may only ask about that claim's claimant
and the serial numbers written on its claim form and documents, and may only receive records linked to
them. Anything else is refused with an "Out of scope" tool error, so a manipulated agent cannot read other
customers' history. Calls without a claim id are operator lookups (e.g. scripts/mcp_client_check.py).
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from claimsleuth import config
from claimsleuth.datastore import load_claim, load_policies
from claimsleuth.documents import extract_fields
from claimsleuth.history import query_history
from claimsleuth.policy_search import search_policies
from claimsleuth.rules import normalise_serial
from claimsleuth.schemas import (ClaimHistoryLookupInput, ClaimHistoryLookupOutput, OptionalId, PolicySearchInput,
                                 PolicySearchOutput, SearchText, SerialText)

REQUIRED_TOOLS = ("warranty_policy_search", "claim_history_lookup")
RUN_ID_META_KEY = "claimsleuth/run_id"
RUN_ID_HEADER = "x-claimsleuth-run-id"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
# Optional correlation fields, read the same way as the run id (MCP _meta first, then HTTP header).
CORRELATION = {"claim_id": ("claimsleuth/claim_id", "x-claimsleuth-claim-id", re.compile(r"^[A-Z0-9][A-Z0-9-]{2,39}$")),
               "caller": ("claimsleuth/caller", "x-claimsleuth-caller", re.compile(r"^[a-z_]+:[a-z_]{1,40}$"))}
SECRET_KEYS = re.compile(r"(token|secret|password|api[_-]?key|authorization)", re.I)
MCP_PATH = "/mcp"
# Claim id of the investigation the current tools/call belongs to (set by the middleware, read by the tool).
_SCOPE_CLAIM: contextvars.ContextVar[str | None] = contextvars.ContextVar("claimsleuth_scope_claim", default=None)

logger = logging.getLogger("claimsleuth.mcp")


class OutOfScopeError(ValueError):
    pass


def investigation_scope(claim_id: str, data_dir: Path) -> tuple[str, set[str]]:
    """(claimant id, serial numbers) of the claim under investigation, from its claim form and documents."""
    try:
        claim, documents = load_claim(claim_id, data_dir)
    except (FileNotFoundError, ValidationError) as exc:
        raise OutOfScopeError(f"claim {claim_id} is not an investigation known to this server") from exc
    serials = {normalise_serial(claim.serial_number)}
    for doc in documents:
        fields = extract_fields(doc)
        ref = fields.values.get("serial_number")
        serials.add(normalise_serial(ref.value if ref else None))
        serials.update(normalise_serial(b.value) for a, b in fields.conflicts if b.field == "serial_number")
    return claim.claimant_id, {s for s in serials if s}


def check_history_scope(request: ClaimHistoryLookupInput, scope_claim: str | None,
                        data_dir: Path) -> tuple[str, set[str]] | None:
    """Refuse identifiers that do not belong to the claim under investigation (no-op for operator calls)."""
    if scope_claim is None:
        return None
    claimant, serials = investigation_scope(scope_claim, data_dir)
    if request.claimant_id is not None and request.claimant_id != claimant:
        raise OutOfScopeError(f"claimant_id {request.claimant_id} is not the claimant of claim {scope_claim}")
    if request.serial_number is not None and normalise_serial(request.serial_number) not in serials:
        raise OutOfScopeError(f"serial_number {request.serial_number} does not appear on claim {scope_claim} "
                              "or its documents")
    return claimant, serials


def check_records_in_scope(output: ClaimHistoryLookupOutput, scope: tuple[str, set[str]] | None,
                           scope_claim: str | None) -> None:
    """Every returned record must be linked to the claim by claimant or serial (guards claim_id lookups)."""
    if scope is None:
        return
    claimant, serials = scope
    unlinked = [r.claim_id for r in output.records if r.claimant_id != claimant and r.serial_number not in serials]
    if unlinked:
        raise OutOfScopeError(f"history record(s) {', '.join(unlinked)} are not linked to claim {scope_claim}")


class ToolCallLog:
    """Append-only JSON-lines log of tool calls (thread-safe within one server process)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        logger.info("tool_call %s", line)

    def recent(self, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines if line.strip()]
        if run_id is not None:
            records = [r for r in records if r.get("run_id") == run_id]
        return records[-limit:]


def _redact(arguments: Any) -> Any:
    if isinstance(arguments, dict):
        return {k: ("[REDACTED]" if SECRET_KEYS.search(str(k)) else _redact(v)) for k, v in arguments.items()}
    if isinstance(arguments, list):
        return [_redact(v) for v in arguments]
    if isinstance(arguments, str) and len(arguments) > 500:
        return arguments[:500] + "...[truncated]"
    return arguments


def _run_id(ctx: Any, params: dict[str, Any]) -> tuple[str | None, str]:
    meta = ctx.meta or params.get("_meta") or {}
    candidate, source = meta.get(RUN_ID_META_KEY), "mcp_meta"
    if candidate is None:
        headers = getattr(ctx.request, "headers", None)
        candidate, source = (headers.get(RUN_ID_HEADER) if headers is not None else None), "http_header"
    if candidate is None:
        return None, "none"
    if not isinstance(candidate, str) or not RUN_ID_PATTERN.fullmatch(candidate):
        return None, "invalid"
    return candidate, source


def _correlation(ctx: Any, params: dict[str, Any], field: str) -> str | None:
    meta_key, header, pattern = CORRELATION[field]
    meta = ctx.meta or params.get("_meta") or {}
    value = meta.get(meta_key)
    if value is None:
        headers = getattr(ctx.request, "headers", None)
        value = headers.get(header) if headers is not None else None
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _summarise(result: Any) -> tuple[bool, str | None, dict[str, Any]]:
    """(is_error, error text, small summary) from a tools/call result model or dict."""
    data = result.model_dump(mode="json", by_alias=True) if isinstance(result, BaseModel) else dict(result or {})
    is_error = bool(data.get("isError", data.get("is_error", False)))
    content = data.get("content") or []
    text = content[0].get("text") if content and isinstance(content[0], dict) else None
    structured = data.get("structuredContent") or data.get("structured_content") or {}
    summary = {k: structured[k] for k in ("total_matches", "returned", "truncated") if k in structured}
    return is_error, (text if is_error else None), summary


def tool_call_logging_middleware(log: ToolCallLog):
    async def middleware(ctx, call_next):
        if ctx.method != "tools/call":
            return await call_next(ctx)
        params = dict(ctx.params or {})
        run_id, run_id_source = _run_id(ctx, params)
        record: dict[str, Any] = {
            "call_id": uuid.uuid4().hex[:12], "run_id": run_id, "run_id_source": run_id_source,
            "claim_id": _correlation(ctx, params, "claim_id"), "caller": _correlation(ctx, params, "caller"),
            "tool": params.get("name"), "arguments": _redact(params.get("arguments") or {}),
            "started_at": datetime.now(timezone.utc).isoformat(), "request_id": ctx.request_id,
        }
        t0 = time.perf_counter()
        token = _SCOPE_CLAIM.set(record["claim_id"])
        try:
            result = await call_next(ctx)
        except Exception as exc:     # protocol-level failure (unknown method params, MCPError ...)
            record.update(status="error", error=f"{type(exc).__name__}: {exc}",
                          duration_ms=round((time.perf_counter() - t0) * 1000, 3))
            log.write(record)
            raise
        finally:
            _SCOPE_CLAIM.reset(token)
        is_error, error, summary = _summarise(result)
        record.update(status="error" if is_error else "ok", error=error, result_summary=summary,
                      duration_ms=round((time.perf_counter() - t0) * 1000, 3))
        log.write(record)
        return result
    return middleware


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"]) or "arguments"
        parts.append(f"{where}: {err['msg']}")
    return "Invalid input - " + "; ".join(parts)


def build_server(data_dir: Path = config.DATA_DIR, db_path: Path = config.DB_PATH,
                 log_path: Path | None = None) -> tuple[MCPServer, ToolCallLog]:
    log = ToolCallLog(log_path or config.RUNTIME_DIR / "logs" / "mcp_tool_calls.jsonl")
    server = MCPServer(
        name="claimsleuth-mcp", version="0.1.0",
        instructions="Read-only warranty tools for ClaimSleuth: policy search and claim history lookup. "
                     "Results are records from the synthetic dataset, never generated text.",
        middleware=[tool_call_logging_middleware(log)],
    )

    @server.tool(name="warranty_policy_search")
    def warranty_policy_search(
        policy_id: OptionalId = None,
        search_query: SearchText = None,
        limit: Annotated[int, Field(ge=1, le=10)] = 5,
    ) -> PolicySearchOutput:
        """Search the warranty policy register by policy id and/or keywords (e.g. "water damage",
        "deductible", "laptop"). Returns the stored policy terms, the terms that matched, and a
        source reference (JSON pointer into policies.json) for each. At least one filter is required."""
        try:
            request = PolicySearchInput(policy_id=policy_id, search_query=search_query, limit=limit)
            return search_policies(request, data_dir)
        except ValidationError as exc:
            raise ToolError(_validation_message(exc)) from exc
        except ValueError as exc:
            raise ToolError(f"Invalid input - {exc}") from exc
        except OSError as exc:
            raise ToolError(f"Policy register unavailable: {type(exc).__name__}") from exc

    @server.tool(name="claim_history_lookup")
    def claim_history_lookup(
        claim_id: OptionalId = None,
        claimant_id: OptionalId = None,
        serial_number: SerialText = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> ClaimHistoryLookupOutput:
        """Look up earlier warranty claims in the claim-history database by historical claim id,
        claimant (customer) id and/or device serial number; filters are combined with AND. Returns the
        matching records newest first, with outcomes, amounts and a source reference per record.
        Inside an investigation only the claim's own claimant and serial numbers may be looked up."""
        scope_claim = _SCOPE_CLAIM.get()
        try:
            request = ClaimHistoryLookupInput(claim_id=claim_id, claimant_id=claimant_id,
                                              serial_number=serial_number, limit=limit)
            scope = check_history_scope(request, scope_claim, data_dir)
            output = query_history(request, db_path)
            check_records_in_scope(output, scope, scope_claim)
            return output
        except OutOfScopeError as exc:
            raise ToolError(f"Out of scope - {exc}; history access is limited to the claim under investigation") from exc
        except ValidationError as exc:
            raise ToolError(_validation_message(exc)) from exc
        except (OSError, sqlite3.Error) as exc:
            raise ToolError(f"Claim history database unavailable: {exc}") from exc

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        """Real checks on every call: tools registered, policy register readable, database queryable."""
        checks: dict[str, dict[str, Any]] = {}
        tools = [t.name for t in await server.list_tools()]
        checks["tools"] = {"ok": sorted(tools) == sorted(REQUIRED_TOOLS), "registered": tools}
        try:
            checks["policy_register"] = {"ok": True, "policies": len(load_policies(data_dir))}
        except Exception as exc:
            checks["policy_register"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            if not Path(db_path).exists():
                raise FileNotFoundError(Path(db_path).name)
            conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
            try:
                rows = conn.execute("SELECT COUNT(*) FROM claim_history").fetchone()[0]
            finally:
                conn.close()
            checks["claim_history_db"] = {"ok": True, "rows": rows}
        except Exception as exc:
            checks["claim_history_db"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        checks["tool_call_log"] = {"ok": log.path.parent.is_dir(), "path": log.path.name}
        healthy = all(c["ok"] for c in checks.values())
        return JSONResponse({"service": "claimsleuth-mcp", "status": "ok" if healthy else "unhealthy",
                             "mcp_sdk_version": version("mcp"), "transport": "streamable-http",
                             "mcp_path": MCP_PATH, "data_dir": str(Path(data_dir).resolve()), "checks": checks},
                            status_code=200 if healthy else 503)

    @server.custom_route("/tool-calls", methods=["GET"])
    async def tool_calls(request: Request) -> JSONResponse:
        """Recorded tool calls, optionally filtered by run id (observability endpoint, not MCP)."""
        run_id = request.query_params.get("run_id")
        try:
            limit = max(1, min(500, int(request.query_params.get("limit", "100"))))
        except ValueError:
            return JSONResponse({"error": "limit must be an integer"}, status_code=400)
        return JSONResponse({"run_id": run_id, "calls": log.recent(run_id, limit)})

    return server, log
