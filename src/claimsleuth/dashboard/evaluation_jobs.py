"""Agent evaluation runs started from the dashboard (one at a time, never alongside an investigation).

OFFLINE runs start directly. A LIVE run must carry the exact confirmation phrase, is limited to a few cases, and the
runner itself re-checks the request budget and the live preflight before any Gemini request is sent.
"""
from __future__ import annotations

import asyncio
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from claimsleuth.evaluation.runner import EVAL_ROOT, EvaluationError, run_evaluation
from claimsleuth.schemas import utc_now

LIVE_CONFIRMATION = "RUN LIVE EVALUATION"
LIVE_MAX_CASES = 5


class EvaluationBusy(RuntimeError):
    pass


@dataclass
class EvalJob:
    mode: str
    cases: int | None
    state: str = "running"               # running | completed | failed
    started_at: str = field(default_factory=lambda: utc_now().isoformat())
    finished_at: str | None = None
    evaluation_id: str | None = None
    done: int = 0
    total: int | None = None
    current: str | None = None
    stage: str | None = None
    error: str | None = None


class EvaluationManager:
    def __init__(self, root: Path = EVAL_ROOT, mcp_port: int = 8122, crew_port: int = 8123) -> None:
        self.root, self.mcp_port, self.crew_port = root, mcp_port, crew_port
        self.job: EvalJob | None = None
        self._task: asyncio.Task | None = None

    def busy(self) -> bool:
        return self.job is not None and self.job.state == "running"

    def start(self, mode: str, limit: int | None, case_ids: list[str] | None, confirmation: str | None) -> EvalJob:
        if self.busy():
            raise EvaluationBusy("an evaluation is already running")
        if mode not in ("offline", "live"):
            raise EvaluationError("mode must be 'offline' or 'live'")
        if mode == "live":
            if confirmation != LIVE_CONFIRMATION:
                raise EvaluationError(f"a live evaluation needs the confirmation phrase '{LIVE_CONFIRMATION}'")
            n = len(case_ids) if case_ids else limit
            if not n or n > LIVE_MAX_CASES:
                raise EvaluationError(f"a live evaluation from the dashboard is limited to 1-{LIVE_MAX_CASES} cases")
        job = EvalJob(mode=mode, cases=len(case_ids) if case_ids else limit)
        self.job = job

        def progress(p: dict[str, Any]) -> None:
            job.evaluation_id = p.get("evaluation_id") or job.evaluation_id
            job.done, job.total, job.stage = p.get("done", job.done), p.get("total"), p.get("stage")
            job.current = p.get("case_id", job.current)

        async def run() -> None:
            try:
                result = await run_evaluation(mode, case_ids, limit, confirm_live=mode == "live", progress=progress,
                                              mcp_port=self.mcp_port, crew_port=self.crew_port, root=self.root)
                job.evaluation_id = result["meta"]["evaluation_id"]
                job.state = "completed"
            except Exception as exc:      # recorded; the evaluation files of a partial run are not invented
                job.state, job.error = "failed", f"{type(exc).__name__}: {exc}"
                (self.root / "failed").mkdir(parents=True, exist_ok=True)
                (self.root / "failed" / f"{utc_now().strftime('%Y%m%d-%H%M%S')}.txt").write_text(
                    traceback.format_exc(), encoding="utf-8")
            job.finished_at = utc_now().isoformat()

        self._task = asyncio.create_task(run())
        return job

    def status(self) -> dict[str, Any] | None:
        return asdict(self.job) if self.job else None
