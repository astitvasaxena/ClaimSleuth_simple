"""Aggregate scored evaluation rows into metrics, heatmap / box-plot / confusion-matrix data and exports.

Every number here is computed from the stored per-case rows; nothing is estimated or filled in. A value that
cannot be computed is None (N/A).
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from claimsleuth.evaluation import metrics as M
from claimsleuth.evaluation.dataset import ANOMALY_TYPES, CATEGORIES
from claimsleuth.evaluation.scoring import APPROACHES

DECISIONS = ["APPROVE", "REJECT", "REQUEST_EVIDENCE", "ESCALATE"]
TITLES = {"evidence": "Evidence Agent", "investigator": "Investigator Agent", "forensic": "Forensic Agent",
          "combined": "Combined Multi-Agent System"}
TASKS = {
    "evidence": "Missing-evidence detection (positive = required evidence missing)",
    "investigator": "Policy-violation detection (positive = NOT_ELIGIBLE)",
    "forensic": "Anomaly detection (positive = forensic anomaly present)",
    "combined": "Final claim decision (4 classes)",
}
SCENARIO_METRIC = {"evidence": "fact-extraction accuracy", "investigator": "policy-violation detection accuracy",
                   "forensic": "anomaly detection accuracy", "combined": "final-decision accuracy"}
FAILURE_STATES = ("failed", "incomplete", "output_invalid")


def _done(rows: list[dict], a: str) -> list[dict]:
    return [r for r in rows if r[a]["status"] == "completed"]


def _decision_pairs(rows: list[dict], a: str) -> list[tuple[str, str | None]]:
    return [(r["expected_decision"], r[a].get("recommendation") if r[a]["status"] == "completed" else None)
            for r in rows]


def _task_pairs(rows: list[dict], a: str) -> list[tuple[bool, bool | None]]:
    if a == "evidence":
        return [(bool(r["missing_evidence"]), r[a].get("missing_detected")) for r in rows]
    if a == "investigator":
        return [(r["policy_violation"], r[a].get("policy_violation_detected")) for r in rows]
    if a == "forensic":
        return [(bool(r["anomalies"]), r[a].get("anomaly_detected")) for r in rows]
    raise ValueError(a)


def _citation_rate(rows: list[dict], a: str) -> dict[str, Any]:
    done = [r[a] for r in _done(rows, a) if r[a].get("citations_total") is not None]
    valid, total = sum(x["citations_valid"] for x in done), sum(x["citations_total"] for x in done)
    return {"valid": valid, "total": total, "rate": M.ratio(valid, total)}


def _fact_accuracy(rows: list[dict]) -> dict[str, Any]:
    done = [r["evidence"] for r in _done(rows, "evidence")]
    matched, expected = sum(x["facts_matched"] for x in done), sum(x["facts_expected"] for x in done)
    return {"matched": matched, "expected": expected, "accuracy": M.ratio(matched, expected)}


def reliability(rows: list[dict], a: str) -> dict[str, Any]:
    states = [r[a]["status"] for r in rows]
    valid = states.count("completed")
    invalid = states.count("output_invalid")
    return {"executions": len(states), "successful": valid, "failed": states.count("failed"),
            "incomplete": states.count("incomplete"), "structured_output_failures": invalid,
            "completion_rate": M.ratio(valid, len(states)),
            "structured_output_validity": M.ratio(valid, valid + invalid),
            "avg_latency_ms": M.mean([r[a].get("latency_ms") for r in rows])}


def aggregate(rows: list[dict], meta: dict[str, Any]) -> dict[str, Any]:
    live = meta.get("mode") == "live"
    out: dict[str, Any] = {"meta": meta, "approaches": {}, "titles": TITLES, "tasks": TASKS}

    # ---- per approach: common decision task, own task, reliability
    for a in APPROACHES:
        decision = M.multiclass_metrics(_decision_pairs(rows, a), DECISIONS)
        entry: dict[str, Any] = {"title": TITLES[a], "task": TASKS[a], "reliability": reliability(rows, a),
                                 "decision": decision,
                                 "decision_role": "final adjudicated decision" if a == "combined"
                                 else "advisory recommendation (never decides)"}
        if a != "combined":
            entry["task_metrics"] = M.binary_metrics(_task_pairs(rows, a))
            entry["citation_validity"] = _citation_rate(rows, a)
        out["approaches"][a] = entry
    ev, inv, fo, co = (out["approaches"][a] for a in APPROACHES)
    ev["fact_extraction"] = _fact_accuracy(rows)
    inv["history_anomaly"] = M.binary_metrics(
        [(bool(set(r["anomalies"]) & {"history_other_claimant", "repeat_claims"}),
          r["investigator"].get("history_anomaly_detected")) for r in rows])
    fo["per_type_recall"] = {
        t: {"cases": len(hits), "detected": sum(hits), "recall": M.ratio(sum(hits), len(hits))}
        for t in ANOMALY_TYPES
        for hits in [[r["forensic"]["type_hits"][t] for r in _done(rows, "forensic") if t in r["anomalies"]]]}
    co["anomaly_escalation"] = M.binary_metrics([(bool(r["anomalies"]), r["combined"].get("anomaly_detected"))
                                                 for r in rows])
    co["review_status"] = {k: sum(1 for r in rows if r["combined"].get("review_status") == k)
                           for k in ("COMPLETE", "PARTIAL", "NONE")}

    # ---- heatmap 1: common metrics (decision task) + completion + validity
    cols1 = [("accuracy", "Accuracy"), ("precision", "Precision (macro)"), ("recall", "Recall (macro)"),
             ("f1", "F1 (macro)"), ("completion", "Completion rate"), ("validity", "Structured output validity")]
    hm1 = []
    for a in APPROACHES:
        d, rel = out["approaches"][a]["decision"], out["approaches"][a]["reliability"]
        hm1.append({"row": TITLES[a], "values": {
            "accuracy": d["accuracy"], "precision": d["macro"]["precision"]["value"],
            "recall": d["macro"]["recall"]["value"], "f1": d["macro"]["f1"]["value"],
            "completion": rel["completion_rate"], "validity": rel["structured_output_validity"]}})
    out["heatmap_comparative"] = {
        "title": "Comparative performance on the shared task: claim decision (4 classes)",
        "note": "Single agents are scored on the decision they recommend (advisory); the combined system on its "
                "adjudicated final decision. Agent-specific task metrics are reported separately.",
        "columns": cols1, "rows": hm1}

    # ---- heatmap 2: scenario x approach, each approach on its own task metric
    hm2 = []
    for key, label in CATEGORIES.items():
        sub = [r for r in rows if r["category"] == key]
        cells = {}
        for a in APPROACHES:
            if a == "evidence":
                fa = _fact_accuracy(sub)
                cells[a] = {"value": fa["accuracy"], "n": len(_done(sub, a))}
            elif a == "combined":
                d = M.multiclass_metrics(_decision_pairs(sub, a), DECISIONS)
                cells[a] = {"value": d["accuracy"], "n": d["n"]}
            else:
                b = M.binary_metrics(_task_pairs(sub, a))
                cells[a] = {"value": b["accuracy"], "n": b["n"]}
        hm2.append({"row": label, "key": key, "cells": cells})
    out["heatmap_scenario"] = {"title": "Scenario performance (each approach on its own task metric)",
                               "columns": [(a, f"{TITLES[a]}: {SCENARIO_METRIC[a]}") for a in APPROACHES],
                               "rows": hm2}

    # ---- heatmap 3: reliability
    out["heatmap_reliability"] = {
        "title": "Execution reliability (a failure is not an incorrect prediction)",
        "columns": [("successful", "Successful"), ("failed", "Failed"), ("incomplete", "Incomplete"),
                    ("structured_output_failures", "Structured output failures"),
                    ("avg_latency_ms", "Average latency (ms)")],
        "rows": [{"row": TITLES[a], "values": out["approaches"][a]["reliability"]} for a in APPROACHES]}

    # ---- box plots (milliseconds; requests; tokens)
    def series(a: str, key: str) -> list[float | None]:
        return [r[a].get(key) for r in rows if r[a]["status"] in ("completed",) + FAILURE_STATES]
    out["boxplots"] = {
        "latency_all": {"title": "Execution latency: four approaches", "unit": "ms",
                        "series": {TITLES[a]: M.box_stats(series(a, "latency_ms")) for a in APPROACHES}},
        "latency_agents": {"title": "Individual agent latency", "unit": "ms",
                           "series": {TITLES[a]: M.box_stats(series(a, "latency_ms")) for a in APPROACHES[:3]}},
        "api_requests": {"title": "Gemini API requests per execution", "unit": "requests",
                         "series": {TITLES[a]: M.box_stats(series(a, "api_requests")) for a in APPROACHES}
                         if live else {},
                         "note": None if live else "Offline evaluation: the test doubles make no Gemini requests "
                                 "(0 per execution, confirmed by the request ledger), so there is no distribution "
                                 "to plot. Live observations are required."},
        "tokens": {"title": "Token usage per execution", "unit": "tokens",
                   "series": {TITLES[a]: M.box_stats(series(a, "tokens")) for a in APPROACHES[:3]}
                   if any(r[a].get("tokens") is not None for r in rows for a in APPROACHES[:3]) else {},
                   "note": None if any(r[a].get("tokens") is not None for r in rows for a in APPROACHES[:3])
                   else "No token usage was reported in this evaluation (offline test doubles report none), so no "
                        "token box plot is drawn."},
    }

    # ---- confusion matrices
    out["confusion"] = {
        "evidence": {"title": "Evidence Agent - missing-evidence detection",
                     **M.confusion_matrix(_task_pairs(rows, "evidence"), [True, False]),
                     "labels": ["Missing", "Complete"]},
        "investigator": {"title": "Investigator Agent - policy-violation detection",
                         **M.confusion_matrix(_task_pairs(rows, "investigator"), [True, False]),
                         "labels": ["Violation", "No violation"]},
        "forensic": {"title": "Forensic Agent - anomaly detection",
                     **M.confusion_matrix(_task_pairs(rows, "forensic"), [True, False]),
                     "labels": ["Anomaly", "No anomaly"]},
        "combined": {"title": "Combined system - final claim decision", **co["decision"]["confusion"]},
        "advisory": {a: {"title": f"{TITLES[a]} - recommended decision (advisory)",
                         **out["approaches"][a]["decision"]["confusion"]} for a in APPROACHES[:3]},
    }

    # ---- scenario-level analysis
    out["scenarios"] = []
    for key, label in CATEGORIES.items():
        sub = [r for r in rows if r["category"] == key]
        out["scenarios"].append({
            "key": key, "label": label, "cases": len(sub),
            "final_decision_accuracy": M.multiclass_metrics(_decision_pairs(sub, "combined"), DECISIONS)["accuracy"],
            "completion": {a: M.ratio(len(_done(sub, a)), len(sub)) for a in APPROACHES},
            "mean_latency_ms": {a: M.mean([r[a].get("latency_ms") for r in sub]) for a in APPROACHES},
            "wrong_final_decisions": [{"case_id": r["case_id"], "expected": r["expected_decision"],
                                       "got": r["combined"].get("final_decision")}
                                      for r in sub if r["combined"]["status"] == "completed"
                                      and r["combined"].get("final_decision") != r["expected_decision"]]})

    # ---- usage
    def total(key: str, approaches=APPROACHES[:3]) -> int | None:
        vals = [r[a].get(key) for r in rows for a in approaches]
        return None if any(v is None for v in vals) or not vals else sum(vals)
    mcp = [r["combined"].get("mcp_calls") for r in rows]
    out["usage"] = {
        "gemini_requests_total": total("api_requests"),
        "gemini_requests_source": "shared request ledger" if live else "offline (no requests; ledger unchanged)",
        "mcp_calls_total": None if any(v is None for v in mcp) else sum(mcp),
        "mcp_calls_mean_per_case": M.mean(mcp),
        "tokens_total": total("tokens"),
        "estimated_cost": None,
        "estimated_cost_note": "N/A: no verified per-token price is configured, so no cost is estimated.",
    }
    return out


# --------------------------------------------------------------------------- exports

CSV_FIELDS = ["case_id", "category", "source", "expected_decision", "policy_violation", "missing_evidence",
              "anomalies", "run_id", "run_error"]


def to_csv(rows: list[dict]) -> str:
    """One line per case: ground truth, then each approach's status, prediction, latency and usage."""
    extra = []
    for a in APPROACHES:
        extra += [f"{a}_{k}" for k in ("status", "recommendation", "latency_ms", "llm_calls", "api_requests", "tokens")]
    extra += ["evidence_facts_matched", "evidence_facts_expected", "evidence_missing_detected",
              "evidence_citations_valid", "evidence_citations_total", "investigator_policy_violation_detected",
              "investigator_history_anomaly_detected", "forensic_anomaly_detected", "forensic_detected_types",
              "combined_decided_by", "combined_review_status", "combined_mcp_calls"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_FIELDS + extra)
    for r in rows:
        line = [r["case_id"], r["category"], r["source"], r["expected_decision"], r["policy_violation"],
                ";".join(r["missing_evidence"]), ";".join(r["anomalies"]), r["run_id"], r["run_error"]]
        for a in APPROACHES:
            line += [r[a].get(k) for k in ("status", "recommendation", "latency_ms", "llm_calls", "api_requests",
                                           "tokens")]
        line += [r["evidence"].get("facts_matched"), r["evidence"].get("facts_expected"),
                 r["evidence"].get("missing_detected"), r["evidence"].get("citations_valid"),
                 r["evidence"].get("citations_total"), r["investigator"].get("policy_violation_detected"),
                 r["investigator"].get("history_anomaly_detected"), r["forensic"].get("anomaly_detected"),
                 ";".join(r["forensic"].get("detected_types") or []), r["combined"].get("decided_by"),
                 r["combined"].get("review_status"), r["combined"].get("mcp_calls")]
        w.writerow(["N/A" if v is None else v for v in line])
    return buf.getvalue()


def _f(v: Any, pct: bool = True) -> str:
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return f"{v * 100:.1f}%" if pct else f"{v:,.1f}"
    return str(v)


METHODOLOGY = """Ground truth is fixed per case by the scenario's design and stored with the dataset
(data/evaluation/eval_dataset_v1.json); it is never computed by ClaimSleuth's rules or agents:
- expected decision: the outcome a correct warranty process should reach;
- policy violation: the documents show the claim is outside the policy (expired or excluded);
- missing evidence: required documents or fields that are absent;
- anomalies: forensic signals planted in the case;
- expected facts: the values written into the documents (ground truth by construction).

Each case runs once through the integrated workflow (deterministic baseline, Google ADK Evidence + Investigator
agents, the CrewAI Forensic Agent over A2A, MCP tools and the deterministic adjudicator). Each agent is scored on its
own report, for its own task; the Investigator runs as deployed and receives the Evidence report through ADK session
state. A failed or incomplete execution is counted under reliability and excluded from accuracy metrics, never
scored as right or wrong. Metrics that cannot be computed are reported as N/A, never as 0.
"""

LIMITATIONS = """- The dataset is synthetic (40 cases); the results do not measure real-world fraud-detection performance.
- 18 cases reuse scenarios the system was developed against, so those are in-distribution; the 22 new cases were
  written by the same developers who wrote the deterministic rules. A high combined-system score therefore shows the
  rules work as designed on these scenario types, not that they generalise to unseen claims.
- Offline evaluation uses the labelled test doubles, deterministic rule-based stand-ins for the language model:
  offline numbers describe the pipeline with those stand-ins, not LLM reasoning.
- Several task predictions are derived from structured report fields with documented rules (for example, the
  Forensic Agent counts as detecting an anomaly when a serial or chronology check is INCONSISTENT, a history pattern
  is reported, or a MEDIUM/HIGH serial, date, history or suspicious-detail finding is present).
- When the ADK workflow does not finish, neither ADK report is released, so the Evidence Agent can be counted
  incomplete because of an Investigator failure.
- Cost is not estimated: no verified per-token price is configured.
"""


def to_markdown(result: dict[str, Any]) -> str:
    meta, agg = result["meta"], result["aggregate"]
    ap = agg["approaches"]
    L = [f"# ClaimSleuth agent evaluation - {meta['evaluation_id']}", "",
         f"- Dataset: {meta['dataset_version']} ({meta['cases_total']} synthetic cases, 8 per category)",
         f"- Evaluation date: {meta['started_at']} to {meta.get('finished_at') or 'not finished'}",
         f"- Execution mode: **{meta['mode'].upper()}**" + (" (labelled offline test doubles, not LLM reasoning)"
                                                           if meta["mode"] == "offline" else " (Gemini)"),
         f"- Cases executed: {meta['cases_executed']} of {meta['cases_requested']} requested"
         + (f"; stopped early: {meta['stopped_reason']}" if meta.get("stopped_reason") else ""),
         f"- Combined system: {ap['combined']['reliability']['successful']} successful, "
         f"{ap['combined']['reliability']['failed']} failed executions",
         f"- Gemini API requests: {_f(agg['usage']['gemini_requests_total'])} "
         f"({agg['usage']['gemini_requests_source']})", "",
         "## Ground-truth methodology", "", METHODOLOGY,
         "## Comparative performance (shared task: claim decision)", "",
         "| Approach | Accuracy | Precision (macro) | Recall (macro) | F1 (macro) | Completion | Output validity |",
         "|---|---|---|---|---|---|---|"]
    for row in agg["heatmap_comparative"]["rows"]:
        v = row["values"]
        L.append(f"| {row['row']} | " + " | ".join(_f(v[k]) for k in ("accuracy", "precision", "recall", "f1",
                                                                         "completion", "validity")) + " |")
    L += ["", agg["heatmap_comparative"]["note"], "", "## Agent-specific metrics", ""]
    for a in ("evidence", "investigator", "forensic"):
        t = ap[a]["task_metrics"]
        L.append(f"**{ap[a]['title']}** - {ap[a]['task']}: accuracy {_f(t['accuracy'])}, precision "
                 f"{_f(t['precision'])}, recall {_f(t['recall'])}, F1 {_f(t['f1'])}, false-positive rate "
                 f"{_f(t['false_positive_rate'])} (n={t['n']}); citation validity "
                 f"{_f(ap[a]['citation_validity']['rate'])} ({ap[a]['citation_validity']['valid']}/"
                 f"{ap[a]['citation_validity']['total']}).")
        L.append("")
    fe = ap["evidence"]["fact_extraction"]
    L += [f"Evidence fact-extraction accuracy: {_f(fe['accuracy'])} ({fe['matched']}/{fe['expected']} facts).", "",
          f"Investigator history-anomaly detection: recall {_f(ap['investigator']['history_anomaly']['recall'])}, "
          f"precision {_f(ap['investigator']['history_anomaly']['precision'])}.", "",
          "Forensic per-anomaly recall: " + "; ".join(
              f"{k} {_f(v['recall'])} ({v['detected']}/{v['cases']})" for k, v in ap["forensic"]["per_type_recall"].items()),
          "", "Combined final decision: accuracy {acc}, macro F1 {mf1}, weighted F1 {wf1}; anomaly escalation recall "
          "{rec}, false-positive rate {fpr}.".format(
              acc=_f(ap["combined"]["decision"]["accuracy"]), mf1=_f(ap["combined"]["decision"]["macro"]["f1"]["value"]),
              wf1=_f(ap["combined"]["decision"]["weighted"]["f1"]),
              rec=_f(ap["combined"]["anomaly_escalation"]["recall"]),
              fpr=_f(ap["combined"]["anomaly_escalation"]["false_positive_rate"])), "",
          "## Scenario performance", "", "| Scenario | " + " | ".join(c[1] for c in agg["heatmap_scenario"]["columns"])
          + " |", "|---|" + "---|" * len(agg["heatmap_scenario"]["columns"])]
    for row in agg["heatmap_scenario"]["rows"]:
        L.append(f"| {row['row']} | " + " | ".join(f"{_f(row['cells'][a]['value'])} (n={row['cells'][a]['n']})"
                                                   for a, _ in agg["heatmap_scenario"]["columns"]) + " |")
    L += ["", "## Execution reliability", "", "| Approach | Successful | Failed | Incomplete | Output failures | "
          "Avg latency (ms) |", "|---|---|---|---|---|---|"]
    for row in agg["heatmap_reliability"]["rows"]:
        v = row["values"]
        L.append(f"| {row['row']} | {v['successful']} | {v['failed']} | {v['incomplete']} | "
                 f"{v['structured_output_failures']} | {_f(v['avg_latency_ms'], pct=False)} |")
    L += ["", "## Latency distribution (ms)", "", "| Approach | n | Q1 | Median | Q3 | Whiskers | Outliers |",
          "|---|---|---|---|---|---|---|"]
    for name, s in agg["boxplots"]["latency_all"]["series"].items():
        if s["sufficient"]:
            L.append(f"| {name} | {s['n']} | {s['q1']:.0f} | {s['median']:.0f} | {s['q3']:.0f} | "
                     f"{s['whisker_low']:.0f}-{s['whisker_high']:.0f} | {len(s['outliers'])} |")
        else:
            L.append(f"| {name} | {s['n']} | {s['note']} | | | | |")
    for key in ("api_requests", "tokens"):
        note = agg["boxplots"][key].get("note")
        if note:
            L += ["", f"{agg['boxplots'][key]['title']}: {note}"]
    cm = agg["confusion"]["combined"]
    L += ["", "## Confusion matrix - combined final decision (rows: expected, columns: predicted)", "",
          "| | " + " | ".join(cm["labels"]) + " |", "|---|" + "---|" * len(cm["labels"])]
    for label, line in zip(cm["labels"], cm["matrix"]):
        L.append(f"| {label} | " + " | ".join(str(x) for x in line) + " |")
    u = agg["usage"]
    L += ["", "## Usage", "", f"- Gemini API requests: {_f(u['gemini_requests_total'])} ({u['gemini_requests_source']})",
          f"- MCP tool calls: {_f(u['mcp_calls_total'])} (mean per case {_f(u['mcp_calls_mean_per_case'], pct=False)})",
          f"- Tokens: {_f(u['tokens_total'])}", f"- Estimated cost: {u['estimated_cost_note']}", "",
          "## Limitations", "", LIMITATIONS]
    return "\n".join(L) + "\n"


def to_json(result: dict[str, Any]) -> str:
    return json.dumps({"meta": result["meta"], "aggregate": result["aggregate"]}, indent=2, default=str) + "\n"
