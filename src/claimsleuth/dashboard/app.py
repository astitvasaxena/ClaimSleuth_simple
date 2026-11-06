"""The ClaimSleuth web application (Starlette): one static page (Claim Investigation), plus a small JSON API.

    GET  /                                the page (static/index.html)
    GET  /api/status                      execution mode (LIVE / OFFLINE), models and whether the services run
    GET  /api/samples, /api/samples/<f>   synthetic sample claim PDFs
    POST /api/upload                      PDF bytes (Content-Type application/pdf) -> extraction preview
    POST /api/runs                        {"upload_id"} (or {"claim_id"}) -> start a real investigation run
    GET  /api/jobs/<run_id>               progress of a running investigation
    GET  /api/runs/<run_id>               the investigation report (stored run bundle)
    POST /api/runs/<run_id>/review        human review action (recorded separately; never changes the decision)
    GET  /api/runs/<run_id>/blackboard    read-only blackboard monitoring view of the run
    GET  /api/runs/<run_id>/trace         the run's unified execution trace and measured metrics (as stored)
    GET  /api/monitoring/agents           agent metrics across the stored runs' traces
    GET  /api/evidence-integrity          latest controlled tamper test, and integrity events of real investigations
    POST /api/evidence-integrity/run      start the controlled tamper test (offline, disposable databases)

    Agent Evaluation & Performance Analytics (separate from the investigation screen):
    GET  /api/evaluation                  dataset summary, current job, list of stored evaluations
    GET  /api/evaluation/estimate?cases=N  live request estimate and local budget (makes no Gemini request)
    POST /api/evaluation/run              {"mode": "offline"} or {"mode": "live", "cases": N, "confirmation": ...}
    GET  /api/evaluation/results/<id>     one stored evaluation (meta, aggregate metrics, per-case rows)
    GET  /api/evaluation/results/<id>/export.{csv,json,md}
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from claimsleuth import config, execution_mode
from claimsleuth.blackboard.monitor import run_monitor
from claimsleuth.blackboard.store import configured_path
from claimsleuth.dashboard.recovery import IdempotencyStore, InvalidKey, decision_problems, unfinished_runs
from claimsleuth.dashboard.evaluation_jobs import EvaluationBusy, EvaluationManager
from claimsleuth.dashboard.evidence_integrity_lab import LabJob, latest_result
from claimsleuth.dashboard.jobs import RUN_ID, JobBusyError, JobManager
from claimsleuth.dashboard.pdf_intake import MAX_PDF_BYTES, PdfIntakeError
from claimsleuth.dashboard.reviews import HumanReviewInput, ReviewNotAllowed, load_reviews, record_review
from claimsleuth.dashboard.sample_pdf import SAMPLES, SAMPLES_DIR, write_samples
from claimsleuth.dashboard.services import ServiceConfig, check_crew, check_mcp, get_json
from claimsleuth.dashboard.views import request_accounting, run_summary, run_view
from claimsleuth.dashboard.workspace import Workspace, WorkspaceError
from claimsleuth.observability.outcome import outcome_of, records_unconfirmed
from claimsleuth.schemas import ClaimRunBundle, RunStatus, RunTrace

STATIC = Path(__file__).parent / "static"
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; "
       "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


class SecurityHeaders:
    """Pure ASGI middleware (does not buffer the streamed PDF upload): CSP, nosniff, no referrer."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CSP
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(workspace: Workspace | None = None, cfg: ServiceConfig | None = None,
               runtime_dir: Path = config.RUNTIME_DIR, live: bool | None = None,
               evaluation_root: Path | None = None) -> Starlette:
    """live: run investigations with genuine Gemini inference (EXECUTION_MODE=live); None reads EXECUTION_MODE."""
    ws = workspace or Workspace()
    cfg = cfg or ServiceConfig()
    ws.prepare()
    if not all((SAMPLES_DIR / s.filename).exists() for s in SAMPLES):
        write_samples()
    live = execution_mode.execution_mode() == execution_mode.LIVE if live is None else live
    jobs = JobManager(ws, cfg, runtime_dir, live=live)
    evaluations = EvaluationManager(evaluation_root or runtime_dir / "evaluation")
    integrity_lab = LabJob(runtime_dir / "security" / "evidence_integrity")
    injection_lab = LabJob(runtime_dir / "security" / "prompt_injection", "claimsleuth.dashboard.prompt_injection_lab")
    runs_dir = runtime_dir / "runs"
    cache: dict[str, tuple[float, ClaimRunBundle]] = {}

    keys = IdempotencyStore(runtime_dir / "dashboard" / "idempotency")

    def load_bundle(run_id: str) -> ClaimRunBundle | None:
        """The run's stored report, or None. A report that cannot be read is never repaired or guessed at."""
        path = runs_dir / run_id / "bundle.json"
        if not RUN_ID.fullmatch(run_id) or not path.exists():
            return None
        try:
            mtime = path.stat().st_mtime
            if run_id not in cache or cache[run_id][0] != mtime:
                cache[run_id] = (mtime, ClaimRunBundle.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
        return cache[run_id][1]

    def unfinished() -> list[dict[str, Any]]:
        return unfinished_runs(runs_dir, jobs.active(), load_bundle, configured_path())

    def unfinished_run(run_id: str) -> dict[str, Any] | None:
        return next((r for r in unfinished() if r["run_id"] == run_id), None) if RUN_ID.fullmatch(run_id) else None

    def no_report(row: dict[str, Any]) -> JSONResponse:
        return JSONResponse({**row, "error": f"run {row['run_id']} has no usable report ({row['state']}, last stage "
                                             f"{row['stage']}); start a new investigation for this claim"}, status_code=409)

    def reliability_summary() -> dict[str, Any]:
        """Totals over this runtime's stored runs, for the reliability line. Runs that did not come from the dashboard
        (scripts, demonstrations) are listed and marked, and left out of the totals."""
        rows = unfinished()
        actual = [r for r in rows if r["actual"]]
        totals = {k: 0 for k in ("stored", "offline_tests", "completed", "escalated", "incomplete", "blocked", "records_unavailable",
                                 "provider_failures", "provider_recovered", "failures_not_classified", "human_review_required",
                                 "accounting_warnings")}
        for path in runs_dir.glob("*/bundle.json") if runs_dir.exists() else []:
            bundle = load_bundle(path.parent.name)
            if bundle is None or not bundle.run_id.startswith("RUN-dash-"):
                continue
            outcome = outcome_of(bundle)
            modes = {meta.llm_mode for meta in (bundle.workflow_metadata.get(side) for side in ("adk", "crewai")) if meta}
            totals["stored"] += 1
            totals["offline_tests"] += "test-double" in modes
            totals["records_unavailable" if records_unconfirmed(bundle) else outcome.run_status.value.lower()] += 1
            totals["provider_failures"] += len(outcome.failures)                # what no retry recovered
            totals["provider_recovered"] += sum(r.outcome == "recovered" for r in outcome.retries)
            totals["failures_not_classified"] += (not outcome.failures_recorded and not outcome.failures
                                                  and outcome.run_status == RunStatus.INCOMPLETE)
            totals["human_review_required"] += bool(outcome.requires_human_approval)
            accounting = request_accounting(bundle)
            totals["accounting_warnings"] += bool(accounting and not accounting["match"])
        for state in ("INTERRUPTED", "FAILED", "DAMAGED", "RECOVERED"):
            totals[state.lower()] = sum(r["state"] == state for r in actual)
        totals["duplicates_prevented"] = keys.duplicates()
        return {"totals": totals, "runs": rows[:50]}

    def meta_for(run_id: str) -> dict | None:
        path = runs_dir / run_id / "dashboard_meta.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def latest_live_attempt() -> dict[str, Any] | None:
        """The most recent run whose CrewAI side used genuine model inference (shown honestly, even if it failed)."""
        live = []
        for path in runs_dir.glob("*/bundle.json") if runs_dir.exists() else []:
            try:
                bundle = load_bundle(path.parent.name)
            except Exception:           # an unreadable bundle is skipped, not repaired
                continue
            meta = bundle.workflow_metadata.get("crewai") if bundle else None
            if meta is not None and meta.llm_mode == "live":
                summary = run_summary(bundle)
                summary["errors"] = (bundle.crewai.errors if bundle.crewai else [])[:3]
                live.append(summary)
        return max(live, key=lambda s: s["started_at"] or "") if live else None

    async def index(request: Request) -> Response:
        return FileResponse(STATIC / "index.html")

    async def status(request: Request) -> JSONResponse:
        mcp, crew = await asyncio.gather(asyncio.to_thread(check_mcp, cfg, ws.data_dir),
                                         asyncio.to_thread(check_crew, cfg, ws.data_dir))
        busy = jobs.busy()
        crew_mismatch = live and crew["status"] == "RUNNING" and crew.get("llm_mode") != execution_mode.GEMINI
        return JSONResponse({
            "execution_mode": "LIVE" if live else "OFFLINE",
            "provider": execution_mode.PROVIDER_GEMINI if live else "offline-test-double",
            "gemini_model": execution_mode.gemini_model() if live else None,
            "gemini_key_configured": not execution_mode.gemini_config_errors() if live else None,   # never the key
            "adk_mode": "gemini" if live else "test-double",
            "crew_mode": crew.get("llm_mode"), "crew_model": crew.get("model"),
            "crew_mode_mismatch": crew_mismatch,
            "services": {"mcp": mcp["status"], "crewai": crew["status"]},
            "warnings": [f"{s['name']}: {s['warning']}" for s in (mcp, crew) if s.get("warning")],
            "busy": busy.run_id if busy else None,
            "currency": config.CURRENCY, "human_approval_threshold": str(config.HUMAN_APPROVAL_THRESHOLD),
            "latest_live_attempt": await asyncio.to_thread(latest_live_attempt)})

    async def samples(request: Request) -> JSONResponse:
        return JSONResponse({"samples": [{"filename": s.filename, "label": s.label, "title": s.title,
                                          "description": s.description, "claim_id": s.claim["claim_id"]}
                                         for s in SAMPLES]})

    async def sample_file(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in {s.filename for s in SAMPLES}:
            return JSONResponse({"error": "unknown sample"}, status_code=404)
        return FileResponse(SAMPLES_DIR / name, media_type="application/pdf", filename=name)

    async def upload(request: Request) -> JSONResponse:
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        filename = unquote(request.headers.get("x-filename", "upload.pdf"))
        if ctype != "application/pdf" or not filename.lower().endswith(".pdf"):
            return JSONResponse({"error": f"Only PDF files are accepted (got '{ctype or 'no type'}', '{filename}')."},
                                status_code=415)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_PDF_BYTES:
                return JSONResponse({"error": "The file is larger than 5 MB."}, status_code=413)
        try:
            draft = await asyncio.to_thread(ws.store_upload, bytes(body), filename)
        except PdfIntakeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(draft.to_dict())

    async def start_run(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "JSON body required"}, status_code=400)
        mcp, crew = await asyncio.gather(asyncio.to_thread(check_mcp, cfg, ws.data_dir),
                                         asyncio.to_thread(check_crew, cfg, ws.data_dir))
        key = request.headers.get("idempotency-key")
        subject = str(body.get("upload_id") or body.get("claim_id", ""))

        def repeated(held: dict[str, Any]) -> JSONResponse:
            if held["subject"] != subject:
                return JSONResponse({"error": "this Idempotency-Key was used for another claim"}, status_code=409)
            if held["run_id"] is None:
                return JSONResponse({"error": "this request is already being processed"}, status_code=409)
            keys.note_duplicate(held["run_id"])
            return JSONResponse({"run_id": held["run_id"], "claim_id": None, "duplicate": True})

        if key is not None:
            try:
                held = keys.lookup(key)
            except InvalidKey as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            if held is not None:
                return repeated(held)
        down = [s["name"] for s in (mcp, crew) if s["status"] != "RUNNING"]
        if down:
            return JSONResponse({"error": f"Cannot start: {' and '.join(down)} not running. Start ClaimSleuth with "
                                          "`python -m claimsleuth.dashboard`, which starts them."}, status_code=503)
        if evaluations.busy():
            return JSONResponse({"error": "An agent evaluation is running; start the investigation when it has "
                                          "finished."}, status_code=409)
        if live and crew.get("llm_mode") != execution_mode.GEMINI:
            return JSONResponse({"error": f"Cannot start a LIVE run: the CrewAI A2A service on port {cfg.crew_port} "
                                          f"runs in mode '{crew.get('llm_mode')}', not Gemini. Stop it and restart "
                                          "the dashboard with --mode live (or scripts/start_live.ps1). Nothing was run."},
                                status_code=503)
        try:
            if body.get("upload_id"):
                claim_id = await asyncio.to_thread(ws.materialise_upload, str(body["upload_id"]))
                origin = ws.origin(claim_id) or {}
                source = {"kind": "uploaded PDF", "label": f"uploaded PDF {origin.get('filename')}"}
            else:
                claim_id = str(body.get("claim_id", ""))
                if claim_id not in {c["claim_id"] for c in ws.claims()}:
                    return JSONResponse({"error": f"unknown claim {claim_id!r}"}, status_code=404)
                origin = ws.origin(claim_id) or {}
                source = {"kind": origin.get("origin", "workspace claim"),
                          "label": f"{origin.get('origin', 'workspace claim')} {claim_id}"}
            recovers = body.get("recovers")
            if recovers is not None:
                row = await asyncio.to_thread(unfinished_run, str(recovers))
                if row is None or row["state"] == "RECOVERED" or row["claim_id"] != claim_id:
                    return JSONResponse({"error": "only an unfinished run of this claim can be recovered"}, status_code=409)
            if key is not None:
                held = keys.reserve(key, subject)
                if held is not None:
                    return repeated(held)
            try:
                job = jobs.start(claim_id, source, crew.get("llm_mode"), recovers=recovers)
            except Exception:
                if key is not None:
                    keys.release(key)
                raise
            if key is not None:
                keys.bind(key, job.run_id)
        except WorkspaceError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except JobBusyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"run_id": job.run_id, "claim_id": claim_id, "crew_mode": job.crew_mode}, status_code=202)

    async def job_status(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        live = await asyncio.to_thread(jobs.live, run_id)
        if live:
            return JSONResponse(live)
        if await asyncio.to_thread(load_bundle, run_id) is not None:       # finished before this process started
            return JSONResponse({"run_id": run_id, "state": "completed", "stages": []})
        row = await asyncio.to_thread(unfinished_run, run_id)
        if row is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        return JSONResponse({**row, "state": row["state"].lower(), "stages": []})

    async def get_run(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        bundle = await asyncio.to_thread(load_bundle, run_id)
        if bundle is None:
            row = await asyncio.to_thread(unfinished_run, run_id)
            return no_report(row) if row else JSONResponse({"error": "unknown run"}, status_code=404)
        try:
            claim, docs = ws.load(bundle.claim_id)
        except Exception:
            claim, docs = None, []
        _, log = await asyncio.to_thread(get_json, f"{cfg.crew_base}/messages?run_id={run_id}")
        view = await asyncio.to_thread(run_view, bundle, claim, docs, ws.origin(bundle.claim_id), meta_for(run_id),
                                       (log or {}).get("messages") if isinstance(log, dict) else None,
                                       load_reviews(runs_dir / run_id))
        return JSONResponse(view)

    async def run_blackboard(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        if not RUN_ID.fullmatch(run_id):
            return JSONResponse({"error": "unknown run"}, status_code=404)
        bundle = await asyncio.to_thread(load_bundle, run_id)
        decision = bundle.final_report.final_decision.value if bundle and bundle.final_report else None
        return JSONResponse(await asyncio.to_thread(run_monitor, run_id, None, decision))

    async def run_trace(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        bundle = await asyncio.to_thread(load_bundle, run_id)
        if bundle is None:
            row = await asyncio.to_thread(unfinished_run, run_id)
            return no_report(row) if row else JSONResponse({"error": "unknown run"}, status_code=404)
        outcome = outcome_of(bundle).model_dump(mode="json")
        if bundle.trace is None:
            return JSONResponse({"available": False, "run_id": run_id, "outcome": outcome,
                                 "reason": "this run was stored before the execution trace was recorded"})
        return JSONResponse({"available": True, "outcome": outcome, **bundle.trace.model_dump(mode="json")})

    def stored_traces(limit: int = 100) -> list[RunTrace]:
        paths = sorted(runs_dir.glob("*/trace.json"), key=lambda q: q.stat().st_mtime, reverse=True)[:limit]
        out = []
        for path in paths:
            try:
                out.append(RunTrace.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue                    # an unreadable trace is skipped, never guessed
        return out

    async def monitoring_agents(request: Request) -> JSONResponse:
        from claimsleuth.observability.metrics import agent_metrics
        traces = await asyncio.to_thread(stored_traces)
        return JSONResponse({"runs": len(traces), "agents": agent_metrics(traces),
                             "executions": sorted({t.metrics.execution for t in traces if t.metrics}),
                             "source": "stored run traces (runtime/runs/<run_id>/trace.json)"})

    async def evidence_integrity(request: Request) -> JSONResponse:
        """Controlled tests and real investigations stay apart: a real run's events come from its stored trace."""
        traces = await asyncio.to_thread(stored_traces)
        events = [e for t in traces for e in t.events
                  if e.event_type == "security.event" and not e.metadata.get("test_mode")]
        return JSONResponse({
            "controlled_test": await asyncio.to_thread(latest_result, integrity_lab.out_dir), "job": integrity_lab.state,
            "real_investigations": {"runs_checked": len(traces), "events": [{
                "run_id": e.run_id, "claim_id": e.claim_id, "timestamp": e.timestamp.isoformat(), "attack_type": e.error_code,
                "consumer": e.agent_name, "section": e.metadata.get("section"), "severity": e.metadata.get("severity"),
                "payload_integrity": e.metadata.get("payload_integrity"), "signature": e.metadata.get("signature_check"),
                "downstream_blocked": e.metadata.get("downstream_blocked"),
                "escalation_status": e.metadata.get("escalation_status")} for e in events]}})

    def start_lab(lab: LabJob) -> JSONResponse:
        if integrity_lab.busy() or injection_lab.busy() or jobs.busy() or evaluations.busy():
            return JSONResponse({"error": "An investigation, an evaluation or a test is running; start the test when it "
                                          "has finished."}, status_code=409)
        lab.start()
        return JSONResponse({"job": lab.state}, status_code=202)

    async def evidence_integrity_run(request: Request) -> JSONResponse:
        return start_lab(integrity_lab)

    async def prompt_injection(request: Request) -> JSONResponse:
        """As for evidence integrity: controlled tests apart from real investigations, whose events come from stored traces."""
        traces = await asyncio.to_thread(stored_traces)
        events = [e for t in traces for e in t.events
                  if e.event_type == "security.injection" and not e.metadata.get("test_mode")]
        return JSONResponse({
            "controlled_test": await asyncio.to_thread(latest_result, injection_lab.out_dir), "job": injection_lab.state,
            "real_investigations": {"runs_checked": len(traces), "events": [{
                "run_id": e.run_id, "claim_id": e.claim_id, "timestamp": e.timestamp.isoformat(), "attack_type": e.error_code,
                "agent": e.agent_name, "source": e.metadata.get("source"), "severity": e.metadata.get("severity"),
                "detection_status": e.metadata.get("detection_status"), "rejection_status": e.metadata.get("rejection_status"),
                "downstream_impact": e.metadata.get("downstream_impact"),
                "escalation_status": e.metadata.get("escalation_status")} for e in events]}})

    async def prompt_injection_run(request: Request) -> JSONResponse:
        return start_lab(injection_lab)

    async def review(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        bundle = await asyncio.to_thread(load_bundle, run_id)
        if bundle is None:
            return JSONResponse({"error": "unknown run"}, status_code=404)
        board = await asyncio.to_thread(run_monitor, run_id, None, None)
        integrity = decision_problems(bundle, board.get("decision"))
        if any(status != "valid" for status in (board.get("authentication") or {})):
            integrity.append("blackboard entries failed authentication")
        try:
            data = HumanReviewInput.model_validate(await request.json())
            record = await asyncio.to_thread(record_review, runs_dir / run_id, bundle, data, integrity)
        except json.JSONDecodeError:
            return JSONResponse({"error": "JSON body required"}, status_code=400)
        except ValidationError as exc:
            return JSONResponse({"error": "; ".join(e["msg"] for e in exc.errors())}, status_code=400)
        except ReviewNotAllowed as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(record.model_dump(mode="json"), status_code=200 if record.effect == "REPEATED" else 201)

    async def reliability(request: Request) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(reliability_summary))

    # ------------------------------------------------------------ agent evaluation & performance analytics
    from claimsleuth.evaluation import dataset as eval_dataset
    from claimsleuth.evaluation import runner as eval_runner

    async def evaluation_home(request: Request) -> JSONResponse:
        try:
            summary = await asyncio.to_thread(lambda: eval_dataset.summary(eval_dataset.load()))
        except Exception as exc:           # a broken dataset file is reported, not hidden
            summary = {"error": f"{type(exc).__name__}: {exc}"}
        return JSONResponse({"dataset": summary, "job": evaluations.status(),
                             "evaluations": await asyncio.to_thread(eval_runner.list_results, evaluations.root),
                             "dashboard_mode": "LIVE" if live else "OFFLINE",
                             "live_confirmation": "RUN LIVE EVALUATION", "live_max_cases": 5})

    async def evaluation_estimate(request: Request) -> JSONResponse:
        try:
            n = int(request.query_params.get("cases", "1"))
        except ValueError:
            return JSONResponse({"error": "cases must be an integer"}, status_code=400)
        execution_mode.load_env()
        return JSONResponse({**eval_runner.live_estimate(n),
                             "key_configured": not execution_mode.gemini_config_errors()})

    async def evaluation_run(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "JSON body required"}, status_code=400)
        if jobs.busy():
            return JSONResponse({"error": "An investigation is running; start the evaluation when it has finished."},
                                status_code=409)
        mode = str(body.get("mode", "offline"))
        cases = body.get("cases")
        try:
            limit = int(cases) if cases not in (None, "", "all") else None
            job = evaluations.start(mode, limit, None, body.get("confirmation"))
        except (ValueError, TypeError):
            return JSONResponse({"error": "cases must be a number or 'all'"}, status_code=400)
        except EvaluationBusy as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except eval_runner.EvaluationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"job": evaluations.status(), "mode": job.mode}, status_code=202)

    async def evaluation_result(request: Request) -> JSONResponse:
        result = await asyncio.to_thread(eval_runner.load_result, request.path_params["eval_id"], evaluations.root)
        if result is None:
            return JSONResponse({"error": "unknown evaluation"}, status_code=404)
        return JSONResponse(result)

    async def evaluation_export(request: Request) -> Response:
        eval_id, fmt = request.path_params["eval_id"], request.path_params["fmt"]
        files = {"csv": ("cases.csv", "text/csv"), "json": ("metrics.json", "application/json"),
                 "md": ("report.md", "text/markdown")}
        if fmt not in files or eval_runner.load_result(eval_id, evaluations.root) is None:
            return JSONResponse({"error": "unknown evaluation or format"}, status_code=404)
        name, media = files[fmt]
        return FileResponse(evaluations.root / eval_id / name, media_type=f"{media}; charset=utf-8",
                            filename=f"{eval_id}-{name}")

    routes = [
        Route("/", index), Route("/api/status", status),
        Route("/api/samples", samples), Route("/api/samples/{name}", sample_file),
        Route("/api/upload", upload, methods=["POST"]),
        Route("/api/runs", start_run, methods=["POST"]),
        Route("/api/jobs/{run_id}", job_status),
        Route("/api/runs/{run_id}", get_run), Route("/api/runs/{run_id}/review", review, methods=["POST"]),
        Route("/api/runs/{run_id}/blackboard", run_blackboard),
        Route("/api/runs/{run_id}/trace", run_trace), Route("/api/monitoring/agents", monitoring_agents),
        Route("/api/reliability", reliability),
        Route("/api/evidence-integrity", evidence_integrity),
        Route("/api/evidence-integrity/run", evidence_integrity_run, methods=["POST"]),
        Route("/api/prompt-injection", prompt_injection),
        Route("/api/prompt-injection/run", prompt_injection_run, methods=["POST"]),
        Route("/api/evaluation", evaluation_home), Route("/api/evaluation/estimate", evaluation_estimate),
        Route("/api/evaluation/run", evaluation_run, methods=["POST"]),
        Route("/api/evaluation/results/{eval_id}", evaluation_result),
        Route("/api/evaluation/results/{eval_id}/export.{fmt}", evaluation_export),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    return Starlette(routes=routes, middleware=[Middleware(SecurityHeaders)])
