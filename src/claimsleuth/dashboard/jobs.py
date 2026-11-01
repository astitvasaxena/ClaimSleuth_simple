"""Background investigation runs started from the dashboard.

A run is exactly the Phase 5 integration (integration.run.run_claim_async): deterministic baseline, Google ADK
(Evidence + Investigator, test-double model), the CrewAI Forensic Agent requested over A2A, MCP records and the
deterministic adjudicator, saved with save_bundle under runtime/runs/<run_id>/. One run at a time.

Progress comes only from real signals: the optional progress callback of run_claim_async (baseline, ADK workflow,
CrewAI request and adjudication actually finished), the MCP server's tool-call log (which agent is calling tools)
and the CrewAI service's A2A message log (request received / response sent). A stage is marked done only when one
of these says so.
"""
from __future__ import annotations

import asyncio
import json
import re
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from claimsleuth import config
from claimsleuth.dashboard.services import ServiceConfig, get_json
from claimsleuth.dashboard.workspace import Workspace
from claimsleuth.integration.run import run_claim_async, save_bundle
from claimsleuth.schemas import utc_now

RUN_ID = re.compile(r"^RUN-[A-Za-z0-9_-]{3,60}$")
OLLAMA_A2A_TIMEOUT = 1500.0            # local CPU inference is slow (see docs/OLLAMA_GUIDE.md)


class JobBusyError(RuntimeError):
    pass


@dataclass
class Job:
    run_id: str
    claim_id: str
    source: dict[str, Any]
    crew_mode: str | None
    a2a_timeout_s: float | None
    submitted_at: str = field(default_factory=lambda: utc_now().isoformat())
    state: str = "running"                 # running | completed | failed
    finished_at: str | None = None
    error: str | None = None
    events: dict[str, dict[str, Any]] = field(default_factory=dict)   # stage -> {"ts", "status"} from run_claim_async

    def progress(self, stage: str, data: dict[str, Any]) -> None:
        status = data.get("status")
        self.events[stage] = {"ts": utc_now().isoformat(), "status": getattr(status, "value", status)}


class JobManager:
    def __init__(self, workspace: Workspace, cfg: ServiceConfig, runtime_dir: Path = config.RUNTIME_DIR) -> None:
        self.ws, self.cfg, self.runtime_dir = workspace, cfg, runtime_dir
        self.jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task] = set()

    def busy(self) -> Job | None:
        return next((j for j in self.jobs.values() if j.state == "running"), None)

    def start(self, claim_id: str, source: dict[str, Any], crew_mode: str | None) -> Job:
        running = self.busy()
        if running is not None:
            raise JobBusyError(f"run {running.run_id} for {running.claim_id} is still in progress")
        timeout = OLLAMA_A2A_TIMEOUT if crew_mode == "ollama-local" else None
        job = Job(run_id=f"RUN-dash-{uuid.uuid4().hex[:10]}", claim_id=claim_id, source=source,
                  crew_mode=crew_mode, a2a_timeout_s=timeout)
        self.jobs[job.run_id] = job
        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job) -> None:
        try:
            bundle = await run_claim_async(job.claim_id, run_id=job.run_id, mcp_url=self.cfg.mcp_url,
                                           crew_card_url=self.cfg.crew_card, data_dir=self.ws.data_dir,
                                           db_path=config.DB_PATH, adk_llm_mode="test-double",
                                           a2a_timeout=job.a2a_timeout_s, progress=job.progress)
            out = save_bundle(bundle, self.runtime_dir)
            job.state = "completed"
        except Exception as exc:      # the run could not produce a bundle: recorded as a failed job, nothing invented
            job.state, job.error = "failed", f"{type(exc).__name__}: {exc}"
            out = self.runtime_dir / "dashboard" / "failed_jobs"
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{job.run_id}.txt").write_text(traceback.format_exc(), encoding="utf-8")
            out = None
        job.finished_at = utc_now().isoformat()
        if out is not None:
            meta = {**asdict(job), "adk_llm_mode": "test-double",
                    "source_label": job.source.get("label"), "workspace": str(self.ws.data_dir)}
            (out / "dashboard_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def live(self, run_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(run_id)
        if job is None:
            return None
        _, calls = get_json(f"http://127.0.0.1:{self.cfg.mcp_port}/tool-calls?run_id={quote(run_id)}&limit=200")
        _, messages = get_json(f"{self.cfg.crew_base}/messages?run_id={quote(run_id)}")
        calls = (calls or {}).get("calls", []) if isinstance(calls, dict) else []
        messages = (messages or {}).get("messages", []) if isinstance(messages, dict) else []
        return {**asdict(job), "stages": stages(job, calls, messages)}


def stages(job: Job, calls: list[dict], messages: list[dict]) -> list[dict[str, str]]:
    """The five user-facing stages. "done" only on a real completion signal; "failed" when that signal says so."""
    ev = job.events
    by = lambda agent: [c for c in calls if (c.get("caller") or "").endswith(agent)]
    finished = job.state != "running"

    def outcome(stage: str) -> str:
        return "failed" if ev[stage].get("status") not in (None, "COMPLETED") else "done"

    adk_done = outcome("adk_finished") if "adk_finished" in ev else None
    if "crewai_finished" in ev:
        forensic = outcome("crewai_finished")
    elif any(m.get("direction") == "sent" for m in messages):
        forensic = "running"                     # reply sent; result not yet received and validated by ADK
    else:
        forensic = "running" if any(m.get("direction") == "received" for m in messages) else "waiting"
    evidence = "done" if by("investigator_agent") else adk_done or ("running" if "baseline_finished" in ev else "waiting")
    investigator = adk_done or ("running" if by("investigator_agent") or evidence == "done" else "waiting")
    if "adjudicated" in ev:
        adjudication = "done"
    elif finished:
        adjudication = "failed"
    else:
        adjudication = "running" if "adk_finished" in ev and "crewai_finished" in ev else "waiting"
    extraction = "done"        # the PDF was parsed (or the stored documents loaded) before the run was accepted
    return [{"key": "extraction", "label": "Document extraction", "state": extraction},
            {"key": "evidence", "label": "Evidence Agent", "state": evidence},
            {"key": "investigator", "label": "Investigator Agent", "state": investigator},
            {"key": "forensic", "label": "Forensic Agent", "state": forensic},
            {"key": "adjudication", "label": "Final adjudication", "state": adjudication}]
