"""Deterministic final adjudicator — never an LLM, never an agent.

Rules, in order:
 1. Recompute the deterministic decision from the claim, documents, policy and SQLite history with the
    rules engine (coverage window, exclusions, serial/date/history checks, Decimal settlement).
 2. The baseline workflow result must exist and match the recomputation; otherwise ESCALATE
    (BASELINE_UNAVAILABLE / INTEGRITY_ESCALATION).
 3. Agent findings (ADK Evidence + Investigator, CrewAI Forensic) count only when verified:
    document citations must quote the cited line; history references must be sources that the agent's
    own recorded MCP call returned AND that the MCP server's log confirms for this run and claim.
    Everything else is listed as unsupported and ignored.
 4. Deterministic ESCALATE or REJECT stand (agents can neither approve coverage nor lift an escalation;
    a forensic APPROVE is "no forensic objection", never a coverage approval).
 5. Deterministic REQUEST_EVIDENCE stands unless an agent raises a verified HIGH concern -> ESCALATE.
 6. Deterministic APPROVE needs at least one completed agent review and no verified HIGH concern;
    otherwise ESCALATE (INSUFFICIENT_REVIEW / CONFLICT_ESCALATION). Unverified agent objections are
    recorded as disagreements, not acted on.
 7. Money only from the rules engine's Decimal settlement, and only on a final APPROVE. REJECT and
    ESCALATE always need a human; APPROVE at or above the threshold needs a human.
"""
from __future__ import annotations

from typing import Any

from claimsleuth import config
from claimsleuth.baseline import evaluate
from claimsleuth.documents import quote_appears
from claimsleuth.schemas import (AgentFinding, AgentSystemResult, BaselineResult, CheckOutcome, Citation, ClaimInput,
                                 ComparisonRow, Decision, EvidenceDocument, ExecutionStatus, FinalClaimReport,
                                 FinalComparison, ForensicsReport, HistoryRecord, InvestigationReport, MCPResult,
                                 PolicyTerms)


def _citation_ok(c: Citation, docs: dict[str, EvidenceDocument]) -> bool:
    return c.doc_id in docs and quote_appears(docs[c.doc_id], c.line, c.quote)


def _confirmed_sources(result: AgentSystemResult, mcp: MCPResult | None) -> set[str]:
    """record_sources that this agent's MCP calls returned AND that the MCP server logged for the run."""
    if mcp is None:
        return set()
    logged = [c for c in mcp.calls if c.status == "ok"]
    out: set[str] = set()
    for call in result.tool_calls:
        if call.protocol != "mcp" or call.status != "ok" or not isinstance(call.result, dict):
            continue
        if any(m.tool == call.tool and all(m.arguments.get(k) == v for k, v in call.arguments.items()) for m in logged):
            out.update(s for s in call.result.get("record_sources", []) if isinstance(s, str))
            out.update(s for s in call.result.get("sources", []) if isinstance(s, str))
    return out


def _review(name: str, result: AgentSystemResult | None, docs: dict[str, EvidenceDocument],
            mcp: MCPResult | None) -> dict[str, Any]:
    """Verified and unsupported findings of one agent system."""
    out: dict[str, Any] = {"name": name, "available": False, "decision": None, "verified_high": [],
                           "unsupported": [], "findings": 0}
    if result is None or result.status != ExecutionStatus.COMPLETED or result.report is None:
        out["reason"] = "no result" if result is None else f"status {result.status.value}"
        return out
    out["available"] = True
    reports: list[Any] = [result.report] + ([result.evidence_report] if result.evidence_report else [])
    sources = _confirmed_sources(result, mcp)
    rep = result.report
    out["decision"] = rep.recommended_outcome if isinstance(rep, ForensicsReport) else rep.recommended_decision
    out["confidence"] = rep.confidence
    history_refs = (list(rep.history_references) if isinstance(rep, InvestigationReport)
                    else [r for p in rep.historical_pattern_findings for r in p.references])
    history_ok = bool(history_refs) and all(r.source in sources for r in history_refs)
    for r in history_refs:
        if r.source not in sources:
            out["unsupported"].append(f"{name}: history reference {r.source} not confirmed by MCP records")
    for report in reports:
        findings: list[AgentFinding] = list(report.findings)
        out["findings"] += len(findings)
        cites = list(getattr(report, "evidence_citations", []) or getattr(report, "evidence_references", []))
        cites += [fact.citation for fact in getattr(report, "facts", [])]
        for c in cites:
            if not _citation_ok(c, docs):
                out["unsupported"].append(f"{name}: citation {c.doc_id}#L{c.line} '{c.quote[:60]}' not in the document")
        for f in findings:
            bad = [c for c in f.citations if not _citation_ok(c, docs)]
            for c in bad:
                out["unsupported"].append(f"{name}: finding '{f.description[:60]}' cites {c.doc_id}#L{c.line}, "
                                          "which does not contain the quote")
            verified = (f.citations and not bad) or (f.category == "CLAIM_HISTORY_PATTERN" and history_ok)
            if f.severity.value == "HIGH":
                if verified:
                    out["verified_high"].append(f"{name}: [{f.category}] {f.description}")
                else:
                    out["unsupported"].append(f"{name}: HIGH finding without verified evidence ignored: {f.description[:80]}")
    out["unsupported"] += [f"{name}: {u}" for u in result.unsupported_references]
    return out


def _row(system: str, result: Any, review: dict[str, Any] | None = None) -> ComparisonRow:
    if system == "DETERMINISTIC_BASELINE":
        ok = result is not None and result.status == ExecutionStatus.COMPLETED
        return ComparisonRow(system=system, status=result.status if result else ExecutionStatus.FAILED,
                             decision=result.decision if ok else None,
                             decision_label=result.decision.value if ok else "unavailable", confidence=None,
                             confidence_label="deterministic", execution_ms=result.execution_ms if result else None,
                             evidence_count=len(result.evidence_refs) if result else 0,
                             evidence_summary=f"{len(result.checks)} rule checks" if result else "no result")
    if system == "MCP_TOOLS":
        calls = result.calls if result else []
        return ComparisonRow(system=system, status=result.status if result else ExecutionStatus.FAILED, decision=None,
                             decision_label="Tool results, not a decision", confidence=None, confidence_label="N/A",
                             execution_ms=sum(c.latency_ms for c in calls) if calls else None, evidence_count=len(calls),
                             evidence_summary=", ".join(sorted({f"{c.tool} by {c.caller}" for c in calls})) or "no calls")
    available = review is not None and review["available"]
    label = "" if result is None or result.model != "test-double" else " (test-double)"
    return ComparisonRow(system=system, status=result.status if result else ExecutionStatus.FAILED,
                         decision=review["decision"] if available else None,
                         decision_label=(review["decision"].value + " (recommendation)") if available else "unavailable",
                         confidence=review.get("confidence") if available else None,
                         confidence_label=f"{review['confidence']:.2f}{label}" if available else "N/A",
                         execution_ms=result.execution_ms if result else None,
                         evidence_count=review["findings"] if available else 0,
                         evidence_summary=(f"{review['findings']} finding(s), {len(review['verified_high'])} verified HIGH, "
                                           f"{len(review['unsupported'])} unsupported") if available else
                                          (review.get("reason", "no result") if review else "no result"))


def adjudicate(run_id: str, claim: ClaimInput, documents: list[EvidenceDocument], policy: PolicyTerms | None,
               history: list[HistoryRecord], baseline: BaselineResult | None, adk: AgentSystemResult | None,
               crewai: AgentSystemResult | None, mcp: MCPResult | None) -> FinalClaimReport:
    docs = {d.doc_id: d for d in documents}
    rules = evaluate(claim, documents, policy, history, run_id)          # step 1: recompute
    d = rules.decision
    rationale = [f"Deterministic rules decide {d.value}: " + " ".join(rules.decision_reasons)]
    agreements, disagreements, unsupported = [], [], []

    adk_review = _review("GOOGLE_ADK", adk, docs, mcp)
    crew_review = _review("CREWAI", crewai, docs, mcp)
    reviews = [r for r in (adk_review, crew_review) if r["available"]]
    for r in (adk_review, crew_review):
        unsupported += r["unsupported"]
        if not r["available"]:
            rationale.append(f"{r['name']} result unavailable ({r.get('reason')}); not used.")
    concerns = [c for r in reviews for c in r["verified_high"]]

    for r in reviews:
        text = f"{r['name']} recommends {r['decision'].value}; deterministic rules decide {d.value}"
        (agreements if r["decision"] == d else disagreements).append(text)
    if len(reviews) == 2 and reviews[0]["decision"] != reviews[1]["decision"]:
        disagreements.append(f"GOOGLE_ADK ({reviews[0]['decision'].value}) and CREWAI ({reviews[1]['decision'].value}) disagree")

    injection = any(g.kind == "prompt_injection" for g in rules.guardrail_events)
    # step 2: the baseline workflow's own result must exist and match the recomputation
    if baseline is None or baseline.status != ExecutionStatus.COMPLETED:
        final, decided_by = Decision.ESCALATE, "BASELINE_UNAVAILABLE"
        rationale.append("The deterministic baseline workflow did not complete; the run is escalated.")
    elif baseline.decision != d or (baseline.settlement and rules.settlement
                                    and baseline.settlement.payout != rules.settlement.payout):
        final, decided_by = Decision.ESCALATE, "INTEGRITY_ESCALATION"
        rationale.append(f"Baseline workflow reported {baseline.decision.value}, recomputation gives {d.value}: escalated.")
    elif d in (Decision.ESCALATE, Decision.REJECT):                       # step 4
        final, decided_by = d, "GUARDRAIL_ESCALATION" if injection else "DETERMINISTIC_RULES"
        if d == Decision.REJECT and any(r["decision"] == Decision.APPROVE for r in reviews):
            rationale.append("Agent APPROVE recommendations are not coverage approvals and cannot override a "
                             "deterministic rejection.")
    elif d == Decision.REQUEST_EVIDENCE:                                  # step 5
        if concerns:
            final, decided_by = Decision.ESCALATE, "CONFLICT_ESCALATION"
            rationale.append("Evidence is incomplete and agents raised verified concerns: " + "; ".join(concerns))
        else:
            final, decided_by = d, "DETERMINISTIC_RULES"
    else:                                                                 # step 6: deterministic APPROVE
        if not reviews:
            final, decided_by = Decision.ESCALATE, "INSUFFICIENT_REVIEW"
            rationale.append("No completed agent review is available to corroborate an approval: escalated.")
        elif concerns:
            final, decided_by = Decision.ESCALATE, "CONFLICT_ESCALATION"
            rationale.append("Rules allow approval, but agents raised verified concerns the rules did not resolve: "
                             + "; ".join(concerns))
        else:
            final, decided_by = Decision.APPROVE, "DETERMINISTIC_RULES"
            if any(r["decision"] != Decision.APPROVE for r in reviews):
                rationale.append("Agent objections without verified evidence were recorded but not acted on.")

    payout = rules.settlement if final == Decision.APPROVE else None      # step 7
    if payout is not None:
        rationale.append(f"Payout from deterministic Decimal arithmetic: {payout.explanation}")
    needs_human = final in (Decision.REJECT, Decision.ESCALATE) or (
        payout is not None and payout.payout >= config.HUMAN_APPROVAL_THRESHOLD)

    policy_refs = []
    if policy is not None:
        policy_refs = [f"policies.json#/{policy.policy_id}/{k}" for k in
                       ("duration_months", "deductible", "max_coverage_limit", "depreciation_rate")]
        exclusion = next((c for c in rules.checks if c.name == "exclusions" and c.outcome == CheckOutcome.FAIL), None)
        if exclusion:
            matched = [i for i, x in enumerate(policy.exclusions) if f"'{x}'" in exclusion.detail]
            policy_refs += [f"policies.json#/{policy.policy_id}/exclusions/{i}" for i in matched]
    rows = [_row("DETERMINISTIC_BASELINE", baseline), _row("GOOGLE_ADK", adk, adk_review),
            _row("CREWAI", crewai, crew_review), _row("MCP_TOOLS", mcp)]
    decisions = {r.decision for r in rows if r.decision is not None}
    comparison = FinalComparison(run_id=run_id, claim_id=claim.claim_id, rows=rows,
                                 decisions_agree=None if sum(r.decision is not None for r in rows) < 2 else len(decisions) == 1)
    return FinalClaimReport(
        run_id=run_id, claim_id=claim.claim_id, final_decision=final, decided_by=decided_by,
        deterministic_decision=d, payout=payout, rule_checks=rules.checks, evidence_references=rules.evidence_refs,
        policy_references=policy_refs, agreements=agreements, disagreements=disagreements,
        unsupported_claims=unsupported, rationale=rationale, requires_human_approval=needs_human,
        approval_status="PENDING" if needs_human else "NOT_REQUIRED", comparison=comparison)
