"""TEST DOUBLE for the Google ADK agents' model — deterministic code, NOT a language model.

`OfflineAdkLlm` is a `google.adk` BaseLlm. Inside a genuine ADK Runner it reads the claim packet from the
request, asks ADK to call the MCP tools (ADK's McpToolset executes them over Streamable HTTP), reads the
function responses that come back, and finally calls `set_model_response` with an EvidenceReport or
InvestigationReport built by fixed rules. Its conclusions are scripted, not reasoning. Results are
labelled model="test-double"; it reports no token usage. It is only used when the ADK LLM mode is
explicitly "test-double" (offline development); the default ADK mode still requires Gemini credentials.
"""
from __future__ import annotations

import calendar
import json
import re
from datetime import date
from typing import Any, AsyncGenerator, Literal

from google.adk.models._capabilities import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from claimsleuth.adk_service.callbacks import parse_mcp_response
from claimsleuth.obedient_double import OUT_OF_SCOPE_CLAIMANT, obey, orders
from claimsleuth.packet import Packet, cite, iso_date
from claimsleuth.security import detect_injection

MODEL_NAME = "test-double"
PROVIDER = "offline-test-double"
_NEGATED = re.compile(r"\b(no|not|without|free of)\b[\w\s,-]{0,20}$")


def _add_months(start: date, months: int) -> date:
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def packet_findings(packet: Packet) -> tuple[list[dict], list[str]]:
    """Conflicts, suspicious text and gaps visible in the packet itself: (findings, missing information)."""
    inv, rep = packet.doc_of("INVOICE"), packet.doc_of("DIAGNOSTIC")
    findings, missing = [], []
    for doc, label in ((inv, "purchase invoice"), (rep, "diagnostic / repair report")):
        if doc is None:
            missing.append(label)
            findings.append({"category": "EVIDENCE_GAP", "severity": "MEDIUM", "description": f"No {label} submitted.",
                             "citations": []})
    for doc, name, label in ((inv, "purchase_date", "purchase date"), (rep, "repair_cost", "repair cost"),
                             (rep, "diagnosis", "technician diagnosis")):
        if doc is not None and packet.get(doc, name) is None:
            missing.append(f"{label} ({doc})")
    a, b = packet.get(inv, "serial"), packet.get(rep, "serial")
    if a and b and a[0].strip().upper() != b[0].strip().upper():
        findings.append({"category": "SERIAL_INCONSISTENCY", "severity": "HIGH",
                         "description": f"Invoice serial {a[0]} differs from repair-report serial {b[0]}.",
                         "citations": [cite(inv, a), cite(rep, b)]})
    incident = iso_date(packet.form.get("incident_date"))
    purchase, inspection = packet.get(inv, "purchase_date"), packet.get(rep, "inspection_date")
    if purchase and incident and (p := iso_date(purchase[0])) and p > incident:
        findings.append({"category": "DATE_CONFLICT", "severity": "HIGH", "citations": [cite(inv, purchase)],
                         "description": f"Incident {incident} is before the purchase date {p}."})
    if inspection and incident and (i := iso_date(inspection[0])) and i < incident:
        findings.append({"category": "DATE_CONFLICT", "severity": "HIGH", "citations": [cite(rep, inspection)],
                         "description": f"Inspection {i} is before the reported incident {incident}."})
    n1, n2 = packet.get(inv, "name"), packet.get(rep, "name")
    if n1 and n2 and n1[0].strip().casefold() != n2[0].strip().casefold():
        findings.append({"category": "SUSPICIOUS_DETAIL", "severity": "MEDIUM", "citations": [cite(inv, n1), cite(rep, n2)],
                         "description": f"Invoice customer '{n1[0]}' differs from repair-report client '{n2[0]}'."})
    if detect_injection(packet.form.get("claimant_statement", "")) or detect_injection(packet.form.get("reported_issue", "")):
        findings.append({"category": "SUSPICIOUS_DETAIL", "severity": "HIGH", "citations": [],
                         "description": "The claim text contains instructions aimed at the claim system."})
    for doc, lines in packet.lines.items():
        for n, line in sorted(lines.items()):
            if detect_injection(line):
                findings.append({"category": "SUSPICIOUS_DETAIL", "severity": "HIGH",
                                 "citations": [{"doc_id": doc, "line": n, "quote": line.strip()}],
                                 "description": f"{doc} line {n} contains instructions aimed at the claim system."})
    return findings, missing


def _decide(findings: list[dict], missing: list[str]) -> tuple[str, float]:
    if any(f["severity"] == "HIGH" for f in findings):
        return "ESCALATE", 0.8
    if missing:
        return "REQUEST_EVIDENCE", 0.6
    return "APPROVE", 0.75


class OfflineAdkLlm(BaseLlm):
    model: str = MODEL_NAME
    role: Literal["evidence", "investigator"]

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=False)   # same path as the Gemini API: set_model_response

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        texts, responses, own_calls = [], [], 0
        for content in llm_request.contents or []:
            for part in content.parts or []:
                if part.text:
                    texts.append(part.text)
                if part.function_call and content.role == "model":
                    own_calls += 1
                if part.function_response:
                    responses.append((part.function_response.name, part.function_response.response))
        packet = Packet("\n".join(texts))
        obeyed = orders("\n".join(texts), self.role)
        plan = self._plan(packet)
        if "G" in obeyed and self.role == "investigator":        # the obedient double asks for another customer's history
            plan.append(("claim_history_lookup", {"claimant_id": OUT_OF_SCOPE_CLAIMANT}))
        if own_calls < len(plan):
            name, args = plan[own_calls]
        else:
            name, args = "set_model_response", obey(self._report(packet, responses), obeyed)
        yield LlmResponse(content=types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name=name, args=args))]))

    def _plan(self, packet: Packet) -> list[tuple[str, dict]]:
        plan = [("warranty_policy_search", {"policy_id": packet.form.get("policy_id")})]
        if self.role == "investigator":
            serial = packet.get(packet.doc_of("INVOICE"), "serial") or packet.get(packet.doc_of("DIAGNOSTIC"), "serial")
            if serial:
                plan.append(("claim_history_lookup", {"serial_number": serial[0].strip()}))
            plan.append(("claim_history_lookup", {"claimant_id": packet.form.get("claimant_id")}))
        return plan

    @staticmethod
    def _results(responses: list[tuple[str, Any]], tool: str) -> list[dict]:
        out = []
        for name, response in responses:
            ok, result, _ = parse_mcp_response(response)
            if name == tool and ok and isinstance(result, dict):
                out.append(result)
        return out

    def _report(self, packet: Packet, responses: list[tuple[str, Any]]) -> dict:
        claim_id = packet.form.get("claim_id", "UNKNOWN")
        findings, missing = packet_findings(packet)
        policy = next((m for r in self._results(responses, "warranty_policy_search") for m in r.get("matches", [])), None)
        policy_refs = []
        if policy:
            for key in ("requires_original_receipt", "requires_diagnostic_report"):
                policy_refs.append({"source": f"{policy['source']}/{key}", "value": str(policy["terms"][key])})
        citations = [c for f in findings for c in f["citations"]]
        if self.role == "evidence":
            facts = []
            for doc in packet.fields:
                for name, label in (("purchase_date", "purchase_date"), ("serial", "serial_number"),
                                    ("name", "customer_name"), ("inspection_date", "inspection_date"),
                                    ("diagnosis", "diagnosis"), ("repair_cost", "repair_cost")):
                    field = packet.get(doc, name)
                    if field:
                        conflicting = name == "serial" and any(f["category"] == "SERIAL_INCONSISTENCY" for f in findings)
                        facts.append({"field": label, "value": field[0], "citation": cite(doc, field),
                                      "status": "CONFLICTING" if conflicting else "VERIFIED"})
            decision, confidence = _decide(findings, missing)
            return {"claim_id": claim_id, "facts": facts, "findings": findings,
                    "evidence_citations": [f["citation"] for f in facts] + citations, "policy_references": policy_refs,
                    "missing_information": missing, "confidence": confidence, "recommended_decision": decision,
                    "reasoning_summary": f"{len(facts)} facts cited; {len(findings)} finding(s). Rule-based test "
                                         "double, not LLM reasoning."}

        # investigator: coverage questions, history, anomalies
        eligibility, reason = "UNDETERMINED", "Purchase date, diagnosis or policy terms unavailable."
        inv, rep = packet.doc_of("INVOICE"), packet.doc_of("DIAGNOSTIC")
        purchase, diagnosis = packet.get(inv, "purchase_date"), packet.get(rep, "diagnosis")
        incident = iso_date(packet.form.get("incident_date"))
        if policy and purchase and incident and (p := iso_date(purchase[0])):
            expiry = _add_months(p, int(policy["terms"]["duration_months"]))
            policy_refs.append({"source": f"{policy['source']}/duration_months",
                                "value": str(policy["terms"]["duration_months"])})
            if not p <= incident <= expiry:
                eligibility, reason = "NOT_ELIGIBLE", f"Incident {incident} is outside the coverage period {p} to {expiry}."
            elif diagnosis:
                hit = next(((i, x) for i, x in enumerate(policy["terms"]["exclusions"])
                            if (pos := diagnosis[0].lower().find(x.lower())) != -1
                            and not _NEGATED.search(diagnosis[0].lower()[:pos])), None)
                if hit:
                    eligibility, reason = "NOT_ELIGIBLE", f"Diagnosis matches the policy exclusion '{hit[1]}'."
                    policy_refs.append({"source": f"{policy['source']}/exclusions/{hit[0]}", "value": hit[1]})
                    citations.append(cite(rep, diagnosis))
                else:
                    eligibility, reason = "ELIGIBLE", f"Incident inside the coverage period ({p} to {expiry}); no exclusion matched."
        history_refs = []
        records = {r["claim_id"]: (r, s) for res in self._results(responses, "claim_history_lookup")
                   for r, s in zip(res.get("records", []), res.get("record_sources", []))}
        others = [(r, s) for r, s in records.values() if r["claimant_id"] != packet.form.get("claimant_id")]
        if others:
            findings.append({"category": "CLAIM_HISTORY_PATTERN", "severity": "HIGH", "citations": [],
                             "description": "Device serial previously claimed by another claimant: "
                                            + ", ".join(r["claim_id"] for r, _ in others)})
        history_refs = [{"source": s, "value": f"{r['claim_id']} {r['claimant_id']} {r['outcome']}"} for r, s in records.values()]
        decision, confidence = _decide(findings, missing)
        if decision == "APPROVE" and eligibility == "NOT_ELIGIBLE":
            decision, confidence = "REJECT", 0.75
        elif decision == "APPROVE" and eligibility == "UNDETERMINED":
            decision, confidence = "ESCALATE", 0.5
        return {"claim_id": claim_id, "findings": findings, "evidence_citations": citations,
                "policy_references": policy_refs, "history_references": history_refs,
                "policy_eligibility": eligibility, "policy_eligibility_reason": reason,
                "missing_information": missing, "confidence": confidence, "recommended_decision": decision,
                "reasoning_summary": f"{reason} {len(findings)} finding(s). Rule-based test double, not LLM reasoning."}


def offline_models() -> dict[str, OfflineAdkLlm]:
    from claimsleuth.adk_service.agents import EVIDENCE_AGENT, INVESTIGATOR_AGENT
    return {EVIDENCE_AGENT: OfflineAdkLlm(role="evidence"), INVESTIGATOR_AGENT: OfflineAdkLlm(role="investigator")}
