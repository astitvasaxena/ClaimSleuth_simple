"""Run the CREWAI forensic investigation of one stored claim and return a validated AgentSystemResult.

Steps: load the same original claim the baseline and ADK use -> check the MCP server -> build the
Agent / Task / Crew -> Crew.kickoff(inputs={"claim_packet": ...}) -> record execution through CrewAI's
step_callback, task_callback and event bus (tool usage and LLM call events) -> validate the output ->
ForensicsReport (model / provider / execution_status set here, not by the model).

LLM mode (CLAIMSLEUTH_CREW_LLM):
  "test-double"  (default) the labelled ForensicTestDoubleLLM -- offline, no language model.
  "ollama-local" genuine local inference through Ollama on 127.0.0.1 (see crew_service/ollama.py).
Any other value returns NOT_CONFIGURED. Live mode never falls back to the test double. Both modes run
inside the loopback-only network guard, so no call can leave the machine.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import claimsleuth.crew_service  # noqa: F401  (telemetry/tracing off before crewai is imported)
from crewai.events.event_bus import crewai_event_bus
from crewai.events.types.llm_events import LLMCallCompletedEvent, LLMCallFailedEvent, LLMCallStartedEvent
from crewai.events.types.tool_usage_events import ToolUsageErrorEvent, ToolUsageFinishedEvent
from pydantic import ValidationError

from claimsleuth import config
from claimsleuth.blackboard import AgentParticipant
from claimsleuth.claim_text import claim_message
from claimsleuth.crew_service import ollama as ollama_mode
from claimsleuth.crew_service.crew import MCP_TOOLS, analysis_from_output, build_crew
from claimsleuth.crew_service.network_guard import loopback_only
from claimsleuth.crew_service.test_double import MODEL_NAME, PROVIDER, ForensicTestDoubleLLM
from claimsleuth.datastore import load_claim
from claimsleuth.schemas import (AgentRunRecord, AgentSystemResult, AgentToolCallRecord, ExecutionStatus,
                                 ForensicsReport, utc_now)

AGENT_NAME = "forensic_agent"
DEFAULT_MCP_URL = "http://127.0.0.1:8102/mcp"
LLM_MODE_ENV = "CLAIMSLEUTH_CREW_LLM"


def _new_run_id() -> str:
    import uuid
    return f"RUN-{uuid.uuid4().hex[:12]}"


def mcp_health(mcp_url: str) -> tuple[bool, str]:
    parts = urlsplit(mcp_url)
    health = f"{parts.scheme}://{parts.netloc}/health"
    try:
        with urllib.request.urlopen(health, timeout=5) as resp:
            status = json.loads(resp.read()).get("status")
            return status == "ok", f"{health}: {status}"
    except urllib.error.HTTPError as exc:
        return False, f"{health}: HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"MCP server unreachable at {health}: {exc}"


def _short_tool(name: str) -> str:
    return next((t for t in MCP_TOOLS if name.endswith(t)), name)


def _parse_output(output: Any) -> Any:
    if isinstance(output, str):
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return output
    return output


class CrewRecorder:
    """Collects CrewAI callback and event-bus records for one run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.tool_calls: list[AgentToolCallRecord] = []
        self.llm_calls = 0
        self.llm_errors: list[str] = []
        self.model_calls: list[dict[str, Any]] = []        # one entry per completed model call (real timings)
        self._llm_started: dict[str, Any] = {}
        self.steps: list[dict[str, Any]] = []

    def step_callback(self, step: Any) -> None:            # Crew(step_callback=...)
        self.steps.append({"ts": time.time(), "step": type(step).__name__,
                           "tool": getattr(step, "tool", None) and _short_tool(str(step.tool)),
                           "text": str(getattr(step, "thought", "") or getattr(step, "output", "") or "")[:300]})

    def task_callback(self, task_output: Any) -> None:     # Crew(task_callback=...)
        self.steps.append({"ts": time.time(), "step": "task_completed", "agent": str(task_output.agent)[:80]})

    def register(self) -> None:                            # inside crewai_event_bus.scoped_handlers()
        @crewai_event_bus.on(ToolUsageFinishedEvent)
        def _tool_finished(source, event):
            self.tool_calls.append(AgentToolCallRecord(
                run_id=self.run_id, agent=AGENT_NAME, tool=_short_tool(event.tool_name),
                protocol="mcp" if event.tool_name.startswith("mcp_") else "internal",
                arguments=event.tool_args if isinstance(event.tool_args, dict) else {"raw": str(event.tool_args)},
                status="ok", result=_parse_output(event.output), started_at=event.started_at,
                latency_ms=round((event.finished_at - event.started_at).total_seconds() * 1000, 3)))

        @crewai_event_bus.on(ToolUsageErrorEvent)
        def _tool_error(source, event):
            self.tool_calls.append(AgentToolCallRecord(
                run_id=self.run_id, agent=AGENT_NAME, tool=_short_tool(event.tool_name),
                protocol="mcp" if event.tool_name.startswith("mcp_") else "internal",
                arguments=event.tool_args if isinstance(event.tool_args, dict) else {"raw": str(event.tool_args)},
                status="error", error=str(event.error)[:500], started_at=utc_now(), latency_ms=0))

        @crewai_event_bus.on(LLMCallStartedEvent)
        def _llm_started(source, event):
            self.llm_calls += 1
            self._llm_started[event.call_id] = event.timestamp

        @crewai_event_bus.on(LLMCallCompletedEvent)
        def _llm_completed(source, event):
            started = self._llm_started.pop(event.call_id, None)
            usage = event.usage or {}
            self.model_calls.append({
                "call_id": event.call_id, "model": event.model, "call_type": getattr(event.call_type, "value", None),
                "started_at": started.isoformat() if started else None, "finished_at": event.timestamp.isoformat(),
                "latency_ms": round((event.timestamp - started).total_seconds() * 1000, 1) if started else None,
                "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                "finish_reason": event.finish_reason})

        @crewai_event_bus.on(LLMCallFailedEvent)
        def _llm_failed(source, event):
            self.llm_errors.append(str(event.error)[:500])

    def returned_sources(self) -> set[str]:
        found: set[str] = set()
        for call in self.tool_calls:
            if call.status == "ok" and isinstance(call.result, dict):
                found.update(call.result.get("record_sources", []))
        return found


def run_forensic_investigation(claim_id: str, run_id: str | None = None, llm: Any = None,
                               mcp_url: str | None = None, data_dir: Path = config.DATA_DIR) -> AgentSystemResult:
    run_id = run_id or _new_run_id()
    mcp_url = mcp_url or os.environ.get("CLAIMSLEUTH_MCP_URL", DEFAULT_MCP_URL)
    started_at, t0 = utc_now(), time.perf_counter()
    errors: list[str] = []

    settings: ollama_mode.OllamaSettings | None = None
    llm_info: dict[str, Any] = {}

    board = AgentParticipant(run_id, claim_id, "crewai", (AGENT_NAME,))   # no-op unless the blackboard is enabled

    def finish(status: ExecutionStatus, **kw) -> AgentSystemResult:
        label = MODEL_NAME if isinstance(llm, ForensicTestDoubleLLM) else (
            getattr(llm, "model", None) or (settings.model if settings else None))
        result = AgentSystemResult(system="CREWAI", run_id=run_id, claim_id=claim_id, status=status, model=label,
                                   prompt_variant="standard", started_at=started_at, finished_at=utc_now(),
                                   execution_ms=round((time.perf_counter() - t0) * 1000, 3), errors=errors, **kw)
        board.finished(result)
        return result

    if llm is None:
        mode = os.environ.get(LLM_MODE_ENV, "test-double")
        if mode == "test-double":
            llm = ForensicTestDoubleLLM(model=MODEL_NAME)
        elif mode == ollama_mode.MODE:
            try:
                settings = ollama_mode.settings_from_env()
            except ollama_mode.OllamaConfigError as exc:
                errors.append(f"ollama-local configuration invalid: {exc}")
                return finish(ExecutionStatus.NOT_CONFIGURED)
            try:
                llm_info = ollama_mode.preflight(settings)
            except ollama_mode.OllamaUnavailableError as exc:
                errors.append(f"ollama-local unavailable, CrewAI run not started: {exc}")
                return finish(ExecutionStatus.FAILED)
            llm = ollama_mode.build_llm(settings)
        else:
            errors.append(f"{LLM_MODE_ENV}={mode!r} is not a known mode (use 'test-double' or 'ollama-local'); "
                          "nothing was run and no fallback was used.")
            return finish(ExecutionStatus.NOT_CONFIGURED)
    offline = isinstance(llm, ForensicTestDoubleLLM)
    live = isinstance(llm, ollama_mode.LocalOllamaLLM)
    if live and settings is None:                      # an LLM passed in directly (tests): use default limits
        settings = ollama_mode.settings_from_env({})

    try:
        claim, documents = load_claim(claim_id, data_dir)
    except (FileNotFoundError, ValidationError) as exc:
        errors.append(f"claim could not be loaded: {exc}")
        return finish(ExecutionStatus.FAILED)
    board.running(documents)
    healthy, detail = mcp_health(mcp_url)
    if not healthy:
        errors.append(f"MCP server not available, CrewAI run not started. {detail}")
        return finish(ExecutionStatus.FAILED)

    recorder = CrewRecorder(run_id)
    agent_run = AgentRunRecord(agent=AGENT_NAME, framework="crewai", started_at=utc_now())
    trace: list[dict[str, Any]] = []
    output, blocked = None, []
    with crewai_event_bus.scoped_handlers():
        recorder.register()
        try:
            with loopback_only() as blocked:            # both modes: nothing may leave the machine
                limits = {"max_iter": settings.max_iter, "guardrail_retries": settings.guardrail_retries} if live else {}
                agent, task, crew = build_crew(llm, mcp_url, run_id, claim_id, documents, recorder.returned_sources,
                                               recorder.step_callback, recorder.task_callback, **limits)
                trace.append({"ts": time.time(), "crew": {"agents": [agent.role], "tasks": 1, "process": "sequential",
                                                          "llm": type(llm).__name__}})
                if live:
                    trace.append({"ts": time.time(), "llm": {"mode": ollama_mode.MODE, **settings.public(), **llm_info}})
                    llm.start_budget(settings.run_budget_s)
                output = crew.kickoff(inputs={"claim_packet": claim_message(claim, documents, run_id)})
        except Exception as exc:     # guardrail exhaustion, timeouts, MCP, CrewAI errors: recorded, never replaced
            kind = "timed out" if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower() else "failed"
            errors.append(f"CrewAI run {kind}: {type(exc).__name__}: {str(exc)[:500]}")
        finally:
            crewai_event_bus.flush(timeout=10)
    agent_run.finished_at = utc_now()
    agent_run.latency_ms = round((agent_run.finished_at - agent_run.started_at).total_seconds() * 1000, 3)
    agent_run.llm_calls = recorder.llm_calls
    agent_run.tool_calls = len(recorder.tool_calls)
    agent_run.errors = [c.error for c in recorder.tool_calls if c.error] + recorder.llm_errors
    # Token usage as reported by the provider on each completed call -- recorded even when the run fails.
    reported = [c for c in recorder.model_calls if c["prompt_tokens"] is not None or c["completion_tokens"] is not None]
    if reported:
        agent_run.prompt_tokens = sum(c["prompt_tokens"] or 0 for c in reported)
        agent_run.completion_tokens = sum(c["completion_tokens"] or 0 for c in reported)
    capped = [c for c in recorder.model_calls if c["finish_reason"] == "length"]
    if capped:
        errors.append(f"{len(capped)} model call(s) stopped at the max_tokens limit (finish_reason=length); "
                      "their output was cut off")
    if blocked:
        errors.append(f"run attempted non-local network access (blocked): {sorted(set(blocked))}")
    trace += recorder.steps
    trace.append({"ts": time.time(), "network_guard": "loopback_only", "blocked_hosts": sorted(set(blocked))})
    if recorder.model_calls:
        trace.append({"ts": time.time(), "model_calls": recorder.model_calls})

    report = None
    if output is not None and not blocked:
        try:
            analysis = analysis_from_output(output)
            if analysis.claim_id != claim_id:
                raise ValueError(f"claim_id {analysis.claim_id} != {claim_id}")
            report = ForensicsReport(**analysis.model_dump(), model=MODEL_NAME if offline else str(llm.model),
                                     provider=PROVIDER if offline else (ollama_mode.PROVIDER if live else "unknown"),
                                     execution_status=ExecutionStatus.COMPLETED)
            agent_run.output_summary = analysis.reasoning_summary[:500]
            usage = getattr(output, "token_usage", None)
            if usage is not None and getattr(usage, "successful_requests", 0) and agent_run.prompt_tokens is None:
                agent_run.prompt_tokens, agent_run.completion_tokens = usage.prompt_tokens, usage.completion_tokens
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"forensic output failed validation: {str(exc).splitlines()[0]}")
    return finish(ExecutionStatus.COMPLETED if report else ExecutionStatus.FAILED, report=report,
                  agent_runs=[agent_run], tool_calls=recorder.tool_calls, trace=trace)


def save_result(result: AgentSystemResult, runtime_dir: Path = config.RUNTIME_DIR) -> Path:
    from claimsleuth.render import crewai_markdown
    out = runtime_dir / "reports" / result.run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "crewai.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    (out / "crewai.md").write_text(crewai_markdown(result), encoding="utf-8")
    return out
