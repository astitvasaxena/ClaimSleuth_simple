"""Run the GOOGLE ADK investigation of one stored claim and return a validated AgentSystemResult.

Lifecycle per run: load the same original claim the baseline uses -> check the MCP server is up ->
create an ADK session (state: run_id, claim_id) -> Runner.run_async (Evidence Agent, then Investigator
Agent) -> read both structured reports from session state -> validate with Pydantic -> verify every
citation against the documents and every policy/history reference against what MCP actually returned
-> close the runner (closes the MCP sessions) -> delete the ADK session.

A run is COMPLETED only when both reports validate. Nothing here falls back to deterministic code.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from google.adk.agents.run_config import RunConfig
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import ValidationError

from claimsleuth import config
from claimsleuth.adk_service import config as adk_config
from claimsleuth.adk_service.agents import EVIDENCE_AGENT, INVESTIGATOR_AGENT, build_agents
from claimsleuth.adk_service.callbacks import ExecutionRecorder
from claimsleuth.baseline import new_run_id
from claimsleuth.claim_text import claim_message
from claimsleuth.datastore import load_claim
from claimsleuth.documents import quote_appears
from claimsleuth.schemas import (AgentSystemResult, EvidenceDocument, EvidenceReport, ExecutionStatus,
                                 InvestigationReport, TokenUsage, ToolCitation, utc_now)

APP_NAME = "claimsleuth_adk"
USER_ID = "claimsleuth"


def mcp_health(mcp_url: str) -> tuple[bool, str]:
    parts = urlsplit(mcp_url)
    health = f"{parts.scheme}://{parts.netloc}/health"
    try:
        with urllib.request.urlopen(health, timeout=5) as resp:
            body = json.loads(resp.read())
            return body.get("status") == "ok", f"{health}: {body.get('status')}"
    except urllib.error.HTTPError as exc:
        return False, f"{health}: HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"MCP server unreachable at {health}: {exc}"


def _resolve_policy_pointer(source: str, recorder: ExecutionRecorder) -> Any:
    """Value at policies.json#/<policy>/<field>[/<index>] inside a policy record MCP returned in this run."""
    for call in recorder.tool_calls:
        if call.tool != "warranty_policy_search" or call.status != "ok" or not isinstance(call.result, dict):
            continue
        for match in call.result.get("matches", []):
            prefix = match.get("source", "") + "/"
            if source.startswith(prefix):
                node: Any = match.get("terms", {})
                for part in source[len(prefix):].split("/"):
                    if isinstance(node, list) and part.isdigit() and int(part) < len(node):
                        node = node[int(part)]
                    elif isinstance(node, dict) and part in node:
                        node = node[part]
                    else:
                        return None
                return node
    return None


def verify_references(report: EvidenceReport | InvestigationReport, agent: str, documents: list[EvidenceDocument],
                      recorder: ExecutionRecorder) -> list[str]:
    """Return one message per citation that is not supported by the documents or by MCP results."""
    docs = {d.doc_id: d for d in documents}
    problems = []
    citations = list(report.evidence_citations) + [c for f in report.findings for c in f.citations]
    if isinstance(report, EvidenceReport):
        citations += [f.citation for f in report.facts]
    for c in citations:
        if c.doc_id not in docs or not quote_appears(docs[c.doc_id], c.line, c.quote):
            problems.append(f"{agent}: citation {c.doc_id}#L{c.line} '{c.quote[:60]}' not found in the document")
    tool_refs: list[ToolCitation] = list(report.policy_references)
    if isinstance(report, InvestigationReport):
        tool_refs += report.history_references
    returned = recorder.mcp_sources()
    for ref in tool_refs:
        if ref.source in returned:
            continue
        value = _resolve_policy_pointer(ref.source, recorder)
        if value is None:
            problems.append(f"{agent}: reference '{ref.source}' was not returned by any MCP tool in this run")
        elif str(value).strip().lower() not in ref.value.strip().lower():
            problems.append(f"{agent}: reference '{ref.source}' has value '{value}' in the MCP result, "
                            f"but the agent wrote '{ref.value[:60]}'")
    return problems


def _event_trace(event) -> dict[str, Any]:
    parts = event.content.parts if event.content and event.content.parts else []
    return {
        "ts": event.timestamp, "author": event.author,
        "function_calls": [p.function_call.name for p in parts if p.function_call],
        "function_responses": [p.function_response.name for p in parts if p.function_response],
        "text": next(((p.text or "")[:300] for p in parts if p.text), None),
        "final": event.is_final_response(),
    }


async def run_adk_investigation_async(claim_id: str, run_id: str | None = None, model: Any = None,
                                      mcp_url: str | None = None, data_dir: Path = config.DATA_DIR,
                                      prompt_variant: str = "standard", llm_mode: str | None = None
                                      ) -> AgentSystemResult:
    """llm_mode: "gemini" (default, needs credentials) or "test-double" (offline OfflineAdkLlm, labelled).
    Can also be set with CLAIMSLEUTH_ADK_LLM. Ignored when `model` is passed explicitly (tests)."""
    run_id = run_id or new_run_id()
    mcp_url = mcp_url or adk_config.mcp_url()
    started_at, t0 = utc_now(), time.perf_counter()
    model_label = model if isinstance(model, str) else ("test-double" if model is not None else None)
    base = dict(system="GOOGLE_ADK", run_id=run_id, claim_id=claim_id, prompt_variant=prompt_variant,
                started_at=started_at)

    def finish(status: ExecutionStatus, errors: list[str], **kw) -> AgentSystemResult:
        return AgentSystemResult(**base, status=status, errors=errors, model=model_label, finished_at=utc_now(),
                                 execution_ms=round((time.perf_counter() - t0) * 1000, 3), **kw)

    if model is None:
        mode = llm_mode or os.environ.get("CLAIMSLEUTH_ADK_LLM", "gemini")
        if mode == "test-double":
            from claimsleuth.adk_service.test_double import offline_models
            model, model_label = offline_models(), "test-double"
        elif mode == "gemini":
            adk_config.load_env()
            problems = adk_config.llm_config_errors()
            if problems:
                return finish(ExecutionStatus.NOT_CONFIGURED, problems)
            model = model_label = adk_config.gemini_model()
        else:
            return finish(ExecutionStatus.NOT_CONFIGURED, [f"unknown ADK LLM mode {mode!r} (use gemini or test-double)"])

    try:
        claim, documents = load_claim(claim_id, data_dir)
    except (FileNotFoundError, ValidationError) as exc:
        return finish(ExecutionStatus.FAILED, [f"claim could not be loaded: {exc}"])

    healthy, detail = mcp_health(mcp_url)
    if not healthy:
        return finish(ExecutionStatus.FAILED, [f"MCP server not available, ADK run not started. {detail}"])

    recorder = ExecutionRecorder(run_id)
    session_service = InMemorySessionService()
    runner = Runner(app=App(name=APP_NAME, root_agent=build_agents(model, mcp_url, recorder)),
                    session_service=session_service)
    session = await session_service.create_session(app_name=APP_NAME, user_id=USER_ID,
                                                   state={"run_id": run_id, "claim_id": claim_id})
    trace: list[dict[str, Any]] = [{"ts": time.time(), "lifecycle": "session_created", "session_id": session.id}]
    errors: list[str] = []
    state: dict[str, Any] = {}
    try:
        message = types.Content(role="user", parts=[types.Part(text=claim_message(claim, documents, run_id))])
        async with asyncio.timeout(adk_config.RUN_TIMEOUT_SECONDS):
            async for event in runner.run_async(user_id=USER_ID, session_id=session.id, new_message=message,
                                                run_config=RunConfig(max_llm_calls=adk_config.MAX_LLM_CALLS)):
                trace.append(_event_trace(event))
                if event.error_code or event.error_message:
                    errors.append(f"{event.author}: {event.error_code}: {event.error_message}")
        final = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session.id)
        state = dict(final.state) if final else {}
    except TimeoutError:
        errors.append(f"ADK run exceeded {adk_config.RUN_TIMEOUT_SECONDS} s")
    except Exception as exc:     # model, MCP or ADK failure: recorded, never replaced by invented output
        errors.append(f"ADK run failed: {type(exc).__name__}: {exc}")
    finally:
        try:
            await runner.close()
            trace.append({"ts": time.time(), "lifecycle": "runner_closed"})
        except Exception as exc:
            errors.append(f"runner close failed: {type(exc).__name__}: {exc}")
        await session_service.delete_session(app_name=APP_NAME, user_id=USER_ID, session_id=session.id)
        trace.append({"ts": time.time(), "lifecycle": "session_deleted", "session_id": session.id})
    errors += [e for e in recorder.model_errors if e not in errors]

    reports: dict[str, Any] = {}
    for agent, key, schema in ((EVIDENCE_AGENT, "evidence_report", EvidenceReport),
                               (INVESTIGATOR_AGENT, "investigation_report", InvestigationReport)):
        raw = state.get(key)
        if raw is None:
            errors.append(f"{agent} produced no structured report")
            continue
        try:
            report = schema.model_validate(json.loads(raw) if isinstance(raw, str) else raw)
        except (ValidationError, json.JSONDecodeError) as exc:
            errors.append(f"{agent} output failed validation: {str(exc).splitlines()[0]}")
            trace.append({"ts": time.time(), "invalid_output": agent, "raw": str(raw)[:500]})
            continue
        if report.claim_id != claim_id:
            errors.append(f"{agent} reported claim_id '{report.claim_id}', expected '{claim_id}'")
            continue
        reports[agent] = report

    unsupported = [p for agent, r in reports.items() for p in verify_references(r, agent, documents, recorder)]
    runs = list(recorder.agent_runs.values())
    usage = TokenUsage(
        prompt_tokens=sum(r.prompt_tokens or 0 for r in runs) if any(r.prompt_tokens is not None for r in runs) else None,
        completion_tokens=sum(r.completion_tokens or 0 for r in runs) if any(r.completion_tokens is not None for r in runs) else None)
    if usage.prompt_tokens is not None or usage.completion_tokens is not None:
        usage.total_tokens = (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)
    trace.append({"ts": time.time(), "tools_offered_to_model": recorder.discovered_tools})

    complete = len(reports) == 2
    return finish(ExecutionStatus.COMPLETED if complete else ExecutionStatus.FAILED, errors,
                  report=reports.get(INVESTIGATOR_AGENT) if complete else None,
                  evidence_report=reports.get(EVIDENCE_AGENT) if complete else None,
                  session_id=session.id, usage=usage, agent_runs=runs, tool_calls=recorder.tool_calls,
                  unsupported_references=unsupported, trace=trace)


def run_adk_investigation(claim_id: str, **kwargs) -> AgentSystemResult:
    return asyncio.run(run_adk_investigation_async(claim_id, **kwargs))


def save_result(result: AgentSystemResult, runtime_dir: Path = config.RUNTIME_DIR) -> Path:
    from claimsleuth.render import adk_markdown
    out = runtime_dir / "reports" / result.run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "adk.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    (out / "adk.md").write_text(adk_markdown(result), encoding="utf-8")
    return out
