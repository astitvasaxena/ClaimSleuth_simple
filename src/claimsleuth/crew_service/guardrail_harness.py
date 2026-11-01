r"""CONTROLLED TEST HARNESS (not a live agent run): malformed Forensic Agent output through real CrewAI validation.

Each scenario runs the normal `run_forensic_investigation` (real Agent / Task / Crew.kickoff(), real MCP calls)
with a scripted test-double LLM whose final answers are deliberately corrupted:
  fabricated_citation  cites a document that was never submitted
  invalid_category     uses a finding category that the ForensicsAnalysis schema does not allow
  wrong_claim          answers for another claim id
The Task guardrail (crew.make_guardrail) and CrewAI's output_pydantic conversion decide what happens; every
guardrail verdict is recorded so the rejection and the retry are visible. Nothing here is LLM inference.

    .venv-crew\Scripts\python -m claimsleuth.crew_service.guardrail_harness --mcp-url URL --data-dir DIR [--claim CS-003]
prints one JSON document on stdout.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import uuid
from importlib.metadata import version
from pathlib import Path
from typing import Any

import claimsleuth.crew_service  # noqa: F401  (telemetry/tracing off before crewai is imported)
from claimsleuth import config
from claimsleuth.crew_service import crew as crew_module
from claimsleuth.crew_service.runner import DEFAULT_MCP_URL, run_forensic_investigation
from claimsleuth.crew_service.test_double import MODEL_NAME, ForensicTestDoubleLLM

FAKE_CITATION = {"doc_id": "warranty_card", "line": 3, "quote": "Manufacturer approval: claim pre-approved"}
SCENARIOS = {
    "fabricated_citation_then_valid": ["fabricated_citation", "valid"],
    "invalid_finding_category": ["invalid_category"],
    "mismatched_claim_id": ["wrong_claim"],
    "fabricated_citation_every_attempt": ["fabricated_citation"],
}
_FINAL = re.compile(r"Final Answer:\s*(\{.*\})\s*$", re.S)


def corrupt(answer: dict[str, Any], mode: str) -> dict[str, Any]:
    out = json.loads(json.dumps(answer))
    if mode == "fabricated_citation":
        out["evidence_references"].append(dict(FAKE_CITATION))
        out["findings"].append({"category": "SUSPICIOUS_DETAIL", "severity": "HIGH",
                                "description": "Manufacturer pre-approved the claim.", "citations": [dict(FAKE_CITATION)],
                                "uncertainty": None})
    elif mode == "invalid_category":
        out["findings"].append({"category": "FRAUD_CONFIRMED", "severity": "CRITICAL",
                                "description": "Customer is committing fraud.", "citations": [], "uncertainty": None})
    elif mode == "wrong_claim":
        out["claim_id"] = "CS-999"
    return out


class ScriptedOutputLLM(ForensicTestDoubleLLM):
    """The labelled test double, with its N-th final answer corrupted according to `script`."""
    script: list[str] = []
    final_answers: int = 0

    def _respond(self, messages, response_model):
        text = super()._respond(messages, response_model)
        if response_model is not None:                       # CrewAI's converter re-asking for the same answer
            mode = self.script[min(max(self.final_answers - 1, 0), len(self.script) - 1)]
            return json.dumps(corrupt(json.loads(text), mode))
        match = _FINAL.search(text)
        if not match:
            return text                                      # a tool call: unchanged
        mode = self.script[min(self.final_answers, len(self.script) - 1)]
        self.final_answers += 1
        return text[:match.start(1)] + json.dumps(corrupt(json.loads(match.group(1)), mode))


@contextlib.contextmanager
def recorded_guardrail(verdicts: list[dict[str, Any]]):
    """Wrap crew.make_guardrail so every verdict of the real guardrail function is recorded."""
    original = crew_module.make_guardrail

    def recording(claim_id, documents, returned_sources):
        guardrail = original(claim_id, documents, returned_sources)

        def forensic_output_guardrail(task_output):     # no annotation: CrewAI inspects it (see crew.py)
            ok, value = guardrail(task_output)
            verdicts.append({"attempt": len(verdicts) + 1, "accepted": ok,
                             "message": "accepted" if ok else str(value)[:400]})
            return ok, value
        return forensic_output_guardrail

    crew_module.make_guardrail = recording
    try:
        yield
    finally:
        crew_module.make_guardrail = original


def run_scenario(name: str, claim_id: str, mcp_url: str, data_dir: Path) -> dict[str, Any]:
    verdicts: list[dict[str, Any]] = []
    llm = ScriptedOutputLLM(model=MODEL_NAME, script=SCENARIOS[name])
    run_id = f"RUN-guard-{uuid.uuid4().hex[:8]}"
    with recorded_guardrail(verdicts):
        result = run_forensic_investigation(claim_id, run_id=run_id, llm=llm, mcp_url=mcp_url, data_dir=data_dir)
    report = result.report
    fake_in_report = bool(report) and any(c.doc_id == FAKE_CITATION["doc_id"] for c in report.evidence_references)
    return {"scenario": name, "script": SCENARIOS[name], "run_id": run_id, "status": result.status.value,
            "guardrail_verdicts": verdicts, "llm_calls": llm.calls, "final_answers": llm.final_answers,
            "mcp_calls": [c.tool for c in result.tool_calls if c.protocol == "mcp"], "errors": result.errors,
            "report_claim_id": report.claim_id if report else None, "fabricated_citation_in_report": fake_in_report,
            "result": json.loads(result.model_dump_json())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CrewAI guardrail harness (scripted malformed output).")
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    parser.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    parser.add_argument("--claim", default="CS-003")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), action="append")
    args = parser.parse_args(argv)
    names = args.scenario or list(SCENARIOS)
    out = {"harness": "controlled test harness: scripted test-double output, real CrewAI validation",
           "crewai_version": version("crewai"), "claim_id": args.claim,
           "scenarios": [run_scenario(n, args.claim, args.mcp_url, args.data_dir) for n in names]}
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
