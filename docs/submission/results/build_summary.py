"""Assemble results/summary.json from the files in this folder (re-runnable; invents nothing).

    .venv/bin/python docs/submission/results/build_summary.py
Reads experiments_summary.json, experiment_runs.csv, baseline_eval_40_metrics.json (the stored 40-case offline run).
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIVE_RUNS = {"RUN-dash-90e0f64064": 9, "RUN-dash-5c40c24a5a": 6, "RUN-dash-9f819f9dbc": 7, "RUN-dash-bcdbabaf97": 7}


def main() -> None:
    exp = json.loads((HERE / "experiments_summary.json").read_text(encoding="utf-8"))
    base_path = HERE / "baseline_eval_40_results.json"
    base = json.loads(base_path.read_text(encoding="utf-8")) if base_path.exists() else None
    out = {
        "mode": exp["mode"], "synthetic": True, "currency": "INR",
        "limits": ["Offline agents are rule-based test doubles: they read the claim packet, not other agents' reports or "
                   "prompt wording, so decisions cannot vary with topology, messaging style or prompt structure.",
                   "Offline latency (about 12 s per ADK agent, about 0.5 s for the Forensic Agent) is a property of the "
                   "local stack with test doubles, not of Gemini; it must not be quoted as live latency.",
                   "No live (Gemini) accuracy, latency or token figure exists; only 4 complete live runs are on record."],
        "runs": {"experiments_total": exp["total_runs"], "experiments_failed": exp["failed_runs"],
                 "e3_architecture": sum(v["runs"] for v in exp["e3_architecture"].values()),
                 "e4_messaging": sum(v["runs"] for v in exp["e4_messaging"].values()),
                 "e5_persona_structure": exp["e5_persona_structure"]["runs"]},
        "personas": exp["e5_persona_structure"]["personas"],
        "prompt_structures": exp["e5_persona_structure"]["structures"],
        "e3_architecture": exp["e3_architecture"], "e4_messaging": exp["e4_messaging"],
        "e5_persona_structure": exp["e5_persona_structure"],
        "variance": {"system_latency_ms_by_config": {k: {x: v["wall_ms"].get(x) for x in ("mean", "stdev", "p25", "median", "p75")}
                                                   for k, v in exp["e3_architecture"].items()},
                     "decision_changed_by_prompt_structure": exp["e5_persona_structure"]["decision_changed_by_structure"],
                     "of_paired_comparisons": exp["e5_persona_structure"]["paired_comparisons"]},
        "per_agent_and_system_latency_ms_all_runs": exp["by_agent_latency_ms"],
        "decisions_all_runs": None,
        "accuracy": {"e3": {k: v["accuracy"] for k, v in exp["e3_architecture"].items()},
                     "e4": {k: v["accuracy"] for k, v in exp["e4_messaging"].items()},
                     "e5_by_structure": {k: v["accuracy"] for k, v in exp["e5_persona_structure"]["by_structure"].items()}},
        "cost": {
            "offline_cost_inr": 0, "offline_gemini_requests": 0,
            "live_cost_model": {
                "source": "src/claimsleuth/observability/pricing.py",
                "formula": "((prompt - cached) * input_rate + cached * cached_rate + completion * output_rate) / 1e6, from a "
                           "price table named by CLAIMSLEUTH_MODEL_PRICING with source and verified_on per model",
                "prices_shipped": False,
                "result_without_table": "cost is None with the reason; never an estimate",
                "model": "gemini-3.5-flash",
                "live_requests_per_claim_estimate": 10,
                "live_requests_in_4_recorded_runs": LIVE_RUNS,
                "live_requests_in_4_runs_total": sum(LIVE_RUNS.values()),
                "live_requests_for_40_runs_range": [6 * 40, 9 * 40],
                "live_requests_for_40_runs_at_estimate": 400,
                "free_tier_requests_per_day_per_model": 20,
                "note": "No token counts or prices are recorded for the 4 live runs, so no live rupee/dollar figure exists."}},
    }
    if base:
        agg = base["aggregate"]["approaches"]["combined"]
        out["baseline_eval_40"] = {"evaluation_id": base["meta"]["evaluation_id"], "cases": base["meta"]["cases_executed"],
                                   "decision_accuracy": agg["decision"]["accuracy"],
                                   "ledger_requests_added": base["meta"]["ledger_requests_added"]}
        rows = base["rows"]
        lat = [r["combined"]["latency_ms"] for r in rows if r["combined"].get("latency_ms")]
        out["baseline_eval_40"]["combined_latency_ms"] = {"mean": round(sum(lat) / len(lat), 1), "min": round(min(lat), 1),
                                                          "max": round(max(lat), 1), "n": len(lat)}
    decisions: dict[str, int] = {}
    import csv
    with (HERE / "experiment_runs.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            decisions[r["final_decision"]] = decisions.get(r["final_decision"], 0) + 1
    out["decisions_all_runs"] = decisions
    (HERE / "summary.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print("wrote summary.json")


if __name__ == "__main__":
    main()
