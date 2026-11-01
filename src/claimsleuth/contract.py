"""Build, parse and validate the WorkflowMessages exchanged between the ADK side and the CrewAI service.

Transport: each WorkflowMessage is the JSON body (media type application/json) of a text part in an A2A
message (A2A 1.0 JSON-RPC, a2a-sdk 1.3.0). This module is plain Python so both virtual environments use
exactly the same rules.
"""
from __future__ import annotations

import uuid

from pydantic import ValidationError

from claimsleuth.schemas import (AgentSystemResult, DocumentDigest, ErrorPayload, EvidenceDocument, ExecutionMetadata,
                                 ForensicRequestPayload, ForensicResponsePayload, WorkflowMessage, utc_now)

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


def forensic_request(run_id: str, claim_id: str, documents: list[EvidenceDocument]) -> WorkflowMessage:
    return WorkflowMessage(
        message_id=uuid.uuid4().hex, run_id=run_id, claim_id=claim_id, sender=ADK_SIDE, recipient=CREW_SIDE,
        message_type="FORENSIC_REQUEST", timestamp=utc_now(),
        payload=ForensicRequestPayload(claim_id=claim_id, documents=digests(documents)),
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
