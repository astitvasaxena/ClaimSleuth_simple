"""Turn one stored ClaimRunBundle into what the dashboard pages show.

Read-only: every value comes from the bundle (the outputs exactly as each workflow produced them), the claim's
own documents, or the services' logs. Nothing here decides, scores or rewrites a result; the only computed
additions are display helpers (time offsets, whether a quoted citation appears on the cited line, and which
MCP server log entry corresponds to which agent-side tool record).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from claimsleuth import config
from claimsleuth.dashboard.recovery import final_review
from claimsleuth.claim_text import claim_message
from claimsleuth.documents import quote_appears
from claimsleuth.observability.outcome import incomplete_text, outcome_of, records_missing, records_only, records_unconfirmed
from claimsleuth.observability.provider_errors import AGENT_TASK, redact
from claimsleuth.schemas import (MCP_LOG_EMPTY, MCP_RECORDS_UNAVAILABLE, AgentSystemResult, ClaimInput, ClaimRunBundle, EvidenceDocument, ExecutionStatus,
                                 RunOutcome)
from claimsleuth.security import DATA_ACCESS_PATTERNS, detect_injection, restricted_output, screen_text

AGENTS = {
    "evidence_agent": {"title": "Evidence Agent", "framework": "Google ADK", "class": "google.adk.agents.LlmAgent",
                       "responsibility": "Extracts facts from the claim documents with line-level citations, "
                                         "identifies relevant evidence and reports missing information.",
                       "tools": ["warranty_policy_search"]},
    "investigator_agent": {"title": "Investigator Agent", "framework": "Google ADK",
                           "class": "google.adk.agents.LlmAgent",
                           "responsibility": "Analyses the evidence, searches the policy and the claim history "
                                             "through MCP, identifies inconsistencies and recommends a decision.",
                           "tools": ["warranty_policy_search", "claim_history_lookup"]},
    "forensic_agent": {"title": "Forensic Agent", "framework": "CrewAI", "class": "crewai.Agent + crewai.Task + crewai.Crew",
                       "responsibility": "Independently checks serial numbers, chronology and claim-history patterns, "
                                         "and lists contradictions and missing evidence.",
                       "tools": ["claim_history_lookup"]},
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def mark_citations(node: Any, docs: dict[str, EvidenceDocument]) -> Any:
    """Copy of a JSON tree where every {doc_id, line, quote} also says whether the quote is on that line."""
    if isinstance(node, list):
        return [mark_citations(x, docs) for x in node]
    if isinstance(node, dict):
        out = {k: mark_citations(v, docs) for k, v in node.items()}
        if {"doc_id", "line", "quote"} <= node.keys():
            doc = docs.get(node["doc_id"])
            out["verified"] = bool(doc and isinstance(node["line"], int) and quote_appears(doc, node["line"], node["quote"]))
        return out
    return node


def _decision(value: Any) -> str | None:
    return getattr(value, "value", value)


# --------------------------------------------------------------------------- agents

def _agent_status(result: AgentSystemResult | None) -> str:
    return result.status.value if result is not None else "NOT RUN"


def _task_status(bundle: ClaimRunBundle, agent: str, result: AgentSystemResult | None) -> str:
    """One agent's own status: its task in the plan (one failed agent does not make the others failed, and a blocked
    agent did not fail). Runs stored without a plan show their system's status."""
    plan = bundle.execution_plan
    task = next((t for t in plan.tasks if t.task_id == AGENT_TASK[agent]), None) if plan else None
    return task.status.value if task is not None else _agent_status(result)


def agent_cards(bundle: ClaimRunBundle, claim: ClaimInput | None, docs: list[EvidenceDocument]) -> list[dict]:
    doc_map = {d.doc_id: d for d in docs}
    adk, crew = bundle.adk, bundle.crewai
    packet = claim_message(claim, docs, bundle.run_id) if claim is not None else None
    doc_summary = [{"doc_id": d.doc_id, "kind": d.kind.value, "sha256": d.sha256, "lines": len(d.text.splitlines())}
                   for d in docs]
    cards = []
    adk_runs = {r.agent: r for r in (adk.agent_runs if adk else [])}
    offered = next((t["tools_offered_to_model"] for t in (adk.trace if adk else []) if "tools_offered_to_model" in t), {})
    for key, report in (("evidence_agent", adk.evidence_report if adk else None),
                        ("investigator_agent", adk.report if adk else None)):
        run = adk_runs.get(key)
        errors = [e for e in (adk.errors if adk else []) if key in e] + (run.errors if run else [])
        status = _task_status(bundle, key, adk)
        if adk is not None and status != "COMPLETED" and not errors:
            errors = list(adk.errors)
        out = report.model_dump(mode="json") if report is not None else None
        adk_meta = bundle.workflow_metadata.get("adk")
        cards.append({
            "key": key, **AGENTS[key], "status": status,
            "model": adk.model if adk else None,
            "provider": adk_meta.provider if adk_meta else None,
            "llm_mode": adk_meta.llm_mode if adk_meta else None,
            "genuine_inference": bool(adk and adk.model and adk.model != "test-double"),
            "session_id": adk.session_id if adk else None,
            "started_at": _iso(run.started_at) if run else None, "finished_at": _iso(run.finished_at) if run else None,
            "latency_ms": run.latency_ms if run else None, "llm_calls": run.llm_calls if run else None,
            "prompt_tokens": run.prompt_tokens if run else None, "completion_tokens": run.completion_tokens if run else None,
            "tools_offered": offered.get(key, []),
            "tool_calls": [c.model_dump(mode="json") for c in (adk.tool_calls if adk else []) if c.agent == key],
            "input": {"packet": packet, "documents": doc_summary,
                      "session_state": "evidence_report from the Evidence Agent ({evidence_report?})"
                      if key == "investigator_agent" else None},
            "output": mark_citations(out, doc_map) if out else None,
            "recommendation": _decision(getattr(report, "recommended_decision", None)),
            "errors": errors,
        })
    crew_run = crew.agent_runs[0] if crew and crew.agent_runs else None
    trace = crew.trace if crew else []
    report = crew.report if crew else None
    out = report.model_dump(mode="json") if report is not None else None
    request = next((m for m in bundle.messages if m.message_type == "FORENSIC_REQUEST"), None)
    meta = bundle.workflow_metadata.get("crewai")
    model_calls = next((t["model_calls"] for t in trace if "model_calls" in t), [])
    tokens = (crew_run.prompt_tokens, crew_run.completion_tokens, "agent run record") if crew_run else (None, None, None)
    reported = [m for m in model_calls if m.get("prompt_tokens") is not None or m.get("completion_tokens") is not None]
    if tokens[0] is None and reported:          # older records: sum the provider's per-call usage
        tokens = (sum(m.get("prompt_tokens") or 0 for m in reported),
                  sum(m.get("completion_tokens") or 0 for m in reported), "sum of per-call usage reported by the provider")
    cards.append({
        "key": "forensic_agent", **AGENTS["forensic_agent"], "status": _task_status(bundle, "forensic_agent", crew),
        "model": crew.model if crew else None,
        "provider": (report.provider if report else None) or (meta.provider if meta else None)
                    or next((t["llm"].get("provider") or t["llm"].get("mode") for t in trace if "llm" in t), None),
        "llm_mode": meta.llm_mode if meta else None,
        "genuine_inference": bool(meta and meta.llm_mode == "live"),
        "started_at": _iso(crew_run.started_at) if crew_run else None,
        "finished_at": _iso(crew_run.finished_at) if crew_run else None,
        "latency_ms": crew_run.latency_ms if crew_run else (crew.execution_ms if crew else None),
        "llm_calls": crew_run.llm_calls if crew_run else None,
        "prompt_tokens": tokens[0], "completion_tokens": tokens[1], "token_source": tokens[2],
        "crew": next((t["crew"] for t in trace if "crew" in t), None),
        "llm_settings": next((t["llm"] for t in trace if "llm" in t), None),
        "model_calls": model_calls,
        "network_guard": next(({"policy": t["network_guard"], "blocked_hosts": t["blocked_hosts"]}
                               for t in trace if "network_guard" in t), None),
        "steps": [t for t in trace if "step" in t],
        "tool_calls": [c.model_dump(mode="json") for c in (crew.tool_calls if crew else [])],
        "input": {"packet": packet, "documents": doc_summary,
                  "a2a_request": request.payload.model_dump(mode="json") if request else None},
        "output": mark_citations(out, doc_map) if out else None,
        "recommendation": _decision(report.recommended_outcome) if report else None,
        "errors": list(crew.errors) if crew else ["no CrewAI result"],
    })
    return cards


# --------------------------------------------------------------------------- MCP

def mcp_view(bundle: ClaimRunBundle) -> dict[str, Any]:
    mcp = bundle.mcp
    agent_records = [("adk", c) for c in (bundle.adk.tool_calls if bundle.adk else []) if c.protocol == "mcp"]
    agent_records += [("crewai", c) for c in (bundle.crewai.tool_calls if bundle.crewai else []) if c.protocol == "mcp"]
    used: set[int] = set()
    calls = []
    for call in (mcp.calls if mcp else []):
        agent = (call.caller or "").split(":")[-1]
        match = next((i for i, (_, rec) in enumerate(agent_records) if i not in used and rec.tool == call.tool
                      and rec.agent == agent and all(call.arguments.get(k) == v for k, v in rec.arguments.items())), None)
        rec = agent_records[match][1] if match is not None else None
        if match is not None:
            used.add(match)
        calls.append({**call.model_dump(mode="json"), "logged_by_server": True,
                      "returned": rec.result if rec is not None else None,
                      "agent_record_status": rec.status if rec is not None else None,
                      "agent_error": rec.error if rec is not None else None})
    for i, (framework, rec) in enumerate(agent_records):
        if i not in used:
            calls.append({"tool": rec.tool, "arguments": rec.arguments, "status": rec.status, "error": rec.error,
                          "started_at": _iso(rec.started_at), "latency_ms": rec.latency_ms, "run_id": rec.run_id,
                          "claim_id": bundle.claim_id, "caller": f"{framework}:{rec.agent}", "call_id": None,
                          "logged_by_server": False, "returned": rec.result})
    calls.sort(key=lambda c: c.get("started_at") or "")
    return {"status": mcp.status.value if mcp else "NOT RUN", "server_url": mcp.server_url if mcp else None,
            "transport": mcp.transport if mcp else None, "errors": mcp.errors if mcp else [],
            "discovered_tools": [t.model_dump(mode="json") for t in (mcp.discovered_tools if mcp else [])],
            "calls": calls}


# --------------------------------------------------------------------------- timeline and execution pattern

def timeline(bundle: ClaimRunBundle, submitted_at: str | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def add(ts: Any, actor: str, event: str, detail: str = "", status: str = "ok", duration_ms: float | None = None):
        t = _ts(ts)
        if t is None:
            return
        iso = ts.isoformat() if isinstance(ts, datetime) else ts if isinstance(ts, str) else \
            datetime.fromtimestamp(t).astimezone().isoformat()
        events.append({"ts": iso, "t": t, "actor": actor, "event": event, "detail": detail, "status": status,
                       "duration_ms": duration_ms})

    if submitted_at:
        add(submitted_at, "dashboard", "Claim submitted", "investigation requested from the dashboard")
    labels = {  # (event, actor) -> (label when it went well, label when it failed)
        ("workflow_started", "baseline"): ("Deterministic baseline started", ""),
        ("workflow_finished", "baseline"): ("Deterministic baseline completed", "Deterministic baseline FAILED"),
        ("workflow_started", "adk"): ("Google ADK workflow started (Runner + session)", ""),
        ("workflow_finished", "adk"): ("ADK reports validated (Pydantic schemas, citation and MCP-reference checks)",
                                       "ADK workflow FAILED (no validated reports)"),
        ("workflow_started", "crewai"): ("CrewAI forensic request started (via ADK RemoteA2aAgent)", ""),
        ("workflow_finished", "crewai"): ("CrewAI result received and validated (A2A contract, ForensicsReport)",
                                          "CrewAI result received: FAILED (not used as evidence)"),
    }
    for e in bundle.audit:
        if e.event == "run_started":
            add(e.ts, "orchestrator", "Investigation run started", e.detail)
        elif (e.event, e.actor) in labels:
            failed = e.event == "workflow_finished" and not e.detail.startswith("COMPLETED")
            add(e.ts, e.actor, labels[(e.event, e.actor)][1 if failed else 0], e.detail, "error" if failed else "ok",
                e.data.get("execution_ms"))
        elif e.event == "message_sent":
            add(e.ts, "a2a", "A2A FORENSIC_REQUEST sent (ADK RemoteA2aAgent to CrewAI service)",
                f"message {e.data.get('message_id', '')[:12]}")
        elif e.event == "message_received":
            add(e.ts, "a2a", f"A2A {e.detail.split(' ')[0]} received from the CrewAI service",
                f"message {e.data.get('message_id', '')[:12]}")
        elif e.event == "validation_failure":
            add(e.ts, e.actor, "Validation failure", e.detail, "error")
        elif e.event == "mcp_records_fetched":
            add(e.ts, "orchestrator", "MCP server log read for this run", e.detail)
        elif e.event == "adjudication":
            add(e.ts, "adjudicator", "Final deterministic adjudication completed", e.detail)
        elif e.event == "run_finished":
            add(e.ts, "orchestrator", "Run finished; all outputs stored separately", e.detail)
    for run in (bundle.adk.agent_runs if bundle.adk else []):
        title = AGENTS.get(run.agent, {}).get("title", run.agent)
        add(run.started_at, run.agent, f"{title} started (Google ADK)")
        if run.finished_at:
            add(run.finished_at, run.agent, f"{title} finished (Google ADK)", run.output_summary and
                f"{run.llm_calls} model call(s), {run.tool_calls} tool call(s)" or "", "error" if run.errors else "ok",
                run.latency_ms)
    for run in (bundle.crewai.agent_runs if bundle.crewai else []):
        add(run.started_at, "forensic_agent", "Forensic Agent started (CrewAI Crew.kickoff)")
        if run.finished_at:
            add(run.finished_at, "forensic_agent", "Forensic Agent finished (CrewAI)",
                f"{run.llm_calls} model call(s), {run.tool_calls} tool call(s)",
                "ok" if bundle.crewai.status == ExecutionStatus.COMPLETED else "error", run.latency_ms)
    for t in (bundle.crewai.trace if bundle.crewai else []):
        for mc in t.get("model_calls", []):
            if mc.get("status") == "error":
                add(mc.get("finished_at"), "forensic_agent", f"Model call FAILED ({mc.get('model')})",
                    mc.get("error_code") or mc.get("error") or "", "error", mc.get("latency_ms"))
                continue
            add(mc.get("finished_at"), "forensic_agent", f"Model call completed ({mc.get('model')})",
                f"{mc.get('prompt_tokens')} prompt / {mc.get('completion_tokens')} completion tokens, "
                f"finish_reason={mc.get('finish_reason')}", "error" if mc.get("finish_reason") == "length" else "ok",
                mc.get("latency_ms"))
    for c in (bundle.mcp.calls if bundle.mcp else []):
        add(c.started_at, "mcp", f"MCP {c.tool} called by {c.caller}",
            ", ".join(f"{k}={v}" for k, v in c.arguments.items()), "ok" if c.status == "ok" else "error", c.latency_ms)
    events.sort(key=lambda e: e["t"])
    if events:
        t0 = events[0]["t"]
        for e in events:
            e["offset_s"] = round(e["t"] - t0, 3)
    return events


def execution_pattern(bundle: ClaimRunBundle) -> dict[str, Any]:
    runs = {r.agent: r for r in (bundle.adk.agent_runs if bundle.adk else [])}
    ev, inv = runs.get("evidence_agent"), runs.get("investigator_agent")
    sent = next((e.ts for e in bundle.audit if e.event == "message_sent"), None)
    received = next((e.ts for e in bundle.audit if e.event == "message_received"), None)
    crew_end = received or (bundle.crewai.finished_at if bundle.crewai else None)
    adk_start, adk_end = (bundle.adk.started_at, bundle.adk.finished_at) if bundle.adk else (None, None)
    bars = [("Deterministic baseline", bundle.baseline.started_at if bundle.baseline else None,
             bundle.baseline.finished_at if bundle.baseline else None),
            ("ADK Evidence Agent", ev.started_at if ev else None, ev.finished_at if ev else None),
            ("ADK Investigator Agent", inv.started_at if inv else None, inv.finished_at if inv else None),
            ("CrewAI Forensic Agent (A2A request to response)", sent, crew_end)]
    starts = [_ts(s) for _, s, e in bars if s and e]
    ends = [_ts(e) for _, s, e in bars if s and e]
    t0, t1 = (min(starts), max(ends)) if starts and ends else (None, None)
    overlap = None
    if adk_start and adk_end and sent and crew_end:
        overlap = max(0.0, min(_ts(adk_end), _ts(crew_end)) - max(_ts(adk_start), _ts(sent)))
    if bundle.context_sharing == "blackboard":
        sequential = ("Inside Google ADK, a SequentialAgent runs the Evidence Agent and then the Investigator Agent. "
                      "The Evidence report is published to the shared blackboard as soon as it is validated, and the "
                      "Investigator reads it from the blackboard.")
        parallel = ("Once the Evidence report is published, the orchestrator sends the A2A request to the CrewAI "
                    "Forensic Agent with a reference to it; the Forensic Agent reads it from the blackboard and runs "
                    "concurrently with the Investigator. Neither sees the other's report.")
    else:
        sequential = ("Inside Google ADK, a SequentialAgent runs the Evidence Agent and then the Investigator Agent, "
                      "which reads the evidence report from session state (blackboard disabled).")
        parallel = ("The orchestrator runs the ADK workflow and the A2A request to the CrewAI Forensic Agent "
                    "concurrently (asyncio.gather); neither sees the other's output.")
    return {
        "sequential": {"description": sequential,
                       "verified": bool(ev and inv and ev.finished_at and inv.started_at >= ev.finished_at)},
        "parallel": {"description": parallel, "overlap_s": round(overlap, 3) if overlap is not None else None},
        "bars": [{"label": label, "start_s": round(_ts(s) - t0, 3), "end_s": round(_ts(e) - t0, 3)}
                 for label, s, e in bars if s and e and t0 is not None],
        "span_s": round(t1 - t0, 3) if t0 is not None else None,
    }


# --------------------------------------------------------------------------- final decision and modes

def comparison(bundle: ClaimRunBundle, cards: list[dict]) -> list[dict[str, Any]]:
    base, final, mcp = bundle.baseline, bundle.final_report, bundle.mcp
    by_key = {c["key"]: c for c in cards}
    rows = [{"system": "Deterministic baseline", "framework": "plain Python rules", "status": base.status.value if base else "NOT RUN",
             "result": _decision(base.decision) if base else None, "kind": "decision (rules)",
             "detail": "; ".join(base.decision_reasons[:2]) if base else "no result"}]
    for key in ("evidence_agent", "investigator_agent", "forensic_agent"):
        c = by_key[key]
        out = c["output"] or {}
        detail = {"evidence_agent": lambda: f"{len(out.get('facts', []))} facts, {len(out.get('findings', []))} findings, "
                                            f"missing: {', '.join(out.get('missing_information', [])) or 'none'}",
                  "investigator_agent": lambda: f"policy eligibility {out.get('policy_eligibility')}; "
                                                f"{len(out.get('findings', []))} findings",
                  "forensic_agent": lambda: f"{len(out.get('findings', []))} findings, "
                                            f"{len(out.get('historical_pattern_findings', []))} history pattern(s)"}[key]
        rows.append({"system": c["title"], "framework": c["framework"], "status": c["status"],
                     "result": c["recommendation"], "kind": "recommendation only",
                     "confidence": out.get("confidence"), "model": c.get("model"),
                     "detail": detail() if c["output"] else "; ".join(c["errors"][:2]) or "no output"})
    calls = mcp.calls if mcp else []
    rows.append({"system": "MCP tools", "framework": "MCP (tool protocol)", "status": mcp.status.value if mcp else "NOT RUN",
                 "result": None, "kind": "tool results, not a decision",
                 "detail": f"{len(calls)} logged call(s): " + ", ".join(sorted({f'{c.tool} by {c.caller}' for c in calls}))})
    rows.append({"system": "Final adjudicator", "framework": "deterministic code", "status": "COMPLETED" if final else "NOT RUN",
                 "result": _decision(final.final_decision) if final else None, "kind": "controlled final decision",
                 "detail": f"decided by {final.decided_by}" if final else "no final report"})
    return rows


def disagreements(cards: list[dict], bundle: ClaimRunBundle) -> list[str]:
    recs = [(c["title"], c["recommendation"]) for c in cards if c["recommendation"]]
    out = []
    for i, (a, ra) in enumerate(recs):
        for b, rb in recs[i + 1:]:
            if ra != rb:
                out.append(f"{a} recommends {ra}; {b} recommends {rb}")
    base = bundle.baseline.decision.value if bundle.baseline and bundle.baseline.decision else None
    for title, rec in recs:
        if base and rec != base:
            out.append(f"{title} recommends {rec}; the deterministic rules decide {base}")
    for c in cards:
        if c["status"] != "COMPLETED":
            out.append(f"{c['title']} has no usable result (status {c['status']})")
    return out


def modes(bundle: ClaimRunBundle, cards: list[dict]) -> list[dict[str, Any]]:
    adk, crew = bundle.adk, bundle.crewai
    forensic = next(c for c in cards if c["key"] == "forensic_agent")
    adk_meta = bundle.workflow_metadata.get("adk")
    return [
        {"system": "Google ADK (Evidence + Investigator)", "mode": adk_meta.llm_mode if adk_meta else None,
         "model": adk.model if adk else None, "provider": adk_meta.provider if adk_meta else None,
         "genuine_inference": bool(adk and adk.model and adk.model != "test-double"),
         "completed": bool(adk and adk.status == ExecutionStatus.COMPLETED),
         "tokens": adk.usage.model_dump() if adk else None, "latency_ms": adk.execution_ms if adk else None,
         "failures": adk.errors if adk else []},
        {"system": "CrewAI (Forensic Agent)", "mode": forensic["llm_mode"], "model": forensic["model"],
         "provider": forensic["provider"], "genuine_inference": forensic["genuine_inference"],
         "completed": bool(crew and crew.status == ExecutionStatus.COMPLETED),
         "tokens": {"prompt_tokens": forensic["prompt_tokens"], "completion_tokens": forensic["completion_tokens"]},
         "latency_ms": crew.execution_ms if crew else None, "model_calls": forensic["model_calls"],
         "failures": crew.errors if crew else []},
    ]


def security_events(bundle: ClaimRunBundle, cards: list[dict]) -> list[dict[str, Any]]:
    out = [{"layer": "deterministic input screen (security.screen_text)", **g.model_dump(mode="json")}
           for g in (bundle.baseline.guardrail_events if bundle.baseline else [])]
    for c in cards:
        for f in (c["output"] or {}).get("findings", []):
            if f.get("category") == "SUSPICIOUS_DETAIL":
                out.append({"layer": f"{c['title']} finding", "kind": "agent_flag", "source": c["framework"],
                            "action": "reported", "detail": f.get("description"), "matched": []})
    for call in (bundle.mcp.calls if bundle.mcp else []):
        if call.status == "error":
            out.append({"layer": "MCP server", "kind": "tool_error", "source": f"{call.tool} by {call.caller}",
                        "action": "rejected", "detail": call.error, "matched": []})
    for text in (bundle.final_report.unsupported_claims if bundle.final_report else []):
        out.append({"layer": "adjudicator evidence verification", "kind": "unsupported_claim", "source": "adjudicator",
                    "action": "ignored", "detail": text, "matched": []})
    return out


# --------------------------------------------------------------------------- security & document integrity

_LABELLED = re.compile(r"^\s*[A-Za-z][A-Za-z ]{1,40}?\s*:")
_DOC_TITLES = {"invoice": "Invoice", "repair_report": "Repair report", "other_text": "Other text"}
_CLAIM_FIELDS = {"claim.claimant_statement": ("claimant_statement", "Customer statement"),
                 "claim.reported_issue": ("reported_issue", "Problem description"),
                 "claim.claimant_name": ("claimant_name", "Claimant name"),
                 "claim.device_model": ("device_model", "Product")}


def _instruction_spans(lines: list[str], pages: list[list[int]] | None) -> list[list[int]]:
    """1-based line spans: each line the screen flags, plus the wrapped continuation lines that follow it
    (same page, no `Label:` of their own, containing words), so the whole instruction is quoted."""
    flagged = {n for n, text in enumerate(lines, 1) if detect_injection(text) or (
        n < len(lines) and detect_injection(f"{text}\n{lines[n]}") and not detect_injection(lines[n]))}   # wrapped over two
    spans, seen = [], set()
    for n in sorted(flagged):
        if n in seen:
            continue
        span, m = [n], n + 1
        while m <= len(lines) and (m in flagged or (re.search(r"[a-z]", lines[m - 1]) and not _LABELLED.match(lines[m - 1])
                                                    and (not pages or pages[m - 1][0] == pages[m - 2][0]))):
            span.append(m)
            m += 1
        seen.update(span)
        spans.append(span)
    return spans


def _reported_by(cards: list[dict], doc_id: str | None, span: list[int]) -> list[str]:
    """Agents whose own output contains a finding about this instruction (cited line, or claim-text finding)."""
    out = []
    for c in cards:
        for f in (c["output"] or {}).get("findings", []):
            cites = f.get("citations") or []
            if (doc_id and any(x.get("doc_id") == doc_id and x.get("line") in span for x in cites)) or \
               (doc_id is None and not cites and "instruction" in (f.get("description") or "").lower()):
                out.append(c["title"])
                break
    return out


def document_integrity(bundle: ClaimRunBundle, claim: ClaimInput | None, docs: list[EvidenceDocument],
                       origin: dict | None, cards: list[dict]) -> dict[str, Any]:
    """The report's Security & Document Integrity section. Built only from this run's records: the deterministic
    screen's events (which drive the security escalation), the extracted lines and their PDF positions, each
    agent's own findings, the MCP server's log of history lookups and the adjudicator's report."""
    events = [g for g in (bundle.baseline.guardrail_events if bundle.baseline else []) if g.kind == "prompt_injection"]
    line_map, fields = (origin or {}).get("line_map") or {}, (origin or {}).get("fields") or {}
    final = bundle.final_report
    history = [c for c in (bundle.mcp.calls if bundle.mcp else []) if c.tool == "claim_history_lookup"]
    refused = [c for c in history if c.status == "error" and "scope" in (c.error or "").lower()]
    scope = {"lookups": len(history), "scoped_to_claim": sum(c.claim_id == bundle.claim_id for c in history),
             "refused": [{"caller": c.caller, "arguments": c.arguments, "error": c.error} for c in refused]}

    located = []
    for g in events:
        if g.source.startswith("doc:"):
            doc = next((d for d in docs if f"doc:{d.doc_id}" == g.source), None)
            if doc is None:
                continue
            lines, pages = doc.text.splitlines(), line_map.get(doc.doc_id)
            for span in _instruction_spans(lines, pages):
                located.append({
                    "doc_id": doc.doc_id, "source": f"{_DOC_TITLES.get(doc.doc_id, doc.doc_id)} section"
                    + (f" of {origin['filename']}" if origin and origin.get("filename") else f" ({doc.filename})"),
                    "lines": span, "pdf": [pages[n - 1] for n in span] if pages else [],
                    "text": " ".join(lines[n - 1] for n in span),
                    "matched": sorted(detect_injection("\n".join(lines[n - 1] for n in span))),
                    "reported_by": _reported_by(cards, doc.doc_id, span)})
        elif g.source in _CLAIM_FIELDS and claim is not None:
            name, title = _CLAIM_FIELDS[g.source]
            where = fields.get(name) or {}
            located.append({"doc_id": None, "source": f"Claim form – {title.lower()}", "lines": [],
                            "pdf": [[where["page"], where["line"]]] if where.get("page") else [],
                            "text": getattr(claim, name), "matched": sorted(g.matched),
                            "reported_by": _reported_by(cards, None, [])})

    escalated = final is not None and final.decided_by == "GUARDRAIL_ESCALATION" and final.payout is None
    checks = final.rule_checks if final else []
    failed = [c.name.replace("_", " ") for c in checks if c.outcome.value == "FAIL"]
    continued = (f"Normal policy-based investigation continued: {len(checks)} rule checks ran on the legitimate "
                 f"evidence and {len(final.evidence_references)} evidence references reached the adjudicator"
                 + (f"; issues still reported: {', '.join(failed)}." if failed else ".")) if final else None
    history_line = (f"Claim-history access: {scope['lookups']} lookup(s) in this run, {scope['scoped_to_claim']} limited "
                    f"by the MCP server to this claim's customer and serial numbers; {len(refused)} refused."
                    if history else "Claim-history access: no lookups were made in this run.")
    findings = []
    for f in located:
        data_access = bool(set(f["matched"]) & DATA_ACCESS_PATTERNS)
        response = ["Untrusted document instruction detected. The instruction was not followed: it was treated as "
                    "evidence text, never as an instruction to the agents or the adjudicator."]
        if escalated:
            response.append(f"The deterministic adjudicator escalated the claim to a human reviewer under the "
                            f"security rule ({final.final_decision.value}); no payout.")
        elif final is not None:
            response.append(f"The decision ({final.final_decision.value}) was made by the deterministic adjudicator "
                            f"({final.decided_by}).")
        if continued:
            response.append(continued)
        if data_access:
            response.append(f"{len(refused)} cross-customer history request(s) refused by the MCP server." if refused
                            else "No agent requested another customer's data. " + history_line)
        findings.append({**f, "type": "Unauthorized data access attempt" if data_access else "Prompt injection",
                         "detection": "Detected by the deterministic input screen (security.screen_text; rules: "
                                      + ", ".join(f["matched"]) + ")",
                         "response": response})

    agent_only = [c["title"] for c in cards for x in (c["output"] or {}).get("findings", [])
                  if x.get("category") == "SUSPICIOUS_DETAIL" and "instruction" in (x.get("description") or "").lower()]
    status = ("Prompt Injection Detected" if findings else "Suspicious" if refused or agent_only else "Clean")
    return {"status": status, "findings": findings, "history_scope": scope, "history_line": history_line,
            "agent_only_flags": sorted(set(agent_only)) if not findings else []}


_AUDIT_TEXT = {
    ("run_started", "orchestrator"): "Investigation started",
    ("workflow_started", "baseline"): "Deterministic policy rules started",
    ("workflow_finished", "baseline"): "Deterministic policy rules finished",
    ("workflow_started", "adk"): "Evidence Agent and Investigator Agent started (Google ADK)",
    ("workflow_finished", "adk"): "Evidence Agent and Investigator Agent finished; reports validated",
    ("workflow_started", "crewai"): "Forensic Agent requested over A2A (CrewAI)",
    ("workflow_finished", "crewai"): "Forensic Agent result received",
    ("message_sent", "adk_orchestrator"): "Forensic request sent to the CrewAI service (A2A)",
    ("mcp_records_fetched", "orchestrator"): "Tool calls confirmed from the MCP server log",
    ("adjudication", "adjudicator"): "Automated decision made by the deterministic adjudicator",
    ("run_finished", "orchestrator"): "Investigation report stored",
}


def audit_trail(bundle: ClaimRunBundle, reviews: list | None = None) -> list[dict[str, Any]]:
    """The run's own audit events in plain words, plus every human review action (who, what, when)."""
    out = []
    for e in bundle.audit:
        text = _AUDIT_TEXT.get((e.event, e.actor))
        if e.event == "message_received":
            text = "Forensic response received from the CrewAI service (A2A)"
        elif e.event == "validation_failure":
            text = "Validation failure"
        if text:
            out.append({"ts": _iso(e.ts), "kind": "system", "text": text, "detail": e.detail})
    for r in reviews or []:
        out.append({"ts": _iso(r.ts), "kind": "human", "text": f"Human review: {r.action} by {r.reviewer}",
                    "detail": r.notes, "action": r.action, "reviewer": r.reviewer,
                    "automated_decision": r.automated_decision, "effect": r.effect})
    return sorted(out, key=lambda x: x["ts"] or "")


def request_accounting(bundle: ClaimRunBundle) -> dict[str, Any] | None:
    """The ledger's count for this run against the model calls the agents recorded; None when no request was recorded.
    A difference means a request one side did not see."""
    finished = next((e for e in bundle.audit if e.event == "run_finished"), None)
    ledger = sum(((finished.data.get("gemini_requests") if finished else None) or {}).values())
    if not ledger:
        return None
    recorded = sum(r.llm_calls or 0 for result in (bundle.adk, bundle.crewai) if result for r in result.agent_runs)
    return {"ledger": ledger, "recorded": recorded, "match": ledger == recorded}


def execution_summary(bundle: ClaimRunBundle, cards: list[dict]) -> dict[str, Any]:
    """LIVE / OFFLINE / MIXED for this run, and per agent: status, model, provider, model calls, MCP calls.
    Every value comes from the stored run records; nothing here is inferred from configuration."""
    rows = []
    for c in cards:
        mcp = [t for t in c["tool_calls"] if t.get("protocol") == "mcp"]
        rows.append({"agent": c["title"], "framework": c["framework"], "status": c["status"], "retry": c.get("retry"), "model": c["model"],
                     "provider": c.get("provider"), "genuine_inference": c["genuine_inference"],
                     "llm_calls": c.get("llm_calls"),
                     "mcp_ok": sorted({t["tool"] for t in mcp if t.get("status") == "ok"}),
                     "mcp_errors": sum(1 for t in mcp if t.get("status") != "ok")})
    live = [r["genuine_inference"] for r in rows]
    orchestrator = bundle.workflow_metadata.get("orchestrator")
    mode = "LIVE" if all(live) else "OFFLINE" if not any(live) else "MIXED"
    response = next((m for m in bundle.messages if m.message_type != "FORENSIC_REQUEST"), None)
    final = bundle.final_report
    finished = next((e for e in bundle.audit if e.event == "run_finished"), None)
    requests = (finished.data.get("gemini_requests") if finished else None) or {}
    return {
        "mode": mode, "requested_live": bool(orchestrator and orchestrator.llm_mode == "live"),
        "agents": rows,
        "mcp_server_calls": len(bundle.mcp.calls) if bundle.mcp else 0,
        "a2a": {"request_sent": any(m.message_type == "FORENSIC_REQUEST" for m in bundle.messages),
                "response": response.message_type if response else None},
        "final_decision": _decision(final.final_decision) if final else None,
        "decided_by": final.decided_by if final else None,
        "requires_human_approval": final.requires_human_approval if final else None,
        "review_status": final.review_status if final else None,
        "incomplete_reasons": list(final.incomplete_reasons) if final else [],
        "gemini_requests": requests, "gemini_requests_total": sum(requests.values()),
        "request_accounting": request_accounting(bundle),
    }


WITHHELD = "[wording withheld: it repeats an instruction found in the documents. See Security & Document Integrity.]"
REPORTS = (("GOOGLE_ADK", "Evidence Agent", "adk", "evidence_report"), ("GOOGLE_ADK", "Investigator Agent", "adk", "report"),
           ("CREWAI", "Forensic Agent", "crewai", "report"))
SYSTEMS = {"GOOGLE_ADK": "Evidence and Investigator Agents", "CREWAI": "Forensic Agent"}
RETRY_RESULT = {"recovered": "RECOVERED AFTER RETRY", "failed": "RETRY FAILED", "refused": "RETRY NOT SENT"}


def provider_retries(outcome: RunOutcome) -> list[dict[str, Any]]:
    """Each automatic retry and what became of it. The failed request behind it stays in the run's trace."""
    return [{**r.model_dump(mode="json"), "result": RETRY_RESULT[r.outcome]} for r in outcome.retries]


def _shown(text: str, docs: list[EvidenceDocument]) -> str:
    """An agent's own words for the reviewer, unless they repeat a document's instruction or carry something restricted."""
    if detect_injection(text) or set(restricted_output(text, docs)) - {"credential"}:
        return WITHHELD
    return redact(text, 600)


def _concern(system: str, agent: str, finding: Any, report: Any, final: Any, records: bool, ignored: bool,
             docs: list[EvidenceDocument]) -> dict[str, Any]:
    advised = getattr(report, "recommended_decision", None) or getattr(report, "recommended_outcome", None)
    decided = final.final_decision.value
    row = {"agent": agent, "severity": finding.severity.value, "category": finding.category,
           "description": _shown(finding.description, docs), "recommended": advised.value if advised else None}
    if not ignored:
        return {**row, "effect": "Used: this verified concern escalated the claim." if final.decided_by == "CONFLICT_ESCALATION"
                else f"Recorded. The automated decision is {decided}, by {final.decided_by}."}
    if finding.category == "CLAIM_HISTORY_PATTERN":
        why = ("The MCP server's records were unavailable, so the history lookup behind this finding could not be confirmed."
               if records else "No MCP server record confirms the history lookup behind this finding.")
    else:
        why = "Its citations do not match the documents." if finding.citations else "It cites no line in the documents."
    return {**row, "why_unverified": why,
            "effect": f"Not used. A finding counts only with verified evidence, so the automated decision stays {decided}. "
                      f"The {agent} recommended {row['recommended']}."}


def reviewer_view(bundle: ClaimRunBundle, docs: list[EvidenceDocument]) -> dict[str, Any]:
    """What a reviewer needs to act: which reviews finished, why a person is needed, and every agent concern, verified or not.
    It reads the stored reports and decides nothing."""
    final = bundle.final_report
    if final is None:
        return {"records_only": False, "agent_reviews": [], "human_reasons": [], "verified_concerns": [], "unverified_concerns": [],
                "other_unverified": []}
    missing = {system for system in SYSTEMS if any(r.startswith(f"{system} review unavailable") for r in final.incomplete_reasons)}
    if final.review_status is None:                 # stored before these fields existed: the agents' own statuses are the record
        missing = {system for system, result in (("GOOGLE_ADK", bundle.adk), ("CREWAI", bundle.crewai))
                   if result is None or result.status != ExecutionStatus.COMPLETED}
    lost = MCP_RECORDS_UNAVAILABLE in final.incomplete_reasons
    only = records_only(final)
    empty = records_missing(bundle)
    unsupported, verified, unverified = list(final.unsupported_claims), [], []
    for system, agent, side, field in REPORTS:
        report = getattr(getattr(bundle, side), field, None) if getattr(bundle, side) else None
        if report is None or system in missing:
            continue
        for finding in report.findings:
            if finding.severity.value != "HIGH":
                continue
            line = f"{system}: HIGH finding without verified evidence ignored: {finding.description[:80]}"
            ignored = line in unsupported
            if ignored:
                unsupported.remove(line)
            (unverified if ignored else verified).append(_concern(system, agent, finding, report, final, lost, ignored, docs))
    other = [{"agent": SYSTEMS.get(text.split(":", 1)[0], "Agent"), "text": _shown(text.split(":", 1)[-1].strip(), docs)}
             for text in unsupported]
    reasons = []
    if empty:
        reasons.append(f"The {MCP_LOG_EMPTY}.")
    elif lost:
        reasons.append("The MCP server's records could not confirm the agents' tool calls.")
    reasons += [f"The {SYSTEMS[system]} did not finish a review." for system in SYSTEMS if system in missing]
    if unverified:
        reasons.append(f"{len(unverified)} HIGH agent concern(s) could not be verified.")
    if final.final_decision.value in ("REJECT", "ESCALATE"):
        reasons.append(f"Every {final.final_decision.value} needs a reviewer.")
    if final.payout is not None and final.payout.payout >= config.HUMAN_APPROVAL_THRESHOLD:
        reasons.append("The payout is at or above the sign-off threshold.")
    gaps = len(missing)
    headline = detail = title = banner = None
    if only:
        title = "SUPPORTING RECORDS UNAVAILABLE"
        headline = ("Both agent reviews finished, but the MCP server's log holds no record of their tool calls." if empty else
                    "Both agent reviews finished, but the MCP server's records could not confirm their tool calls.")
        detail = "The automated decision is unchanged. Anything that depended on those records is listed as unverified, and a person must review this run."
        banner = incomplete_text(final)
    elif empty and not gaps:
        title = "SUPPORTING RECORDS UNAVAILABLE"
        headline = "Both agent reviews finished, but the MCP server's log holds no record of their tool calls."
        detail = ("The server reported no fault, so this did not send the run to a person and the automated decision stands. "
                  "Anything that depended on those records is listed as unverified.")
        banner = f"{title} (both agent reviews finished)"
    elif gaps:
        title = "INCOMPLETE INVESTIGATION"
        headline = ("No agent review finished." if gaps == len(SYSTEMS) else "One agent review did not finish.") + (
            " The MCP server's records could not be confirmed either." if lost or empty else "")
        detail = "The decision below comes from the deterministic rules alone and needs a human reviewer."
        banner = incomplete_text(final)
    return {"records_only": only, "records_missing": empty, "title": title, "headline": headline, "detail": detail, "banner": banner,
            "agent_reviews": [{"agent": label, "state": "did not finish" if system in missing else "finished"}
                              for system, label in SYSTEMS.items()],
            "human_reasons": reasons if final.requires_human_approval else [],
            "verified_concerns": verified, "unverified_concerns": unverified, "other_unverified": other}


def run_view(bundle: ClaimRunBundle, claim: ClaimInput | None, docs: list[EvidenceDocument],
             origin: dict | None = None, meta: dict | None = None, crew_log: list[dict] | None = None,
             reviews: list | None = None) -> dict[str, Any]:
    stands = final_review(reviews or [])
    cards = agent_cards(bundle, claim, docs)
    final = bundle.final_report
    outcome = outcome_of(bundle)
    retries = provider_retries(outcome)
    for card in cards:
        card["retry"] = next((r["result"] for r in retries if r["agent"] == card["key"]), None)
    request_docs = next(({d.doc_id: d.sha256 for d in m.payload.documents} for m in bundle.messages
                         if m.message_type == "FORENSIC_REQUEST"), {})
    docs_changed = bool(request_docs) and request_docs != {d.doc_id: d.sha256 for d in docs}
    return {
        "run_id": bundle.run_id, "claim_id": bundle.claim_id,
        "run_status": outcome.run_status.value, "outcome": outcome.model_dump(mode="json"),
        "status_badge": "SUPPORTING RECORDS UNAVAILABLE" if records_unconfirmed(bundle) else outcome.run_status.value,
        "claim": claim.model_dump(mode="json") if claim else None,
        "documents": [{"doc_id": d.doc_id, "kind": d.kind.value, "filename": d.filename, "sha256": d.sha256,
                       "lines": d.text.splitlines()} for d in docs],
        "documents_changed_since_run": docs_changed,
        "origin": origin, "meta": meta,
        "agents": cards,
        "baseline": bundle.baseline.model_dump(mode="json") if bundle.baseline else None,
        "final": final.model_dump(mode="json") if final else None,
        "comparison": comparison(bundle, cards),
        "disagreements": disagreements(cards, bundle),
        "modes": modes(bundle, cards),
        "execution": execution_summary(bundle, cards),
        "provider_retries": retries,
        "mcp": mcp_view(bundle),
        "a2a": {"messages": [m.model_dump(mode="json") for m in bundle.messages],
                "service_log": crew_log, "errors": [e.detail for e in bundle.audit if e.event == "validation_failure"]},
        "timeline": timeline(bundle, (meta or {}).get("submitted_at")),
        "pattern": execution_pattern(bundle),
        "security_events": security_events(bundle, cards),
        "flagged_lines": [{"doc_id": d.doc_id, "line": n, "matched": e.matched} for d in docs
                          for n, text in d.numbered_lines() for e in screen_text(text, f"doc:{d.doc_id}#L{n}")
                          if e.kind == "prompt_injection"],
        "integrity": document_integrity(bundle, claim, docs, origin, cards),
        "evidence_integrity": [e.model_dump(mode="json") for e in bundle.security_events],
        "audit_trail": audit_trail(bundle, reviews),
        "human_reviews": [r.model_dump(mode="json") for r in reviews or []],
        "final_review": stands.model_dump(mode="json") if stands else None,
        "reviewer": reviewer_view(bundle, docs),
        "reviews_unreadable": getattr(reviews, "unreadable", 0),
    }


def run_summary(bundle: ClaimRunBundle, meta: dict | None = None) -> dict[str, Any]:
    final = bundle.final_report
    crew_meta = bundle.workflow_metadata.get("crewai")
    started = next((e.ts for e in bundle.audit if e.event == "run_started"), None)
    return {"run_id": bundle.run_id, "claim_id": bundle.claim_id, "started_at": _iso(started),
            "run_status": outcome_of(bundle).run_status.value,
            "final_decision": _decision(final.final_decision) if final else None,
            "decided_by": final.decided_by if final else None,
            "review_status": final.review_status if final else None,
            "adk_status": _agent_status(bundle.adk), "adk_model": bundle.adk.model if bundle.adk else None,
            "crewai_status": _agent_status(bundle.crewai), "crewai_model": bundle.crewai.model if bundle.crewai else None,
            "crewai_mode": crew_meta.llm_mode if crew_meta else None,
            "crewai_provider": crew_meta.provider if crew_meta else None,
            "source": (meta or {}).get("source", "command line / earlier phase")}
