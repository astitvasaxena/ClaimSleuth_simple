r"""Course experiments E3 (architecture), E4 (messaging style) and E5 (persona x prompt structure), OFFLINE only.

    .venv/bin/python -m claimsleuth.evaluation.experiments run [--out DIR] [--limit N] [--repeats N]

Every run goes through the real integrated workflow (InvestigationManager, ADK Runner, MCP server, CrewAI A2A service,
deterministic adjudicator) with the labelled offline test doubles. Nothing here changes the product: a variant is a set
of temporary, in-process substitutions (patched for the duration of one run and then restored) that change how the
SAME components are wired:

  config           topology                                          context between agents
  hier_blackboard  hierarchical manager: Evidence first, then the    signed blackboard (ContextRef), the
                   Investigator and the Forensic Agent in parallel,  Investigator and Forensic Agent each
                   gated and monitored (the product default)         consume the Evidence report from it
  seq_direct       strictly sequential: Evidence -> Investigator ->  Evidence report handed on through ADK
                   Forensic Agent, one after the other               session state; Forensic gets none
  par_direct       parallel-only: Evidence, Investigator and         none: no agent sees another's output
                   Forensic Agent all start together                 (the "no sharing" arm)
  p2p_direct       Evidence -> Investigator in one ADK sequence,     direct message passing: ADK session state
                   Forensic Agent in parallel (the old direct mode)  Evidence -> Investigator, no store

E3 compares hier_blackboard, seq_direct and par_direct; E4 compares hier_blackboard (blackboard), p2p_direct (direct
message passing) and par_direct (no sharing). Each config is run once per case and both views read those runs.

LIMIT (stated in every output): the offline agents are rule-based and read the claim packet, not the other agents'
reports, so the final decision cannot depend on the topology or on the context shared. Decision accuracy is therefore
identical by construction when everything works; what differs, and is measured, is wall time, agent/tool/message
counts and the context actually passed. E5 prompt structures change the real ADK packet (document order) and the
instruction text; the doubles ignore both, so any difference in decisions would be a defect, and latency or packet size
is the only variance reported. The CrewAI agent runs in another process and always gets the standard packet.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claimsleuth import config, execution_mode
from claimsleuth.evaluation import dataset as ds
from claimsleuth.evaluation.runner import EVAL_ROOT, EvalServices
from claimsleuth.schemas import ClaimRunBundle, utc_now

ARCH_CONFIGS = {"hier_blackboard": "hierarchical manager + blackboard", "seq_direct": "sequential-only (direct)",
                "par_direct": "parallel-only (no sharing)", "p2p_direct": "direct message passing"}
E3_CONFIGS = ("seq_direct", "par_direct", "hier_blackboard")
E4_CONFIGS = ("hier_blackboard", "p2p_direct", "par_direct")
E4_LABELS = {"hier_blackboard": "blackboard", "p2p_direct": "direct message passing", "par_direct": "no sharing"}
STRUCTURES = ("standard", "evidence_first_react")

REACT_SUFFIX = """
Work in a ReAct loop and write your reasoning as you go: Thought (what you still need to know), Action (one tool call or
one document line to read), Observation (what came back). Evidence first: cite the document line for every fact before
you draw any conclusion, and only then state the decision.
"""


# ------------------------------------------------------------------------------------------------ subjects
@dataclass(frozen=True)
class Subject:
    case_id: str
    source: str                 # "eval40" (dataset v1) or "samples" (data/claims)
    persona: str
    category: str
    expected: str


def persona_of(case: ds.EvalCase) -> str:
    suffix = case.source.rsplit("-", 1)[-1] if case.source.startswith("existing:") else ""
    if suffix in ("VAG", "ABU", "INJ"):
        return {"VAG": "vague", "ABU": "abusive", "INJ": "injection"}[suffix]
    return "injection" if {"prompt_injection", "data_access_request"} & set(case.ground_truth.anomalies) else "honest"


def eval_subjects(limit: int | None = None) -> list[Subject]:
    cases = ds.load().cases[:limit]
    return [Subject(c.case_id, "eval40", persona_of(c), c.ground_truth.category, c.ground_truth.expected_decision.value)
            for c in cases]


def sample_subjects() -> list[Subject]:
    truth = json.loads((config.DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    return [Subject(k, "samples", v["persona"], v["case_type"], v["expected_decision"]) for k, v in truth.items()]


# ------------------------------------------------------------------------------------------------ variants
@contextlib.contextmanager
def _patched(target: Any, name: str, value: Any):
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


def _parallel_agents(model, mcp_url, recorder):
    """The two ADK agents side by side in a ParallelAgent: the Investigator's instruction has no Evidence report."""
    from google.adk.agents import ParallelAgent
    from claimsleuth.adk_service import agents as A
    from claimsleuth.schemas import EvidenceReport, InvestigationReport
    models = model if isinstance(model, dict) else {A.EVIDENCE_AGENT: model, A.INVESTIGATOR_AGENT: model}
    evidence = A._agent(A.EVIDENCE_AGENT, "Extracts document facts.", A.EVIDENCE_INSTRUCTION, models[A.EVIDENCE_AGENT],
                        EvidenceReport, "evidence_report", A.mcp_toolset(mcp_url, ["warranty_policy_search"]), recorder)
    instruction = A.INVESTIGATOR_INSTRUCTION.replace(
        "{evidence_report?}", "(none: the agents run in parallel and no other agent's report is shared)")
    investigator = A._agent(A.INVESTIGATOR_AGENT, "Investigates policy and history.", instruction,
                            models[A.INVESTIGATOR_AGENT], InvestigationReport, "investigation_report",
                            A.mcp_toolset(mcp_url, ["warranty_policy_search", "claim_history_lookup"]), recorder)
    return ParallelAgent(name="claimsleuth_adk_parallel", sub_agents=[evidence, investigator],
                         description="Evidence Agent and Investigator Agent side by side.")


def _evidence_first_message(claim, documents, run_id: str, sink: dict) -> str:
    """The same packet with the documents first and the claim form last (evidence first)."""
    from claimsleuth.claim_text import claim_message
    standard = claim_message(claim, documents, run_id)
    blocks = standard.split("\n\n")
    head, form, docs = blocks[:2], blocks[2:3], blocks[3:]
    # the claim form is one block (json with indent has no blank line); documents follow it
    text = "\n\n".join(head + docs + form)
    sink["packet_chars"], sink["standard_packet_chars"] = len(text), len(standard)
    return text


@contextlib.contextmanager
def variant(config_name: str, structure: str, sink: dict, adk_done: asyncio.Event | None = None):
    """Apply a config and a prompt structure for one run, then restore everything."""
    from claimsleuth.adk_service import agents as A
    from claimsleuth.adk_service import runner as R
    from claimsleuth.orchestration import manager as M
    with contextlib.ExitStack() as stack:
        if config_name == "par_direct":
            stack.enter_context(_patched(R, "build_agents", _parallel_agents))
        if config_name == "seq_direct":
            original_adk, original_req = M.run_adk_investigation_async, M.request_forensics

            async def adk_then_signal(*a, **kw):
                try:
                    return await original_adk(*a, **kw)
                finally:
                    adk_done.set()

            async def forensics_after_adk(*a, **kw):
                await adk_done.wait()           # strictly sequential: the Forensic Agent starts when ADK has finished
                return await original_req(*a, **kw)
            stack.enter_context(_patched(M, "run_adk_investigation_async", adk_then_signal))
            stack.enter_context(_patched(M, "request_forensics", forensics_after_adk))
        if structure == "evidence_first_react":
            stack.enter_context(_patched(R, "claim_message",
                                         lambda claim, docs, run_id: _evidence_first_message(claim, docs, run_id, sink)))
            stack.enter_context(_patched(A, "EVIDENCE_INSTRUCTION", A.EVIDENCE_INSTRUCTION + REACT_SUFFIX))
            stack.enter_context(_patched(A, "INVESTIGATOR_INSTRUCTION", A.INVESTIGATOR_INSTRUCTION + REACT_SUFFIX))
            sink["instruction_chars"] = len(A.EVIDENCE_INSTRUCTION) + len(A.INVESTIGATOR_INSTRUCTION)
        else:
            sink["packet_chars"] = sink["standard_packet_chars"] = None
            sink["instruction_chars"] = len(A.EVIDENCE_INSTRUCTION) + len(A.INVESTIGATOR_INSTRUCTION)
        yield


# ------------------------------------------------------------------------------------------------ one run
def _ms(a, b) -> float | None:
    return round((b - a).total_seconds() * 1000, 3) if a and b else None


def row_from_bundle(bundle: ClaimRunBundle | None, subject: Subject, config_name: str, structure: str, wall_ms: float,
                    error: str | None, sink: dict, repeat: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "config": config_name, "structure": structure, "repeat": repeat, "case_id": subject.case_id,
        "source": subject.source, "persona": subject.persona, "category": subject.category,
        "expected_decision": subject.expected, "run_error": error, "wall_ms": wall_ms}
    adk, crew = (bundle.adk, bundle.crewai) if bundle else (None, None)
    runs = {r.agent: r for r in (adk.agent_runs if adk else [])}
    crew_run = crew.agent_runs[0] if crew and crew.agent_runs else None
    final = bundle.final_report if bundle else None
    consumed = {c.consumer: c for c in (adk.consumed_context if adk else []) + (crew.consumed_context if crew else [])}
    evidence_chars = len(adk.evidence_report.model_dump_json()) if adk and adk.evidence_report else 0
    inv_saw = "investigator_agent" in consumed or (config_name in ("seq_direct", "p2p_direct")
                                                   and adk is not None and adk.evidence_report is not None)
    forensic_saw = "forensic_agent" in consumed
    adk_span = (adk.started_at, adk.finished_at) if adk else (None, None)
    crew_span = (crew.started_at, crew.finished_at) if crew else (None, None)
    overlap = None
    if all(adk_span) and all(crew_span):
        overlap = max(0.0, (min(adk_span[1], crew_span[1]) - max(adk_span[0], crew_span[0])).total_seconds() * 1000)
    row.update({
        "final_decision": final.final_decision.value if final else None,
        "correct": bool(final and final.final_decision.value == subject.expected),
        "decided_by": final.decided_by if final else None,
        "requires_human_approval": final.requires_human_approval if final else None,
        "evidence_ms": runs["evidence_agent"].latency_ms if "evidence_agent" in runs else None,
        "investigator_ms": runs["investigator_agent"].latency_ms if "investigator_agent" in runs else None,
        "forensic_ms": (crew_run.latency_ms if crew_run else None),
        "adk_ms": adk.execution_ms if adk else None, "crew_ms": crew.execution_ms if crew else None,
        "agents_run": len(runs) + (1 if crew_run else 0),
        "llm_calls": sum(r.llm_calls for r in runs.values()) + (crew_run.llm_calls if crew_run else 0),
        "agent_tool_calls": sum(r.tool_calls for r in runs.values()) + (crew_run.tool_calls if crew_run else 0),
        "mcp_calls_logged": len(bundle.mcp.calls) if bundle and bundle.mcp else None,
        "a2a_messages": len(bundle.messages) if bundle else None,
        "blackboard_reads": len(consumed), "investigator_saw_evidence": inv_saw, "forensic_saw_evidence": forensic_saw,
        "context_chars_shared": evidence_chars * (int(inv_saw) + int(forensic_saw)),
        "adk_crew_overlap_ms": None if overlap is None else round(overlap, 3),
        "context_sharing": bundle.context_sharing if bundle else None,
        "packet_chars": sink.get("packet_chars"), "standard_packet_chars": sink.get("standard_packet_chars"),
        "instruction_chars": sink.get("instruction_chars"),
        "model": adk.model if adk else None})
    return row


async def run_one(subject: Subject, config_name: str, structure: str, services: EvalServices, data_dir: Path,
                  repeat: int, tag: str) -> dict[str, Any]:
    from claimsleuth.integration.run import run_claim_async
    sink: dict[str, Any] = {}
    bb = services.blackboard if config_name == "hier_blackboard" else None
    run_id = f"RUN-{tag}-{config_name}-{structure[:3]}-{subject.case_id}-r{repeat}"
    t0, bundle, error = time.perf_counter(), None, None
    with variant(config_name, structure, sink, asyncio.Event()):
        try:
            bundle = await run_claim_async(subject.case_id, run_id=run_id, mcp_url=services.mcp_url,
                                           crew_card_url=services.crew_card, data_dir=data_dir,
                                           db_path=data_dir / "claimsleuth.db", blackboard_path=bb)
        except Exception as exc:          # a failed execution is recorded, never counted as a prediction
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
    wall = round((time.perf_counter() - t0) * 1000, 3)
    return row_from_bundle(bundle, subject, config_name, structure, wall, error, sink, repeat)


def _guard_offline() -> None:
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "CLAIMSLEUTH_BLACKBOARD_DB"):
        os.environ.pop(name, None)
    os.environ[execution_mode.ENV_VAR] = execution_mode.OFFLINE       # never live; also inherited by the services
    if execution_mode.execution_mode() != execution_mode.OFFLINE:
        raise SystemExit("experiments are offline only")


async def run_all(out: Path, limit: int | None, repeats: int, mcp_port: int, crew_port: int,
                  progress=print) -> dict[str, Any]:
    _guard_offline()
    os.environ.setdefault("CLAIMSLEUTH_GEMINI_LEDGER", str(out / "scratch" / "ledger.sqlite3"))
    (out / "scratch").mkdir(parents=True, exist_ok=True)
    dataset = ds.load()
    work = EVAL_ROOT / f"EXP-{utc_now().strftime('%Y%m%d-%H%M%S')}"
    work.mkdir(parents=True)
    tag = work.name[4:]
    rows: list[dict[str, Any]] = []
    started = utc_now()

    async def phase(subjects: list[Subject], data_dir: Path, plan: list[tuple[str, str]], name: str) -> None:
        services = EvalServices(data_dir, work / name, execution_mode.OFFLINE, mcp_port, crew_port,
                                blackboard=work / name / "blackboard.sqlite3")
        await asyncio.to_thread(services.start)
        try:
            with execution_mode.forced(execution_mode.OFFLINE):
                total = len(subjects) * len(plan) * repeats
                for rep in range(repeats):
                    for subject in subjects:
                        for cfg, structure in plan:
                            row = await run_one(subject, cfg, structure, services, data_dir, rep, f"{tag}-{name}")
                            row["phase"] = name
                            rows.append(row)
                            progress(f"[{name} {len(rows)}] {cfg}/{structure} {subject.case_id} -> "
                                     f"{row['final_decision']} ({row['wall_ms']:.0f} ms) of {total}")
        finally:
            await asyncio.to_thread(services.stop)

    arch_plan = [(c, "standard") for c in ARCH_CONFIGS]
    react_plan = [("hier_blackboard", "evidence_first_react")]
    eval_dir = ds.materialize(dataset, work / f"data-{dataset.version}")
    # E3/E4: 40 cases x 4 configs (standard structure); E5: the 40 cases again under the ReAct / evidence-first structure
    await phase(eval_subjects(limit), eval_dir, arch_plan + react_plan, "eval40")
    # E5 personas: the 18 sample claims (honest, vague, abusive, injection) x both structures, default product wiring
    await phase(sample_subjects()[:limit], config.DATA_DIR,
                [("hier_blackboard", "standard"), ("hier_blackboard", "evidence_first_react")], "samples")
    return {"rows": rows, "started_at": started.isoformat(), "finished_at": utc_now().isoformat(), "work": str(work)}


# ------------------------------------------------------------------------------------------------ aggregation
def _stats(values: list[float]) -> dict[str, Any]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"n": 0}

    def q(p: float) -> float:
        k = (len(vals) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
        return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)
    return {"n": len(vals), "mean": round(statistics.fmean(vals), 1), "median": round(q(0.5), 1),
            "p25": round(q(0.25), 1), "p75": round(q(0.75), 1), "p95": round(q(0.95), 1), "min": round(vals[0], 1),
            "max": round(vals[-1], 1), "stdev": round(statistics.stdev(vals), 1) if len(vals) > 1 else 0.0}


def _group(rows: list[dict], key) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(key(r), []).append(r)
    return out


def _summ(rows: list[dict]) -> dict[str, Any]:
    n = len(rows)
    ok = [r for r in rows if not r["run_error"]]
    decisions: dict[str, int] = {}
    for r in rows:
        decisions[r["final_decision"] or "none"] = decisions.get(r["final_decision"] or "none", 0) + 1
    avg = lambda k: round(statistics.fmean([r[k] for r in ok if r[k] is not None]), 2) if ok else None  # noqa: E731
    return {"runs": n, "failed_runs": n - len(ok), "correct": sum(r["correct"] for r in rows),
            "accuracy": round(sum(r["correct"] for r in rows) / n, 4) if n else None, "decisions": decisions,
            "wall_ms": _stats([r["wall_ms"] for r in rows]), "evidence_ms": _stats([r["evidence_ms"] for r in rows]),
            "investigator_ms": _stats([r["investigator_ms"] for r in rows]),
            "forensic_ms": _stats([r["forensic_ms"] for r in rows]),
            "mean_agents_run": avg("agents_run"), "mean_llm_calls": avg("llm_calls"),
            "mean_agent_tool_calls": avg("agent_tool_calls"), "mean_mcp_calls_logged": avg("mcp_calls_logged"),
            "mean_a2a_messages": avg("a2a_messages"), "mean_blackboard_reads": avg("blackboard_reads"),
            "mean_context_chars_shared": avg("context_chars_shared"), "mean_adk_crew_overlap_ms": avg("adk_crew_overlap_ms"),
            "investigator_saw_evidence_runs": sum(bool(r["investigator_saw_evidence"]) for r in rows),
            "forensic_saw_evidence_runs": sum(bool(r["forensic_saw_evidence"]) for r in rows)}


def write_csv(path: Path, rows: list[dict]) -> None:
    keys = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def views(rows: list[dict]) -> dict[str, Any]:
    e40 = [r for r in rows if r["phase"] == "eval40"]
    e3 = [r for r in e40 if r["structure"] == "standard" and r["config"] in E3_CONFIGS]
    e4 = [dict(r, messaging=E4_LABELS[r["config"]]) for r in e40
          if r["structure"] == "standard" and r["config"] in E4_CONFIGS]
    e5 = [r for r in rows if r["config"] == "hier_blackboard"]
    return {"e3": e3, "e4": e4, "e5": e5}


def summarise(result: dict[str, Any]) -> dict[str, Any]:
    rows = result["rows"]
    v = views(rows)
    e5 = v["e5"]
    by_persona = {p: {s: _summ([r for r in rs if r["structure"] == s]) for s in STRUCTURES}
                  for p, rs in _group(e5, lambda r: r["persona"]).items()}
    pairs = _group(e5, lambda r: (r["phase"], r["case_id"], r["repeat"]))
    flips = sum(1 for g in pairs.values() if len({r["final_decision"] for r in g}) > 1)
    return {
        "mode": "offline (labelled rule-based test doubles; not LLM reasoning)", "synthetic": True,
        "started_at": result["started_at"], "finished_at": result["finished_at"],
        "total_runs": len(rows), "failed_runs": sum(bool(r["run_error"]) for r in rows),
        "e3_architecture": {cfg: _summ([r for r in v["e3"] if r["config"] == cfg]) | {"label": ARCH_CONFIGS[cfg]}
                            for cfg in E3_CONFIGS},
        "e4_messaging": {E4_LABELS[cfg]: _summ([r for r in v["e4"] if r["config"] == cfg]) | {"config": cfg}
                         for cfg in E4_CONFIGS},
        "e5_persona_structure": {
            "runs": len(e5), "personas": sorted(by_persona), "structures": list(STRUCTURES),
            "by_persona_and_structure": by_persona,
            "by_structure": {s: _summ([r for r in e5 if r["structure"] == s]) for s in STRUCTURES},
            "decision_changed_by_structure": flips, "paired_comparisons": len(pairs),
            "mean_packet_chars_standard": round(statistics.fmean([r["standard_packet_chars"] for r in e5
                                                                  if r["standard_packet_chars"]]), 1) if e5 else None,
            "note": "the doubles do not read prompt wording; only the document order (real packet change) and "
                    "instruction text (ignored by the doubles) differ. No behavioural variance is claimed."},
        "by_persona_all_runs": {p: _summ(rs) for p, rs in _group(rows, lambda r: r["persona"]).items()},
        "by_agent_latency_ms": {a: _stats([r[a] for r in rows]) for a in
                                ("evidence_ms", "investigator_ms", "forensic_ms", "wall_ms")},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClaimSleuth E3/E4/E5 experiments (offline only)")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--limit", type=int, default=None, help="cases per phase (smoke test)")
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--mcp-port", type=int, default=8142)
    r.add_argument("--crew-port", type=int, default=8143)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    args.out.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run_all(args.out, args.limit, args.repeats, args.mcp_port, args.crew_port,
                                 progress=lambda s: print(s, flush=True)))
    v = views(result["rows"])
    write_csv(args.out / "experiment_runs.csv", result["rows"])
    write_csv(args.out / "architecture_comparison_runs.csv", v["e3"])
    write_csv(args.out / "messaging_comparison_runs.csv", v["e4"])
    write_csv(args.out / "persona_prompt_runs.csv", v["e5"])
    summary = summarise(result)
    (args.out / "experiments_summary.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    print(f"{summary['total_runs']} runs, {summary['failed_runs']} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
