"""The ClaimSleuth web application (Starlette): one static page (Claim Investigation), plus a small JSON API.

    GET  /                                the page (static/index.html)
    GET  /api/status                      execution mode and whether the services are running
    GET  /api/samples, /api/samples/<f>   synthetic sample claim PDFs
    POST /api/upload                      PDF bytes (Content-Type application/pdf) -> extraction preview
    POST /api/runs                        {"upload_id"} (or {"claim_id"}) -> start a real investigation run
    GET  /api/jobs/<run_id>               progress of a running investigation
    GET  /api/runs/<run_id>               the investigation report (stored run bundle)
    POST /api/runs/<run_id>/review        human review action (recorded separately; never changes the decision)
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

from claimsleuth import config
from claimsleuth.dashboard.jobs import RUN_ID, JobBusyError, JobManager
from claimsleuth.dashboard.pdf_intake import MAX_PDF_BYTES, PdfIntakeError
from claimsleuth.dashboard.reviews import HumanReviewInput, ReviewNotAllowed, load_reviews, record_review
from claimsleuth.dashboard.sample_pdf import SAMPLES, SAMPLES_DIR, write_samples
from claimsleuth.dashboard.services import ServiceConfig, check_crew, check_mcp, get_json
from claimsleuth.dashboard.views import run_summary, run_view
from claimsleuth.dashboard.workspace import Workspace, WorkspaceError
from claimsleuth.schemas import ClaimRunBundle

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
               runtime_dir: Path = config.RUNTIME_DIR) -> Starlette:
    ws = workspace or Workspace()
    cfg = cfg or ServiceConfig()
    ws.prepare()
    if not all((SAMPLES_DIR / s.filename).exists() for s in SAMPLES):
        write_samples()
    jobs = JobManager(ws, cfg, runtime_dir)
    runs_dir = runtime_dir / "runs"
    cache: dict[str, tuple[float, ClaimRunBundle]] = {}

    def load_bundle(run_id: str) -> ClaimRunBundle | None:
        path = runs_dir / run_id / "bundle.json"
        if not RUN_ID.fullmatch(run_id) or not path.exists():
            return None
        mtime = path.stat().st_mtime
        if run_id not in cache or cache[run_id][0] != mtime:
            cache[run_id] = (mtime, ClaimRunBundle.model_validate_json(path.read_text(encoding="utf-8")))
        return cache[run_id][1]

    def meta_for(run_id: str) -> dict | None:
        path = runs_dir / run_id / "dashboard_meta.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def latest_live_attempt() -> dict[str, Any] | None:
        """The most recent run whose CrewAI side used genuine local inference (shown honestly, even if it failed)."""
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
        return JSONResponse({
            "adk_mode": "test-double", "crew_mode": crew.get("llm_mode"), "crew_model": crew.get("model"),
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
        down = [s["name"] for s in (mcp, crew) if s["status"] != "RUNNING"]
        if down:
            return JSONResponse({"error": f"Cannot start: {' and '.join(down)} not running. Start ClaimSleuth with "
                                          "`python -m claimsleuth.dashboard`, which starts them."}, status_code=503)
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
            job = jobs.start(claim_id, source, crew.get("llm_mode"))
        except WorkspaceError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except JobBusyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"run_id": job.run_id, "claim_id": claim_id, "crew_mode": job.crew_mode}, status_code=202)

    async def job_status(request: Request) -> JSONResponse:
        live = await asyncio.to_thread(jobs.live, request.path_params["run_id"])
        return JSONResponse(live) if live else JSONResponse({"error": "unknown job"}, status_code=404)

    async def get_run(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        bundle = await asyncio.to_thread(load_bundle, run_id)
        if bundle is None:
            return JSONResponse({"error": "unknown run"}, status_code=404)
        try:
            claim, docs = ws.load(bundle.claim_id)
        except Exception:
            claim, docs = None, []
        _, log = await asyncio.to_thread(get_json, f"{cfg.crew_base}/messages?run_id={run_id}")
        view = await asyncio.to_thread(run_view, bundle, claim, docs, ws.origin(bundle.claim_id), meta_for(run_id),
                                       (log or {}).get("messages") if isinstance(log, dict) else None,
                                       load_reviews(runs_dir / run_id))
        return JSONResponse(view)

    async def review(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        bundle = await asyncio.to_thread(load_bundle, run_id)
        if bundle is None:
            return JSONResponse({"error": "unknown run"}, status_code=404)
        try:
            data = HumanReviewInput.model_validate(await request.json())
            record = await asyncio.to_thread(record_review, runs_dir / run_id, bundle, data)
        except json.JSONDecodeError:
            return JSONResponse({"error": "JSON body required"}, status_code=400)
        except ValidationError as exc:
            return JSONResponse({"error": "; ".join(e["msg"] for e in exc.errors())}, status_code=400)
        except ReviewNotAllowed as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse(record.model_dump(mode="json"), status_code=201)

    routes = [
        Route("/", index), Route("/api/status", status),
        Route("/api/samples", samples), Route("/api/samples/{name}", sample_file),
        Route("/api/upload", upload, methods=["POST"]),
        Route("/api/runs", start_run, methods=["POST"]),
        Route("/api/jobs/{run_id}", job_status),
        Route("/api/runs/{run_id}", get_run), Route("/api/runs/{run_id}/review", review, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    return Starlette(routes=routes, middleware=[Middleware(SecurityHeaders)])
