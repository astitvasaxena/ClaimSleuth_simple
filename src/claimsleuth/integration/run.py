"""Run all four independent ClaimSleuth outputs for one claim under one run id, then adjudicate.

  1. DETERMINISTIC BASELINE   in this process (no LLM)
  2. GOOGLE ADK RESULT        Evidence + Investigator agents, ADK Runner, MCP tools      } run concurrently,
  3. CREWAI RESULT            requested by ADK's RemoteA2aAgent over A2A from the        } neither sees the
                              CrewAI service in .venv-crew                               } other's output
  4. MCP TOOL RESULTS         MCP discovery + the MCP server's own log for this run and claim
  5. Final report             deterministic adjudicator (integration/adjudicator.py)

Each output is stored exactly as produced under runtime/runs/<run_id>/, with an audit trail.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any

from claimsleuth import config
from claimsleuth.adk_service.forensic_a2a import request_forensics
from claimsleuth.adk_service.runner import run_adk_investigation_async
from claimsleuth.baseline import new_run_id, run_baseline
from claimsleuth.datastore import load_claim, load_policies
from claimsleuth.documents import extract_fields
from claimsleuth.history import lookup_history
from claimsleuth.integration.adjudicator import adjudicate
from claimsleuth.integration.mcp_records import fetch_mcp_records
from claimsleuth.rules import normalise_serial
from claimsleuth.schemas import (AuditEvent, ClaimRunBundle, DocumentKind, ExecutionMetadata,
                                 WorkflowMessage)

DEFAULT_MCP_URL = "http://127.0.0.1:8102/mcp"
DEFAULT_CREW_CARD = "http://127.0.0.1:8103/.well-known/agent-card.json"


ProgressCallback = Callable[[str, dict[str, Any]], None]


def _notify(progress: ProgressCallback | None, stage: str, **data: Any) -> None:
    """Tell an optional observer (the dashboard) that a stage really finished. Never affects the run."""
    if progress is None:
        return
    try:
        progress(stage, data)
    except Exception:
        pass


async def _observed(coro, progress: ProgressCallback, stage: str):
    result = await coro
    _notify(progress, stage, status=getattr(getattr(result, "result", result), "status", None))
    return result


class Audit:
    def __init__(self, run_id: str, claim_id: str) -> None:
        self.run_id, self.claim_id, self.events = run_id, claim_id, []

    def __call__(self, event: str, actor: str, detail: str, ts=None, **data) -> None:
        kw = {"ts": ts} if ts is not None else {}
        self.events.append(AuditEvent(run_id=self.run_id, claim_id=self.claim_id, event=event, actor=actor,
                                      detail=detail, data=data, **kw))


async def run_claim_async(claim_id: str, run_id: str | None = None, mcp_url: str = DEFAULT_MCP_URL,
                          crew_card_url: str = DEFAULT_CREW_CARD, data_dir: Path = config.DATA_DIR,
                          db_path: Path = config.DB_PATH, adk_llm_mode: str = "test-double",
                          a2a_timeout: float | None = None, progress: ProgressCallback | None = None) -> ClaimRunBundle:
    """`progress` (optional, default off) is called as progress(stage, data) when the baseline, the ADK workflow,
    the CrewAI request and the adjudication have actually finished. It does not change what runs or how."""
    run_id = run_id or new_run_id()
    audit = Audit(run_id, claim_id)
    audit("run_started", "orchestrator", f"integrated run for {claim_id}", mcp_url=mcp_url, crew_card=crew_card_url)
    claim, documents = load_claim(claim_id, data_dir)
    policy = load_policies(data_dir).get(claim.policy_id)

    audit("workflow_started", "baseline", "deterministic baseline")
    baseline = run_baseline(claim_id, run_id=run_id, data_dir=data_dir, db_path=db_path)
    audit("workflow_finished", "baseline", f"{baseline.status.value} {baseline.decision.value if baseline.decision else ''}",
          execution_ms=baseline.execution_ms)
    _notify(progress, "baseline_finished", status=baseline.status)

    audit("workflow_started", "adk", f"Evidence + Investigator agents (llm_mode={adk_llm_mode})")
    audit("workflow_started", "crewai", "forensic request over A2A via ADK RemoteA2aAgent")
    adk_task = run_adk_investigation_async(claim_id, run_id=run_id, mcp_url=mcp_url, data_dir=data_dir,
                                           llm_mode=adk_llm_mode)
    crew_task = request_forensics(claim, documents, run_id, crew_card_url, timeout=a2a_timeout)
    if progress is not None:
        adk_task, crew_task = (_observed(adk_task, progress, "adk_finished"),
                               _observed(crew_task, progress, "crewai_finished"))
    adk, exchange = await asyncio.gather(adk_task, crew_task)
    audit("workflow_finished", "adk", f"{adk.status.value}", ts=adk.finished_at, execution_ms=adk.execution_ms,
          model=adk.model, errors=adk.errors[:5])
    audit("message_sent", "adk_orchestrator", "FORENSIC_REQUEST over A2A", ts=exchange.request.timestamp,
          message_id=exchange.request.message_id, recipient=exchange.request.recipient)
    if exchange.response is not None:
        audit("message_received", "crewai_forensic_service", f"{exchange.response.message_type} over A2A",
              ts=exchange.response.timestamp, message_id=exchange.response.message_id)
    for error in exchange.errors:
        audit("validation_failure", "adk_orchestrator", error)
    crewai = exchange.result
    audit("workflow_finished", "crewai", f"{crewai.status.value}", ts=crewai.finished_at,
          execution_ms=crewai.execution_ms, model=crewai.model, errors=crewai.errors[:5])

    mcp = await fetch_mcp_records(mcp_url, run_id, claim_id)
    audit("mcp_records_fetched", "orchestrator", f"{len(mcp.calls)} tool call(s) logged by the MCP server",
          callers=sorted({c.caller or "unknown" for c in mcp.calls}), discovered=[t.name for t in mcp.discovered_tools])
    for error in mcp.errors:
        audit("validation_failure", "orchestrator", error)

    serial = next((extract_fields(d).value("serial_number") for d in documents if d.kind == DocumentKind.INVOICE), None)
    serial = normalise_serial(serial or claim.serial_number)
    history = lookup_history(serial, claim.claimant_id, claim.claim_id, db_path).records if serial else []
    final = adjudicate(run_id, claim, documents, policy, history, baseline, adk, crewai, mcp)
    audit("adjudication", "adjudicator", f"{final.final_decision.value} decided by {final.decided_by}",
          deterministic=final.deterministic_decision.value if final.deterministic_decision else None,
          human_approval=final.requires_human_approval)
    audit("run_finished", "orchestrator", "all outputs stored independently")
    _notify(progress, "adjudicated", decision=final.final_decision)

    messages: list[WorkflowMessage] = [exchange.request] + ([exchange.response] if exchange.response else [])
    crew_meta = exchange.response.metadata if exchange.response else ExecutionMetadata(framework="crewai")
    return ClaimRunBundle(
        run_id=run_id, claim_id=claim_id, baseline=baseline, adk=adk, crewai=crewai, mcp=mcp, messages=messages,
        final_report=final, audit=sorted(audit.events, key=lambda e: e.ts),
        workflow_metadata={
            "baseline": ExecutionMetadata(framework="deterministic", llm_mode="none"),
            "adk": ExecutionMetadata(framework="adk", model=adk.model,
                                     provider="offline-test-double" if adk.model == "test-double" else "google-gemini",
                                     llm_mode="test-double" if adk.model == "test-double" else "live"),
            "crewai": crew_meta,
            "mcp": ExecutionMetadata(framework="mcp", protocol=f"MCP streamable-http (mcp {version('mcp')})"),
        })


def run_claim(claim_id: str, **kwargs) -> ClaimRunBundle:
    return asyncio.run(run_claim_async(claim_id, **kwargs))


def save_bundle(bundle: ClaimRunBundle, runtime_dir: Path = config.RUNTIME_DIR) -> Path:
    from claimsleuth.render import final_markdown
    out = runtime_dir / "runs" / bundle.run_id
    out.mkdir(parents=True, exist_ok=True)
    for name, item in (("baseline", bundle.baseline), ("adk", bundle.adk), ("crewai", bundle.crewai),
                       ("mcp", bundle.mcp), ("final_report", bundle.final_report)):
        if item is not None:
            (out / f"{name}.json").write_text(item.model_dump_json(indent=2), encoding="utf-8")
    (out / "messages.jsonl").write_text("".join(m.model_dump_json() + "\n" for m in bundle.messages), encoding="utf-8")
    (out / "audit.jsonl").write_text("".join(e.model_dump_json() + "\n" for e in bundle.audit), encoding="utf-8")
    (out / "bundle.json").write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
    (out / "final_report.md").write_text(final_markdown(bundle), encoding="utf-8")
    return out
