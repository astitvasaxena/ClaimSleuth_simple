"""TEST DOUBLE for Google's side of the Forensic Agent's model calls: deterministic code, not a language model.

It replaces only the provider call inside the real BudgetedGeminiLLM, so everything around it is real: the call and
time budgets, the shared request ledger, CrewAI's native tool-calling loop, the guardrail, the MCP server and the
blackboard. It answers the way Gemini does in the stored live runs: both history lookups requested together, then the
report. `behaviour` scripts the ways a model can misbehave. `latency_s` and `per_kchar_s` simulate request time.
"""
from __future__ import annotations

import json
import re
import time

from crewai.events.types.llm_events import LLMCallType
from crewai.llms.base_llm import llm_call_context
from google.genai import errors

from claimsleuth.crew_service.crew import HISTORY_DONE_MARKER
from claimsleuth.crew_service.test_double import forensic_analysis
from claimsleuth.packet import Packet
from claimsleuth.packet import json_objects


UNAVAILABLE = object()


def text_of(messages) -> str:
    if isinstance(messages, str):
        return messages
    return "\n".join(str(m.get("content") or "") for m in messages)


class ProviderDouble:
    """behaviour: "normal", "bad_once" (first report has a fabricated citation), "bad_always", "one_by_one" (one lookup
    per turn), "runaway" (never stops asking for tools), "runaway_bad" (the same, and its report is invalid),
    "ignores_note" (repeats the lookups on a guardrail retry), "unavailable_once" (the first report request gets a 503),
    "unavailable_then_bad" (the same, then an invalid report)."""

    def __init__(self, behaviour: str = "normal", latency_s: float = 0.0, per_kchar_s: float = 0.0) -> None:
        self.behaviour, self.latency_s, self.per_kchar_s = behaviour, latency_s, per_kchar_s
        self.prompts: list[int] = []
        self.texts: list[str] = []
        self.reports = 0
        self.unavailable = False

    def __call__(self, llm, messages, tools=None, callbacks=None, available_functions=None, from_task=None,
                 from_agent=None, response_model=None):
        text = text_of(messages)
        self.prompts.append(len(text))
        self.texts.append(text)
        time.sleep(self.latency_s + self.per_kchar_s * len(text) / 1000)
        with llm_call_context():
            llm._emit_call_started_event(messages=messages, tools=tools, callbacks=callbacks,
                                         available_functions=available_functions, from_task=from_task, from_agent=from_agent)
            response = self._answer(messages, text, tools)
            if response is UNAVAILABLE:
                llm._emit_call_failed_event(error="Google Gemini API error: 503 - busy", from_task=from_task, from_agent=from_agent)
                raise errors.ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})
            kind = LLMCallType.TOOL_CALL if isinstance(response, list) else LLMCallType.LLM_CALL
            llm._emit_call_completed_event(response=response, call_type=kind, from_task=from_task, from_agent=from_agent,
                                           messages=messages)
        return response

    def _lookups(self, text: str) -> list[dict]:
        packet = Packet(text)
        serial = packet.get(packet.doc_of("INVOICE"), "serial") or packet.get(packet.doc_of("DIAGNOSTIC"), "serial")
        plan = [{"serial_number": serial[0].strip()}] if serial else []
        return plan + ([{"claimant_id": packet.form["claimant_id"]}] if packet.form.get("claimant_id") else [])

    def _answer(self, messages, text, tools):
        done = 0 if isinstance(messages, str) else sum(m.get("role") == "tool" for m in messages)
        told = HISTORY_DONE_MARKER in text and self.behaviour != "ignores_note"
        names = re.findall(r"[\w-]*claim_history_lookup", json.dumps(tools, default=str)) if tools else []
        plan = self._lookups(text)
        looping = self.behaviour in ("runaway", "runaway_bad")
        if names and (looping or (done < len(plan) and not told)):
            wanted = [plan[(done + i) % len(plan)] for i in range(1 if self.behaviour == "one_by_one" or looping else len(plan))]
            return [{"id": f"call-{done}-{i}", "type": "function",
                     "function": {"name": names[0], "arguments": json.dumps(args)}} for i, args in enumerate(wanted)]
        if self.behaviour in ("unavailable_once", "unavailable_then_bad") and not self.unavailable:
            self.unavailable = True
            return UNAVAILABLE
        self.reports += 1
        history = [o for o in json_objects(text) if "records" in o and "total_matches" in o]
        analysis = forensic_analysis(Packet(text), history, history_checked=True)
        if self.behaviour in ("bad_always", "runaway_bad") or (
                self.behaviour in ("bad_once", "ignores_note", "unavailable_then_bad") and self.reports == 1):
            analysis["evidence_references"].append({"doc_id": "invoice", "line": 10, "quote": "Serial Number: FAKE-0000"})
        return json.dumps(analysis)
