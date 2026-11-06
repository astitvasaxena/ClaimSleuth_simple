"""Run the agent evaluation: every selected case once through the existing integrated workflow, then score.

OFFLINE (default): the labelled test doubles for all three agents; no Gemini request is possible (the CrewAI run is
inside the loopback-only network guard, the ADK agents use OfflineAdkLlm) and the request ledger is checked to be
unchanged.

LIVE (explicit opt-in only): genuine Gemini inference. Before anything runs it requires `confirm_live=True`, shows
the request estimate, checks the shared request ledger's daily budget and the live preflight. Requests go through
the shared ledger (per-minute pacing; only an HTTP 503 is retried, once per agent). The evaluation stops at the first
case that hits a 429, a 503 the retry did not recover or a budget refusal; the remaining cases are reported as not run.

The evaluation uses its own MCP server and CrewAI A2A service (default ports 8122/8123) on its own data directory
(runtime/evaluation/data-eval-v1), so a dashboard and its services are never touched. Results are written to
runtime/evaluation/<evaluation id>/: results.json, cases.csv, metrics.json, report.md and runs/<run id>/.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claimsleuth import config, execution_mode, gemini_rate
from claimsleuth.datastore import load_claim
from claimsleuth.evaluation import dataset as ds
from claimsleuth.evaluation import report
from claimsleuth.evaluation.scoring import score_case
from claimsleuth.integration.run import LIVE_REQUESTS_ESTIMATE, LiveModeError, live_preflight, run_claim_async, save_bundle
from claimsleuth.schemas import utc_now

EVAL_ROOT = config.RUNTIME_DIR / "evaluation"
MAIN_PYTHON = config.PROJECT_ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
CREW_PYTHON = config.PROJECT_ROOT / ".venv-crew" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
STOP_MARKERS = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "GeminiQuotaGuard", "budget", "quota")
ProgressCallback = Callable[[dict[str, Any]], None]


class EvaluationError(RuntimeError):
    """The evaluation cannot start (configuration, services, live budget or confirmation)."""


def _get(url: str, timeout: float = 5.0) -> int | None:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


@dataclass
class EvalServices:
    """The evaluation's own MCP server (.venv) and CrewAI A2A service (.venv-crew)."""
    data_dir: Path
    work_dir: Path
    mode: str
    mcp_port: int = 8122
    crew_port: int = 8123
    blackboard: Path | None = None        # the evaluation's own blackboard, shared with its CrewAI service

    def __post_init__(self) -> None:
        self.procs: list[tuple[subprocess.Popen, Any]] = []

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.mcp_port}/mcp"

    @property
    def crew_card(self) -> str:
        return f"http://127.0.0.1:{self.crew_port}/.well-known/agent-card.json"

    def _spawn(self, name: str, cmd: list[str], env: dict[str, str]) -> subprocess.Popen:
        log = (self.work_dir / "logs" / f"{name}.log").open("a", encoding="utf-8")
        proc = subprocess.Popen(cmd, cwd=config.PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8", **env})
        self.procs.append((proc, log))
        return proc

    def _wait(self, url: str, proc: subprocess.Popen, seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if _get(url, timeout=10) == 200:
                return True
            if proc.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def start(self) -> None:
        for port in (self.mcp_port, self.crew_port):
            if _get(f"http://127.0.0.1:{port}/health", timeout=2) is not None:
                raise EvaluationError(f"port {port} is already in use; the evaluation needs its own services")
        if not CREW_PYTHON.exists():
            raise EvaluationError(f"CrewAI environment not found at {CREW_PYTHON}")
        for sub in ("logs", "pids"):
            (self.work_dir / sub).mkdir(parents=True, exist_ok=True)
        db = self.data_dir / "claimsleuth.db"
        mcp = self._spawn("mcp_server", [str(MAIN_PYTHON), "-m", "claimsleuth.mcp_server", "--port", str(self.mcp_port),
                                         "--data-dir", str(self.data_dir), "--db", str(db),
                                         "--log-file", str(self.work_dir / "logs" / "mcp_tool_calls.jsonl"),
                                         "--pid-file", str(self.work_dir / "pids" / "mcp_server.pid")], {})
        if not self._wait(f"http://127.0.0.1:{self.mcp_port}/health", mcp, 60):
            self.stop()
            raise EvaluationError("the evaluation MCP server did not start (see its log in the evaluation folder)")
        crew_mode = execution_mode.GEMINI if self.mode == execution_mode.LIVE else execution_mode.TEST_DOUBLE
        crew = self._spawn("crew_a2a", [str(CREW_PYTHON), "-m", "claimsleuth.crew_service.a2a_server",
                                        "--port", str(self.crew_port), "--mcp-url", self.mcp_url,
                                        "--data-dir", str(self.data_dir),
                                        "--log-file", str(self.work_dir / "logs" / "a2a_messages.jsonl"),
                                        "--pid-file", str(self.work_dir / "pids" / "crew_a2a.pid")],
                           {execution_mode.ENV_VAR: self.mode, "CLAIMSLEUTH_CREW_LLM": crew_mode,
                            "CLAIMSLEUTH_BLACKBOARD_DB": str(self.blackboard) if self.blackboard else "off"})
        if not self._wait(f"http://127.0.0.1:{self.crew_port}/health", crew, 180):
            self.stop()
            raise EvaluationError("the evaluation CrewAI service did not become healthy (see its log)")

    def stop(self) -> None:
        for proc, log in reversed(self.procs):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
            log.close()
        self.procs.clear()


def select_cases(dataset: ds.EvalDataset, case_ids: list[str] | None = None, limit: int | None = None
                 ) -> list[ds.EvalCase]:
    """The requested cases. `limit` takes cases round-robin across the five categories (stratified)."""
    cases = dataset.cases
    if case_ids:
        unknown = set(case_ids) - {c.case_id for c in cases}
        if unknown:
            raise EvaluationError(f"unknown case id(s): {sorted(unknown)}")
        cases = [c for c in cases if c.case_id in case_ids]
    if limit is not None:
        buckets = [[c for c in cases if c.ground_truth.category == k] for k in ds.CATEGORIES]
        picked, i = [], 0
        while len(picked) < min(limit, len(cases)):
            for b in buckets:
                if i < len(b) and len(picked) < limit:
                    picked.append(b[i])
            i += 1
        cases = sorted(picked, key=lambda c: c.case_id)
    return cases


def live_estimate(n_cases: int) -> dict[str, Any]:
    """What a live evaluation of n cases is expected to cost, and whether the local daily budget allows it."""
    model = execution_mode.gemini_model()
    rpm, rpd = gemini_rate.limits()
    used = gemini_rate.usage(model)["today"]
    expected = n_cases * LIVE_REQUESTS_ESTIMATE
    return {"cases": n_cases, "requests_per_case_estimate": LIVE_REQUESTS_ESTIMATE, "requests_estimate": expected,
            "requests_upper_bound": n_cases * 18, "model": model, "per_minute_limit": rpm, "daily_budget": rpd,
            "used_today": used, "remaining_today": (rpd - used) if rpd else None,
            "within_budget": (not rpd) or used + expected <= rpd,
            "minimum_minutes": round(expected / rpm, 1),
            "note": "The local budget only counts requests ClaimSleuth recorded; Google's own quota may be lower."}


def _stop_reason(row: dict[str, Any]) -> str | None:
    texts = [row.get("run_error") or ""] + [e for a in ("evidence", "investigator", "forensic", "combined")
                                             for e in (row[a].get("errors") or [])]
    for text in texts:
        if any(m in text for m in STOP_MARKERS):
            return text[:300]
    return None


async def run_evaluation(mode: str = "offline", case_ids: list[str] | None = None, limit: int | None = None,
                         confirm_live: bool = False, progress: ProgressCallback | None = None,
                         mcp_port: int = 8122, crew_port: int = 8123, root: Path = EVAL_ROOT,
                         dataset_path: Path = ds.DATASET_FILE) -> dict[str, Any]:
    if mode not in (execution_mode.LIVE, execution_mode.OFFLINE):
        raise EvaluationError(f"mode must be 'offline' or 'live', not {mode!r}")
    dataset = ds.load(dataset_path)
    cases = select_cases(dataset, case_ids, limit)
    live = mode == execution_mode.LIVE
    estimate = None
    if live:
        if not confirm_live:
            raise EvaluationError("a live evaluation sends real Gemini requests and needs explicit confirmation")
        execution_mode.load_env()
        problems = execution_mode.gemini_config_errors()
        if problems:
            raise EvaluationError(problems[0])
        estimate = live_estimate(len(cases))
        if not estimate["within_budget"]:
            raise EvaluationError(f"not enough local daily Gemini budget: {estimate['used_today']} of "
                                  f"{estimate['daily_budget']} used, about {estimate['requests_estimate']} needed for "
                                  f"{len(cases)} case(s). Nothing was run.")

    eval_id = f"EV-{utc_now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    work = root / eval_id
    work.mkdir(parents=True)
    data_dir = ds.materialize(dataset, root / f"data-{dataset.version}-{eval_id}")
    ledger_before = gemini_rate.total_requests()
    meta: dict[str, Any] = {
        "evaluation_id": eval_id, "dataset_version": dataset.version, "mode": mode,
        "mode_label": "LIVE (Gemini)" if live else "OFFLINE (labelled test doubles, not LLM reasoning)",
        "started_at": utc_now().isoformat(), "finished_at": None, "cases_total": len(dataset.cases),
        "cases_requested": len(cases), "cases_executed": 0, "stopped_reason": None, "live_estimate": estimate,
        "gemini_model": execution_mode.gemini_model() if live else None, "synthetic": True,
        "context_sharing": "blackboard", "blackboard": str(work / "blackboard.sqlite3")}
    rows: list[dict[str, Any]] = []

    def emit(**kw) -> None:
        if progress is not None:
            try:
                progress({"evaluation_id": eval_id, "total": len(cases), "done": len(rows), **kw})
            except Exception:
                pass

    # Each evaluation has its own blackboard: the shared-context path is exercised exactly as in the dashboard,
    # without touching runtime/blackboard.sqlite3.
    services = EvalServices(data_dir, work, mode, mcp_port, crew_port, blackboard=work / "blackboard.sqlite3")
    emit(stage="starting services")
    try:
        await asyncio.to_thread(services.start)
        with execution_mode.forced(mode):
            if live:
                try:
                    await asyncio.to_thread(live_preflight, services.crew_card)
                except LiveModeError as exc:
                    raise EvaluationError(f"live preflight refused: {exc}") from exc
            for i, case in enumerate(cases, start=1):
                emit(stage="running", case_id=case.case_id, index=i)
                run_id = f"RUN-{eval_id}-{case.case_id}"
                _, docs = load_claim(case.case_id, data_dir)
                t0 = time.perf_counter()
                bundle, error = None, None
                try:
                    bundle = await run_claim_async(case.case_id, run_id=run_id, mcp_url=services.mcp_url,
                                                   crew_card_url=services.crew_card, data_dir=data_dir,
                                                   db_path=data_dir / "claimsleuth.db",
                                                   blackboard_path=services.blackboard)
                except Exception as exc:         # recorded as a failed execution, never as a prediction
                    error = f"{type(exc).__name__}: {exc}"
                wall = round((time.perf_counter() - t0) * 1000, 3)
                if bundle is not None:
                    save_bundle(bundle, work)
                row = score_case(case, bundle, wall, docs, live, error)
                rows.append(row)
                meta["cases_executed"] = len(rows)
                if live:
                    reason = _stop_reason(row)
                    if reason:
                        meta["stopped_reason"] = f"stopped after {case.case_id}: {reason}"
                        break
    finally:
        await asyncio.to_thread(services.stop)
    meta["finished_at"] = utc_now().isoformat()
    meta["ledger_requests_added"] = gemini_rate.total_requests() - ledger_before
    if not live and meta["ledger_requests_added"]:
        meta["warning"] = "the request ledger changed during an offline evaluation"
    result = {"meta": meta, "rows": rows, "aggregate": report.aggregate(rows, meta),
              "not_run": [c.case_id for c in cases[len(rows):]]}
    write_outputs(result, work)
    emit(stage="finished")
    return result


def write_outputs(result: dict[str, Any], work: Path) -> None:
    (work / "results.json").write_text(json.dumps(result, indent=1, default=str) + "\n", encoding="utf-8")
    (work / "cases.csv").write_text(report.to_csv(result["rows"]), encoding="utf-8")
    (work / "metrics.json").write_text(report.to_json(result), encoding="utf-8")
    (work / "report.md").write_text(report.to_markdown(result), encoding="utf-8")


def latest_result(root: Path = EVAL_ROOT) -> dict[str, Any] | None:
    files = sorted(root.glob("EV-*/results.json"), key=lambda p: p.stat().st_mtime) if root.exists() else []
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def load_result(eval_id: str, root: Path = EVAL_ROOT) -> dict[str, Any] | None:
    path = root / eval_id / "results.json"
    if not eval_id.startswith("EV-") or "/" in eval_id or "\\" in eval_id or ".." in eval_id or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_results(root: Path = EVAL_ROOT) -> list[dict[str, Any]]:
    out = []
    for path in sorted(root.glob("EV-*/results.json"), key=lambda p: p.stat().st_mtime, reverse=True) if root.exists() else []:
        meta = json.loads(path.read_text(encoding="utf-8"))["meta"]
        out.append({k: meta.get(k) for k in ("evaluation_id", "mode", "started_at", "finished_at",
                                              "cases_requested", "cases_executed", "stopped_reason")})
    return out
