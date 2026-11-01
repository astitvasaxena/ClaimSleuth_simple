r"""Run the CREWAI forensic investigation of one stored claim (offline test-double mode).

    .venv-crew\Scripts\python -m claimsleuth.crew_service CS-003 [--mcp-url URL] [--run-id RUN-...] [--json]

Needs the MCP server running. Exit codes: 0 completed, 1 failed, 3 not configured.
"""
from __future__ import annotations

import argparse
import sys

import claimsleuth.crew_service  # noqa: F401  (telemetry/tracing off before crewai is imported)
from claimsleuth.crew_service.runner import run_forensic_investigation, save_result
from claimsleuth.render import crewai_markdown
from claimsleuth.schemas import ExecutionStatus


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClaimSleuth CREWAI forensic investigation")
    parser.add_argument("claim_id")
    parser.add_argument("--mcp-url", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json", action="store_true", help="print the JSON result instead of Markdown")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    result = run_forensic_investigation(args.claim_id, run_id=args.run_id, mcp_url=args.mcp_url)
    print(result.model_dump_json(indent=2) if args.json else crewai_markdown(result))
    if result.status != ExecutionStatus.NOT_CONFIGURED:
        print(f"\nSaved to {save_result(result)}", file=sys.stderr)
    return {ExecutionStatus.COMPLETED: 0, ExecutionStatus.NOT_CONFIGURED: 3}.get(result.status, 1)


if __name__ == "__main__":
    raise SystemExit(main())
