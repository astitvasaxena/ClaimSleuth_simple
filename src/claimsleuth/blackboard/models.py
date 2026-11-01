"""Records of the shared coordination blackboard (shared by both virtual environments).

The blackboard coordinates and records an investigation run; it never carries reasoning from one agent to another.
The three agents stay independent: each publishes only its own status and its own validated report.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from claimsleuth.schemas import EvidenceDocument

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


def document_fingerprint(documents: list[EvidenceDocument]) -> str:
    """One hash over the claim's documents (doc id + content hash), independent of their order."""
    lines = sorted(f"{d.doc_id}:{d.sha256}" for d in documents)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
