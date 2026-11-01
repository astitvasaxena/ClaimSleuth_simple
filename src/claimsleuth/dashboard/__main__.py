r"""Start the ClaimSleuth dashboard (and, unless told not to, the MCP server and the CrewAI A2A service).

    .venv\Scripts\python -m claimsleuth.dashboard [--port 8110] [--crew-mode test-double|ollama-local] [--no-services]

Binds to 127.0.0.1 only. Services it starts use the dashboard workspace (runtime/dashboard/data) and are stopped
again when the dashboard exits; services that were already running are used as they are and left running.
The CrewAI mode is chosen here, explicitly; the dashboard never switches it and never falls back.
"""
from __future__ import annotations

import argparse
import sys

import uvicorn

from claimsleuth.dashboard.services import ServiceConfig, ServiceLauncher
from claimsleuth.dashboard.workspace import Workspace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClaimSleuth demonstration dashboard")
    parser.add_argument("--port", type=int, default=8110)
    parser.add_argument("--mcp-port", type=int, default=8102)
    parser.add_argument("--crew-port", type=int, default=8103)
    parser.add_argument("--crew-mode", choices=["test-double", "ollama-local"], default="test-double",
                        help="LLM mode for a CrewAI service started by this command (default: test-double)")
    parser.add_argument("--no-services", action="store_true", help="do not start the MCP / CrewAI services")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    from claimsleuth.dashboard.app import create_app      # imports google-adk (a few seconds)

    workspace, cfg = Workspace(), ServiceConfig(mcp_port=args.mcp_port, crew_port=args.crew_port)
    workspace.prepare()
    launcher = ServiceLauncher(cfg, workspace.data_dir, args.crew_mode)
    if not args.no_services:
        launcher.start(log=lambda m: print(m, flush=True))
    try:
        print(f"ClaimSleuth dashboard: http://127.0.0.1:{args.port}/  (workspace {workspace.data_dir})", flush=True)
        uvicorn.run(create_app(workspace, cfg), host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        launcher.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
