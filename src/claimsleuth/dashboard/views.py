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

from claimsleuth.claim_text import claim_message
from claimsleuth.documents import quote_appears
from claimsleuth.schemas import (AgentSystemResult, ClaimInput, ClaimRunBundle, EvidenceDocument, ExecutionStatus)
from claimsleuth.security import DATA_ACCESS_PATTERNS, detect_injection, screen_text

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
        if adk is not None and adk.status != ExecutionStatus.COMPLETED and not errors:
            errors = list(adk.errors)
        out = report.model_dump(mode="json") if report is not None else None
        cards.append({
            "key": key, **AGENTS[key], "status": _agent_status(adk),
            "model": adk.model if adk else None,
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
        "key": "forensic_agent", **AGENTS["forensic_agent"], "status": _agent_status(crew),
        "model": crew.model if crew else None,
        "provider": (report.provider if report else None) or (meta.provider if meta else None)
                    or ("ollama-local" if meta and meta.llm_mode == "live" else None),
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
    return {
        "sequential": {"description": "Inside Google ADK, a SequentialAgent runs the Evidence Agent and then the "
                                      "Investigator Agent, which reads the evidence report from session state.",
                       "verified": bool(ev and inv and ev.finished_at and inv.started_at >= ev.finished_at)},
        "parallel": {"description": "The orchestrator runs the ADK workflow and the A2A request to the CrewAI "
                                    "Forensic Agent concurrently (asyncio.gather); neither sees the other's output.",
                     "overlap_s": round(overlap, 3) if overlap is not None else None},
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
                 "claim.reported_issue": ("reported_issue", "Problem description")}


def _instruction_spans(lines: list[str], pages: list[list[int]] | None) -> list[list[int]]:
    """1-based line spans: each line the screen flags, plus the wrapped continuation lines that follow it
    (same page, no `Label:` of their own, containing words), so the whole instruction is quoted."""
    flagged = {n for n, text in enumerate(lines, 1) if detect_injection(text)}
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
                    "matched": sorted({p for n in span for p in detect_injection(lines[n - 1])}),
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
                    "automated_decision": r.automated_decision})
    return sorted(out, key=lambda x: x["ts"] or "")


def run_view(bundle: ClaimRunBundle, claim: ClaimInput | None, docs: list[EvidenceDocument],
             origin: dict | None = None, meta: dict | None = None, crew_log: list[dict] | None = None,
             reviews: list | None = None) -> dict[str, Any]:
    cards = agent_cards(bundle, claim, docs)
    final = bundle.final_report
    request_docs = next(({d.doc_id: d.sha256 for d in m.payload.documents} for m in bundle.messages
                         if m.message_type == "FORENSIC_REQUEST"), {})
    docs_changed = bool(request_docs) and request_docs != {d.doc_id: d.sha256 for d in docs}
    return {
        "run_id": bundle.run_id, "claim_id": bundle.claim_id,
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
        "audit_trail": audit_trail(bundle, reviews),
        "human_reviews": [r.model_dump(mode="json") for r in reviews or []],
    }


def run_summary(bundle: ClaimRunBundle, meta: dict | None = None) -> dict[str, Any]:
    final = bundle.final_report
    crew_meta = bundle.workflow_metadata.get("crewai")
    started = next((e.ts for e in bundle.audit if e.event == "run_started"), None)
    return {"run_id": bundle.run_id, "claim_id": bundle.claim_id, "started_at": _iso(started),
            "final_decision": _decision(final.final_decision) if final else None,
            "decided_by": final.decided_by if final else None,
            "adk_status": _agent_status(bundle.adk), "adk_model": bundle.adk.model if bundle.adk else None,
            "crewai_status": _agent_status(bundle.crewai), "crewai_model": bundle.crewai.model if bundle.crewai else None,
            "crewai_mode": crew_meta.llm_mode if crew_meta else None,
            "source": (meta or {}).get("source", "command line / earlier phase")}
