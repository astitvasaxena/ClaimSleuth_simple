"""Run the GOOGLE ADK investigation of one stored claim and return a validated AgentSystemResult.

Lifecycle per run: load the same original claim the baseline uses -> check the MCP server is up ->
create an ADK session (state: run_id, claim_id) -> Runner.run_async (Evidence Agent, then Investigator
Agent) -> read both structured reports from session state -> validate with Pydantic -> verify every
citation against the documents and every policy/history reference against what MCP actually returned
-> close the runner (closes the MCP sessions) -> delete the ADK session.

A run is COMPLETED only when both reports validate. Nothing here falls back to deterministic code.

Blackboard mode (CLAIMSLEUTH_BLACKBOARD_DB set, or `blackboard_path`): the Evidence Agent's report is validated
(including its citations and MCP references) and published to the blackboard as soon as the agent finishes; the
Investigator receives only the copy it consumed from the blackboard (blackboard/adk_plugin.py), and this run's
Evidence report IS that published report - the raw session-state value is never used. If the report cannot be
validated, published or consumed, the Investigator is not started and the run is FAILED (fail closed).
Direct mode (blackboard disabled): the Investigator reads the Evidence report from ADK session state, as before.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from google.adk.agents.run_config import RunConfig
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import ValidationError

from claimsleuth import config, execution_mode, gemini_rate
from claimsleuth.adk_service import config as adk_config
from claimsleuth.adk_service.agents import EVIDENCE_AGENT, INVESTIGATOR_AGENT, build_agents
from claimsleuth.adk_service.callbacks import ErrorRecorderPlugin, EvidenceIsolation, ExecutionRecorder
from claimsleuth.baseline import new_run_id
from claimsleuth.blackboard import AgentParticipant, ContextRef
from claimsleuth.blackboard.models import payload_hash
from claimsleuth.claim_text import claim_message
from claimsleuth.datastore import load_claim
from claimsleuth.documents import quote_appears, value_on_line
from claimsleuth.observability.provider_errors import failure_text, redact
from claimsleuth.schemas import (AgentSystemResult, EvidenceDocument, EvidenceReport, ExecutionStatus, ExtractedFact,
                                 InvestigationReport, TokenUsage, ToolCitation, utc_now)

APP_NAME = "claimsleuth_adk"
USER_ID = "claimsleuth"
LIVE_REQUIRED_TOOLS = {EVIDENCE_AGENT: ("warranty_policy_search",), INVESTIGATOR_AGENT: ("claim_history_lookup",)}


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


def unbound_facts(report: EvidenceReport, documents: list[EvidenceDocument]) -> list[ExtractedFact]:
    """Facts marked VERIFIED whose value is not on their cited line: other text cannot borrow that word."""
    docs = {d.doc_id: d for d in documents}
    return [f for f in report.facts if f.status == "VERIFIED" and f.citation.doc_id in docs
            and not value_on_line(docs[f.citation.doc_id], f.citation.line, f.value)]


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
    if isinstance(report, EvidenceReport):
        problems += [f"{agent}: fact '{f.field}' is marked VERIFIED but is not written on {f.citation.doc_id}#L{f.citation.line}"
                     for f in unbound_facts(report, documents)]
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


def check_evidence(raw: Any, claim_id: str, documents: list[EvidenceDocument], recorder: ExecutionRecorder,
                   live: bool) -> tuple[EvidenceReport | None, list[str]]:
    """Validate the Evidence Agent's raw output before it may be shared: schema, claim id, (live) its required MCP
    call, and every citation and MCP reference. Returns (report, []) or (None, problems)."""
    if raw is None:
        recorder.record_output_failure(EVIDENCE_AGENT, ValueError(f"{EVIDENCE_AGENT} produced no structured report"))
        return None, [f"{EVIDENCE_AGENT} produced no structured report"]
    try:
        report = EvidenceReport.model_validate(json.loads(raw) if isinstance(raw, str) else raw)
    except (ValidationError, json.JSONDecodeError) as exc:
        recorder.record_output_failure(EVIDENCE_AGENT, exc)
        return None, [f"{EVIDENCE_AGENT} output failed validation: {str(exc).splitlines()[0]}"]
    if report.claim_id != claim_id:
        return None, [f"{EVIDENCE_AGENT} reported claim_id '{report.claim_id}', expected '{claim_id}'"]
    refusal = recorder.refuse_restricted(EVIDENCE_AGENT, report, documents)
    if refusal:
        return None, [refusal]
    problems = []
    if live:
        done = {c.tool for c in recorder.tool_calls if c.agent == EVIDENCE_AGENT and c.status == "ok"}
        problems += [f"{EVIDENCE_AGENT} did not complete the required MCP call {t}"
                     for t in LIVE_REQUIRED_TOOLS[EVIDENCE_AGENT] if t not in done]
    problems += verify_references(report, EVIDENCE_AGENT, documents, recorder)
    if unbound_facts(report, documents):
        recorder.refuse_output(EVIDENCE_AGENT, claim_id, ["verified_fact_not_on_cited_line"], "UNSUPPORTED_FACT")
    return (None, problems) if problems else (report, [])


def _timing(record) -> dict[str, Any]:
    if record is None:
        return {}
    return {"started_at": record.started_at.isoformat() if record.started_at else None,
            "finished_at": utc_now().isoformat(), "llm_calls": record.llm_calls, "tool_calls": record.tool_calls}


def _event_trace(event) -> dict[str, Any]:
    parts = event.content.parts if event.content and event.content.parts else []
    return {
        "ts": event.timestamp, "author": event.author,
        "function_calls": [p.function_call.name for p in parts if p.function_call],
        "function_responses": [p.function_response.name for p in parts if p.function_response],
        "text": next((redact(p.text, 300) for p in parts if p.text), None),
        "final": event.is_final_response(),
    }


async def run_adk_investigation_async(claim_id: str, run_id: str | None = None, model: Any = None,
                                      mcp_url: str | None = None, data_dir: Path = config.DATA_DIR,
                                      prompt_variant: str = "standard", llm_mode: str | None = None,
                                      blackboard_path: Path | None = None,
                                      on_evidence: Callable[[ContextRef | None, str | None], None] | None = None,
                                      authorise: Callable[[str], str | None] | None = None,
                                      on_agent: Callable[[str, str, dict[str, Any]], None] | None = None,
                                      on_recorder: Callable[[ExecutionRecorder], None] | None = None
                                      ) -> AgentSystemResult:
    """llm_mode: "gemini" (genuine Gemini inference, needs credentials) or "test-double" (offline OfflineAdkLlm,
    labelled). When not passed: CLAIMSLEUTH_ADK_LLM, else EXECUTION_MODE (live -> gemini, offline -> test-double),
    else gemini. A mode that contradicts EXECUTION_MODE is NOT_CONFIGURED. Ignored when `model` is passed (tests).

    blackboard_path: the blackboard database (default: CLAIMSLEUTH_BLACKBOARD_DB). on_evidence(ref, error): called
    once in blackboard mode, as soon as the Evidence report was published (ref) or could not be (error).
    authorise(agent) and on_agent(agent, event, data): the orchestration manager's supervision in blackboard mode
    (blackboard/adk_plugin.EvidenceContext): the Investigator starts only when authorise returns None.
    on_recorder(recorder): gets this run's execution record, which outlives a cancelled run (the manager's deadline)."""
    run_id = run_id or new_run_id()
    mcp_url = mcp_url or adk_config.mcp_url()
    started_at, t0 = utc_now(), time.perf_counter()
    model_label = model if isinstance(model, str) else ("test-double" if model is not None else None)
    base = dict(system="GOOGLE_ADK", run_id=run_id, claim_id=claim_id, prompt_variant=prompt_variant,
                started_at=started_at)

    board = AgentParticipant(run_id, claim_id, "adk", (EVIDENCE_AGENT, INVESTIGATOR_AGENT),   # no-op unless enabled
                             path=blackboard_path)
    shared = board.requested              # blackboard mode: Evidence -> Investigator context goes through it

    def finish(status: ExecutionStatus, errors: list[str], **kw) -> AgentSystemResult:
        if shared and on_evidence is not None and "evidence_published" not in notified:
            notified.add("evidence_published")      # the run ended before the Evidence Agent could publish
            try:
                on_evidence(None, errors[0] if errors else "the ADK run ended without an Evidence report")
            except Exception:
                pass
        result = AgentSystemResult(**base, status=status, errors=errors, model=model_label, finished_at=utc_now(),
                                   execution_ms=round((time.perf_counter() - t0) * 1000, 3),
                                   consumed_context=list(board.consumed),
                                   security_events=list(board.security_events), **kw)
        board.finished(result)            # final statuses; the Investigator's report (the Evidence report is
        return result                     # already published in blackboard mode)

    notified: set[str] = set()

    def evidence_outcome(ref: ContextRef | None, error: str | None) -> None:
        notified.add("evidence_published")
        if on_evidence is not None:
            on_evidence(ref, error)

    live = False                         # genuine Gemini inference selected by this function (not a passed-in model)
    if model is None:
        try:
            mode = execution_mode.adk_llm_mode(llm_mode)
        except execution_mode.ExecutionModeError as exc:
            return finish(ExecutionStatus.NOT_CONFIGURED, [str(exc)])
        if mode == "test-double":
            from claimsleuth.adk_service.test_double import offline_models
            model, model_label = offline_models(), "test-double"
        elif mode == "gemini":
            adk_config.load_env()
            problems = adk_config.llm_config_errors()
            if problems:
                return finish(ExecutionStatus.NOT_CONFIGURED, problems)
            model_label = adk_config.gemini_model()
            model = adk_config.gemini_llm(model_label)       # genuine Gemini inference
            live = True
        else:
            return finish(ExecutionStatus.NOT_CONFIGURED, [f"unknown ADK LLM mode {mode!r} (use gemini or test-double)"])

    try:
        claim, documents = load_claim(claim_id, data_dir)
    except (FileNotFoundError, ValidationError) as exc:
        return finish(ExecutionStatus.FAILED, [f"claim could not be loaded: {exc}"])
    board.running(documents, mark_running=False)   # each agent is marked RUNNING when it actually starts

    healthy, detail = mcp_health(mcp_url)
    if not healthy:
        return finish(ExecutionStatus.FAILED, [f"MCP server not available, ADK run not started. {detail}"])

    before_request = None
    if live:      # every Gemini request takes a slot in the ledger shared with the CrewAI process, a retry too
        def before_request(agent_name: str) -> None:
            gemini_rate.acquire(model_label, f"adk:{agent_name}", run_id)
    recorder = ExecutionRecorder(run_id, before_request=before_request, model=model_label,
                                 provider=execution_mode.PROVIDER_GEMINI if live else "offline-test-double",
                                 retry_model=None if isinstance(model, str) else
                                 (lambda agent: model[agent] if isinstance(model, dict) else model))
    if on_recorder is not None:
        on_recorder(recorder)
    run_timeout = adk_config.LIVE_RUN_TIMEOUT_SECONDS if live else adk_config.RUN_TIMEOUT_SECONDS
    session_service = InMemorySessionService()
    plugins = [ErrorRecorderPlugin(recorder)]        # exceptions escaping an agent (notification only)
    context, isolation = None, EvidenceIsolation()
    if shared:                            # lifecycle + Evidence -> Investigator context, via ADK's plugin mechanism
        from claimsleuth.blackboard.adk_plugin import BlackboardLifecyclePlugin, EvidenceContext
        context = EvidenceContext(board, validate=lambda raw: check_evidence(raw, claim_id, documents, recorder, live),
                                  notify=evidence_outcome, timing=lambda: _timing(recorder.agent_runs.get(EVIDENCE_AGENT)),
                                  authorise=authorise, observe=on_agent)
        plugins.append(BlackboardLifecyclePlugin(board, context))
    else:
        plugins.append(isolation)
    runner = Runner(app=App(name=APP_NAME, root_agent=build_agents(model, mcp_url, recorder), plugins=plugins),
                    session_service=session_service)
    session = await session_service.create_session(app_name=APP_NAME, user_id=USER_ID,
                                                   state={"run_id": run_id, "claim_id": claim_id})
    trace: list[dict[str, Any]] = [{"ts": time.time(), "lifecycle": "session_created", "session_id": session.id}]
    errors: list[str] = []
    state: dict[str, Any] = {}
    try:
        message = types.Content(role="user", parts=[types.Part(text=claim_message(claim, documents, run_id))])
        async with asyncio.timeout(run_timeout):
            async for event in runner.run_async(user_id=USER_ID, session_id=session.id, new_message=message,
                                                run_config=RunConfig(max_llm_calls=adk_config.LIVE_MAX_LLM_CALLS
                                                                     if live else adk_config.MAX_LLM_CALLS)):
                trace.append(_event_trace(event))
                if event.error_code or event.error_message:
                    errors.append(f"{event.author}: {event.error_code}: {event.error_message}")
        final = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=session.id)
        state = dict(final.state) if final else {}
    except TimeoutError:
        errors.append(f"ADK run exceeded {run_timeout} s")
        trace.append({"ts": time.time(), "timed_out_after_s": run_timeout})
        recorder.close_open("timeout", TimeoutError(f"the ADK run exceeded {run_timeout} s"))
        recorder.mark_timed_out()
    except asyncio.CancelledError:      # the manager's deadline: its record is kept, the cancellation goes on
        recorder.close_open("cancelled", TimeoutError("the manager's deadline for the ADK workflow passed"))
        raise
    except Exception as exc:     # model, MCP or ADK failure: recorded, never replaced by invented output
        recorder.record_failure(exc)
        errors.append(f"ADK run failed: {failure_text(exc)}")
    finally:
        recorder.close_open("exception")
        try:
            await runner.close()
            trace.append({"ts": time.time(), "lifecycle": "runner_closed"})
        except Exception as exc:
            errors.append(f"runner close failed: {type(exc).__name__}: {exc}")
        await session_service.delete_session(app_name=APP_NAME, user_id=USER_ID, session_id=session.id)
        trace.append({"ts": time.time(), "lifecycle": "session_deleted", "session_id": session.id})
        if context is not None:
            context.finish()
    errors += [e for e in recorder.model_errors if e not in errors]

    reports: dict[str, Any] = {}
    if context is not None:               # blackboard mode: the Evidence report is the one published (validated)
        trace.append({"ts": time.time(), "blackboard_context": {
            "published": context.ref.model_dump() if context.ref else None, "error": context.error,
            "consumed": [c.model_dump() for c in board.consumed],
            "supplied_to_investigator_sha256": payload_hash(context.supplied) if context.supplied else None}})
        if context.report is not None:
            reports[EVIDENCE_AGENT] = context.report
        else:
            errors.append(context.error or f"{EVIDENCE_AGENT}: no report published to the blackboard")
    for agent, key, schema in ((EVIDENCE_AGENT, "evidence_report", EvidenceReport),
                               (INVESTIGATOR_AGENT, "investigation_report", InvestigationReport)):
        if context is not None and agent == EVIDENCE_AGENT:
            continue                      # never the raw session-state value (it now holds the shared copy)
        raw = isolation.raw if agent == EVIDENCE_AGENT and isolation.raw is not None else state.get(key)
        if raw is None:
            if context is not None and context.supplied is None:
                errors.append(f"{agent} was not started: no verified Evidence report could be consumed from the "
                              "blackboard (fail closed)")
            elif agent not in recorder.agent_runs:     # SequentialAgent stops at the first failed agent
                errors.append(f"{agent} was not started: the ADK workflow stopped before it (see the errors above)")
            else:
                errors.append(f"{agent} produced no structured report")
                recorder.record_output_failure(agent, ValueError(f"{agent} produced no structured report"))
            continue
        try:
            report = schema.model_validate(json.loads(raw) if isinstance(raw, str) else raw)
        except (ValidationError, json.JSONDecodeError) as exc:
            errors.append(f"{agent} output failed validation: {str(exc).splitlines()[0]}")
            trace.append({"ts": time.time(), "invalid_output": agent, "raw": redact(raw, 500)})
            recorder.record_output_failure(agent, exc)
            continue
        if report.claim_id != claim_id:
            errors.append(f"{agent} reported claim_id '{report.claim_id}', expected '{claim_id}'")
            continue
        refusal = recorder.refuse_restricted(agent, report, documents)
        if refusal:
            errors.append(refusal)
            continue
        reports[agent] = report

    if live:     # a live agent must actually have used its required MCP tool, not only produced a report
        for agent, tools in LIVE_REQUIRED_TOOLS.items():
            done = {c.tool for c in recorder.tool_calls if c.agent == agent and c.status == "ok"}
            missing = [t for t in tools if t not in done]
            if missing and agent in reports:
                errors.append(f"{agent} did not complete the required MCP call(s) {missing}; a live report without "
                              "its required tool use is not accepted")
                del reports[agent]

    unsupported = [p for agent, r in reports.items() for p in verify_references(r, agent, documents, recorder)]
    runs = list(recorder.agent_runs.values())
    usage = TokenUsage(
        prompt_tokens=sum(r.prompt_tokens or 0 for r in runs) if any(r.prompt_tokens is not None for r in runs) else None,
        completion_tokens=sum(r.completion_tokens or 0 for r in runs) if any(r.completion_tokens is not None for r in runs) else None)
    if usage.prompt_tokens is not None or usage.completion_tokens is not None:
        usage.total_tokens = (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)
    trace.append({"ts": time.time(), "tools_offered_to_model": recorder.discovered_tools})
    trace += recorder.trace_records()                  # per-call model records and agent errors (observability)
    trace.append({"ts": time.time(), "blackboard_ops": list(board.operations)})

    complete = len(reports) == 2
    # A published Evidence report stays with the result when a later agent fails: it is valid, it is signed on the
    # blackboard, and the adjudicator still ignores it because the ADK result is not COMPLETED.
    keep_evidence = complete or context is not None
    return finish(ExecutionStatus.COMPLETED if complete else ExecutionStatus.FAILED, errors,
                  report=reports.get(INVESTIGATOR_AGENT) if complete else None,
                  evidence_report=reports.get(EVIDENCE_AGENT) if keep_evidence else None,
                  session_id=session.id, usage=usage, agent_runs=runs, tool_calls=recorder.tool_calls,
                  unsupported_references=unsupported, trace=trace, provider_failures=list(recorder.failures),
                  provider_retries=list(recorder.retries), injection_events=list(recorder.injection_events))


def run_adk_investigation(claim_id: str, **kwargs) -> AgentSystemResult:
    return asyncio.run(run_adk_investigation_async(claim_id, **kwargs))


def save_result(result: AgentSystemResult, runtime_dir: Path = config.RUNTIME_DIR) -> Path:
    from claimsleuth.render import adk_markdown
    out = runtime_dir / "reports" / result.run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "adk.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    (out / "adk.md").write_text(adk_markdown(result), encoding="utf-8")
    return out
