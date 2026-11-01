r"""Show the blackboard of one investigation run (read-only).

    python -m claimsleuth.blackboard RUN-dash-1234567890 [--db runtime\blackboard.sqlite3] [--json]
    python -m claimsleuth.blackboard --list
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from claimsleuth import config
from claimsleuth.blackboard.controller import read_monitoring_failures
from claimsleuth.blackboard.store import Blackboard, BlackboardError, configured_path

DEFAULT_DB = config.RUNTIME_DIR / "blackboard.sqlite3"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show a run's shared coordination blackboard")
    parser.add_argument("run_id", nargs="?")
    parser.add_argument("--db", type=Path, default=None, help=f"database (default: $CLAIMSLEUTH_BLACKBOARD_DB or {DEFAULT_DB})")
    parser.add_argument("--list", action="store_true", help="list the most recent runs")
    parser.add_argument("--json", action="store_true", help="print the entries as JSON")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    path = args.db or configured_path() or DEFAULT_DB
    if not path.exists():
        print(f"no blackboard database at {path}", file=sys.stderr)
        return 1
    try:
        board = Blackboard(path)
        if args.list or not args.run_id:
            for run in board.runs():
                print(f"{run['created_at']}  {run['run_id']}  {run['claim_id']}  documents {run['fingerprint'][:12]}...")
            return 0
        registration = board.registration(args.run_id)
        if registration is None:
            print(f"run {args.run_id} is not on the blackboard {path}", file=sys.stderr)
            return 1
        entries = board.entries(args.run_id, registration["claim_id"])
        state = board.state(args.run_id, registration["claim_id"])
    except BlackboardError as exc:
        print(f"blackboard unavailable: {exc}", file=sys.stderr)
        return 2
    failures = read_monitoring_failures(path, args.run_id)
    if args.json:
        print(json.dumps({"registration": registration, "entries": [e.model_dump(mode="json") for e in entries],
                          "monitoring_failures": failures}, indent=2))
        return 0
    print(f"Run {args.run_id}  claim {registration['claim_id']}  documents {registration['fingerprint'][:16]}...  ({path})")
    for e in entries:
        what = (e.payload.get("state") or e.payload.get("event") or e.payload.get("reason")
                or e.payload.get("final_decision") or "")
        ref = f" -> entry {e.payload.get('retracted_run_seq')}" if e.kind == "retract" else ""
        flags = ("  LATE" if e.late else "") + ("" if e.auth == "valid" else f"  AUTH:{e.auth.upper()}")
        print(f"{e.run_seq:>3} {e.ts:%H:%M:%S.%f}"[:-3] + f"  {e.kind:<8} {e.section:<28} {e.author:<20} "
              f"{(e.principal or '-'):<13} pid {e.process.pid:<6} {e.process.environment:<10} {what}{ref}{flags}")
    print("Agents: " + ", ".join(f"{a.agent}={a.state}" + (" (late)" if a.late else "") for a in state.agents.values()))
    print(f"Sealed at entry: {state.sealed_at}  late entries: {len(state.late)}  authentication: {state.authentication}")
    print(f"Live posts: {len(state.live_posts)}  retracted: {len(state.retracted)}  monitoring failures: {len(failures)}")
    for failure in failures:
        print(f"  MONITORING FAILURE [{failure['where']}] ({failure['environment']} pid {failure['pid']}): {failure['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
