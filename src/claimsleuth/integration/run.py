"""Run all four ClaimSleuth outputs for one claim under one run id, then adjudicate.

  1. DETERMINISTIC BASELINE   in this process (no LLM)
  2. GOOGLE ADK RESULT        Evidence + Investigator agents, ADK Runner, MCP tools
  3. CREWAI RESULT            requested by ADK's RemoteA2aAgent over A2A from the CrewAI service in .venv-crew
  4. MCP TOOL RESULTS         MCP discovery + the MCP server's own log for this run and claim
  5. Final report             deterministic adjudicator (integration/adjudicator.py)

run_claim_async() hands the run to the hierarchical orchestration manager (orchestration/manager.py), which
plans, releases, dispatches, monitors and collects these tasks. This module keeps the entry points used by the
dashboard, the evaluation and the command line, the live preflight and the helpers the manager uses.

Blackboard mode (CLAIMSLEUTH_BLACKBOARD_DB set - the dashboard default - or `blackboard_path`), the shared context:
  Evidence Agent -> validated report published to the blackboard -> ContextRef
    -> Investigator Agent consumes it from the blackboard (ADK, same workflow)          } run concurrently once the
    -> FORENSIC_REQUEST over A2A carries the ContextRef; the CrewAI service consumes   } Evidence report is published;
       the report from the blackboard with its own key                                 } neither sees the other's report
  -> both reports published -> the controller seals the run -> the adjudicator receives only reports it verified on
  the blackboard (RunController.verified_inputs). Fail closed: no verified Evidence report -> the Investigator is not
  started and no FORENSIC_REQUEST is sent; an unverifiable report is never adjudicated; an incomplete investigation
  always needs a human (the adjudicator's own rules).
Direct mode (blackboard disabled): ADK and the A2A request start together, the Investigator reads the Evidence report
from ADK session state and the adjudicator receives the results as returned - the earlier behaviour, labelled
context_sharing="direct" in the bundle.

Each output is stored exactly as produced under runtime/runs/<run_id>/, with an audit trail.

EXECUTION_MODE=live: before anything runs, the Gemini credentials and the CrewAI service's model mode are
checked; a problem raises LiveModeError (nothing registered, nothing adjudicated). After the agents return, a
result that claims success without genuine inference is turned into a FAILED result with the reason.
"""
from __future__ import annotations
import os
import time

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from claimsleuth import config, execution_mode, gemini_rate
from claimsleuth.schemas import AgentSystemResult, AuditEvent, ClaimRunBundle, ExecutionStatus

DEFAULT_MCP_URL = "http://127.0.0.1:8102/mcp"
DEFAULT_CREW_CARD = "http://127.0.0.1:8103/.well-known/agent-card.json"
# Forensic time limits, inner to outer: one model request (120 s) < the CrewAI run (at most its 300 s budget plus one
# request) < this A2A wait < the manager's task deadline (this + 60 s).
LIVE_A2A_TIMEOUT = 450.0
# Gemini requests one live claim normally needs: Evidence 2, Investigator 2-3, Forensic 2 (one more when the guardrail
# asks for a corrected answer). Upper bounds: ADK 10 and CrewAI 4 model calls per run. A 503 can add one
# retry per agent; each takes a ledger slot, and a retry the daily budget refuses is simply not sent.
LIVE_REQUESTS_ESTIMATE = 10
ATOMIC_REPLACE_ATTEMPTS = 5


def atomic_write(path: Path, text: str) -> None:
    """A reader sees the old file or the whole new one, never part of a write. Windows can hold a just-written file
    briefly (a virus scan, an indexer), so a blocked replace is tried again."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(ATOMIC_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == ATOMIC_REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.1)
    finally:
        tmp.unlink(missing_ok=True)


class LiveModeError(RuntimeError):
    """EXECUTION_MODE=live cannot be honoured; raised before anything runs."""


def crew_service_llm(crew_card_url: str) -> dict[str, Any]:
    """The CrewAI service's own report of its model mode (GET /health), or {} when it does not answer."""
    base = crew_card_url.split("/.well-known/")[0]
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=30) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except json.JSONDecodeError:
            return {}
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return {}
    return (body.get("checks") or {}).get("llm") or {}


def live_preflight(crew_card_url: str) -> str:
    """Raise LiveModeError unless both sides can run genuine Gemini inference. Returns the Gemini model name."""
    execution_mode.load_env()
    problems = execution_mode.gemini_config_errors()
    if problems:
        raise LiveModeError(problems[0])
    try:
        execution_mode.adk_llm_mode()
    except execution_mode.ExecutionModeError as exc:
        raise LiveModeError(str(exc)) from exc
    model = execution_mode.gemini_model()
    try:
        _, rpd = gemini_rate.limits()
        used = gemini_rate.usage(model)["today"]
    except gemini_rate.GeminiQuotaGuard as exc:
        raise LiveModeError(str(exc)) from exc
    if rpd and used + LIVE_REQUESTS_ESTIMATE > rpd:
        raise LiveModeError(f"not enough daily Gemini budget for a live claim: {used} of {rpd} requests for {model} "
                            f"already used today (Pacific time) by this machine, and a claim needs about "
                            f"{LIVE_REQUESTS_ESTIMATE}. Nothing was run.")
    llm = crew_service_llm(crew_card_url)
    if not llm:
        raise LiveModeError(f"the CrewAI A2A service does not answer at {crew_card_url}; "
                            "start it with EXECUTION_MODE=live")
    if llm.get("mode") != execution_mode.GEMINI or not llm.get("ok"):
        detail = f" ({llm['error']})" if llm.get("error") else ""
        raise LiveModeError(f"the CrewAI A2A service runs in mode {llm.get('mode')!r}{detail}, not Gemini; restart "
                            "it with EXECUTION_MODE=live. Nothing was run and no fallback was used.")
    return execution_mode.gemini_model()


def require_genuine_inference(result: AgentSystemResult, provider: str | None) -> AgentSystemResult:
    """Live mode: a COMPLETED result without genuine Gemini inference becomes FAILED (never the other way)."""
    if result.status != ExecutionStatus.COMPLETED:
        return result
    problems = []
    if not result.model or result.model == "test-double":
        problems.append(f"live mode: {result.system} result has no live model (model={result.model!r})")
    if provider != execution_mode.PROVIDER_GEMINI:
        problems.append(f"live mode: {result.system} result came from provider {provider!r}, not Gemini")
    if not any(r.llm_calls for r in result.agent_runs):
        problems.append(f"live mode: {result.system} recorded no model call")
    if not problems:
        return result
    return result.model_copy(update={"status": ExecutionStatus.FAILED, "report": None, "evidence_report": None,
                                     "errors": list(result.errors) + problems})


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
                          db_path: Path = config.DB_PATH, adk_llm_mode: str | None = None,
                          a2a_timeout: float | None = None, progress: ProgressCallback | None = None,
                          blackboard_path: Path | None = None) -> ClaimRunBundle:
    """`progress` (optional, default off) is called as progress(stage, data) when the baseline, the ADK workflow,
    the CrewAI request and the adjudication have actually finished. It does not change what runs or how.

    adk_llm_mode: "test-double" or "gemini"; when None, EXECUTION_MODE decides (live -> gemini), else test-double.
    blackboard_path: the blackboard database for this run (default: CLAIMSLEUTH_BLACKBOARD_DB; the CrewAI service
    must use the same file). Raises LiveModeError (before anything runs) when EXECUTION_MODE=live cannot be
    honoured, and orchestration.manager.ManagerError when the manager itself fails (no decision is produced)."""
    from claimsleuth.orchestration.manager import InvestigationManager     # the manager imports this module
    return await InvestigationManager(
        claim_id, run_id=run_id, mcp_url=mcp_url, crew_card_url=crew_card_url, data_dir=data_dir, db_path=db_path,
        adk_llm_mode=adk_llm_mode, a2a_timeout=a2a_timeout, progress=progress, blackboard_path=blackboard_path).run()


def run_claim(claim_id: str, **kwargs) -> ClaimRunBundle:
    return asyncio.run(run_claim_async(claim_id, **kwargs))


def save_bundle(bundle: ClaimRunBundle, runtime_dir: Path = config.RUNTIME_DIR) -> Path:
    from claimsleuth.render import final_markdown
    out = runtime_dir / "runs" / bundle.run_id
    out.mkdir(parents=True, exist_ok=True)
    for name, item in (("baseline", bundle.baseline), ("adk", bundle.adk), ("crewai", bundle.crewai),
                       ("mcp", bundle.mcp), ("final_report", bundle.final_report)):
        if item is not None:
            atomic_write(out / f"{name}.json", item.model_dump_json(indent=2))
    if bundle.trace is not None:
        atomic_write(out / "trace.json", bundle.trace.model_dump_json(indent=2))
    if bundle.execution_plan is not None:
        atomic_write(out / "execution_plan.json", bundle.execution_plan.model_dump_json(indent=2))
    atomic_write(out / "messages.jsonl", "".join(m.model_dump_json() + "\n" for m in bundle.messages))
    atomic_write(out / "audit.jsonl", "".join(e.model_dump_json() + "\n" for e in bundle.audit))
    atomic_write(out / "bundle.json", bundle.model_dump_json(indent=2))
    atomic_write(out / "final_report.md", final_markdown(bundle))
    return out
