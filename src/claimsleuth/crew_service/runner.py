"""Run the CREWAI forensic investigation of one stored claim and return a validated AgentSystemResult.

Steps: load the same original claim the baseline and ADK use -> check the MCP server -> build the
Agent / Task / Crew -> Crew.kickoff(inputs={"claim_packet": ...}) -> record execution through CrewAI's
step_callback, task_callback and event bus (tool usage and LLM call events) -> validate the output ->
ForensicsReport (model / provider / execution_status set here, not by the model).

LLM mode (CLAIMSLEUTH_CREW_LLM, else EXECUTION_MODE: live -> gemini, offline -> test-double):
  "test-double"  (default) the labelled ForensicTestDoubleLLM -- offline, no language model.
  "gemini"       genuine Gemini inference through CrewAI's native Gemini provider (crew_service/gemini.py);
                 the mode EXECUTION_MODE=live uses. A completed run must include a successful
                 claim_history_lookup MCP call.
  "ollama-local" experimental local inference through Ollama on 127.0.0.1 (see crew_service/ollama.py).
Any other value, or one that contradicts EXECUTION_MODE, returns NOT_CONFIGURED. A live mode never falls back
to the test double. Every mode runs inside the network guard: loopback only, plus the Gemini API host in
gemini mode.

Shared context (blackboard mode): `context` holds the ContextRefs from the A2A request. Each is consumed from the
blackboard with this service's own "crewai" key (verified, signed receipt) and the verified Evidence report is
added to the claim_packet input as an untrusted data block; the task description is unchanged. If any reference
cannot be consumed, the crew is not run (FAILED, fail closed). The Forensic Agent may read only the Evidence report.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import claimsleuth.crew_service  # noqa: F401  (telemetry/tracing off before crewai is imported)
from crewai.events.event_bus import crewai_event_bus
from crewai.events.types.llm_events import LLMCallCompletedEvent, LLMCallFailedEvent, LLMCallStartedEvent
from crewai.events.types.agent_events import (AgentExecutionCompletedEvent, AgentExecutionErrorEvent,
                                              AgentExecutionStartedEvent)
from crewai.events.types.crew_events import CrewKickoffCompletedEvent, CrewKickoffFailedEvent, CrewKickoffStartedEvent
from crewai.events.types.llm_guardrail_events import LLMGuardrailCompletedEvent, LLMGuardrailStartedEvent
from crewai.events.types.task_events import TaskCompletedEvent, TaskFailedEvent, TaskStartedEvent
from crewai.events.types.tool_usage_events import ToolUsageErrorEvent, ToolUsageFinishedEvent, ToolUsageStartedEvent
from pydantic import ValidationError

from claimsleuth import config, execution_mode
from claimsleuth.blackboard import AgentParticipant, ContextRef, context_facts
from claimsleuth.blackboard.models import payload_hash
from claimsleuth.claim_text import claim_message, shared_context
from claimsleuth.crew_service import gemini as gemini_mode
from claimsleuth.crew_service import ollama as ollama_mode
from claimsleuth.crew_service.crew import MCP_TOOLS, analysis_from_output, build_crew, visible_error
from claimsleuth.crew_service.network_guard import loopback_only
from claimsleuth.crew_service.test_double import MODEL_NAME, PROVIDER, ForensicTestDoubleLLM
from claimsleuth.datastore import load_claim
from claimsleuth.observability.provider_errors import (STRUCTURED_OUTPUT_INVALID, UNCLASSIFIED, agent_outcome, classify,
                                                       failure_text, provider_text, redact)
from claimsleuth.schemas import (AgentRunRecord, AgentSystemResult, AgentToolCallRecord, ExecutionStatus,
                                 ForensicsReport, ProviderFailure, SecurityEvent, utc_now)
from claimsleuth.security import output_event

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


def _utc(value: datetime) -> datetime:
    """CrewAI's tool events carry naive local time (datetime.fromtimestamp); a naive value here means local time."""
    return value.astimezone(timezone.utc)


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
        self.crew_events: list[dict[str, Any]] = []       # crew / task / agent / guardrail / tool lifecycle

    def _lifecycle(self, kind: str, event: Any, status: str = "ok", **detail: Any) -> None:
        """One CrewAI lifecycle event as CrewAI emitted it: its own timestamp, id and parent id; short detail only."""
        self.crew_events.append({"event": kind, "status": status, "ts": event.timestamp.isoformat(),
                                 "event_id": getattr(event, "event_id", None),
                                 "parent_event_id": getattr(event, "parent_event_id", None), **detail})

    def step_callback(self, step: Any) -> None:            # Crew(step_callback=...)
        self.steps.append({"ts": time.time(), "step": type(step).__name__,
                           "tool": getattr(step, "tool", None) and _short_tool(str(step.tool)),
                           "text": redact(getattr(step, "thought", "") or getattr(step, "output", "") or "", 300)})

    def task_callback(self, task_output: Any) -> None:     # Crew(task_callback=...)
        self.steps.append({"ts": time.time(), "step": "task_completed", "agent": str(task_output.agent)[:80]})

    def register(self) -> None:                            # inside crewai_event_bus.scoped_handlers()
        @crewai_event_bus.on(ToolUsageFinishedEvent)
        def _tool_finished(source, event):
            self.tool_calls.append(AgentToolCallRecord(
                run_id=self.run_id, agent=AGENT_NAME, tool=_short_tool(event.tool_name),
                protocol="mcp" if event.tool_name.startswith("mcp_") else "internal",
                arguments=event.tool_args if isinstance(event.tool_args, dict) else {"raw": str(event.tool_args)},
                status="ok", result=_parse_output(event.output), started_at=_utc(event.started_at),
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
            started = self._llm_started.pop(event.call_id, None)
            error = provider_text(event.error)
            self.llm_errors.append(error)
            self.model_calls.append({"call_id": event.call_id, "model": getattr(event, "model", None),
                                     "status": "error", "error": error, "error_code": None, "http_status": None,
                                     "started_at": started.isoformat() if started else None,
                                     "finished_at": event.timestamp.isoformat(),
                                     "latency_ms": round((event.timestamp - started).total_seconds() * 1000, 1)
                                     if started else None, "prompt_tokens": None, "completion_tokens": None,
                                     "finish_reason": None})

        # Lifecycle events of the installed CrewAI version (1.15): crew, task, agent, guardrail, tool start.
        crew_handlers = {
            CrewKickoffStartedEvent: ("crew.kickoff_started", "ok"), CrewKickoffCompletedEvent: ("crew.kickoff_completed", "ok"),
            CrewKickoffFailedEvent: ("crew.kickoff_failed", "error"), TaskStartedEvent: ("task.started", "ok"),
            TaskCompletedEvent: ("task.completed", "ok"), TaskFailedEvent: ("task.failed", "error"),
            AgentExecutionStartedEvent: ("agent.execution_started", "ok"),
            AgentExecutionCompletedEvent: ("agent.execution_completed", "ok"),
            AgentExecutionErrorEvent: ("agent.execution_error", "error")}
        def lifecycle_handler(kind: str, status: str):
            # Exactly (source, event): CrewAI passes a third argument to handlers that accept more parameters.
            def handler(source, event):
                error = getattr(event, "error", None)
                self._lifecycle(kind, event, status, **({"error": provider_text(visible_error(error))} if error else {}))
            return handler

        for event_type, (kind, status) in crew_handlers.items():
            crewai_event_bus.on(event_type)(lifecycle_handler(kind, status))

        @crewai_event_bus.on(LLMGuardrailStartedEvent)
        def _guardrail_started(source, event):
            self._lifecycle("guardrail.started", event, retry_count=event.retry_count)

        @crewai_event_bus.on(LLMGuardrailCompletedEvent)
        def _guardrail_completed(source, event):
            self._lifecycle("guardrail.completed", event, "ok" if event.success else "rejected",
                            success=bool(event.success), retry_count=event.retry_count,
                            error=visible_error(event.error)[:200] if event.error else None)

        @crewai_event_bus.on(ToolUsageStartedEvent)
        def _tool_started(source, event):
            self._lifecycle("tool.started", event, tool=_short_tool(event.tool_name))

    def returned_history(self) -> list[Any]:
        """The successful claim_history_lookup results of this run, for a guardrail retry to reuse."""
        return [c.result for c in self.tool_calls if c.tool == "claim_history_lookup" and c.status == "ok"]

    def returned_sources(self) -> set[str]:
        found: set[str] = set()
        for call in self.tool_calls:
            if call.status == "ok" and isinstance(call.result, dict):
                found.update(call.result.get("record_sources", []))
        return found


def run_forensic_investigation(claim_id: str, run_id: str | None = None, llm: Any = None,
                               mcp_url: str | None = None, data_dir: Path = config.DATA_DIR,
                               context: list[ContextRef] | None = None) -> AgentSystemResult:
    run_id = run_id or _new_run_id()
    mcp_url = mcp_url or os.environ.get("CLAIMSLEUTH_MCP_URL", DEFAULT_MCP_URL)
    started_at, t0 = utc_now(), time.perf_counter()
    errors: list[str] = []

    settings: ollama_mode.OllamaSettings | gemini_mode.GeminiSettings | None = None
    llm_info: dict[str, Any] = {}
    failures: list[ProviderFailure] = []

    board = AgentParticipant(run_id, claim_id, "crewai", (AGENT_NAME,))   # no-op unless the blackboard is enabled

    def finish(status: ExecutionStatus, **kw) -> AgentSystemResult:
        label = MODEL_NAME if isinstance(llm, ForensicTestDoubleLLM) else (
            getattr(llm, "model", None) or (settings.model if settings else None))
        result = AgentSystemResult(system="CREWAI", run_id=run_id, claim_id=claim_id, status=status, model=label,
                                   prompt_variant="standard", started_at=started_at, finished_at=utc_now(),
                                   execution_ms=round((time.perf_counter() - t0) * 1000, 3), errors=errors,
                                   consumed_context=list(board.consumed), provider_failures=failures,
                                   security_events=list(board.security_events), **kw)
        board.finished(result)
        return result

    if llm is None:
        try:
            mode = execution_mode.crew_llm_mode()
        except execution_mode.ExecutionModeError as exc:
            errors.append(str(exc))
            return finish(ExecutionStatus.NOT_CONFIGURED)
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
        elif mode == gemini_mode.MODE:
            execution_mode.load_env()
            try:
                settings = gemini_mode.settings_from_env()
                llm = gemini_mode.build_llm(settings)
            except gemini_mode.GeminiConfigError as exc:
                errors.append(f"gemini configuration invalid, CrewAI run not started: {exc}")
                return finish(ExecutionStatus.NOT_CONFIGURED)
        else:
            errors.append(f"{LLM_MODE_ENV}={mode!r} is not a known mode (use 'test-double', 'gemini' or "
                          "'ollama-local'); nothing was run and no fallback was used.")
            return finish(ExecutionStatus.NOT_CONFIGURED)
    offline = isinstance(llm, ForensicTestDoubleLLM)
    ollama = isinstance(llm, ollama_mode.LocalOllamaLLM)
    gemini = gemini_mode.is_gemini_llm(llm)
    if ollama and settings is None:                    # an LLM passed in directly (tests): use default limits
        settings = ollama_mode.settings_from_env({})
    if gemini and settings is None:
        settings = gemini_mode.GeminiSettings(model=str(llm.model))
    provider = PROVIDER if offline else ollama_mode.PROVIDER if ollama else gemini_mode.PROVIDER if gemini else "unknown"

    def failure(exc: BaseException, output_failure: bool = False) -> ProviderFailure:
        recorded = classify(exc, run_id=run_id, agent=AGENT_NAME, model=str(getattr(llm, "model", "")) or None,
                            provider=provider, output_failure=output_failure)
        failures.append(recorded)
        return recorded

    try:
        claim, documents = load_claim(claim_id, data_dir)
    except (FileNotFoundError, ValidationError) as exc:
        errors.append(f"claim could not be loaded: {exc}")
        return finish(ExecutionStatus.FAILED)
    board.running(documents)
    blocks, supplied = [], []
    for ref in context or []:             # shared context: read from the blackboard, never from the A2A message
        report = board.consume_context(AGENT_NAME, ref)
        if report is None:
            errors.append(f"shared context {getattr(ref, 'section', '?')}@{getattr(ref, 'run_seq', '?')} could not be "
                          f"consumed from the blackboard ({board.last_failure}); the Forensic Agent was not run")
            return finish(ExecutionStatus.FAILED, trace=[{"ts": time.time(), "blackboard_ops": list(board.operations)}])
        blocks.append(context_facts(report, ref))         # verified and receipted whole; only its facts are passed on
        supplied.append({"section": ref.section, "run_seq": ref.run_seq, "sha256": ref.sha256,
                         "receipt_run_seq": board.consumed[-1].receipt_run_seq})
    extra = shared_context(blocks)
    healthy, detail = mcp_health(mcp_url)
    if not healthy:
        errors.append(f"MCP server not available, CrewAI run not started. {detail}")
        return finish(ExecutionStatus.FAILED)

    recorder = CrewRecorder(run_id)
    agent_run = AgentRunRecord(agent=AGENT_NAME, framework="crewai", started_at=utc_now())
    refused: list[SecurityEvent] = []             # output the guardrail refused for restricted content: one record a run

    def output_refused(labels: list[str]) -> None:
        if not refused:
            refused.append(output_event(run_id, claim_id, AGENT_NAME, labels))
    trace: list[dict[str, Any]] = []
    if supplied:
        trace.append({"ts": time.time(), "blackboard_context": {"consumed": supplied,
                                                                "supplied_sha256": payload_hash(extra)}})
    output, blocked = None, []
    failed: Exception | None = None
    with crewai_event_bus.scoped_handlers():
        recorder.register()
        try:
            # Nothing may leave the machine, except calls to the Gemini API in gemini mode.
            with loopback_only(gemini_mode.API_HOSTS if gemini else ()) as blocked:
                limits = ({"max_iter": settings.max_iter, "guardrail_retries": settings.guardrail_retries}
                          if ollama or gemini else {})
                agent, task, crew = build_crew(llm, mcp_url, run_id, claim_id, documents, recorder.returned_sources,
                                               recorder.step_callback, recorder.task_callback,
                                               returned_history=recorder.returned_history,
                                               on_refused=output_refused, **limits)
                trace.append({"ts": time.time(), "crew": {"agents": [agent.role], "tasks": 1, "process": "sequential",
                                                          "llm": type(llm).__name__}})
                if ollama:
                    trace.append({"ts": time.time(), "llm": {"mode": ollama_mode.MODE, **settings.public(), **llm_info}})
                    llm.start_budget(settings.run_budget_s)
                if gemini:
                    trace.append({"ts": time.time(), "llm": {"mode": gemini_mode.MODE, **settings.public()}})
                    if hasattr(llm, "start_budget"):
                        llm.start_budget(settings.run_budget_s, settings.max_calls, run_id)
                packet = claim_message(claim, documents, run_id) + (f"\n\n{extra}" if extra else "")
                output = crew.kickoff(inputs={"claim_packet": packet})
        except Exception as exc:     # guardrail exhaustion, timeouts, MCP, CrewAI errors: recorded, never replaced
            kind = "timed out" if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower() else "failed"
            errors.append(f"CrewAI run {kind}: {visible_error(failure_text(exc))}")
            failed = exc
        finally:
            crewai_event_bus.flush(timeout=10)
    agent_run.finished_at = utc_now()
    agent_run.latency_ms = round((agent_run.finished_at - agent_run.started_at).total_seconds() * 1000, 3)
    agent_run.outcome = "finished"
    retries = list(getattr(llm, "provider_retries", []))
    failed_calls = [c for c in recorder.model_calls if c.get("status") == "error"]
    for call, retry in zip(failed_calls, retries):        # the request each retry followed
        call.update(error_code=retry.error_code, http_status=retry.http_status)
    if failed is not None:
        rejected = any(e["event"] == "guardrail.completed" and e["status"] == "rejected" for e in recorder.crew_events)
        recorded = failure(failed, output_failure=rejected)
        recorded.retried = any(r.outcome == "failed" for r in retries)
        agent_run.outcome = agent_outcome(recorded)
        for call in failed_calls:                  # the classification of the request that failed
            if not call.get("error_code") and recorded.error_code not in (UNCLASSIFIED, STRUCTURED_OUTPUT_INVALID):
                call.update(error_code=recorded.error_code, http_status=recorded.http_status)
    agent_run.llm_calls = recorder.llm_calls
    agent_run.tool_calls = len(recorder.tool_calls)
    recovered = sum(r.outcome == "recovered" for r in retries)     # a 503 the retry recovered from is not an agent error
    agent_run.errors = [c.error for c in recorder.tool_calls if c.error] + recorder.llm_errors[recovered:]
    # Token usage as reported by the provider on each completed call -- recorded even when the run fails.
    reported = [c for c in recorder.model_calls if c["prompt_tokens"] is not None or c["completion_tokens"] is not None]
    if reported:       # a count the provider did not report stays None (CrewAI's Gemini provider omits prompt tokens)
        prompts = [c["prompt_tokens"] for c in reported if c["prompt_tokens"] is not None]
        completions = [c["completion_tokens"] for c in reported if c["completion_tokens"] is not None]
        agent_run.prompt_tokens = sum(prompts) if prompts else None
        agent_run.completion_tokens = sum(completions) if completions else None
    capped = [c for c in recorder.model_calls if c["finish_reason"] == "length"]
    if capped:
        errors.append(f"{len(capped)} model call(s) stopped at the max_tokens limit (finish_reason=length); "
                      "their output was cut off")
    if blocked:
        errors.append(f"run attempted non-local network access (blocked): {sorted(set(blocked))}")
    # A refused attempt's text stays out of the record; a later attempt that passed is the report, and is kept there.
    trace += [{k: v for k, v in step.items() if k != "text"} if refused else step for step in recorder.steps]
    trace.append({"ts": time.time(), "network_guard": "loopback_only", "blocked_hosts": sorted(set(blocked))})
    if recorder.model_calls:
        trace.append({"ts": time.time(), "model_calls": recorder.model_calls})
    trace.append({"ts": time.time(), "crew_events": recorder.crew_events})
    trace.append({"ts": time.time(), "blackboard_ops": list(board.operations)})

    history_calls = [c for c in recorder.tool_calls if c.tool == "claim_history_lookup" and c.status == "ok"]
    if gemini and output is not None and not history_calls:
        errors.append("the Forensic Agent did not complete a claim_history_lookup MCP call; a live run without its "
                      "required tool is not accepted")
    report = None
    if output is not None and not blocked and not (gemini and not history_calls):
        try:
            analysis = analysis_from_output(output)
            if analysis.claim_id != claim_id:
                raise ValueError(f"claim_id {analysis.claim_id} != {claim_id}")
            report = ForensicsReport(**analysis.model_dump(), model=MODEL_NAME if offline else str(llm.model),
                                     provider=provider,
                                     execution_status=ExecutionStatus.COMPLETED)
            agent_run.output_summary = analysis.reasoning_summary[:500]
            usage = getattr(output, "token_usage", None)
            if usage is not None and getattr(usage, "successful_requests", 0) and agent_run.prompt_tokens is None:
                agent_run.prompt_tokens, agent_run.completion_tokens = usage.prompt_tokens, usage.completion_tokens
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"forensic output failed validation: {str(exc).splitlines()[0]}")
            failure(exc, output_failure=True)
    return finish(ExecutionStatus.COMPLETED if report else ExecutionStatus.FAILED, report=report, agent_runs=[agent_run],
                  tool_calls=recorder.tool_calls, trace=trace, provider_retries=retries, injection_events=refused)


def save_result(result: AgentSystemResult, runtime_dir: Path = config.RUNTIME_DIR) -> Path:
    from claimsleuth.render import crewai_markdown
    out = runtime_dir / "reports" / result.run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "crewai.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    (out / "crewai.md").write_text(crewai_markdown(result), encoding="utf-8")
    return out
