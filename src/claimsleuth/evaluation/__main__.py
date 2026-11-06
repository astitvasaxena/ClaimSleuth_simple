r"""Agent performance evaluation from the command line.

    .venv\Scripts\python -m claimsleuth.evaluation dataset [--write]      check (or regenerate) the stored dataset
    .venv\Scripts\python -m claimsleuth.evaluation run [--limit N] [--cases EVAL-01,EVAL-02]
    .venv\Scripts\python -m claimsleuth.evaluation run --mode live --limit 2 [--confirm-live]

`run` defaults to OFFLINE (labelled test doubles, no Gemini requests). A LIVE run first prints the request estimate
and the local daily budget, and only starts with --confirm-live. Results go to runtime\evaluation\<id>\.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from claimsleuth import execution_mode
from claimsleuth.evaluation import dataset as ds
from claimsleuth.evaluation.runner import EvaluationError, live_estimate, run_evaluation, select_cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClaimSleuth agent performance evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("dataset", help="check the stored dataset against the generator (or --write it)")
    d.add_argument("--write", action="store_true")
    r = sub.add_parser("run", help="run the evaluation (offline by default)")
    r.add_argument("--mode", choices=["offline", "live"], default="offline")
    r.add_argument("--limit", type=int, default=None, help="number of cases, taken across the five categories")
    r.add_argument("--cases", default=None, help="comma-separated case ids")
    r.add_argument("--confirm-live", action="store_true", help="required for --mode live (sends Gemini requests)")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")

    if args.command == "dataset":
        if args.write:
            print(f"Wrote {ds.write()}")
            return 0
        stored = ds.DATASET_FILE.read_text(encoding="utf-8") if ds.DATASET_FILE.exists() else ""
        fresh = ds.to_json(ds.build())
        ds.load()
        print("Dataset", ds.VERSION, "matches the generator" if stored == fresh else "DIFFERS from the generator")
        print(json.dumps({k: v for k, v in ds.summary(ds.load()).items() if k != "categories"}, indent=1))
        return 0 if stored == fresh else 1

    cases = args.cases.split(",") if args.cases else None
    if args.mode == "live":
        execution_mode.load_env()
        n = len(select_cases(ds.load(), cases, args.limit))
        est = live_estimate(n)
        print(f"LIVE evaluation of {n} case(s): about {est['requests_estimate']} Gemini requests "
              f"(upper bound {est['requests_upper_bound']}); local budget today: {est['used_today']} of "
              f"{est['daily_budget']} used; at least {est['minimum_minutes']} min at {est['per_minute_limit']}/min.")
        print(est["note"])
        if not args.confirm_live:
            print("Not started: add --confirm-live to send these requests.")
            return 2

    def show(p: dict) -> None:
        if p.get("stage") == "running":
            print(f"  [{p['index']}/{p['total']}] {p['case_id']}", flush=True)
    try:
        result = asyncio.run(run_evaluation(args.mode, cases, args.limit, confirm_live=args.confirm_live,
                                            progress=show))
    except EvaluationError as exc:
        print(f"Evaluation not run: {exc}", file=sys.stderr)
        return 2
    meta, combined = result["meta"], result["aggregate"]["approaches"]["combined"]
    print(f"\n{meta['evaluation_id']}  mode {meta['mode_label']}  cases {meta['cases_executed']}/"
          f"{meta['cases_requested']}  final-decision accuracy {combined['decision']['accuracy']}  "
          f"Gemini requests added to the ledger: {meta['ledger_requests_added']}")
    if meta.get("stopped_reason"):
        print("Stopped early:", meta["stopped_reason"])
    print(f"Results: runtime\\evaluation\\{meta['evaluation_id']}\\ (results.json, cases.csv, metrics.json, report.md)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
