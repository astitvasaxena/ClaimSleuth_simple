"""Pydantic models shared by every ClaimSleuth service.

One module so the dashboard, the deterministic baseline, the MCP server, the ADK service and the
CrewAI service all validate the same shapes. Inputs and tool outputs forbid unknown fields.
Money is always Decimal and only ever produced by deterministic code (rules.compute_settlement).
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID_PATTERN = r"^[A-Z0-9][A-Z0-9-]{2,39}$"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- enums

class Decision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    ESCALATE = "ESCALATE"
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"


class ExecutionStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class DocumentKind(str, Enum):
    INVOICE = "INVOICE"
    DIAGNOSTIC = "DIAGNOSTIC"
    OTHER = "OTHER"


class CheckOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUABLE = "NOT_EVALUABLE"


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


Persona = Literal["honest", "vague", "abusive", "injection"]

# Source-authority labels (handoff §8): purchase record and technician report outrank customer text;
# LLM text is never evidence.
Authority = Literal["PURCHASE_RECORD", "TECH_REPORT", "CLAIM_FORM", "POLICY", "HISTORY_DB", "CUSTOMER_TEXT"]


# --------------------------------------------------------------------------- claim input and evidence

class DocumentRef(StrictModel):
    doc_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    kind: DocumentKind
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.txt$")


class ClaimInput(StrictModel):
    """A claim as submitted. Free-text fields are untrusted customer text."""
    claim_id: str = Field(pattern=ID_PATTERN)
    policy_id: str = Field(pattern=ID_PATTERN)
    claimant_id: str = Field(pattern=ID_PATTERN)
    claimant_name: str = Field(min_length=1, max_length=120)
    device_model: str = Field(min_length=1, max_length=120)
    serial_number: str | None = Field(default=None, max_length=40)
    incident_date: date
    reported_issue: str = Field(min_length=1, max_length=2000)
    claimant_statement: str = Field(default="", max_length=4000)
    documents: list[DocumentRef] = Field(default_factory=list, max_length=10)

    @field_validator("incident_date")
    @classmethod
    def _not_in_future(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("incident_date is in the future")
        return value

    @model_validator(mode="after")
    def _unique_documents(self) -> "ClaimInput":
        ids = [d.doc_id for d in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("document ids must be unique")
        return self


class EvidenceDocument(StrictModel):
    """A submitted document. Its text is data, never instructions."""
    doc_id: str
    kind: DocumentKind
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(max_length=20000)
    trust: Literal["UNTRUSTED"] = "UNTRUSTED"

    def numbered_lines(self) -> list[tuple[int, str]]:
        return list(enumerate(self.text.splitlines(), start=1))


def make_evidence_id(doc_id: str, line: int | None, field: str) -> str:
    """Stable across processes (unlike hash()); same input, same id."""
    digest = hashlib.sha1(f"{doc_id}|{line}|{field}".encode("utf-8")).hexdigest()
    return f"EV-{digest[:10]}"


class EvidenceRef(StrictModel):
    evidence_id: str = Field(pattern=r"^EV-[0-9a-f]{10}$")
    doc_id: str
    line: int | None = Field(default=None, ge=1)
    field: str
    value: str
    quote: str = Field(max_length=500)
    authority: Authority


class Citation(StrictModel):
    """How an LLM agent points at evidence; verifiable against the document line."""
    doc_id: str = Field(max_length=80)
    line: int = Field(ge=1, le=2000)
    quote: str = Field(min_length=1, max_length=300)


# --------------------------------------------------------------------------- policies and history

class PolicyTerms(StrictModel):
    policy_id: str = Field(pattern=ID_PATTERN)
    name: str
    product_category: str
    duration_months: int = Field(ge=1, le=120)
    max_coverage_limit: Decimal = Field(ge=0, decimal_places=2)
    deductible: Decimal = Field(ge=0, decimal_places=2)
    depreciation_rate: Decimal = Field(ge=0, le=1, decimal_places=4)
    requires_original_receipt: bool
    requires_diagnostic_report: bool
    exclusions: list[str]


class PolicyReference(StrictModel):
    """One retrieved policy passage (returned by the MCP warranty_policy_search tool)."""
    policy_id: str
    section: str
    text: str
    source: str
    score: float | None = None


class HistoryRecord(StrictModel):
    """One earlier claim from the SQLite claim_history table."""
    claim_id: str
    serial_number: str
    claimant_id: str
    device_model: str
    claim_date: date
    outcome: Literal["APPROVED", "REJECTED", "ESCALATED", "PENDING"]
    amount: Decimal | None = None


# --------------------------------------------------------------------------- guardrails

GuardrailKind = Literal[
    "prompt_injection", "abusive_language", "invalid_input", "invalid_tool_output",
    "untrusted_document", "missing_evidence", "decision_validation", "unsupported_claim",
]


class GuardrailEvent(StrictModel):
    ts: datetime = Field(default_factory=utc_now)
    run_id: str | None = None
    kind: GuardrailKind
    source: str                        # e.g. "claim.claimant_statement" or "doc:repair_estimate#L12"
    action: Literal["logged", "neutralised", "blocked", "escalated"]
    detail: str
    matched: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- deterministic baseline

class RuleCheck(StrictModel):
    name: str
    outcome: CheckOutcome
    detail: str
    evidence_ids: list[str] = Field(default_factory=list)


class Settlement(StrictModel):
    repair_cost: Decimal
    deductible: Decimal
    eligible_amount: Decimal
    coverage_limit: Decimal
    capped_amount: Decimal
    depreciation_rate: Decimal
    depreciation_amount: Decimal
    payout: Decimal
    currency: Literal["INR"] = "INR"
    explanation: str


class BaselineResult(StrictModel):
    system: Literal["DETERMINISTIC_BASELINE"] = "DETERMINISTIC_BASELINE"
    run_id: str
    claim_id: str
    status: ExecutionStatus
    decision: Decision | None = None
    decision_reasons: list[str] = Field(default_factory=list)
    checks: list[RuleCheck] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    settlement: Settlement | None = None
    requires_human_approval: bool = True
    guardrail_events: list[GuardrailEvent] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime
    execution_ms: float = Field(ge=0)
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _decision_consistency(self) -> "BaselineResult":
        if self.status == ExecutionStatus.COMPLETED and self.decision is None:
            raise ValueError("a completed baseline run must carry a decision")
        if self.status == ExecutionStatus.FAILED and self.decision is not None:
            raise ValueError("a failed baseline run must not carry a decision")
        if self.settlement is not None and self.decision != Decision.APPROVE:
            raise ValueError("a settlement may only accompany an APPROVE decision")
        return self


# --------------------------------------------------------------------------- LLM agent outputs

class AgentFinding(StrictModel):
    category: Literal[
        "SERIAL_INCONSISTENCY", "SUSPICIOUS_DETAIL", "DATE_CONFLICT", "CLAIM_HISTORY_PATTERN",
        "EVIDENCE_GAP", "POLICY_ELIGIBILITY", "OTHER",
    ]
    severity: Severity
    description: str = Field(max_length=1000)
    citations: list[Citation] = Field(default_factory=list, max_length=10)
    uncertainty: str | None = Field(default=None, max_length=500)


class ToolCitation(StrictModel):
    """A reference to a record returned by an MCP tool during this run (checked after the run)."""
    source: str = Field(max_length=200, description="Exact source string from a tool result, e.g. "
                        "policies.json#/POL-LAPTOP-STD-02/exclusions/0 or "
                        "sqlite:claimsleuth.db#claim_history/claim_id=HST-2025-1102")
    value: str = Field(max_length=300, description="The value found at that source")


class ExtractedFact(StrictModel):
    field: str = Field(max_length=60, description="e.g. purchase_date, serial_number, repair_cost, diagnosis")
    value: str = Field(max_length=300)
    citation: Citation
    status: Literal["VERIFIED", "UNCERTAIN", "CONFLICTING"] = Field(
        description="VERIFIED only when the value is stated verbatim in the cited document line")
    note: str | None = Field(default=None, max_length=300)


class EvidenceReport(StrictModel):
    """Structured output of the Google ADK Evidence Agent."""
    claim_id: str
    facts: list[ExtractedFact] = Field(max_length=30)
    findings: list[AgentFinding] = Field(max_length=20)
    evidence_citations: list[Citation] = Field(max_length=30)
    policy_references: list[ToolCitation] = Field(max_length=20)
    missing_information: list[str] = Field(max_length=20)
    confidence: float = Field(ge=0, le=1)
    recommended_decision: Decision
    reasoning_summary: str = Field(max_length=2000)


class InvestigationReport(StrictModel):
    """Structured output of the Google ADK Investigator Agent (the ADK system's final report)."""
    claim_id: str
    findings: list[AgentFinding] = Field(max_length=20)
    evidence_citations: list[Citation] = Field(max_length=30)
    policy_references: list[ToolCitation] = Field(default_factory=list, max_length=20)
    history_references: list[ToolCitation] = Field(default_factory=list, max_length=20)
    policy_eligibility: Literal["ELIGIBLE", "NOT_ELIGIBLE", "UNDETERMINED"]
    policy_eligibility_reason: str = Field(max_length=1000)
    missing_information: list[str] = Field(max_length=20)
    confidence: float = Field(ge=0, le=1)
    recommended_decision: Decision
    reasoning_summary: str = Field(max_length=2000)


class ConsistencyCheck(StrictModel):
    """One forensic comparison, e.g. invoice serial vs repair-report serial, or purchase vs incident date."""
    check: str = Field(max_length=120)
    result: Literal["CONSISTENT", "INCONSISTENT", "NOT_EVALUABLE"]
    detail: str = Field(max_length=500)
    citations: list[Citation] = Field(default_factory=list, max_length=6)


class HistoryPatternFinding(StrictModel):
    pattern: Literal["SAME_SERIAL_OTHER_CLAIMANT", "REPEAT_CLAIMS", "PRIOR_CLAIM_SAME_CLAIMANT",
                     "NO_PRIOR_CLAIMS", "NOT_CHECKED"]
    detail: str = Field(max_length=500)
    references: list[ToolCitation] = Field(default_factory=list, max_length=10)


class ForensicsAnalysis(StrictModel):
    """What the CrewAI Forensic Agent's task must return (Task.output_pydantic). Written by the model."""
    claim_id: str
    findings: list[AgentFinding] = Field(max_length=20)
    evidence_references: list[Citation] = Field(max_length=30)
    serial_number_checks: list[ConsistencyCheck] = Field(max_length=10)
    chronology_checks: list[ConsistencyCheck] = Field(max_length=10)
    historical_pattern_findings: list[HistoryPatternFinding] = Field(max_length=10)
    missing_information: list[str] = Field(max_length=20)
    uncertainty: list[str] = Field(max_length=10)
    confidence: float = Field(ge=0, le=1)
    recommended_outcome: Decision
    reasoning_summary: str = Field(max_length=2000)


class ForensicsReport(ForensicsAnalysis):
    """The validated CrewAI result. model/provider/execution_status are set by code, never by the model.
    It recommends an outcome only: it has no payout or approval field and cannot authorise anything."""
    model: str                         # "test-double" for offline runs
    provider: str                      # e.g. "offline-test-double"
    execution_status: ExecutionStatus


class TokenUsage(StrictModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost_usd: float | None = None


class AgentRunRecord(StrictModel):
    """One agent's execution inside a run, filled from framework callbacks."""
    agent: str
    framework: Literal["adk", "crewai"]
    started_at: datetime
    finished_at: datetime | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    llm_calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tool_calls: int = 0
    errors: list[str] = Field(default_factory=list)
    output_summary: str | None = Field(default=None, max_length=500)


class AgentToolCallRecord(StrictModel):
    """One tool call made by an agent, recorded by framework callbacks (not by the tool itself)."""
    run_id: str
    agent: str
    tool: str
    protocol: Literal["mcp", "internal"]
    arguments: dict[str, Any]
    status: Literal["ok", "error"]
    result: Any = None
    error: str | None = None
    started_at: datetime
    latency_ms: float = Field(ge=0)


class AgentSystemResult(StrictModel):
    """Envelope around one LLM system's output, with its own execution record."""
    system: Literal["GOOGLE_ADK", "CREWAI"]
    run_id: str
    claim_id: str
    status: ExecutionStatus
    report: InvestigationReport | ForensicsReport | None = None
    evidence_report: EvidenceReport | None = None   # ADK only: the Evidence Agent's intermediate report
    model: str | None = None
    prompt_variant: str | None = None
    session_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    execution_ms: float | None = Field(default=None, ge=0)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    agent_runs: list[AgentRunRecord] = Field(default_factory=list)
    tool_calls: list[AgentToolCallRecord] = Field(default_factory=list)
    unsupported_references: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _report_matches_status(self) -> "AgentSystemResult":
        if self.status == ExecutionStatus.COMPLETED and self.report is None:
            raise ValueError("a completed agent run must carry a report")
        if self.status != ExecutionStatus.COMPLETED and self.report is not None:
            raise ValueError("only a completed agent run may carry a report")
        return self


# --------------------------------------------------------------------------- MCP tool results

# Field types shared by the MCP tool signatures (advertised in the tool input schema) and the input models.
OptionalId = Annotated[str | None, Field(pattern=ID_PATTERN, description="Upper-case identifier: letters, digits and hyphens")]
SearchText = Annotated[str | None, Field(min_length=2, max_length=300, description="Free-text keywords")]
SerialText = Annotated[str | None, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{2,39}$", description="Device serial number")]


class PolicySearchInput(StrictModel):
    policy_id: OptionalId = None
    search_query: SearchText = None
    limit: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def _needs_a_filter(self) -> "PolicySearchInput":
        if self.policy_id is None and self.search_query is None:
            raise ValueError("provide policy_id, search_query, or both")
        return self


class PolicyMatchedTerm(StrictModel):
    field: str
    value: str
    source: str                       # JSON pointer into policies.json, e.g. policies.json#/POL-X/exclusions/0


class PolicyMatch(StrictModel):
    policy_id: str
    name: str
    product_category: str
    terms: PolicyTerms
    matched_terms: list[PolicyMatchedTerm]
    score: float | None               # share of query words matched; None when no query was given
    source: str


class PolicySearchOutput(StrictModel):
    policy_id: str | None
    search_query: str | None
    matches: list[PolicyMatch]
    total_matches: int = Field(ge=0)
    sources: list[str]


class ClaimHistoryLookupInput(StrictModel):
    claim_id: OptionalId = None
    claimant_id: OptionalId = None
    serial_number: SerialText = None
    limit: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def _needs_a_filter(self) -> "ClaimHistoryLookupInput":
        if not (self.claim_id or self.claimant_id or self.serial_number):
            raise ValueError("provide claim_id, claimant_id or serial_number")
        return self


class ClaimHistoryLookupOutput(StrictModel):
    claim_id: str | None
    claimant_id: str | None
    serial_number: str | None
    records: list[HistoryRecord]
    returned: int = Field(ge=0)
    total_matches: int = Field(ge=0)
    truncated: bool
    source: str
    record_sources: list[str]


class HistoryLookupOutput(StrictModel):
    serial_number: str
    claimant_id: str | None
    records: list[HistoryRecord]
    same_serial_count: int = Field(ge=0)
    other_claimant_count: int = Field(ge=0)
    source: str


class MCPToolInfo(StrictModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class MCPToolCall(StrictModel):
    """One tool call as recorded by the MCP server itself (its tool-call log)."""
    tool: str
    arguments: dict[str, Any]
    status: Literal["ok", "error"]
    result: dict[str, Any] | None = None       # the server logs a result summary (match counts), not full data
    error: str | None = None
    started_at: datetime
    latency_ms: float = Field(ge=0)
    run_id: str | None = None
    claim_id: str | None = None
    caller: str | None = None                  # e.g. "adk:investigator_agent", "crewai:forensic_agent"
    call_id: str | None = None


class MCPResult(StrictModel):
    """Raw tool results. MCP is a tool protocol, so this carries no decision."""
    system: Literal["MCP_TOOLS"] = "MCP_TOOLS"
    run_id: str
    claim_id: str
    status: ExecutionStatus
    server_url: str | None = None
    transport: str | None = None
    discovered_tools: list[MCPToolInfo] = Field(default_factory=list)
    calls: list[MCPToolCall] = Field(default_factory=list)
    execution_ms: float | None = Field(default=None, ge=0)
    errors: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- comparison and final report

class ComparisonRow(StrictModel):
    system: Literal["DETERMINISTIC_BASELINE", "GOOGLE_ADK", "CREWAI", "MCP_TOOLS"]
    status: ExecutionStatus
    decision: Decision | None
    decision_label: str               # e.g. "APPROVE" or "Tool results, not a decision"
    confidence: float | None
    confidence_label: str             # e.g. "0.82", "deterministic", "N/A"
    execution_ms: float | None
    evidence_count: int = Field(ge=0)
    evidence_summary: str


class FinalComparison(StrictModel):
    run_id: str
    claim_id: str
    rows: list[ComparisonRow]
    decisions_agree: bool | None      # None when fewer than two decisions are available


class FinalClaimReport(StrictModel):
    """Produced by the controlled adjudication function; never by an LLM."""
    run_id: str
    claim_id: str
    final_decision: Decision
    decided_by: Literal["DETERMINISTIC_RULES", "CONFLICT_ESCALATION", "GUARDRAIL_ESCALATION", "BASELINE_UNAVAILABLE",
                        "INSUFFICIENT_REVIEW", "INTEGRITY_ESCALATION"]
    deterministic_decision: Decision | None = None   # what the rules alone decide, recomputed by the adjudicator
    payout: Settlement | None
    rule_checks: list[RuleCheck] = Field(default_factory=list)
    evidence_references: list[EvidenceRef] = Field(default_factory=list)
    policy_references: list[str] = Field(default_factory=list)
    agreements: list[str]
    disagreements: list[str]
    unsupported_claims: list[str]
    rationale: list[str]
    requires_human_approval: bool
    approval_status: Literal["NOT_REQUIRED", "PENDING", "APPROVED", "REJECTED"]
    comparison: FinalComparison
    generated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _payout_only_on_approve(self) -> "FinalClaimReport":
        if self.payout is not None and self.final_decision != Decision.APPROVE:
            raise ValueError("a payout may only accompany a final APPROVE")
        if self.final_decision in (Decision.REJECT, Decision.ESCALATE) and not self.requires_human_approval:
            raise ValueError("REJECT and ESCALATE always need human approval")
        return self


# --------------------------------------------------------------------------- ADK <-> CrewAI communication contract

class ExecutionMetadata(StrictModel):
    framework: Literal["deterministic", "adk", "crewai", "mcp", "orchestrator"]
    model: str | None = None                   # "test-double" for offline runs
    provider: str | None = None
    llm_mode: Literal["none", "test-double", "live"] = "none"
    protocol: str | None = None                # e.g. "A2A 1.0 JSON-RPC (a2a-sdk 1.3.0)"


class DocumentDigest(StrictModel):
    doc_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ForensicRequestPayload(StrictModel):
    kind: Literal["forensic_request"] = "forensic_request"
    claim_id: str = Field(pattern=ID_PATTERN)
    documents: list[DocumentDigest]            # the receiver must hold exactly this evidence


class ForensicResponsePayload(StrictModel):
    kind: Literal["forensic_response"] = "forensic_response"
    result: AgentSystemResult


class ErrorPayload(StrictModel):
    kind: Literal["error"] = "error"
    code: Literal["INVALID_MESSAGE", "CLAIM_MISMATCH", "RUN_MISMATCH", "EVIDENCE_MISMATCH", "EXECUTION_FAILED"]
    detail: str = Field(max_length=2000)


MessageType = Literal["FORENSIC_REQUEST", "FORENSIC_RESPONSE", "ERROR"]
_PAYLOAD_FOR = {"FORENSIC_REQUEST": "forensic_request", "FORENSIC_RESPONSE": "forensic_response", "ERROR": "error"}


class WorkflowMessage(StrictModel):
    """Envelope carried as the JSON body of an A2A message between the ADK side and the CrewAI service."""
    message_id: str = Field(min_length=8, max_length=64)
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
    claim_id: str = Field(pattern=ID_PATTERN)
    sender: Literal["adk_orchestrator", "crewai_forensic_service"]
    recipient: Literal["adk_orchestrator", "crewai_forensic_service"]
    message_type: MessageType
    timestamp: datetime
    payload: Annotated[ForensicRequestPayload | ForensicResponsePayload | ErrorPayload, Field(discriminator="kind")]
    metadata: ExecutionMetadata

    @model_validator(mode="after")
    def _consistent(self) -> "WorkflowMessage":
        if self.sender == self.recipient:
            raise ValueError("sender and recipient must differ")
        if self.payload.kind != _PAYLOAD_FOR[self.message_type]:
            raise ValueError(f"{self.message_type} message cannot carry a {self.payload.kind} payload")
        if isinstance(self.payload, ForensicRequestPayload) and self.payload.claim_id != self.claim_id:
            raise ValueError("payload claim_id differs from the message claim_id")
        if isinstance(self.payload, ForensicResponsePayload):
            r = self.payload.result
            if r.system != "CREWAI":
                raise ValueError("a forensic response must carry a CREWAI result")
            if (r.claim_id, r.run_id) != (self.claim_id, self.run_id):
                raise ValueError("result claim_id/run_id differ from the message")
        return self


class AuditEvent(StrictModel):
    ts: datetime = Field(default_factory=utc_now)
    run_id: str
    claim_id: str
    event: Literal["run_started", "workflow_started", "workflow_finished", "message_sent", "message_received",
                   "mcp_records_fetched", "validation_failure", "adjudication", "run_finished"]
    actor: str
    detail: str
    data: dict[str, Any] = Field(default_factory=dict)


class ClaimRunBundle(StrictModel):
    """Everything one integrated run produced; each workflow's output is kept as it was returned."""
    run_id: str
    claim_id: str
    baseline: BaselineResult | None
    adk: AgentSystemResult | None
    crewai: AgentSystemResult | None
    mcp: MCPResult | None
    messages: list[WorkflowMessage]
    final_report: FinalClaimReport | None
    audit: list[AuditEvent]
    workflow_metadata: dict[str, ExecutionMetadata]


# --------------------------------------------------------------------------- blackboard and events

class BlackboardEvent(StrictModel):
    ts: datetime = Field(default_factory=utc_now)
    run_id: str
    key: str
    author: str
    operation: Literal["write", "status"]
    summary: str


class AgentMessage(StrictModel):
    ts: datetime = Field(default_factory=utc_now)
    run_id: str
    mode: Literal["blackboard", "peer_to_peer", "broadcast"]
    sender: str
    recipients: list[str]
    topic: str
    payload_summary: str


class BlackboardState(StrictModel):
    run_id: str
    claim: ClaimInput
    evidence: list[EvidenceDocument] = Field(default_factory=list)
    policy_references: list[PolicyReference] = Field(default_factory=list)
    history_results: list[HistoryRecord] = Field(default_factory=list)
    baseline_output: BaselineResult | None = None
    adk_output: AgentSystemResult | None = None
    crewai_output: AgentSystemResult | None = None
    mcp_results: MCPResult | None = None
    final_comparison: FinalComparison | None = None
    final_decision: FinalClaimReport | None = None
    execution_status: dict[str, ExecutionStatus] = Field(default_factory=dict)
    events: list[BlackboardEvent] = Field(default_factory=list)
    messages: list[AgentMessage] = Field(default_factory=list)


class RunEvent(StrictModel):
    """One observability row per callback event (agents, tools, A2A, MCP)."""
    ts: datetime = Field(default_factory=utc_now)
    run_id: str
    claim_id: str
    framework: Literal["baseline", "adk", "crewai", "mcp", "a2a", "orchestrator"]
    agent: str
    phase: str                        # before_agent / after_tool / step / task ...
    architecture: str | None = None
    communication_mode: str | None = None
    tool: str | None = None
    latency_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_cost_usd: float | None = None
    status: Literal["ok", "error", "blocked"] = "ok"
    error: str | None = None
    guardrail: str | None = None
    output_summary: str | None = Field(default=None, max_length=500)
