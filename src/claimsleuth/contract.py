"""Build, parse and validate the WorkflowMessages exchanged between the ADK side and the CrewAI service.

Transport: each WorkflowMessage is the JSON body (media type application/json) of a text part in an A2A
message (A2A 1.0 JSON-RPC, a2a-sdk 1.3.0). This module is plain Python so both virtual environments use
exactly the same rules.

In blackboard mode a FORENSIC_REQUEST carries ContextRefs (a claim-check: run, claim, section, version, hash,
fingerprint) instead of report content; the service reads the report from the blackboard with its own key.
"""
from __future__ import annotations

import uuid

from collections.abc import Sequence

from pydantic import ValidationError

from claimsleuth.blackboard.models import CONTEXT_READERS, document_fingerprint
from claimsleuth.schemas import (AgentSystemResult, ContextRef, DocumentDigest, ErrorPayload, EvidenceDocument,
                                 ExecutionMetadata, ForensicRequestPayload, ForensicResponsePayload, WorkflowMessage,
                                 utc_now)

FORENSIC_AGENT = "forensic_agent"

ADK_SIDE = "adk_orchestrator"
CREW_SIDE = "crewai_forensic_service"
PROTOCOL = "A2A 1.0 JSON-RPC (a2a-sdk 1.3.0)"


class ContractError(ValueError):
    """A message that does not satisfy the contract. `code` is one of ErrorPayload.code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def digests(documents: list[EvidenceDocument]) -> list[DocumentDigest]:
    return [DocumentDigest(doc_id=d.doc_id, sha256=d.sha256) for d in documents]


def forensic_request(run_id: str, claim_id: str, documents: list[EvidenceDocument],
                     context: Sequence[ContextRef] = ()) -> WorkflowMessage:
    """context: blackboard references (never report content) the service must consume; empty in direct mode."""
    return WorkflowMessage(
        message_id=uuid.uuid4().hex, run_id=run_id, claim_id=claim_id, sender=ADK_SIDE, recipient=CREW_SIDE,
        message_type="FORENSIC_REQUEST", timestamp=utc_now(),
        payload=ForensicRequestPayload(claim_id=claim_id, documents=digests(documents), context=list(context)),
        metadata=ExecutionMetadata(framework="adk", protocol=PROTOCOL))


def forensic_response(request: WorkflowMessage, result: AgentSystemResult, llm_mode: str) -> WorkflowMessage:
    return WorkflowMessage(
        message_id=uuid.uuid4().hex, run_id=request.run_id, claim_id=request.claim_id, sender=CREW_SIDE,
        recipient=ADK_SIDE, message_type="FORENSIC_RESPONSE", timestamp=utc_now(),
        payload=ForensicResponsePayload(result=result),
        metadata=ExecutionMetadata(framework="crewai", model=result.model,
                                   provider=result.report.provider if result.report is not None else None,
                                   llm_mode=llm_mode, protocol=PROTOCOL))


def error_message(run_id: str, claim_id: str, code: str, detail: str) -> WorkflowMessage:
    return WorkflowMessage(
        message_id=uuid.uuid4().hex, run_id=run_id, claim_id=claim_id, sender=CREW_SIDE, recipient=ADK_SIDE,
        message_type="ERROR", timestamp=utc_now(), payload=ErrorPayload(code=code, detail=detail[:2000]),
        metadata=ExecutionMetadata(framework="crewai", protocol=PROTOCOL))


def parse(text: str) -> WorkflowMessage:
    try:
        return WorkflowMessage.model_validate_json(text)
    except ValidationError as exc:
        raise ContractError("INVALID_MESSAGE", str(exc).splitlines()[0] + " | " + "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])) from exc


def check_request(message: WorkflowMessage, documents: list[EvidenceDocument]) -> None:
    """Service side: the request is for us, and our copy of the evidence is byte-identical to the sender's."""
    if message.message_type != "FORENSIC_REQUEST" or message.recipient != CREW_SIDE:
        raise ContractError("INVALID_MESSAGE", f"expected a FORENSIC_REQUEST to {CREW_SIDE}")
    sent = {(d.doc_id, d.sha256) for d in message.payload.documents}
    held = {(d.doc_id, d.sha256) for d in digests(documents)}
    if sent != held:
        raise ContractError("EVIDENCE_MISMATCH", f"sender documents {sorted(sent)} differ from service copy {sorted(held)}")
    fingerprint = document_fingerprint(documents)
    for ref in message.payload.context:     # references only; the service reads the content from the blackboard
        if (ref.run_id, ref.claim_id) != (message.run_id, message.claim_id):
            raise ContractError("CONTEXT_REJECTED", f"context {ref.section}@{ref.run_seq} belongs to run {ref.run_id} / "
                                                    f"claim {ref.claim_id}, not this request")
        if ref.fingerprint != fingerprint:
            raise ContractError("CONTEXT_REJECTED", f"context {ref.section}@{ref.run_seq} was made for other documents")
        if FORENSIC_AGENT not in CONTEXT_READERS.get(ref.section, frozenset()):
            raise ContractError("CONTEXT_REJECTED", f"the Forensic Agent is not authorised to read {ref.section}")


def check_context_required(message: WorkflowMessage, registration: dict | None) -> None:
    """Service side: a run registered on the blackboard shares context only through it, so a request for that run
    without a ContextRef is refused: nobody can reach the Forensic Agent around the blackboard's verification."""
    if registration is not None and not message.payload.context:
        raise ContractError("CONTEXT_REJECTED", f"run {message.run_id} is registered on the blackboard; a request "
                                                "without a context reference is refused")


def check_response(response: WorkflowMessage, request: WorkflowMessage) -> None:
    """ADK side: the reply answers this request (same run, same claim, right direction)."""
    if response.run_id != request.run_id:
        raise ContractError("RUN_MISMATCH", f"response run_id {response.run_id} != request run_id {request.run_id}")
    if response.claim_id != request.claim_id:
        raise ContractError("CLAIM_MISMATCH", f"response claim_id {response.claim_id} != {request.claim_id}")
    if (response.sender, response.recipient) != (CREW_SIDE, ADK_SIDE):
        raise ContractError("INVALID_MESSAGE", "response must go from the CrewAI service to the ADK side")
    if response.message_type not in ("FORENSIC_RESPONSE", "ERROR"):
        raise ContractError("INVALID_MESSAGE", f"unexpected message_type {response.message_type}")
