"""Turn one executed evaluation case (a ClaimRunBundle from the integrated workflow) into a scored result row.

Each approach is scored only on what it is responsible for:
  Evidence Agent      fact extraction, missing-evidence detection, citation validity
  Investigator Agent  policy-violation detection (policy_eligibility), claim-history anomaly detection, citations
  Forensic Agent      anomaly detection (serial, chronology, history, identity, injection), citations
  Combined system     the adjudicated final decision (four classes)
plus one task all four share: the claim decision each one recommends (the agents' recommendations are advisory).

An agent's execution status is recorded separately from the correctness of its output: "completed",
"output_invalid" (structured output rejected by Pydantic), "failed" (model/MCP/A2A/configuration error),
"incomplete" (not started, or its validated report was withheld because its workflow did not finish).
Only "completed" outputs are scored.
"""
from __future__ import annotations

from typing import Any

from claimsleuth.documents import quote_appears
from claimsleuth.evaluation.dataset import EvalCase
from claimsleuth.rules import normalise_serial, parse_money
from claimsleuth.schemas import ClaimRunBundle, EvidenceDocument, ExecutionStatus

APPROACHES = ("evidence", "investigator", "forensic", "combined")
AGENT_KEYS = {"evidence": "evidence_agent", "investigator": "investigator_agent", "forensic": "forensic_agent"}
SERIOUS = ("MEDIUM", "HIGH")


def _norm(field: str, value: Any) -> str:
    text = str(value or "").strip()
    if field == "repair_cost":
        money = parse_money(text)
        return f"{money:.2f}" if money is not None else text.casefold()
    if field == "serial_number":
        return normalise_serial(text)
    return " ".join(text.casefold().split())


def _citations_valid(citations: list, docs: dict[str, EvidenceDocument]) -> tuple[int, int]:
    valid = sum(1 for c in citations if c.doc_id in docs and quote_appears(docs[c.doc_id], c.line, c.quote))
    return valid, len(citations)


def _adk_status(bundle: ClaimRunBundle, agent: str) -> str:
    adk = bundle.adk
    if adk is None:
        return "failed"
    if adk.status == ExecutionStatus.COMPLETED:
        return "completed"
    if any(f"{agent} output failed validation" in e for e in adk.errors):
        return "output_invalid"
    run = next((r for r in adk.agent_runs if r.agent == agent), None)
    if run is None:
        return "failed" if adk.status == ExecutionStatus.NOT_CONFIGURED else "incomplete"
    if run.errors or any(e.startswith(f"{agent}:") for e in adk.errors):
        return "failed"
    return "incomplete"        # the agent finished, but the ADK workflow did not, so its report was not released


def _crew_status(bundle: ClaimRunBundle) -> str:
    crew = bundle.crewai
    if crew is None:
        return "failed"
    if crew.status == ExecutionStatus.COMPLETED and crew.report is not None:
        return "completed"
    if any("failed validation" in e for e in crew.errors):
        return "output_invalid"
    return "failed"


def _requests(bundle: ClaimRunBundle, live: bool) -> tuple[dict[str, int] | None, str]:
    if not live:
        return {"adk:evidence_agent": 0, "adk:investigator_agent": 0, "crewai:forensic_agent": 0}, "offline"
    finished = next((e for e in bundle.audit if e.event == "run_finished"), None)
    data = (finished.data.get("gemini_requests") if finished else None)
    return (dict(data) if isinstance(data, dict) else None), "ledger"


def _forensic_detectors(report) -> dict[str, bool]:
    findings = report.findings
    cats = {f.category for f in findings if f.severity.value in SERIOUS}
    patterns = {p.pattern for p in report.historical_pattern_findings}
    serial_inconsistent = any(c.result == "INCONSISTENT" for c in report.serial_number_checks)
    chrono_inconsistent = any(c.result == "INCONSISTENT" for c in report.chronology_checks)
    name_flag = any(f.category in ("SUSPICIOUS_DETAIL", "OTHER") and f.severity.value in SERIOUS
                    and any(w in f.description.lower() for w in ("name", "client", "customer"))
                    for f in findings)
    suspicious = "SUSPICIOUS_DETAIL" in cats
    return {
        "serial_mismatch": serial_inconsistent or "SERIAL_INCONSISTENCY" in cats,
        "history_other_claimant": "SAME_SERIAL_OTHER_CLAIMANT" in patterns or "CLAIM_HISTORY_PATTERN" in cats,
        "repeat_claims": bool(patterns & {"REPEAT_CLAIMS", "PRIOR_CLAIM_SAME_CLAIMANT"})
                         or "CLAIM_HISTORY_PATTERN" in cats,
        "chronology": chrono_inconsistent or "DATE_CONFLICT" in cats,
        "identity_mismatch": name_flag,
        "prompt_injection": suspicious,
        "data_access_request": suspicious,
    }


def score_case(case: EvalCase, bundle: ClaimRunBundle | None, wall_ms: float | None, docs: list[EvidenceDocument],
               live: bool, run_error: str | None = None) -> dict[str, Any]:
    gt = case.ground_truth
    row: dict[str, Any] = {
        "case_id": case.case_id, "category": gt.category, "source": case.source,
        "expected_decision": gt.expected_decision.value, "policy_violation": gt.policy_violation,
        "missing_evidence": list(gt.missing_evidence), "anomalies": list(gt.anomalies),
        "run_id": bundle.run_id if bundle else None, "run_error": run_error,
    }
    if bundle is None:
        for a in APPROACHES:
            row[a] = {"status": "failed", "error": run_error}
        return row
    doc_map = {d.doc_id: d for d in docs}
    requests, req_source = _requests(bundle, live)
    adk_runs = {r.agent: r for r in (bundle.adk.agent_runs if bundle.adk else [])}

    def adk_common(key: str) -> dict[str, Any]:
        agent = AGENT_KEYS[key]
        run = adk_runs.get(agent)
        tokens = (None if run is None or run.prompt_tokens is None and run.completion_tokens is None
                  else (run.prompt_tokens or 0) + (run.completion_tokens or 0))
        return {"status": _adk_status(bundle, agent), "latency_ms": run.latency_ms if run else None,
                "llm_calls": run.llm_calls if run else None, "tokens": tokens,
                "api_requests": requests.get(f"adk:{agent}", 0) if requests is not None else None,
                "model": bundle.adk.model if bundle.adk else None,
                "errors": [e[:300] for e in (run.errors if run else [])]}

    # ---- A. Evidence Agent
    ev = adk_common("evidence")
    report = bundle.adk.evidence_report if bundle.adk else None
    if ev["status"] == "completed" and report is not None:
        matched = 0
        for fact in gt.expected_facts:
            want = _norm(fact.field, fact.value)
            doc_specific = fact.field in ("serial_number", "customer_name")
            if any(f.field == fact.field and _norm(fact.field, f.value) == want
                   and (not doc_specific or f.citation.doc_id == fact.doc_id) for f in report.facts):
                matched += 1
        cites = [f.citation for f in report.facts] + list(report.evidence_citations) + [
            c for f in report.findings for c in f.citations]
        valid, total = _citations_valid(cites, doc_map)
        ev.update(recommendation=report.recommended_decision.value, facts_expected=len(gt.expected_facts),
                  facts_matched=matched, missing_detected=bool(report.missing_information),
                  citations_valid=valid, citations_total=total)
    row["evidence"] = ev

    # ---- B. Investigator Agent
    inv = adk_common("investigator")
    report = bundle.adk.report if bundle.adk else None
    if inv["status"] == "completed" and report is not None:
        cites = list(report.evidence_citations) + [c for f in report.findings for c in f.citations]
        valid, total = _citations_valid(cites, doc_map)
        inv.update(recommendation=report.recommended_decision.value,
                   policy_violation_detected=report.policy_eligibility == "NOT_ELIGIBLE",
                   eligibility=report.policy_eligibility,
                   history_anomaly_detected=any(f.category == "CLAIM_HISTORY_PATTERN" and f.severity.value in SERIOUS
                                                for f in report.findings),
                   citations_valid=valid, citations_total=total)
    row["investigator"] = inv

    # ---- C. Forensic Agent
    crew = bundle.crewai
    crew_run = crew.agent_runs[0] if crew and crew.agent_runs else None
    fo: dict[str, Any] = {
        "status": _crew_status(bundle),
        "latency_ms": crew_run.latency_ms if crew_run else (crew.execution_ms if crew else None),
        "llm_calls": crew_run.llm_calls if crew_run else None,
        "tokens": (None if crew_run is None or crew_run.prompt_tokens is None and crew_run.completion_tokens is None
                   else (crew_run.prompt_tokens or 0) + (crew_run.completion_tokens or 0)),
        "api_requests": requests.get("crewai:forensic_agent", 0) if requests is not None else None,
        "model": crew.model if crew else None, "errors": [e[:300] for e in (crew.errors if crew else [])]}
    if fo["status"] == "completed":
        rep = crew.report
        detectors = _forensic_detectors(rep)
        cites = list(rep.evidence_references) + [c for f in rep.findings for c in f.citations] + [
            c for chk in rep.serial_number_checks + rep.chronology_checks for c in chk.citations]
        valid, total = _citations_valid(cites, doc_map)
        fo.update(recommendation=rep.recommended_outcome.value, anomaly_detected=any(detectors.values()),
                  detected_types=sorted(k for k, v in detectors.items() if v),
                  type_hits={t: detectors[t] for t in gt.anomalies},
                  citations_valid=valid, citations_total=total)
    row["forensic"] = fo

    # ---- D. Combined system (agents + deterministic adjudicator)
    final = bundle.final_report
    total_requests = (sum(requests.values()) if requests is not None else None)
    row["combined"] = {
        "status": "completed" if final is not None else "failed", "latency_ms": wall_ms,
        "final_decision": final.final_decision.value if final else None,
        "recommendation": final.final_decision.value if final else None,
        "decided_by": final.decided_by if final else None,
        "review_status": final.review_status if final else None,
        "requires_human_approval": final.requires_human_approval if final else None,
        "anomaly_detected": (final.final_decision.value == "ESCALATE") if final else None,
        "mcp_calls": len(bundle.mcp.calls) if bundle.mcp else None,
        "api_requests": total_requests, "api_requests_source": req_source,
        "tokens": None if any(row[a].get("tokens") is None for a in ("evidence", "investigator", "forensic"))
        else sum(row[a]["tokens"] for a in ("evidence", "investigator", "forensic")),
    }
    return row
