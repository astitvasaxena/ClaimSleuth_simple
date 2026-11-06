"""TEST HELPER, run as its own process by test_reliability.py: starts a run on a disposable blackboard, takes it to one
stage, then dies with os._exit, so nothing is flushed, closed or cleaned up. No agent and no model are involved.

    python crash_worker.py <runtime dir> <run id> <stage>
"""
import hashlib
import os
import sys
from pathlib import Path

from claimsleuth.blackboard import AgentParticipant, RunController
from claimsleuth.blackboard.store import SEAL_SECTION
from claimsleuth.dashboard.jobs import Job, write_meta
from claimsleuth.schemas import DocumentKind, EvidenceDocument, EvidenceReport

CLAIM = "CS-CRASH"
TEXT = "Purchase Date: 2025-02-10\n"
DOCUMENT = EvidenceDocument(doc_id="invoice", kind=DocumentKind.INVOICE, filename="invoice.txt", text=TEXT,
                            sha256=hashlib.sha256(TEXT.encode()).hexdigest())


def report() -> EvidenceReport:
    return EvidenceReport.model_validate({
        "claim_id": CLAIM, "confidence": 0.9, "recommended_decision": "APPROVE", "reasoning_summary": "a summary",
        "facts": [], "findings": [], "evidence_citations": [], "policy_references": [], "missing_information": []})


def main(runtime: Path, run_id: str, stage: str) -> None:
    write_meta(runtime, Job(run_id=run_id, claim_id=CLAIM, source={"kind": "test", "label": "crash worker"}, crew_mode=None,
                            a2a_timeout_s=None))
    if stage == "started":
        os._exit(9)
    board = RunController(run_id, CLAIM)
    board.register([DOCUMENT])
    adk = AgentParticipant(run_id, CLAIM, "adk", ("evidence_agent", "investigator_agent"))
    adk.running([DOCUMENT], mark_running=False)
    adk.agent_started("evidence_agent")
    if stage == "agents_running":
        os._exit(9)
    assert adk.publish_report("evidence_agent", report()) is not None
    if stage == "report_published":
        os._exit(9)
    assert board.guarded("seal", lambda b: b.monitor(run_id, CLAIM, "controller", "orchestrator", SEAL_SECTION,
                                                     {"reason": "test"}, writer=board.writer())) is not None
    if stage == "sealed":
        os._exit(9)
    assert board.guarded("decide", lambda b: b.post(run_id, CLAIM, "adjudicator", "deterministic", "decision:final",
                                                    {"final_decision": "APPROVE", "decided_by": "DETERMINISTIC_RULES",
                                                     "payout": "100.00"}, board.fingerprint, writer=board.writer())) is not None
    os._exit(9)                         # stage "decided": after the decision, before the report is stored


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
