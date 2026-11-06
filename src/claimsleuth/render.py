"""Markdown rendering of results (the JSON form is always model_dump_json)."""
from __future__ import annotations

import json

from claimsleuth.observability.outcome import incomplete_text, outcome_of
from claimsleuth.rules import format_inr
from claimsleuth.schemas import AgentSystemResult, BaselineResult


def baseline_markdown(r: BaselineResult) -> str:
    lines = [
        "# DETERMINISTIC BASELINE",
        "",
        f"- Run: `{r.run_id}` · Claim: `{r.claim_id}` · Status: **{r.status.value}**",
        f"- Decision: **{r.decision.value if r.decision else '—'}**"
        f" · Human approval required: {'yes' if r.requires_human_approval else 'no'}",
        f"- Execution time: {r.execution_ms:.1f} ms (no LLM)",
    ]
    if r.settlement:
        lines.append(f"- Payout (deterministic): **{format_inr(r.settlement.payout)} ({r.settlement.currency})**")
    if r.errors:
        lines += ["", "## Errors", *[f"- {e}" for e in r.errors]]
    if r.decision_reasons:
        lines += ["", "## Reasons", *[f"- {x}" for x in r.decision_reasons]]
    if r.checks:
        lines += ["", "## Rule checks", "", "| Check | Outcome | Detail | Evidence |", "|---|---|---|---|"]
        lines += [f"| {c.name} | {c.outcome.value} | {c.detail.replace('|', '/')} | {', '.join(c.evidence_ids)} |"
                  for c in r.checks]
    if r.missing_information:
        lines += ["", "## Missing information", *[f"- {m}" for m in r.missing_information]]
    if r.evidence_refs:
        lines += ["", "## Evidence", "", "| ID | Source | Field | Quote |", "|---|---|---|---|"]
        lines += [f"| {e.evidence_id} | {e.doc_id}{'#L' + str(e.line) if e.line else ''} | {e.field} | "
                  f"`{e.quote.replace('|', '/')}` |" for e in r.evidence_refs]
    if r.guardrail_events:
        lines += ["", "## Guardrail events"]
        lines += [f"- {g.kind} in {g.source}: {g.action} ({', '.join(g.matched)})" for g in r.guardrail_events]
    return "\n".join(lines) + "\n"


def adk_markdown(r: AgentSystemResult) -> str:
    lines = [
        "# GOOGLE ADK RESULT",
        "",
        f"- Run: `{r.run_id}` · Claim: `{r.claim_id}` · Status: **{r.status.value}**",
        f"- Model: {r.model or '—'} · Prompt variant: {r.prompt_variant or '—'} · ADK session: `{r.session_id or '—'}`",
        f"- Execution time: {r.execution_ms:.0f} ms" if r.execution_ms is not None else "- Execution time: —",
        f"- Tokens: prompt {r.usage.prompt_tokens}, completion {r.usage.completion_tokens}",
    ]
    if r.errors:
        lines += ["", "## Errors", *[f"- {e}" for e in r.errors]]
    for title, rep in (("Evidence Agent", r.evidence_report), ("Investigator Agent (final ADK report)", r.report)):
        if rep is None:
            continue
        lines += ["", f"## {title}",
                  f"- Recommended decision: **{rep.recommended_decision.value}** · confidence {rep.confidence:.2f}"]
        if hasattr(rep, "policy_eligibility"):
            lines.append(f"- Policy eligibility: {rep.policy_eligibility} — {rep.policy_eligibility_reason}")
        lines.append(f"- Reasoning: {rep.reasoning_summary}")
        if getattr(rep, "facts", None):
            lines += ["", "| Fact | Value | Status | Citation |", "|---|---|---|---|"]
            lines += [f"| {f.field} | {f.value} | {f.status} | {f.citation.doc_id}#L{f.citation.line} |" for f in rep.facts]
        if rep.findings:
            lines += ["", "Findings:"]
            lines += [f"- [{f.severity.value}] {f.category}: {f.description} "
                      f"({', '.join(f'{c.doc_id}#L{c.line}' for c in f.citations) or 'no citation'})" for f in rep.findings]
        refs = list(rep.policy_references) + list(getattr(rep, "history_references", []))
        if refs:
            lines += ["", "References:", *[f"- `{x.source}` = {x.value}" for x in refs]]
        if rep.missing_information:
            lines += ["", "Missing information:", *[f"- {m}" for m in rep.missing_information]]
    if r.unsupported_references:
        lines += ["", "## Unsupported references (failed verification)", *[f"- {u}" for u in r.unsupported_references]]
    if r.agent_runs:
        lines += ["", "## Agent execution", "", "| Agent | Latency ms | LLM calls | Tool calls | Prompt tok | Completion tok | Errors |",
                  "|---|---|---|---|---|---|---|"]
        lines += [f"| {a.agent} | {a.latency_ms} | {a.llm_calls} | {a.tool_calls} | {a.prompt_tokens} | "
                  f"{a.completion_tokens} | {len(a.errors)} |" for a in r.agent_runs]
    if r.tool_calls:
        lines += ["", "## Tool calls", "", "| Agent | Tool | Protocol | Arguments | Status | Latency ms |",
                  "|---|---|---|---|---|---|"]
        lines += [f"| {c.agent} | {c.tool} | {c.protocol} | "
                  f"{'`' + json.dumps(c.arguments) + '`' if c.protocol == 'mcp' else '(structured report, shown above)'} | "
                  f"{c.status}{': ' + c.error[:80] if c.error else ''} | {c.latency_ms} |" for c in r.tool_calls]
    return "\n".join(lines) + "\n"


def crewai_markdown(r: AgentSystemResult) -> str:
    rep = r.report
    offline = r.model == "test-double"
    lines = [
        "# CREWAI RESULT",
        "",
        *(["> **Offline run: the model is a deterministic TEST DOUBLE, not a language model.** "
           "Agent, Task, Crew.kickoff() and the MCP calls are real; the conclusions are scripted rules.", ""]
          if offline else []),
        f"- Run: `{r.run_id}` · Claim: `{r.claim_id}` · Status: **{r.status.value}**",
        f"- Model: {r.model or '—'} · Provider: {rep.provider if rep else '—'}",
        f"- Execution time: {r.execution_ms:.0f} ms" if r.execution_ms is not None else "- Execution time: —",
    ]
    if r.errors:
        lines += ["", "## Errors", *[f"- {e}" for e in r.errors]]
    if rep is not None:
        lines += ["", "## Forensic Agent",
                  f"- Recommended outcome: **{rep.recommended_outcome.value}** · confidence {rep.confidence:.2f} "
                  "(recommendation only; CrewAI cannot authorise a payout)",
                  f"- Reasoning: {rep.reasoning_summary}"]
        for title, checks in (("Serial-number checks", rep.serial_number_checks), ("Chronology checks", rep.chronology_checks)):
            if checks:
                lines += ["", f"{title}:", "", "| Check | Result | Detail | Citations |", "|---|---|---|---|"]
                lines += [f"| {c.check} | {c.result} | {c.detail} | "
                          f"{', '.join(f'{x.doc_id}#L{x.line}' for x in c.citations) or '—'} |" for c in checks]
        if rep.findings:
            lines += ["", "Findings:"]
            lines += [f"- [{f.severity.value}] {f.category}: {f.description} "
                      f"({', '.join(f'{c.doc_id}#L{c.line}' for c in f.citations) or 'no citation'})" for f in rep.findings]
        if rep.historical_pattern_findings:
            lines += ["", "Claim history (via MCP):"]
            lines += [f"- {p.pattern}: {p.detail} " + " ".join(f"`{x.source}`" for x in p.references)
                      for p in rep.historical_pattern_findings]
        for title, items in (("Missing information", rep.missing_information), ("Uncertainty", rep.uncertainty)):
            if items:
                lines += ["", f"{title}:", *[f"- {m}" for m in items]]
    if r.agent_runs:
        a = r.agent_runs[0]
        lines += ["", "## Execution", f"- Agent `{a.agent}` (crewai): {a.latency_ms} ms, {a.llm_calls} model call(s), "
                  f"{a.tool_calls} tool call(s), tokens {a.prompt_tokens}/{a.completion_tokens}"]
    if r.tool_calls:
        lines += ["", "| Tool | Protocol | Arguments | Status | Latency ms |", "|---|---|---|---|---|"]
        lines += [f"| {c.tool} | {c.protocol} | `{json.dumps(c.arguments)}` | {c.status} | {c.latency_ms} |"
                  for c in r.tool_calls]
    return "\n".join(lines) + "\n"


def final_markdown(bundle) -> str:
    """The side-by-side comparison and the adjudicated final report of one integrated run."""
    f = bundle.final_report
    lines = ["# ClaimSleuth integrated run", "",
             f"- Run: `{bundle.run_id}` · Claim: `{bundle.claim_id}`"]
    offline = [k for k, m in bundle.workflow_metadata.items() if m.llm_mode == "test-double"]
    if offline:
        lines += ["", f"> **Offline run:** {', '.join(offline)} used a deterministic TEST DOUBLE instead of a language "
                      "model. Framework execution, A2A and MCP are real; agent conclusions are scripted rules."]
    lines += ["", "## Side-by-side comparison", "",
              "| System | Status | Decision | Confidence | Execution time | Evidence |", "|---|---|---|---|---|---|"]
    for r in (f.comparison.rows if f else []):
        ms = f"{r.execution_ms:.0f} ms" if r.execution_ms is not None else "—"
        lines.append(f"| {r.system} | {r.status.value} | {r.decision_label} | {r.confidence_label} | {ms} | "
                     f"{r.evidence_summary} |")
    if f is None:
        return "\n".join(lines + ["", "No final report was produced."]) + "\n"
    lines += ["", "## Final adjudicated report", "",
              f"- **Final decision: {f.final_decision.value}** (decided by {f.decided_by}; deterministic rules: "
              f"{f.deterministic_decision.value if f.deterministic_decision else '—'})",
              f"- Payout: **{format_inr(f.payout.payout)} ({f.payout.currency})**" if f.payout else "- Payout: none",
              f"- Run status: **{outcome_of(bundle).run_status.value}**",
              f"- Human approval required: {'yes' if f.requires_human_approval else 'no'} ({f.approval_status})",
              *([f"- **{incomplete_text(f)}:** " + "; ".join(f.incomplete_reasons)]
                if f.review_status in ("PARTIAL", "NONE") else []),
              "", "Rationale:", *[f"- {x}" for x in f.rationale]]
    for title, items in (("Agreements", f.agreements), ("Disagreements", f.disagreements),
                         ("Unsupported claims (ignored)", f.unsupported_claims), ("Policy references", f.policy_references)):
        if items:
            lines += ["", f"{title}:", *[f"- {x}" for x in items]]
    failed = [c for c in f.rule_checks if c.outcome.value == "FAIL"]
    if failed:
        lines += ["", "Failed rule checks:", *[f"- {c.name}: {c.detail} ({', '.join(c.evidence_ids) or 'no evidence id'})"
                                               for c in failed]]
    lines += ["", "## Messages (A2A)", *[f"- {m.timestamp.isoformat()} {m.message_type} {m.sender} → {m.recipient} "
                                         f"(`{m.message_id}`)" for m in bundle.messages]]
    if bundle.mcp is not None:
        lines += ["", "## MCP tool calls (server log)", *[f"- {c.started_at.isoformat()} {c.tool} by {c.caller} "
                                                          f"`{json.dumps(c.arguments)}` {c.status} {c.latency_ms} ms"
                                                          for c in bundle.mcp.calls]]
    lines += ["", "## Audit trail", *[f"- {e.ts.isoformat()} {e.event} [{e.actor}] {e.detail}" for e in bundle.audit]]
    return "\n".join(lines) + "\n"
