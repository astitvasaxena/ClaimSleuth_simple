"""ADK callbacks that build the execution record of one run.

Registered on every LlmAgent through `ExecutionRecorder.callbacks()`:
  before/after_agent_callback  -> per-agent start, end, latency
  before/after_model_callback  -> LLM call count, token usage (usage_metadata), tools offered to the model
  before/after_tool_callback   -> every tool call: agent, tool, arguments, result, latency, status
  on_tool_error_callback       -> a tool exception becomes an error result the agent can see (fail visible)
Nothing here changes what an agent decides.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from google.adk.tools.mcp_tool.mcp_tool import McpTool

from claimsleuth.schemas import AgentRunRecord, AgentToolCallRecord, utc_now


def _summary(value: Any, limit: int = 500) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
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


class ExecutionRecorder:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.agent_runs: dict[str, AgentRunRecord] = {}
        self.tool_calls: list[AgentToolCallRecord] = []
        self.discovered_tools: dict[str, list[str]] = {}
        self.model_errors: list[str] = []
        self._agent_t0: dict[str, float] = {}
        self._tool_t0: dict[str, tuple[float, datetime]] = {}

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
        record = self.agent_runs.get(name)
        if record is not None:
            record.finished_at = utc_now()
            record.latency_ms = round((time.perf_counter() - self._agent_t0[name]) * 1000, 3)
            key = {"evidence_agent": "evidence_report", "investigator_agent": "investigation_report"}.get(name)
            output = callback_context.state.get(key) if key else None
            record.output_summary = _summary(output) if output is not None else "no structured output"
        return None

    # ------------------------------------------------------------------ model
    async def before_model(self, callback_context, llm_request):
        record = self.agent_runs[callback_context.agent_name]
        record.llm_calls += 1
        names = sorted({d.name for t in (llm_request.config.tools or []) for d in (t.function_declarations or [])})
        self.discovered_tools[callback_context.agent_name] = names
        return None

    async def after_model(self, callback_context, llm_response):
        usage = llm_response.usage_metadata
        if usage is not None:
            record = self.agent_runs[callback_context.agent_name]
            record.prompt_tokens = (record.prompt_tokens or 0) + (usage.prompt_token_count or 0)
            record.completion_tokens = (record.completion_tokens or 0) + (usage.candidates_token_count or 0)
        return None

    async def on_model_error(self, callback_context, llm_request, error):
        message = f"{callback_context.agent_name}: model call failed: {type(error).__name__}: {error}"
        self.model_errors.append(message)
        self.agent_runs[callback_context.agent_name].errors.append(message)
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
