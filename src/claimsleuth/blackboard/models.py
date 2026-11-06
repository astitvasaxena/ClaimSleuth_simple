"""Records of the shared blackboard (shared by both virtual environments).

The blackboard is the run's shared state: it coordinates and records the investigation, and it is the channel
through which intermediate context passes between agents. Each agent writes only its own status and its own
validated report; a report is read by another agent only through Blackboard.consume, under CONTEXT_READERS, and
every such read leaves a signed receipt. The Forensic Agent reads only the Evidence report, never the
Investigator's, so ADK and CrewAI stay two independent opinions.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from claimsleuth.schemas import ConsumedContext, ContextRef, EvidenceDocument  # noqa: F401  (re-exported)

EntryKind = Literal["register", "status", "post", "retract", "monitor"]
AgentState = Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "NOT_CONFIGURED"]
TERMINAL: frozenset[str] = frozenset({"COMPLETED", "FAILED", "NOT_CONFIGURED"})
# Allowed status changes. Repeating the current status is an idempotent no-op, never a new entry.
TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"RUNNING", "FAILED", "NOT_CONFIGURED"}),
    "RUNNING": frozenset({"COMPLETED", "FAILED", "NOT_CONFIGURED"}),
    "COMPLETED": frozenset(), "FAILED": frozenset(), "NOT_CONFIGURED": frozenset(),
}
# The agents a run expects, and the framework each belongs to.
EXPECTED_AGENTS: dict[str, str] = {"evidence_agent": "adk", "investigator_agent": "adk", "forensic_agent": "crewai"}
# Read access to shared context: which consumer may read which report section (writers: see auth.PRINCIPALS).
# Enforced by Blackboard.consume; nothing else hands one agent's report to another.
CONTEXT_READERS: dict[str, frozenset[str]] = {
    "report:evidence_agent": frozenset({"investigator_agent", "forensic_agent", "adjudicator"}),
    "report:investigator_agent": frozenset({"adjudicator"}),
    "report:forensic_agent": frozenset({"adjudicator"}),
}
RECEIPT_PREFIX = "context:consumed:"     # monitor section of a consumption receipt: context:consumed:<consumer>
# Every way a read can be refused for a security reason: attack_type -> (event type, severity, rejection reason).
# Events carry these fixed texts, never an exception's text, so nothing from a stored entry can reach monitoring.
_INTEGRITY = "EVIDENCE_INTEGRITY_VIOLATION"
SECURITY_RESPONSES: dict[str, tuple[str, str, str]] = {
    "PAYLOAD_TAMPERING": (_INTEGRITY, "CRITICAL", "The stored payload does not match the hash signed at publication."),
    "HASH_AND_PAYLOAD_TAMPERING": (_INTEGRITY, "CRITICAL", "The stored hash was changed and has no valid HMAC signature."),
    "SIGNATURE_FORGERY": (_INTEGRITY, "CRITICAL", "The entry's HMAC-SHA256 signature is missing or does not verify."),
    "CROSS_CLAIM_SUBSTITUTION": (_INTEGRITY, "CRITICAL", "The report is bound to another claim or to other documents."),
    "REPLAY": (_INTEGRITY, "HIGH", "The report was retracted, superseded or referenced from another run."),
    "REFERENCE_MISMATCH": (_INTEGRITY, "HIGH", "The context reference does not match a current report entry."),
    "MALFORMED_REPORT": (_INTEGRITY, "HIGH", "The report failed its schema or does not match its recorded report hash."),
    "UNAUTHORISED_ACCESS": ("UNAUTHORISED_ACCESS", "HIGH", "The reader is not authorised to read this section."),
}


class BoardModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcessInfo(BoardModel):
    pid: int
    environment: str                    # the virtual environment that wrote the entry, e.g. ".venv" or ".venv-crew"


class BoardEntry(BoardModel):
    """One append-only row. Nothing on the blackboard is ever updated or deleted."""
    seq: int                            # global order in the database file
    run_seq: int                        # order within the run (1, 2, 3 ...; unique per run)
    run_id: str
    claim_id: str
    ts: datetime
    kind: EntryKind
    section: str                        # e.g. "status:forensic_agent", "report:evidence_agent"
    author: str
    framework: str
    process: ProcessInfo
    fingerprint: str | None = None      # document fingerprint the writer investigated
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_sha256: str
    ref_seq: int | None = None          # retract: the entry it retracts
    principal: str | None = None        # authenticated writer: orchestrator, adk or crewai
    signature: str | None = None        # HMAC-SHA256 over the entry (see auth.py)
    auth: str = "unsigned"              # verified on read: valid / unsigned / invalid / identity / unknown-key
    late: bool = False                  # an agent entry written after the adjudicator's inputs were fixed


class AgentStatus(BoardModel):
    agent: str
    framework: str
    state: AgentState
    seq: int                            # the entry that set this state
    late: bool = False                  # that state arrived after the adjudicator's inputs were fixed
    state_at_seal: AgentState | None = None   # what the controller saw when it fixed the inputs


class RunState(BoardModel):
    """The run as the blackboard currently shows it (derived from the append-only history)."""
    run_id: str
    claim_id: str
    fingerprint: str
    agents: dict[str, AgentStatus]
    live_posts: list[BoardEntry]        # posts that have not been retracted
    retracted: list[BoardEntry]         # retracted posts
    retractions: list[BoardEntry]
    monitor: list[BoardEntry]
    entries: int
    sealed_at: int | None = None        # run_seq of the controller's seal (adjudicator inputs fixed)
    late: list[BoardEntry] = Field(default_factory=list)
    authentication: dict[str, int] = Field(default_factory=dict)
    unverified: list[str] = Field(default_factory=list)       # section@run_seq of every entry that failed verification


def document_fingerprint(documents: list[EvidenceDocument]) -> str:
    """One hash over the claim's documents (doc id + content hash), independent of their order."""
    lines = sorted(f"{d.doc_id}:{d.sha256}" for d in documents)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
