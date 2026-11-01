r"""The CrewAI Forensic Agent as an A2A service (a2a-sdk 1.3.0, A2A protocol 1.0, JSON-RPC over HTTP).

    .venv-crew\Scripts\python -m claimsleuth.crew_service.a2a_server [--port 8103] [--mcp-url URL]

Routes:
  GET  /.well-known/agent-card.json   Agent Card (a2a-sdk create_agent_card_routes)
  POST /a2a                            A2A JSON-RPC endpoint (a2a-sdk create_jsonrpc_routes)
  GET  /health                         real checks (crewai import, MCP server reachable)   -- not A2A
  GET  /messages?run_id=...            this service's own log of received/sent messages     -- not A2A

The executor accepts one A2A text part holding a WorkflowMessage (FORENSIC_REQUEST), validates it
(contract, known claim, byte-identical evidence by SHA-256), runs the real CrewAI Crew for that claim
and replies with a FORENSIC_RESPONSE (or ERROR) WorkflowMessage. Runs are serialised: CrewAI's event-bus
handlers are process-wide.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import threading
from importlib.metadata import version
from pathlib import Path
from typing import Any

import claimsleuth.crew_service  # noqa: F401  (telemetry/tracing off before crewai is imported)
import uvicorn
from a2a.helpers.proto_helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from claimsleuth import config, contract
from claimsleuth.crew_service import ollama as ollama_mode
from claimsleuth.crew_service.runner import DEFAULT_MCP_URL, mcp_health, run_forensic_investigation
from claimsleuth.datastore import load_claim
from claimsleuth.schemas import WorkflowMessage, utc_now

RPC_PATH = "/a2a"


def llm_mode() -> str:
    return os.environ.get("CLAIMSLEUTH_CREW_LLM", "test-double")


def llm_check() -> dict:
    """Real readiness of the configured model: the test double always; ollama-local only after preflight."""
    mode = llm_mode()
    if mode == "test-double":
        return {"ok": True, "mode": mode, "model": "test-double"}
    if mode == ollama_mode.MODE:
        try:
            settings = ollama_mode.settings_from_env()
            info = ollama_mode.preflight(settings)
            return {"ok": True, "mode": mode, "model": settings.model, "provider": ollama_mode.PROVIDER,
                    "ollama_version": info["ollama_version"], "endpoint": settings.base_url}
        except (ollama_mode.OllamaConfigError, ollama_mode.OllamaUnavailableError) as exc:
            return {"ok": False, "mode": mode, "error": str(exc)}
    return {"ok": False, "mode": mode, "error": "unknown mode"}


def agent_card(base_url: str) -> AgentCard:
    return AgentCard(
        name="ClaimSleuth CrewAI Forensic Agent",
        description="Forensic review of one warranty claim: serial numbers, chronology, suspicious details, "
                    "claim-history patterns (via MCP) and evidence gaps. Recommends an outcome only; never "
                    "authorises a payout. Model: " + ("a labelled offline test double (no language model)."
                                                      if llm_mode() == "test-double" else
                                                      f"local Ollama inference ({llm_mode()}) on 127.0.0.1."),
        version="0.1.0",
        supported_interfaces=[AgentInterface(url=base_url + RPC_PATH, protocol_binding="JSONRPC", protocol_version="1.0")],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["application/json"], default_output_modes=["application/json"],
        skills=[AgentSkill(
            id="warranty_claim_forensics", name="Warranty claim forensics",
            description="Input: a ClaimSleuth WorkflowMessage FORENSIC_REQUEST (JSON). Output: FORENSIC_RESPONSE "
                        "with a validated ForensicsReport, or ERROR.",
            tags=["warranty", "forensics", "crewai"], input_modes=["application/json"],
            output_modes=["application/json"])],
    )


class MessageLog:
    def __init__(self, path: Path) -> None:
        self.path, self._lock = path, threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, direction: str, message: WorkflowMessage | None, note: str = "") -> None:
        record = {"ts": utc_now().isoformat(), "direction": direction, "note": note}
        if message is not None:
            record |= {"message_id": message.message_id, "run_id": message.run_id, "claim_id": message.claim_id,
                       "message_type": message.message_type, "sender": message.sender, "recipient": message.recipient}
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def read(self, run_id: str | None) -> list[dict]:
        if not self.path.exists():
            return []
        rows = [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]
        return [r for r in rows if run_id is None or r.get("run_id") == run_id]


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
_CLAIM_ID = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,39}$")


def _ids_from_raw(text: str) -> tuple[str, str]:
    """Best-effort ids for an ERROR reply to a message that failed validation (placeholders if unusable)."""
    try:
        raw = json.loads(text)
        run_id, claim_id = str(raw.get("run_id", "")), str(raw.get("claim_id", ""))
    except (json.JSONDecodeError, AttributeError):
        run_id, claim_id = "", ""
    return (run_id if _RUN_ID.fullmatch(run_id) else "unknown-run",
            claim_id if _CLAIM_ID.fullmatch(claim_id) else "UNKNOWN")


class ForensicExecutor(AgentExecutor):
    def __init__(self, mcp_url: str, data_dir: Path, log: MessageLog) -> None:
        self.mcp_url, self.data_dir, self.log = mcp_url, data_dir, log
        self._lock = asyncio.Lock()

    async def handle(self, text: str) -> WorkflowMessage:
        try:
            request = contract.parse(text)
        except contract.ContractError as exc:
            run_id, claim_id = _ids_from_raw(text)
            self.log.write("received", None, f"rejected: {exc}")
            return contract.error_message(run_id, claim_id, exc.code, exc.detail)
        self.log.write("received", request)
        try:
            _, documents = load_claim(request.claim_id, self.data_dir)
            contract.check_request(request, documents)
        except FileNotFoundError:
            return contract.error_message(request.run_id, request.claim_id, "CLAIM_MISMATCH",
                                          f"claim {request.claim_id} is not known to the CrewAI service")
        except contract.ContractError as exc:
            return contract.error_message(request.run_id, request.claim_id, exc.code, exc.detail)
        async with self._lock:
            result = await asyncio.to_thread(run_forensic_investigation, request.claim_id, run_id=request.run_id,
                                             mcp_url=self.mcp_url, data_dir=self.data_dir)
        mode = "test-double" if result.model == "test-double" else ("live" if result.model else "none")
        return contract.forensic_response(request, result, llm_mode=mode)

    async def execute(self, context, event_queue) -> None:
        reply = await self.handle(context.get_user_input())
        self.log.write("sent", reply)
        await event_queue.enqueue_event(new_text_message(reply.model_dump_json(), context_id=context.context_id,
                                                         task_id=context.task_id))

    async def cancel(self, context, event_queue) -> None:
        raise NotImplementedError("forensic runs cannot be cancelled")


def build_app(base_url: str, mcp_url: str, data_dir: Path, log_path: Path) -> Starlette:
    card = agent_card(base_url)
    log = MessageLog(log_path)
    executor = ForensicExecutor(mcp_url, data_dir, log)
    handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card)

    async def health(request: Request) -> JSONResponse:
        mcp_ok, mcp_detail = await asyncio.to_thread(mcp_health, mcp_url)
        checks = {"crewai": {"ok": True, "version": version("crewai")},
                  "a2a_sdk": {"ok": True, "version": version("a2a-sdk")},
                  "mcp_server": {"ok": mcp_ok, "detail": mcp_detail},
                  "llm": await asyncio.to_thread(llm_check)}
        healthy = all(c["ok"] for c in checks.values())
        return JSONResponse({"service": "claimsleuth-crewai-a2a", "status": "ok" if healthy else "unhealthy",
                             "agent_card": "/.well-known/agent-card.json", "rpc": RPC_PATH,
                             "data_dir": str(Path(data_dir).resolve()), "checks": checks},
                            status_code=200 if healthy else 503)

    async def messages(request: Request) -> JSONResponse:
        return JSONResponse({"messages": log.read(request.query_params.get("run_id"))})

    routes = create_agent_card_routes(card) + create_jsonrpc_routes(handler, rpc_url=RPC_PATH)
    routes += [Route("/health", health, methods=["GET"]), Route("/messages", messages, methods=["GET"])]
    return Starlette(routes=routes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClaimSleuth CrewAI Forensic Agent A2A service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CLAIMSLEUTH_CREW_PORT", "8103")))
    parser.add_argument("--mcp-url", default=os.environ.get("CLAIMSLEUTH_MCP_URL", DEFAULT_MCP_URL))
    parser.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    parser.add_argument("--log-file", type=Path, default=config.RUNTIME_DIR / "logs" / "a2a_crew_messages.jsonl")
    parser.add_argument("--pid-file", type=Path, default=config.RUNTIME_DIR / "pids" / "crew_a2a.pid")
    args = parser.parse_args(argv)
    base_url = f"http://{args.host}:{args.port}"
    args.pid_file.parent.mkdir(parents=True, exist_ok=True)
    args.pid_file.write_text(str(os.getpid()), encoding="ascii")
    print(f"ClaimSleuth CrewAI A2A service pid={os.getpid()} card={base_url}/.well-known/agent-card.json "
          f"rpc={base_url}{RPC_PATH} mcp={args.mcp_url} llm_mode={llm_mode()}", file=sys.stderr, flush=True)
    try:
        uvicorn.run(build_app(base_url, args.mcp_url, args.data_dir, args.log_file), host=args.host, port=args.port,
                    log_level="warning")
    finally:
        if args.pid_file.exists() and args.pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
            args.pid_file.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
