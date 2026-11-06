"""Measured metrics of a run, and agent metrics across runs. Everything comes from recorded timestamps, latencies and
counts; a value that was not recorded stays None. Token counts come only from provider-reported usage."""
from __future__ import annotations

import statistics
from typing import Any, Iterable

from claimsleuth.observability import pricing
from claimsleuth.observability.trace_util import aware, ms_between, overlap_ms
from claimsleuth.schemas import (ClaimRunBundle, DurationStats, ModelUsage, RunMetrics, RunTrace, TaskMetrics,
                                 TaskStatus)

OFFLINE = pricing.OFFLINE_MODELS


def stats(values: Iterable[float | None]) -> DurationStats | None:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return DurationStats(count=len(vals), min_ms=round(min(vals), 3), max_ms=round(max(vals), 3),
                         mean_ms=round(statistics.fmean(vals), 3), median_ms=round(statistics.median(vals), 3))


def trace_entries(result, key: str) -> list[dict[str, Any]]:
    """Records an agent system put in its trace under `key` (model_calls, crew_events, blackboard_ops ...)."""
    if result is None:
        return []
    return [item for t in result.trace if isinstance(t.get(key), list) for item in t[key]]


def _sum(values: list[int | None]) -> int | None:
    reported = [v for v in values if v is not None]
    return sum(reported) if reported else None


def _model_requests(bundle: ClaimRunBundle) -> dict[str, list[dict[str, Any]]]:
    """Each agent's recorded model requests."""
    calls: dict[str, list[dict[str, Any]]] = {}
    for result, default_agent in ((bundle.adk, None), (bundle.crewai, "forensic_agent")):
        for call in trace_entries(result, "model_calls"):
            calls.setdefault(call.get("agent") or default_agent or "unknown", []).append(call)
    return calls


def model_usage(bundle: ClaimRunBundle) -> list[ModelUsage]:
    table, note = pricing.load_pricing()
    out: list[ModelUsage] = []
    groups: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for result, default_agent in ((bundle.adk, None), (bundle.crewai, "forensic_agent")):
        if result is None:
            continue
        for call in trace_entries(result, "model_calls"):
            groups.setdefault((call.get("agent") or default_agent, result.model), []).append(call)
    for (agent, system_model), calls in groups.items():
        model = next((c.get("model") for c in calls if c.get("model")), None) or system_model
        offline = system_model in OFFLINE or model in OFFLINE
        reported = [c for c in calls if c.get("usage_reported") or c.get("prompt_tokens") is not None
                    or c.get("completion_tokens") is not None]
        prompt, completion = _sum([c.get("prompt_tokens") for c in reported]), _sum([c.get("completion_tokens") for c in reported])
        cached = _sum([c.get("cached_tokens") for c in reported])
        total = _sum([c.get("total_tokens") for c in reported])
        if total is None and prompt is not None and completion is not None:
            total = prompt + completion
        amount, currency, cost_note = pricing.cost("test-double" if offline else model, prompt, completion, cached,
                                                   table, note)
        out.append(ModelUsage(agent=agent or "unknown", model=model, execution="offline test double" if offline else
                              ("live" if model else "unknown"), calls=len(calls),
                              failed_calls=sum(1 for c in calls if c.get("status") == "error"),
                              usage_reported_calls=len(reported), prompt_tokens=prompt, completion_tokens=completion,
                              total_tokens=total, cached_tokens=cached,
                              request_ms=stats(c.get("latency_ms") for c in calls), cost=amount, currency=currency,
                              cost_note=cost_note))
    return out


def run_metrics(bundle: ClaimRunBundle, operations: list[dict[str, Any]]) -> RunMetrics:
    plan = bundle.execution_plan
    models = {m for m in (bundle.adk.model if bundle.adk else None, bundle.crewai.model if bundle.crewai else None) if m}
    execution = ("offline test double" if models and models <= OFFLINE else "live gemini" if models and not models & OFFLINE
                 else "mixed" if models else "unknown")
    runs = {r.agent: r for res in (bundle.adk, bundle.crewai) if res for r in res.agent_runs}
    interval = lambda r: (aware(r.started_at), aware(r.finished_at)) if r and r.started_at and r.finished_at else None
    ev, inv, fo = (runs.get(a) for a in ("evidence_agent", "investigator_agent", "forensic_agent"))
    tasks: list[TaskMetrics] = []
    lifecycle = {}
    for t in plan.tasks if plan else []:
        lifecycle[t.task_id] = ms_between(t.started_at, t.finished_at) if t.started_at and t.finished_at else None
    executions = {
        "baseline": bundle.baseline.execution_ms if bundle.baseline else None,
        "evidence": ev.latency_ms if ev else None, "investigation": inv.latency_ms if inv else None,
        "forensics": fo.latency_ms if fo else None, "adjudication": lifecycle.get("adjudication")}
    requests = _model_requests(bundle)
    failures = {f.agent: f for res in (bundle.adk, bundle.crewai) if res for f in reversed(res.provider_failures)}
    for t in plan.tasks if plan else []:
        calls = requests.get(t.agent, [])
        times = [c["latency_ms"] for c in calls if c.get("latency_ms") is not None]
        measured = executions.get(t.task_id)
        tasks.append(TaskMetrics(
            task_id=t.task_id, agent=t.agent, status=t.status, lifecycle_ms=lifecycle[t.task_id], execution_ms=measured,
            model_request_ms=round(sum(times), 3) if times else None, model_calls=len(calls),
            duration_source="agent record" if measured is not None else
            "manager lifecycle" if lifecycle[t.task_id] is not None else None,
            error_code=failures[t.agent].error_code if t.agent in failures else None))
    started = [t.started_at for t in plan.tasks if t.started_at] if plan else []
    finished = [t.finished_at for t in plan.tasks if t.finished_at] if plan else []
    agent_intervals = [i for i in (interval(ev), interval(inv), interval(fo)) if i]
    agent_exec = [x for x in (executions["evidence"], executions["investigation"], executions["forensics"]) if x is not None]
    a2a = []
    request = next((m for m in bundle.messages if m.message_type == "FORENSIC_REQUEST"), None)
    response = next((m for m in bundle.messages if m.message_type != "FORENSIC_REQUEST"), None)
    if request is not None and response is not None:
        a2a.append(ms_between(request.timestamp, response.timestamp))
    ops = list(operations) + trace_entries(bundle.adk, "blackboard_ops") + trace_entries(bundle.crewai, "blackboard_ops")
    op_stats = lambda kind: stats(o["latency_ms"] for o in ops if o.get("op") == kind and o.get("ok"))
    return RunMetrics(
        execution=execution,
        total_ms=ms_between(min(started), max(finished)) if started and finished else None,
        baseline_ms=executions["baseline"], evidence_ms=executions["evidence"], investigator_ms=executions["investigation"],
        forensic_ms=executions["forensics"], adjudication_ms=executions["adjudication"],
        agents_sequential_sum_ms=round(sum(agent_exec), 3) if agent_exec else None,
        agents_elapsed_ms=ms_between(min(i[0] for i in agent_intervals), max(i[1] for i in agent_intervals))
        if agent_intervals else None,
        parallel_overlap_ms=overlap_ms(interval(inv), interval(fo)) if interval(inv) and interval(fo) else None,
        tasks=tasks, mcp_calls=len(bundle.mcp.calls) if bundle.mcp else 0,
        mcp_latency=stats(c.latency_ms for c in bundle.mcp.calls) if bundle.mcp else None,
        a2a_requests=1 if request is not None else 0, a2a_latency=stats(a2a),
        blackboard_publish=op_stats("publish"), blackboard_consume=op_stats("consume"),
        blackboard_verify=op_stats("verify"), blackboard_seal=op_stats("seal"), models=model_usage(bundle))


def agent_metrics(traces: Iterable[RunTrace]) -> list[dict[str, Any]]:
    """Per task/agent across runs: executions, successes, failures, timeouts and execution-time statistics."""
    rows: dict[str, dict[str, Any]] = {}
    for trace in traces:
        for t in trace.metrics.tasks if trace.metrics else []:
            row = rows.setdefault(t.task_id, {"task_id": t.task_id, "agent": t.agent, "executions": 0, "successes": 0,
                                              "failures": 0, "timeouts": 0, "blocked": 0, "_ms": []})
            if t.status == TaskStatus.BLOCKED:
                row["blocked"] += 1
                continue
            row["executions"] += 1
            row["successes"] += t.status == TaskStatus.COMPLETED
            row["failures"] += t.status == TaskStatus.FAILED
            row["timeouts"] += t.status == TaskStatus.TIMED_OUT
            if t.execution_ms is not None:
                row["_ms"].append(t.execution_ms)
    out = []
    for row in rows.values():
        durations = stats(row.pop("_ms"))
        out.append({**row, "duration": durations.model_dump() if durations else None})
    return out

