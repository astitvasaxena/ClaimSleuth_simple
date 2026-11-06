"""Run one claim through all four ClaimSleuth outputs and the deterministic adjudicator.

    python -m claimsleuth.integration CS-003 [--mode live|offline] [--run-id RUN-...] [--mcp-url URL]
                                              [--crew-card URL] [--json]

Needs the MCP server (.venv, port 8102) and the CrewAI A2A service (.venv-crew, port 8103) running.
--mode (or EXECUTION_MODE, also read from .env) chooses how the agents run:
  offline  (default) the labelled offline test doubles for all three agents (no LLM, no key);
  live     genuine Gemini inference for all three agents. The CrewAI service must have been started with
           EXECUTION_MODE=live too; otherwise the run is refused before anything starts (exit code 2).
Exit codes: 0 complete investigation, 1 no final report, 2 refused, 4 INCOMPLETE investigation (an agent review is
missing, or the records confirming the reviews are; the deterministic decision needs a human reviewer).
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings

from claimsleuth import execution_mode
from claimsleuth.integration.run import DEFAULT_CREW_CARD, DEFAULT_MCP_URL, LiveModeError, run_claim, save_bundle
from claimsleuth.observability.outcome import incomplete_text
from claimsleuth.render import final_markdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClaimSleuth integrated run (baseline, ADK, CrewAI via A2A, MCP)")
    parser.add_argument("claim_id")
    parser.add_argument("--mode", choices=[execution_mode.LIVE, execution_mode.OFFLINE], default=None,
                        help="live (Gemini for all three agents) or offline (test doubles); default EXECUTION_MODE "
                             "or offline")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    parser.add_argument("--crew-card", default=DEFAULT_CREW_CARD)
    parser.add_argument("--json", action="store_true", help="print the whole bundle as JSON")
    parser.add_argument("--a2a-timeout", type=float, default=None,
                        help="seconds to wait for the CrewAI service (default: 600 in live mode, otherwise "
                             "CLAIMSLEUTH_A2A_TIMEOUT or 180; use ~1200 when the service runs ollama-local)")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    execution_mode.load_env()
    if args.mode:
        os.environ[execution_mode.ENV_VAR] = args.mode
    try:
        mode = execution_mode.execution_mode() or execution_mode.OFFLINE
    except execution_mode.ExecutionModeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Execution mode: {mode.upper()}"
          + (f" (Gemini {execution_mode.gemini_model()} for all three agents)" if mode == execution_mode.LIVE
             else " (offline test doubles, not LLM reasoning)"), file=sys.stderr)
    try:
        bundle = run_claim(args.claim_id, run_id=args.run_id, mcp_url=args.mcp_url, crew_card_url=args.crew_card,
                           a2a_timeout=args.a2a_timeout)
    except LiveModeError as exc:
        print(f"LIVE MODE NOT AVAILABLE: {exc}", file=sys.stderr)
        return 2
    print(bundle.model_dump_json(indent=2) if args.json else final_markdown(bundle))
    print(f"\nSaved to {save_bundle(bundle)}", file=sys.stderr)
    final = bundle.final_report
    if final is None:
        return 1
    if final.review_status in ("PARTIAL", "NONE"):
        print(f"{incomplete_text(final)}: " + "; ".join(final.incomplete_reasons), file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
