"""TEST DOUBLE for the CrewAI Forensic Agent's LLM — deterministic code, NOT a language model.

`ForensicTestDoubleLLM` is a `crewai.llms.base_llm.BaseLLM`. CrewAI calls it exactly like a real model
inside a genuine Agent / Task / Crew.kickoff(): it receives CrewAI's ReAct prompt and must answer in
CrewAI's format. It
  1. asks CrewAI to run the MCP tool `claim_history_lookup` (by serial, then by claimant) when the
     prompt lists that tool -- CrewAI executes the call over MCP and returns an "Observation";
  2. then writes a "Final Answer" JSON (ForensicsAnalysis) computed with fixed rules from the claim
     packet and the observations in its own transcript.

Its conclusions are scripted rules, not reasoning. Every result built from it is labelled
model="test-double", provider="offline-test-double". Modes other than "forensic" exist only to test
CrewAI's output validation ("invalid_json", "fabricated_citation", "wrong_claim").
"""
from __future__ import annotations

import json
import re
from typing import Literal

from crewai.events.types.llm_events import LLMCallType
from crewai.llms.base_llm import BaseLLM, llm_call_context

from claimsleuth.packet import Packet, cite
from claimsleuth.packet import iso_date as _iso
from claimsleuth.packet import json_objects as _json_objects
from claimsleuth.packet import transcript as _transcript
from claimsleuth.security import detect_injection

MODEL_NAME = "test-double"
PROVIDER = "offline-test-double"

_TOOL_NAME = re.compile(r"Tool Name: (\S*claim_history_lookup)")


def forensic_analysis(packet: Packet, history: list[dict], history_checked: bool) -> dict:
    """Fixed forensic rules over the packet and the MCP observations (this is the 'model output')."""
    claim_id = packet.form.get("claim_id", "UNKNOWN")
    inv, rep = packet.doc_of("INVOICE"), packet.doc_of("DIAGNOSTIC")
    findings, refs, missing, uncertainty = [], [], [], []
    serial_checks, chronology = [], []

    def finding(category, severity, description, citations=(), uncertainty_note=None):
        findings.append({"category": category, "severity": severity, "description": description,
                         "citations": list(citations), "uncertainty": uncertainty_note})
        refs.extend(c for c in citations if c not in refs)

    # Evidence gaps
    for kind, doc, label in (("INVOICE", inv, "purchase invoice"), ("DIAGNOSTIC", rep, "diagnostic / repair report")):
        if doc is None:
            missing.append(label)
            finding("EVIDENCE_GAP", "MEDIUM", f"No {label} was submitted.")
    for doc, name, label in ((inv, "purchase_date", "purchase date"), (inv, "serial", "invoice serial number"),
                             (rep, "serial", "repair-report serial number"), (rep, "repair_cost", "repair cost"),
                             (rep, "diagnosis", "technician diagnosis")):
        if doc is not None and packet.get(doc, name) is None:
            missing.append(f"{label} ({doc})")
            finding("EVIDENCE_GAP", "MEDIUM", f"The {label} is not stated in {doc}.")

    # Serial numbers
    form_serial = (packet.form.get("serial_number") or "").strip().upper()
    inv_serial, rep_serial = packet.get(inv, "serial"), packet.get(rep, "serial")
    if inv_serial and rep_serial:
        same = inv_serial[0].strip().upper() == rep_serial[0].strip().upper()
        serial_checks.append({"check": "invoice serial vs repair-report serial",
                              "result": "CONSISTENT" if same else "INCONSISTENT",
                              "detail": f"invoice '{inv_serial[0]}', repair report '{rep_serial[0]}'",
                              "citations": [cite(inv, inv_serial), cite(rep, rep_serial)]})
        if not same:
            finding("SERIAL_INCONSISTENCY", "HIGH",
                    f"Invoice serial {inv_serial[0]} differs from repair-report serial {rep_serial[0]}.",
                    [cite(inv, inv_serial), cite(rep, rep_serial)])
    else:
        serial_checks.append({"check": "invoice serial vs repair-report serial", "result": "NOT_EVALUABLE",
                              "detail": "one of the two documents or its serial line is missing", "citations": []})
    if form_serial and inv_serial:
        same = form_serial == inv_serial[0].strip().upper()
        serial_checks.append({"check": "claim-form serial vs invoice serial",
                              "result": "CONSISTENT" if same else "INCONSISTENT",
                              "detail": f"claim form '{form_serial}', invoice '{inv_serial[0]}'",
                              "citations": [cite(inv, inv_serial)]})
        if not same:
            finding("SERIAL_INCONSISTENCY", "HIGH", f"Claim-form serial {form_serial} differs from the invoice.",
                    [cite(inv, inv_serial)])

    # Chronology
    incident = _iso(packet.form.get("incident_date"))
    purchase, inspection = packet.get(inv, "purchase_date"), packet.get(rep, "inspection_date")
    for label, earlier, later, cites, desc in (
            ("purchase date before incident date", _iso(purchase[0]) if purchase else None, incident,
             [cite(inv, purchase)] if purchase else [], "Incident is dated before the purchase."),
            ("incident date before inspection date", incident, _iso(inspection[0]) if inspection else None,
             [cite(rep, inspection)] if inspection else [], "Repair inspection is dated before the reported incident.")):
        if earlier is None or later is None:
            chronology.append({"check": label, "result": "NOT_EVALUABLE", "detail": "a date is missing or unreadable",
                               "citations": cites})
        elif earlier <= later:
            chronology.append({"check": label, "result": "CONSISTENT", "detail": f"{earlier} <= {later}",
                               "citations": cites})
        else:
            chronology.append({"check": label, "result": "INCONSISTENT", "detail": f"{earlier} > {later}",
                               "citations": cites})
            finding("DATE_CONFLICT", "HIGH", f"{desc} ({earlier} > {later})", cites)

    # Contradictions between invoice and repair report
    inv_name, rep_name = packet.get(inv, "name"), packet.get(rep, "name")
    if inv_name and rep_name and inv_name[0].strip().casefold() != rep_name[0].strip().casefold():
        finding("SUSPICIOUS_DETAIL", "MEDIUM", f"Invoice customer '{inv_name[0]}' differs from repair-report client "
                f"'{rep_name[0]}'.", [cite(inv, inv_name), cite(rep, rep_name)])

    # Suspicious instructions in untrusted text
    for source, text in (("claimant statement", packet.form.get("claimant_statement", "")),
                         ("reported issue", packet.form.get("reported_issue", ""))):
        if detect_injection(text):
            finding("SUSPICIOUS_DETAIL", "HIGH", f"The {source} contains instructions aimed at the claim system.")
    for doc, lines in packet.lines.items():
        for n, line in sorted(lines.items()):
            if detect_injection(line):
                finding("SUSPICIOUS_DETAIL", "HIGH", f"{doc} line {n} contains instructions aimed at the claim system.",
                        [{"doc_id": doc, "line": n, "quote": line.strip()}])

    # Claim history (only from MCP observations)
    patterns = []
    if not history_checked:
        patterns.append({"pattern": "NOT_CHECKED", "detail": "claim_history_lookup was not available or failed.",
                         "references": []})
        uncertainty.append("Claim history could not be checked.")
    else:
        claimant = packet.form.get("claimant_id")
        records = {r["claim_id"]: (r, src) for h in history for r, src in zip(h.get("records", []),
                                                                             h.get("record_sources", []))}
        others = [(r, s) for r, s in records.values() if r.get("claimant_id") != claimant]
        same_serial = [(r, s) for r, s in records.values() if inv_serial and
                       r.get("serial_number", "").upper() == inv_serial[0].strip().upper()]
        ref = lambda r, s: {"source": s, "value": f"{r['claim_id']} {r['claimant_id']} {r['claim_date']} {r['outcome']}"}
        if others:
            patterns.append({"pattern": "SAME_SERIAL_OTHER_CLAIMANT",
                             "detail": "The device serial was claimed before by another claimant.",
                             "references": [ref(r, s) for r, s in others]})
            finding("CLAIM_HISTORY_PATTERN", "HIGH", "Serial previously claimed by a different claimant: "
                    + ", ".join(f"{r['claim_id']} ({r['claimant_id']})" for r, _ in others) + ".")
        elif len(same_serial) >= 2:
            patterns.append({"pattern": "REPEAT_CLAIMS", "detail": f"{len(same_serial)} earlier claims on this serial.",
                             "references": [ref(r, s) for r, s in same_serial]})
            finding("CLAIM_HISTORY_PATTERN", "MEDIUM", f"{len(same_serial)} earlier claims on the same serial.")
        elif records:
            patterns.append({"pattern": "PRIOR_CLAIM_SAME_CLAIMANT", "detail": "Earlier claim(s) by the same claimant.",
                             "references": [ref(r, s) for r, s in records.values()]})
        else:
            patterns.append({"pattern": "NO_PRIOR_CLAIMS", "detail": "No earlier claims for this serial or claimant.",
                             "references": []})

    high = any(f["severity"] == "HIGH" for f in findings)
    gap = any(f["category"] == "EVIDENCE_GAP" for f in findings)
    outcome = "ESCALATE" if high else "REQUEST_EVIDENCE" if gap else "APPROVE"
    confidence = 0.85 if high else 0.6 if gap else 0.7
    uncertainty.append("Coverage period, policy exclusions and payout are outside the forensic review.")
    summary = (f"{len(findings)} forensic finding(s): "
               + (", ".join(sorted({f['category'] for f in findings})) or "none")
               + f". Recommended outcome {outcome} (rule-based test double, not LLM reasoning).")
    for check in serial_checks + chronology:
        refs.extend(c for c in check["citations"] if c not in refs)
    return {"claim_id": claim_id, "findings": findings, "evidence_references": refs[:30],
            "serial_number_checks": serial_checks, "chronology_checks": chronology,
            "historical_pattern_findings": patterns, "missing_information": missing, "uncertainty": uncertainty,
            "confidence": confidence, "recommended_outcome": outcome, "reasoning_summary": summary}


class ForensicTestDoubleLLM(BaseLLM):
    model: str = MODEL_NAME
    mode: Literal["forensic", "invalid_json", "fabricated_citation", "wrong_claim"] = "forensic"
    calls: int = 0

    def supports_function_calling(self) -> bool:
        # Declared capability: no native function calling, so CrewAI uses its ReAct text protocol.
        return False

    def get_input_tokens_count(self, messages, tools=None, response_model=None) -> int:
        # Same 4-characters-per-token estimate CrewAI uses by default, without its per-call warning.
        return sum(len(m["content"]) for m in _transcript(messages)) // 4

    def call(self, messages, tools=None, callbacks=None, available_functions=None, from_task=None,
             from_agent=None, response_model=None):
        # Emit CrewAI's standard LLM call events, as its built-in providers do, so callbacks count this call.
        with llm_call_context():
            self._emit_call_started_event(messages=messages, tools=tools, callbacks=callbacks,
                                          available_functions=available_functions, from_task=from_task,
                                          from_agent=from_agent)
            response = self._respond(messages, response_model)
            self._emit_call_completed_event(response=response, call_type=LLMCallType.LLM_CALL, from_task=from_task,
                                            from_agent=from_agent, messages=messages)
        return response

    def _respond(self, messages, response_model):
        self.calls += 1
        transcript = _transcript(messages)
        full = "\n".join(m["content"] for m in transcript)
        packet = Packet(full)
        tool = _TOOL_NAME.search(full)
        issued = sum(m["content"].count("Action:") for m in transcript if m["role"] == "assistant")
        plan = []
        serial = packet.get(packet.doc_of("INVOICE"), "serial") or packet.get(packet.doc_of("DIAGNOSTIC"), "serial")
        if serial:
            plan.append({"serial_number": serial[0].strip()})
        if packet.form.get("claimant_id"):
            plan.append({"claimant_id": packet.form["claimant_id"]})
        if tool and issued < len(plan) and response_model is None:
            return (f"Thought: I need the claim history from the MCP tool.\n"
                    f"Action: {tool.group(1)}\nAction Input: {json.dumps(plan[issued])}")

        history = [o for o in _json_objects(full) if "records" in o and "total_matches" in o]
        analysis = forensic_analysis(packet, history, history_checked=bool(tool) and len(history) >= len(plan) > 0)
        if self.mode == "invalid_json":
            answer = '{"claim_id": "' + str(analysis["claim_id"]) + '", "findings": "not a list"'
        elif self.mode == "fabricated_citation":
            analysis["evidence_references"].append({"doc_id": "invoice", "line": 10, "quote": "Serial Number: FAKE-0000"})
            answer = json.dumps(analysis)
        elif self.mode == "wrong_claim":
            answer = json.dumps({**analysis, "claim_id": "CS-999"})
        else:
            answer = json.dumps(analysis)
        return answer if response_model is not None else f"Thought: I now know the final answer\nFinal Answer: {answer}"
