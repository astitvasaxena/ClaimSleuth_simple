"""ADK callbacks that build the execution record of one run.

Registered on every LlmAgent through `ExecutionRecorder.callbacks()`:
  before/after_agent_callback  -> per-agent start, end, latency
  before/after_model_callback  -> LLM call count, token usage (usage_metadata), tools offered to the model
  before/after_tool_callback   -> every tool call: agent, tool, arguments, result, latency, status
  on_tool_error_callback       -> a tool exception becomes an error result the agent can see (fail visible)
  on_model_error_callback      -> a failed model call is recorded and propagates; after an HTTP 503 the request is
                                  sent once more, through the ledger, and a successful answer replaces the error
Every model call is also kept as one record (agent, model, start, end, latency, and the token counts only when the
response carried usage_metadata - otherwise None, never 0). ErrorRecorderPlugin records an exception escaping an
agent, through ADK's notification-only on_agent_error_callback. Nothing here changes what an agent decides.

ADK does not call after_agent when an agent raises, so an agent's record is also closed by that error callback. On a
cancellation (a time limit) ADK does call after_agent, as cleanup, and the agent did not finish: cancelling() tells the
two apart. close_open() closes whatever is left. Whichever comes first closes a record, once: nothing is counted twice.
Failures are classified (observability/provider_errors.py) and their response bodies dropped.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.mcp_tool.mcp_tool import McpTool

from claimsleuth import gemini_rate
from claimsleuth.observability.provider_errors import (agent_outcome, classify, failure_text, redact, retry_record,
                                                       should_retry)
from claimsleuth.schemas import (AgentRunRecord, AgentToolCallRecord, EvidenceDocument, ProviderFailure, ProviderRetry,
                                 SecurityEvent, utc_now)
from claimsleuth.security import output_event, restricted_output, wrap_untrusted

REFUSED_OUTPUT = "refused: restricted content"


def _summary(value: Any, limit: int = 500) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = redact(text, len(text))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def parse_mcp_response(response: Any) -> tuple[bool, Any, str | None]:
    """(ok, structured result, error text) from what ADK's McpTool returns (a CallToolResult dump)."""
    if not isinstance(response, dict):
        return True, response, None
    content = response.get("content") or []
    text = next((c.get("text") for c in content if isinstance(c, dict) and c.get("text")), None)
    if response.get("isError") or response.get("is_error"):
        return False, None, text or "MCP tool returned isError without a message"
    if "error" in response and len(response) == 1:          # ADK-level error dict
        return False, None, str(response["error"])
    structured = response.get("structuredContent") or response.get("structured_content")
    if structured is None and text:
        try:
            structured = json.loads(text)
        except json.JSONDecodeError:
            structured = text
    return True, structured, None


def cancelling() -> bool:
    """True while this task is being cancelled, which is when ADK runs after_agent callbacks only as cleanup."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class ExecutionRecorder:
    def __init__(self, run_id: str, before_request: Any = None, model: str | None = None,
                 provider: str | None = None, retry_model: Any = None) -> None:
        """before_request(agent_name): optional blocking hook run before every model request (live mode: the
        shared Gemini request ledger). An exception from it stops the request; nothing is sent.
        retry_model(agent_name): the BaseLlm a 503 is retried on, always the agent's own model; None: no retry."""
        self.run_id = run_id
        self.before_request = before_request
        self.model, self.provider = model, provider
        self.retry_model = retry_model
        self.retries: list[ProviderRetry] = []             # the automatic retries after a 503
        self._retried: set[str] = set()                    # one per agent
        self.failures: list[ProviderFailure] = []          # classified failures of this run, safe to store
        self.injection_events: list[SecurityEvent] = []    # agent output refused by security.restricted_output
        self._classified: list[tuple[BaseException, ProviderFailure]] = []
        self.agent_runs: dict[str, AgentRunRecord] = {}
        self.tool_calls: list[AgentToolCallRecord] = []
        self.discovered_tools: dict[str, list[str]] = {}
        self.model_errors: list[str] = []
        self.model_calls: list[dict[str, Any]] = []        # one record per model call (real timings)
        self.agent_errors: list[dict[str, Any]] = []       # exceptions that escaped an agent (ErrorRecorderPlugin)
        self._agent_t0: dict[str, float] = {}
        self._tool_t0: dict[str, tuple[float, datetime]] = {}
        self._model_t0: dict[str, tuple[float, datetime, str | None]] = {}

    # ------------------------------------------------------------------ registration
    def callbacks(self) -> dict[str, Any]:
        return dict(
            before_agent_callback=self.before_agent, after_agent_callback=self.after_agent,
            before_model_callback=self.before_model, after_model_callback=self.after_model,
            on_model_error_callback=self.on_model_error,
            before_tool_callback=self.before_tool, after_tool_callback=self.after_tool,
            on_tool_error_callback=self.on_tool_error,
        )

    # ------------------------------------------------------------------ agents
    async def before_agent(self, callback_context):
        name = callback_context.agent_name
        self._agent_t0[name] = time.perf_counter()
        self.agent_runs[name] = AgentRunRecord(agent=name, framework="adk", started_at=utc_now())
        return None

    async def after_agent(self, callback_context):
        name = callback_context.agent_name
        if cancelling():
            self.close_open("cancelled", TimeoutError(f"a time limit stopped {name}"))
            return None
        self.close_agent(name, "finished")
        record = self.agent_runs.get(name)
        if record is not None:
            key = {"evidence_agent": "evidence_report", "investigator_agent": "investigation_report"}.get(name)
            output = callback_context.state.get(key) if key else None
            refused = any(e.consumer == name for e in self.injection_events)
            record.output_summary = (REFUSED_OUTPUT if refused else _summary(output) if output is not None
                                     else "no structured output")
        return None

    def refuse_output(self, agent: str, claim_id: str, labels: list[str], attack_type: str = "RESTRICTED_OUTPUT") -> str:
        """Record a refused report and the error to report. A report reaches ADK as a call of the internal set_model_response
        tool, so the record of that call goes too: neither it, the error nor the agent's record repeats the report's text."""
        self.injection_events.append(output_event(self.run_id, claim_id, agent, labels, attack_type))
        if agent in self.agent_runs:
            self.agent_runs[agent].output_summary = REFUSED_OUTPUT
        self.tool_calls = [c.model_copy(update={"arguments": {}, "result": None})
                           if c.agent == agent and c.protocol == "internal" else c for c in self.tool_calls]
        return f"{agent} output refused: {', '.join(labels)}"

    def refuse_restricted(self, agent: str, report: Any, documents: list[EvidenceDocument]) -> str | None:
        """The error to record if this report carries what no report may carry (security.restricted_output), else None."""
        labels = restricted_output(report.model_dump_json(), documents)
        return self.refuse_output(agent, report.claim_id, labels) if labels else None

    def close_agent(self, name: str, outcome: str) -> None:
        """End an agent's record. Only the first call counts, so a duration is never measured twice."""
        record = self.agent_runs.get(name)
        if record is not None and record.finished_at is None:
            record.finished_at = utc_now()
            record.latency_ms = round((time.perf_counter() - self._agent_t0[name]) * 1000, 3)
            record.outcome = outcome

    def close_open(self, outcome: str, error: BaseException | None = None) -> None:
        """A run that ended without the agent's own callbacks (timeout, cancellation): close what was still running,
        with the in-flight model request recorded as failed and its measured time."""
        for name, record in self.agent_runs.items():
            if record.finished_at is None:
                failure = self.record_failure(error, name) if error is not None else None
                if name in self._model_t0:
                    self._model_call(name, "error", error=type(error).__name__ if error else outcome, failure=failure)
                self.close_agent(name, outcome)

    def mark_timed_out(self) -> None:
        """The cancellation that closed these records was the run's own time limit."""
        for record in self.agent_runs.values():
            if record.outcome == "cancelled":
                record.outcome = "timeout"

    # ------------------------------------------------------------------ failures
    def record_failure(self, error: BaseException, agent: str | None = None, output_failure: bool = False) -> ProviderFailure:
        """Classify an exception once, however many callbacks see it."""
        for seen, failure in self._classified:
            if seen is error:
                return failure
        failure = classify(error, run_id=self.run_id, agent=agent, model=self.model, provider=self.provider,
                           output_failure=output_failure)
        self._classified.append((error, failure))
        self.failures.append(failure)
        return failure

    def record_output_failure(self, agent: str, error: BaseException) -> None:
        """The agent's output was missing or invalid, unless an earlier failure of this agent already explains it."""
        if not any(f.agent == agent for f in self.failures):
            self.record_failure(error, agent, output_failure=True)

    # ------------------------------------------------------------------ model
    async def _start_request(self, agent: str, llm_request) -> None:
        if self.before_request is not None:    # may wait for a free per-minute slot; raises when over budget
            await asyncio.to_thread(self.before_request, agent)
        self.agent_runs[agent].llm_calls += 1
        self._model_t0[agent] = (time.perf_counter(), utc_now(), getattr(llm_request, "model", None))

    async def before_model(self, callback_context, llm_request):
        await self._start_request(callback_context.agent_name, llm_request)
        names = sorted({d.name for t in (llm_request.config.tools or []) for d in (t.function_declarations or [])})
        self.discovered_tools[callback_context.agent_name] = names
        return None

    def _model_call(self, agent: str, status: str, usage: Any = None, error: str | None = None,
                    failure: ProviderFailure | None = None) -> None:
        t0, started, model = self._model_t0.pop(agent, (None, None, None))
        if t0 is None:
            return                         # no matching before_model: nothing to time, nothing invented
        get = (lambda name: getattr(usage, name, None)) if usage is not None else (lambda name: None)
        self.model_calls.append({
            "agent": agent, "model": model, "status": status, "error": error,
            "error_code": failure.error_code if failure else None, "http_status": failure.http_status if failure else None,
            "started_at": started.isoformat(), "finished_at": utc_now().isoformat(),
            "latency_ms": round((time.perf_counter() - t0) * 1000, 3), "usage_reported": usage is not None,
            "prompt_tokens": get("prompt_token_count"), "completion_tokens": get("candidates_token_count"),
            "cached_tokens": get("cached_content_token_count"), "total_tokens": get("total_token_count")})

    async def after_model(self, callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage is not None:
            record = self.agent_runs[callback_context.agent_name]
            record.prompt_tokens = (record.prompt_tokens or 0) + (usage.prompt_token_count or 0)
            record.completion_tokens = (record.completion_tokens or 0) + (usage.candidates_token_count or 0)
        self._model_call(callback_context.agent_name, "ok", usage)
        return None

    def _model_failed(self, agent: str, error: Exception, retried: bool = False) -> None:
        message = f"{agent}: model call failed: {failure_text(error)}"
        self.model_errors.append(message)
        self.agent_runs[agent].errors.append(message)
        failure = self.record_failure(error, agent)
        failure.retried = retried
        self._model_call(agent, "error", error=type(error).__name__, failure=failure)

    async def _retry(self, agent: str, llm_request, error: Exception) -> Any:
        """The one retry after an HTTP 503: the same request to the same model, after a wait, through the ledger. A
        response goes on as the model's answer (after_model records it). No response is ever invented."""
        self._retried.add(agent)
        delay = gemini_rate.UNAVAILABLE_RETRY_DELAY_S
        self._model_call(agent, "error", error=type(error).__name__,
                         failure=classify(error, run_id=self.run_id, agent=agent))      # the attempt that failed
        await asyncio.sleep(delay)
        try:
            await self._start_request(agent, llm_request)
        except gemini_rate.GeminiQuotaGuard:         # the budget did not allow a second request: nothing was sent
            self.retries.append(retry_record(error, agent, "refused", delay))
            self._model_failed(agent, error)
            return None
        response = None
        try:
            async for response in self.retry_model(agent).generate_content_async(llm_request, stream=False):
                pass
        except Exception as again:
            self.retries.append(retry_record(error, agent, "failed", delay))
            self._model_failed(agent, again, retried=True)
            raise
        self.retries.append(retry_record(error, agent, "recovered", delay))
        return response

    async def on_model_error(self, callback_context, llm_request, error):
        agent = callback_context.agent_name
        if self.retry_model is not None and agent not in self._retried and should_retry(error):
            return await self._retry(agent, llm_request, error)
        self._model_failed(agent, error)
        return None     # propagate: a failed model call is never replaced by an invented response

    # ------------------------------------------------------------------ tools
    def _key(self, tool_context) -> str:
        return tool_context.function_call_id or f"{tool_context.agent_name}:{len(self.tool_calls)}"

    async def before_tool(self, tool, args, tool_context):
        self._tool_t0[self._key(tool_context)] = (time.perf_counter(), utc_now())
        self.agent_runs[tool_context.agent_name].tool_calls += 1
        return None

    def _record(self, tool, args, tool_context, ok: bool, result: Any, error: str | None) -> None:
        t0, started = self._tool_t0.pop(self._key(tool_context), (time.perf_counter(), utc_now()))
        self.tool_calls.append(AgentToolCallRecord(
            run_id=self.run_id, agent=tool_context.agent_name, tool=tool.name,
            protocol="mcp" if isinstance(tool, McpTool) else "internal", arguments=dict(args or {}),
            status="ok" if ok else "error", result=result, error=error, started_at=started,
            latency_ms=round((time.perf_counter() - t0) * 1000, 3)))
        if not ok:
            self.agent_runs[tool_context.agent_name].errors.append(f"{tool.name}: {error}")

    async def after_tool(self, tool, args, tool_context, tool_response):
        if isinstance(tool, McpTool):
            ok, result, error = parse_mcp_response(tool_response)
        else:
            ok, result, error = (not (isinstance(tool_response, dict) and "error" in tool_response),
                                 tool_response, None)
            if not ok:
                error = _summary(tool_response.get("error"))
        self._record(tool, args, tool_context, ok, result, error)
        return None

    async def on_tool_error(self, tool, args, tool_context, error):
        message = f"{type(error).__name__}: {error}"
        self._record(tool, args, tool_context, False, None, message)
        return {"error": f"Tool {tool.name} failed: {message}"}    # the agent sees the failure, nothing invented

    # ------------------------------------------------------------------ views
    def trace_records(self) -> list[dict[str, Any]]:
        """The per-call records for the run's trace (AgentSystemResult.trace)."""
        return [{"ts": time.time(), "model_calls": list(self.model_calls)},
                {"ts": time.time(), "agent_errors": list(self.agent_errors)}]

    def mcp_sources(self) -> set[str]:
        """Every `source` / `record_sources` string that an MCP tool actually returned in this run."""
        found: set[str] = set()

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    if k in ("source", "sources", "record_sources"):
                        found.update([v] if isinstance(v, str) else [x for x in v if isinstance(x, str)])
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)
        for call in self.tool_calls:
            if call.protocol == "mcp" and call.status == "ok":
                walk(call.result)
        return found


class EvidenceIsolation(BasePlugin):
    """Direct mode (no blackboard): the Evidence Agent's output reaches the Investigator's instruction as untrusted data,
    as it does in blackboard mode (blackboard/adk_plugin.py). `raw` keeps what the agent wrote, for validation."""

    def __init__(self) -> None:
        super().__init__(name="claimsleuth_evidence_isolation")
        self.raw: Any = None

    async def before_agent_callback(self, *, agent, callback_context):
        if agent.name == "investigator_agent" and callback_context.state.get("evidence_report") is not None:
            self.raw = callback_context.state["evidence_report"]
            text = self.raw if isinstance(self.raw, str) else json.dumps(self.raw, default=str)
            callback_context.state["evidence_report"] = wrap_untrusted(text, "agent:evidence_agent")
        return None


class ErrorRecorderPlugin(BasePlugin):
    """Records an exception that escapes an ADK agent (ADK's notification-only on_agent_error_callback; ADK re-raises
    it afterwards). Only the error type and a short message are kept."""

    def __init__(self, recorder: ExecutionRecorder) -> None:
        super().__init__(name="claimsleuth_error_recorder")
        self.recorder = recorder

    async def on_agent_error_callback(self, *, agent, callback_context, error):
        recorder = self.recorder
        running = agent.name in recorder.agent_runs      # the enclosing workflow agent sees the same exception
        failure = recorder.record_failure(error, agent.name if running else None)
        recorder.agent_errors.append({"agent": agent.name, "ts": utc_now().isoformat(),
                                      "error_type": type(error).__name__, "message": failure_text(error, 200),
                                      "error_code": failure.error_code, "propagated": not running})
        if running:
            recorder.close_agent(agent.name, agent_outcome(failure))
        return None
