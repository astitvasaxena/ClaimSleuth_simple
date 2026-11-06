"""ADK -> CrewAI over A2A: Google ADK's RemoteA2aAgent calls the CrewAI Forensic Agent service.

The RemoteA2aAgent runs under its own ADK Runner and session (separate from the Evidence/Investigator
pipeline; the Forensic Agent never sees the Investigator's result). It resolves the service's Agent Card, sends one A2A
message whose text part is a WorkflowMessage FORENSIC_REQUEST, and receives the FORENSIC_RESPONSE (or
ERROR) WorkflowMessage. Both messages are validated against the contract; the CrewAI result is returned
exactly as the service produced it.

Blackboard mode: the request carries ContextRefs to the verified Evidence report (references, not content); the
service consumes the report from the blackboard itself. When no verified Evidence report exists, no request is
sent at all (not_sent): the Forensic Agent never runs on unverified context.
"""
from __future__ import annotations

import asyncio
import os
import time
import warnings
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from claimsleuth import contract
from claimsleuth.schemas import (AgentSystemResult, ClaimInput, ContextRef, EvidenceDocument, ExecutionStatus,
                                 ForensicResponsePayload, WorkflowMessage, utc_now)

# google-adk 2.13.0 flags its A2A components (RemoteA2aAgent, converters) as experimental on every call;
# the notice is recorded in docs/PHASE5_GUIDE.md and silenced here.
warnings.filterwarnings("ignore", message=r"\[EXPERIMENTAL\]", category=UserWarning)

APP_NAME = "claimsleuth_adk_a2a"
REMOTE_AGENT = "crewai_forensic_remote"
A2A_TIMEOUT_SECONDS = 180.0            # default; enough for the offline test double
A2A_TIMEOUT_RANGE = (30.0, 3600.0)     # local CPU inference (ollama-local) needs CLAIMSLEUTH_A2A_TIMEOUT ~1200


def a2a_timeout(value: float | None = None) -> float:
    """Explicit A2A wait: argument, else CLAIMSLEUTH_A2A_TIMEOUT, else 180 s; must lie in 30..3600 s."""
    raw = value if value is not None else os.environ.get("CLAIMSLEUTH_A2A_TIMEOUT", A2A_TIMEOUT_SECONDS)
    seconds = float(raw)
    if not A2A_TIMEOUT_RANGE[0] <= seconds <= A2A_TIMEOUT_RANGE[1]:
        raise ValueError(f"A2A timeout {seconds} s is outside {A2A_TIMEOUT_RANGE[0]:.0f}..{A2A_TIMEOUT_RANGE[1]:.0f} s")
    return seconds


@dataclass
class ForensicExchange:
    request: WorkflowMessage
    response: WorkflowMessage | None
    result: AgentSystemResult                      # the CrewAI result, or a FAILED stand-in if none arrived
    errors: list[str] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    sent: bool = True                              # False: the request was never sent (see not_sent)
    timed_out: bool = False                        # no answer within the A2A timeout


def _failed(request: WorkflowMessage, errors: list[str]) -> AgentSystemResult:
    now = utc_now()
    return AgentSystemResult(system="CREWAI", run_id=request.run_id, claim_id=request.claim_id,
                             status=ExecutionStatus.FAILED, started_at=now, finished_at=now, errors=errors)


def not_sent(claim: ClaimInput, documents: list[EvidenceDocument], run_id: str, reason: str) -> ForensicExchange:
    """The Forensic Agent was not asked (no verified shared context): an explicit FAILED result, no A2A message."""
    request = contract.forensic_request(run_id, claim.claim_id, documents)
    errors = [f"Forensic Agent not started: {reason}"]
    return ForensicExchange(request, None, _failed(request, errors), errors,
                            [{"ts": time.time(), "a2a": "request_not_sent", "reason": reason}], sent=False)


async def request_forensics(claim: ClaimInput, documents: list[EvidenceDocument], run_id: str, card_url: str,
                            timeout: float | None = None, context: list[ContextRef] | None = None) -> ForensicExchange:
    timeout = a2a_timeout(timeout)
    request = contract.forensic_request(run_id, claim.claim_id, documents, context or ())
    trace: list[dict[str, Any]] = []
    texts: list[str] = []
    errors: list[str] = []
    timed_out = False

    async def before_agent(callback_context):
        trace.append({"ts": time.time(), "a2a": "request_sent", "agent": callback_context.agent_name,
                      "message_id": request.message_id, "card": card_url})

    async def after_agent(callback_context):
        trace.append({"ts": time.time(), "a2a": "agent_finished", "agent": callback_context.agent_name})

    remote = RemoteA2aAgent(name=REMOTE_AGENT, agent_card=card_url, timeout=timeout,
                            description="CrewAI Forensic Agent (A2A service)",
                            before_agent_callback=before_agent, after_agent_callback=after_agent)
    sessions = InMemorySessionService()
    runner = Runner(app=App(name=APP_NAME, root_agent=remote), session_service=sessions)
    session = await sessions.create_session(app_name=APP_NAME, user_id="claimsleuth",
                                            state={"run_id": run_id, "claim_id": claim.claim_id})
    try:
        message = types.Content(role="user", parts=[types.Part(text=request.model_dump_json())])
        async with asyncio.timeout(timeout + 10):
            async for event in runner.run_async(user_id="claimsleuth", session_id=session.id, new_message=message):
                meta = event.custom_metadata or {}
                if event.error_code or event.error_message:
                    errors.append(f"A2A error: {event.error_code}: {event.error_message}")
                if event.author == REMOTE_AGENT and event.content and event.content.parts:
                    texts += [p.text for p in event.content.parts if p.text]
                    trace.append({"ts": time.time(), "a2a": "response_received",
                                  "a2a_task_id": str(meta.get("a2a:task_id", "")) or None,
                                  "a2a_context_id": str(meta.get("a2a:context_id", "")) or None})
    except TimeoutError:
        timed_out = True
        errors.append(f"A2A request timed out after {timeout:.0f} s (CLAIMSLEUTH_A2A_TIMEOUT); no result accepted")
    except Exception as exc:          # unreachable card, HTTP error: recorded, nothing invented
        errors.append(f"A2A call failed: {type(exc).__name__}: {str(exc)[:500]}")
    finally:
        await runner.close()
        await sessions.delete_session(app_name=APP_NAME, user_id="claimsleuth", session_id=session.id)

    if not texts:
        errors.append("no A2A response received from the CrewAI service")
        return ForensicExchange(request, None, _failed(request, errors), errors, trace, timed_out=timed_out)
    try:
        response = contract.parse(texts[-1])
        contract.check_response(response, request)
    except contract.ContractError as exc:
        errors.append(f"invalid A2A response: {exc}")
        return ForensicExchange(request, None, _failed(request, errors), errors, trace)
    if isinstance(response.payload, ForensicResponsePayload):
        return ForensicExchange(request, response, response.payload.result, errors, trace)
    errors.append(f"CrewAI service returned ERROR {response.payload.code}: {response.payload.detail}")
    return ForensicExchange(request, response, _failed(request, errors), errors, trace)
