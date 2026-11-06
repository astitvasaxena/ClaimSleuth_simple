r"""Start the ClaimSleuth dashboard (and, unless told not to, the MCP server and the CrewAI A2A service).

    .venv\Scripts\python -m claimsleuth.dashboard [--mode live|offline] [--port 8110] [--no-services]
                                                [--no-blackboard] [--crew-mode ollama-local]

Binds to 127.0.0.1 only. Services it starts use the dashboard workspace (runtime/dashboard/data) and are stopped
again when the dashboard exits; services that were already running are used as they are and left running.

--mode (default: EXECUTION_MODE from the environment or .env, else offline):
  offline  all three agents use the labelled offline test doubles (not LLM reasoning); no key needed.
  live     all three agents use genuine Gemini inference; needs GOOGLE_API_KEY. The CrewAI service is started in
           Gemini mode; a service already running in another mode is reported and LIVE runs are refused.
--crew-mode ollama-local is the older experimental local-model option for the Forensic Agent (offline ADK); it
cannot be combined with --mode live. The mode is chosen here, explicitly; the dashboard never switches it and never
falls back.
"""
from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from claimsleuth import config, execution_mode
from claimsleuth.dashboard.services import ServiceConfig, ServiceLauncher
from claimsleuth.dashboard.workspace import Workspace


DEFAULT_BLACKBOARD = config.RUNTIME_DIR / "blackboard.sqlite3"


def blackboard_setting(no_blackboard: bool, env: dict[str, str] | None = None) -> str:
    """The CLAIMSLEUTH_BLACKBOARD_DB value the dashboard runs with: "off" with --no-blackboard; otherwise an
    explicitly configured value is respected (including "off" or an empty value); otherwise
    runtime/blackboard.sqlite3."""
    if no_blackboard:
        return "off"
    value = (os.environ if env is None else env).get("CLAIMSLEUTH_BLACKBOARD_DB")
    return value if value is not None else str(DEFAULT_BLACKBOARD)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClaimSleuth demonstration dashboard")
    parser.add_argument("--port", type=int, default=8110)
    parser.add_argument("--mcp-port", type=int, default=8102)
    parser.add_argument("--crew-port", type=int, default=8103)
    parser.add_argument("--mode", choices=[execution_mode.LIVE, execution_mode.OFFLINE], default=None,
                        help="live: Gemini for all three agents; offline: test doubles (default EXECUTION_MODE or offline)")
    parser.add_argument("--crew-mode", choices=["test-double", "ollama-local"], default=None,
                        help="offline only: experimental local Ollama model for the Forensic Agent")
    parser.add_argument("--no-services", action="store_true", help="do not start the MCP / CrewAI services")
    parser.add_argument("--no-blackboard", action="store_true",
                        help="disable the shared blackboard: direct mode (also: CLAIMSLEUTH_BLACKBOARD_DB=off)")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    execution_mode.load_env()                 # GOOGLE_API_KEY, EXECUTION_MODE from .env (values already set win)
    try:
        mode = args.mode or execution_mode.execution_mode() or execution_mode.OFFLINE
    except execution_mode.ExecutionModeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if mode == execution_mode.LIVE:
        if args.crew_mode not in (None, execution_mode.GEMINI):
            print("ERROR: --crew-mode cannot be combined with --mode live (live uses Gemini for every agent)",
                  file=sys.stderr)
            return 2
        problems = execution_mode.gemini_config_errors()
        if problems:
            print(f"ERROR: LIVE mode needs Gemini credentials. {problems[0]}", file=sys.stderr)
            return 2
        crew_mode = execution_mode.GEMINI
    else:
        crew_mode = args.crew_mode or execution_mode.TEST_DOUBLE
    if crew_mode == "ollama-local":           # experimental per-component mode: EXECUTION_MODE does not apply
        os.environ.pop(execution_mode.ENV_VAR, None)
    else:
        os.environ[execution_mode.ENV_VAR] = mode
    live = mode == execution_mode.LIVE and crew_mode == execution_mode.GEMINI
    if live:
        banner = f"LIVE - Gemini {execution_mode.gemini_model()} for all three agents"
    elif crew_mode == execution_mode.TEST_DOUBLE:
        banner = "OFFLINE - labelled test doubles for all three agents (not LLM reasoning)"
    else:
        banner = "EXPERIMENTAL - test-double ADK agents, ollama-local Forensic Agent"
    print(f"Execution mode: {banner}", flush=True)
    # Set before the services start, so the CrewAI service inherits the same database.
    os.environ["CLAIMSLEUTH_BLACKBOARD_DB"] = blackboard_setting(args.no_blackboard)

    from claimsleuth.dashboard.app import create_app      # imports google-adk (a few seconds)

    workspace, cfg = Workspace(), ServiceConfig(mcp_port=args.mcp_port, crew_port=args.crew_port)
    workspace.prepare()
    launcher = ServiceLauncher(cfg, workspace.data_dir, crew_mode)
    if not args.no_services:
        launcher.start(log=lambda m: print(m, flush=True))
    try:
        print(f"ClaimSleuth dashboard: http://127.0.0.1:{args.port}/  (workspace {workspace.data_dir})", flush=True)
        uvicorn.run(create_app(workspace, cfg, live=live), host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        launcher.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
