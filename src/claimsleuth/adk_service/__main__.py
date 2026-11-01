"""Run the GOOGLE ADK investigation of one stored claim with a real Gemini model.

    python -m claimsleuth.adk_service CS-001 [--mcp-url URL] [--model NAME] [--run-id RUN-...] [--json]
    python -m claimsleuth.adk_service --check-config

Needs GOOGLE_API_KEY (or GEMINI_API_KEY) in the environment or .env, and the MCP server running.
Exit codes: 0 completed, 1 failed, 3 not configured.
"""
from __future__ import annotations

import argparse
import os
import sys

from claimsleuth.adk_service import config as adk_config
from claimsleuth.adk_service.runner import mcp_health, run_adk_investigation, save_result
from claimsleuth.schemas import ExecutionStatus


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClaimSleuth GOOGLE ADK investigation (Evidence + Investigator agents)")
    parser.add_argument("claim_id", nargs="?")
    parser.add_argument("--mcp-url", default=None)
    parser.add_argument("--model", default=None, help="Gemini model name (default from GEMINI_MODEL or "
                                                      f"{adk_config.DEFAULT_GEMINI_MODEL})")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json", action="store_true", help="print the JSON result instead of Markdown")
    parser.add_argument("--offline", action="store_true",
                        help="use the labelled OfflineAdkLlm TEST DOUBLE instead of Gemini (no LLM, no key)")
    parser.add_argument("--check-config", action="store_true", help="only check model credentials and the MCP server")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    adk_config.load_env()
    if args.model:
        os.environ["CLAIMSLEUTH_GEMINI_MODEL"] = args.model

    if args.check_config or not args.claim_id:
        problems = adk_config.llm_config_errors()
        mcp_ok, mcp_detail = mcp_health(args.mcp_url or adk_config.mcp_url())
        print(f"Model:  {adk_config.gemini_model()}  credentials: {'configured' if not problems else 'NOT CONFIGURED'}")
        for p in problems:
            print(f"        {p}")
        print(f"MCP:    {'ok' if mcp_ok else 'NOT AVAILABLE'} ({mcp_detail})")
        return 0 if not problems and mcp_ok else 3

    from claimsleuth.render import adk_markdown
    result = run_adk_investigation(args.claim_id, run_id=args.run_id, mcp_url=args.mcp_url,
                                   llm_mode="test-double" if args.offline else None)
    print(result.model_dump_json(indent=2) if args.json else adk_markdown(result))
    if result.status != ExecutionStatus.NOT_CONFIGURED:
        print(f"\nSaved to {save_result(result)}", file=sys.stderr)
    return {ExecutionStatus.COMPLETED: 0, ExecutionStatus.NOT_CONFIGURED: 3}.get(result.status, 1)


if __name__ == "__main__":
    raise SystemExit(main())
