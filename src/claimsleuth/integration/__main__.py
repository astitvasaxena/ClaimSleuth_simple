"""Run one claim through all four ClaimSleuth outputs and the deterministic adjudicator.

    python -m claimsleuth.integration CS-003 [--run-id RUN-...] [--mcp-url URL] [--crew-card URL] [--json]

Needs the MCP server (.venv, port 8102) and the CrewAI A2A service (.venv-crew, port 8103) running.
The ADK agents use the labelled offline test double (no LLM, no key).
"""
from __future__ import annotations

import argparse
import sys
import warnings

from claimsleuth.integration.run import DEFAULT_CREW_CARD, DEFAULT_MCP_URL, run_claim, save_bundle
from claimsleuth.render import final_markdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClaimSleuth integrated run (baseline, ADK, CrewAI via A2A, MCP)")
    parser.add_argument("claim_id")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    parser.add_argument("--crew-card", default=DEFAULT_CREW_CARD)
    parser.add_argument("--json", action="store_true", help="print the whole bundle as JSON")
    parser.add_argument("--a2a-timeout", type=float, default=None,
                        help="seconds to wait for the CrewAI service (default CLAIMSLEUTH_A2A_TIMEOUT or 180; "
                             "use ~1200 when the service runs ollama-local)")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    bundle = run_claim(args.claim_id, run_id=args.run_id, mcp_url=args.mcp_url, crew_card_url=args.crew_card,
                       a2a_timeout=args.a2a_timeout)
    print(bundle.model_dump_json(indent=2) if args.json else final_markdown(bundle))
    print(f"\nSaved to {save_bundle(bundle)}", file=sys.stderr)
    return 0 if bundle.final_report is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
