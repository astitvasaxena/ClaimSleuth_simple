"""The blackboard as the shared context between agents (Phase 1: genuine inter-agent context sharing).

  Evidence Agent -> report:evidence_agent on the blackboard -> consumed by the Investigator Agent (ADK) and by the
  Forensic Agent (CrewAI, through a ContextRef in the A2A request) -> both reports published -> seal -> the
  deterministic adjudicator consumes only reports it verified on the blackboard.

Everything is real except the language models, which are the labelled offline test doubles (no Gemini request):
the SQLite blackboard and its keys, ADK Runner/sessions/plugins, the MCP server process, the CrewAI A2A service
process in .venv-crew, and the A2A protocol. The CrewAI-side model input is tested in
tests/crew/test_blackboard_context_crew.py.
"""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from adk_test_double import ScriptedLlm, call
from claimsleuth import contract, gemini_rate
from claimsleuth.adk_service.runner import check_evidence, run_adk_investigation_async
from claimsleuth.blackboard import (ENV_VAR, AgentParticipant, Blackboard, BlackboardRejected, ContextRef,
                                    RunController, context_block, context_facts, document_fingerprint, load_writer,
                                    run_monitor)
from claimsleuth.blackboard.adk_plugin import BlackboardLifecyclePlugin
from claimsleuth.blackboard.controller import SECTION_SCHEMAS, report_sha256
from claimsleuth.blackboard.models import RECEIPT_PREFIX, payload_hash
from claimsleuth.blackboard.store import SEAL_SECTION
from claimsleuth.claim_text import shared_context
from claimsleuth.datastore import load_claim
from claimsleuth.integration.run import run_claim
from claimsleuth.schemas import (AgentSystemResult, EvidenceReport, ExecutionStatus, ForensicsReport,
                                 InvestigationReport)
from test_adk_investigation import CLAIM as ADK_CLAIM
from test_adk_investigation import evidence_report, happy_models, investigation_report, models
from test_integration_e2e import CREW_PYTHON, CrewService
from test_mcp_server import RunningServer, _free_port

ROOT = Path(__file__).resolve().parents[1]
CLAIM_ID, RUN = "CS-001", "RUN-ctx-1"
DOCS = [SimpleNamespace(doc_id="invoice", sha256="1" * 64), SimpleNamespace(doc_id="repair_report", sha256="2" * 64)]
FP = document_fingerprint(DOCS)


def evidence(claim_id=CLAIM_ID, **over) -> EvidenceReport:
    return EvidenceReport.model_validate({
        "claim_id": claim_id, "facts": [], "findings": [], "evidence_citations": [], "policy_references": [],
        "missing_information": [], "confidence": 0.8, "recommended_decision": "APPROVE",
        "reasoning_summary": "Evidence report shared through the blackboard.", **over})


def investigation(**over) -> InvestigationReport:
    return InvestigationReport.model_validate({
        "claim_id": CLAIM_ID, "findings": [], "evidence_citations": [], "policy_eligibility": "ELIGIBLE",
        "policy_eligibility_reason": "Inside the coverage window.", "missing_information": [], "confidence": 0.7,
        "recommended_decision": "APPROVE", "reasoning_summary": "Investigator report.", **over})


def forensics(**over) -> ForensicsReport:
    return ForensicsReport.model_validate({
        "claim_id": CLAIM_ID, "findings": [], "evidence_references": [], "serial_number_checks": [],
        "chronology_checks": [], "historical_pattern_findings": [], "missing_information": [], "uncertainty": [],
        "confidence": 0.6, "recommended_outcome": "APPROVE", "reasoning_summary": "Forensic report.",
        "model": "test-double", "provider": "offline-test-double", "execution_status": "COMPLETED", **over})


def setup_run(db: Path, run_id: str = RUN, claim_id: str = CLAIM_ID, publish: bool = True) -> SimpleNamespace:
    """A registered run whose Evidence Agent published its report during execution (as the ADK plugin does)."""
    ctl = RunController(run_id, claim_id, path=db)
    ctl.register(DOCS)
    adk = AgentParticipant(run_id, claim_id, "adk", ("evidence_agent", "investigator_agent"), path=db)
    crew = AgentParticipant(run_id, claim_id, "crewai", ("forensic_agent",), path=db)
    adk.running(DOCS, mark_running=False)
    crew.running(DOCS)
    adk.agent_started("evidence_agent")
    ref = adk.publish_report("evidence_agent", evidence(claim_id)) if publish else None
    return SimpleNamespace(db=db, ctl=ctl, adk=adk, crew=crew, ref=ref, board=Blackboard(db), run_id=run_id,
                           claim_id=claim_id)


def entries(s) -> list:
    return s.board.entries(s.run_id, s.claim_id)


def receipts(s) -> list:
    return [e for e in entries(s) if e.kind == "monitor" and e.section.startswith(RECEIPT_PREFIX)]


def seal(s) -> None:
    s.board.monitor(s.run_id, s.claim_id, "controller", "orchestrator", SEAL_SECTION, {"reason": "test"},
                    writer=load_writer(s.db, "orchestrator"))


@pytest.fixture
def s(tmp_path):
    return setup_run(tmp_path / "bb.sqlite3")


# =========================================================================== publication and consumption API

def test_evidence_report_is_published_with_a_versioned_reference(s):
    post = next(e for e in entries(s) if e.kind == "post")
    assert s.ref == ContextRef(run_id=RUN, claim_id=CLAIM_ID, section="report:evidence_agent", run_seq=post.run_seq,
                               sha256=post.payload_sha256, fingerprint=FP)
    assert (post.author, post.principal, post.auth, post.payload["published"]) == (
        "evidence_agent", "adk", "valid", "during execution")
    assert post.payload["report_sha256"] == report_sha256(evidence())
    status = s.board.state(RUN, CLAIM_ID).agents["evidence_agent"]
    assert status.state == "COMPLETED"
    assert s.adk.published == {"evidence_agent": s.ref}


def test_consumers_receive_the_verified_report_and_leave_signed_receipts(s):
    assert s.adk.consume_context("investigator_agent", s.ref) == evidence()
    assert s.crew.consume_context("forensic_agent", s.ref) == evidence()
    post = next(e for e in entries(s) if e.kind == "post")
    got = receipts(s)
    assert [(r.author, r.principal, r.auth, r.ref_seq) for r in got] == [
        ("investigator_agent", "adk", "valid", post.seq), ("forensic_agent", "crewai", "valid", post.seq)]
    assert all(r.payload["source_sha256"] == s.ref.sha256 and r.payload["source_run_seq"] == s.ref.run_seq and
               r.payload["source_principal"] == "adk" and r.fingerprint == FP for r in got)
    assert [c.receipt_run_seq for c in s.adk.consumed + s.crew.consumed] == [r.run_seq for r in got]
    assert s.board.state(RUN, CLAIM_ID).authentication == {"valid": len(entries(s))}


def test_cross_run_and_cross_claim_references_are_rejected(s):
    other = setup_run(s.db, run_id="RUN-ctx-2", publish=False)     # same database, same documents, another run
    before = len(entries(s)) + len(entries(other))
    assert other.adk.consume_context("investigator_agent", s.ref) is None
    assert "belongs to run RUN-ctx-1" in other.adk.last_failure
    w = load_writer(s.db, "adk")
    with pytest.raises(BlackboardRejected):          # the same version number in the other run is not this report
        s.board.consume("RUN-ctx-2", CLAIM_ID, FP, "investigator_agent", s.ref.section, s.ref.run_seq, s.ref.sha256,
                        writer=w)
    with pytest.raises(BlackboardRejected, match="belongs to claim"):
        s.board.consume(RUN, "CS-002", FP, "investigator_agent", s.ref.section, s.ref.run_seq, s.ref.sha256, writer=w)
    with pytest.raises(BlackboardRejected, match="fingerprint"):
        s.board.consume(RUN, CLAIM_ID, "f" * 64, "investigator_agent", s.ref.section, s.ref.run_seq, s.ref.sha256,
                        writer=w)
    assert len(entries(s)) + len(entries(other)) == before and receipts(s) == []        # nothing recorded


def test_unauthorised_section_reads_are_rejected(s):
    s.adk.agent_started("investigator_agent")
    inv_ref = s.adk.publish_report("investigator_agent", investigation())
    assert inv_ref is not None
    assert s.crew.consume_context("forensic_agent", inv_ref) is None           # Forensic never reads the Investigator
    assert "not authorised to read report:investigator_agent" in s.crew.last_failure
    crew, adk = load_writer(s.db, "crewai"), load_writer(s.db, "adk")
    with pytest.raises(BlackboardRejected, match="not authorised"):
        s.board.consume(RUN, CLAIM_ID, FP, "forensic_agent", inv_ref.section, inv_ref.run_seq, inv_ref.sha256, writer=crew)
    with pytest.raises(BlackboardRejected, match="may not write as"):         # identity: crewai cannot act as ADK
        s.board.consume(RUN, CLAIM_ID, FP, "investigator_agent", s.ref.section, s.ref.run_seq, s.ref.sha256, writer=crew)
    with pytest.raises(BlackboardRejected, match="may not write as"):
        s.board.consume(RUN, CLAIM_ID, FP, "forensic_agent", s.ref.section, s.ref.run_seq, s.ref.sha256, writer=adk)
    with pytest.raises(BlackboardRejected, match="not authorised"):           # no agent reads the Forensic report
        s.board.consume(RUN, CLAIM_ID, FP, "investigator_agent", "report:forensic_agent", 1, "0" * 64, writer=adk)
    assert s.adk.consume_context("forensic_agent", s.ref) is None             # not this participant's agent
    assert receipts(s) == []
    # The A2A contract refuses the same request before anything runs.
    request = contract.forensic_request(RUN, CLAIM_ID, DOCS, [inv_ref])
    with pytest.raises(contract.ContractError) as exc:
        contract.check_request(request, DOCS)
    assert exc.value.code == "CONTEXT_REJECTED"


def test_invalid_reports_are_never_published(tmp_path):
    s = setup_run(tmp_path / "bb.sqlite3", publish=False)
    assert s.adk.publish_report("evidence_agent", evidence(claim_id="CS-999")) is None          # wrong claim
    assert s.adk.publish_report("evidence_agent", investigation()) is None                     # wrong schema
    assert [e for e in entries(s) if e.kind == "post"] == []
    # The ADK validator refuses invalid output before publication: schema and claim id ...
    recorder = SimpleNamespace(tool_calls=[], mcp_sources=lambda: set(), record_output_failure=lambda agent, error: None)
    report, problems = check_evidence({"claim_id": CLAIM_ID, "facts": "not a list"}, CLAIM_ID, [], recorder, False)
    assert report is None and "failed validation" in problems[0]
    report, problems = check_evidence(evidence(claim_id="CS-999").model_dump(), CLAIM_ID, [], recorder, False)
    assert report is None and "expected 'CS-001'" in problems[0]
    # (citations and MCP references: test_unsupported_evidence_is_never_published_and_stops_the_investigator)


def _raw_insert(db: Path, like, payload: dict, signature) -> tuple[int, str]:
    text = json.dumps(payload, sort_keys=True)
    with sqlite3.connect(db) as conn:
        run_seq = conn.execute("SELECT MAX(run_seq) + 1 FROM entries WHERE run_id = ?", (like.run_id,)).fetchone()[0]
        conn.execute("INSERT INTO entries (run_id, run_seq, claim_id, ts, kind, section, author, framework, process, "
                     "fingerprint, payload, payload_sha256, ref_seq, principal, signature) "
                     "VALUES (?, ?, ?, ?, 'post', ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                     (like.run_id, run_seq, like.claim_id, like.ts.isoformat(), like.section, like.author,
                      like.framework, like.process.model_dump_json(), like.fingerprint, text, payload_hash(text),
                      like.principal, signature))
    return run_seq, payload_hash(text)


def test_forged_and_unsigned_context_is_rejected(s):
    post = next(e for e in entries(s) if e.kind == "post")
    forged = {**post.payload, "report": {**post.payload["report"], "recommended_decision": "REJECT"}}
    seq, sha = _raw_insert(s.db, post, forged, post.signature)            # raw SQL, old signature: does not verify
    ref = s.ref.model_copy(update={"run_seq": seq, "sha256": sha})
    assert s.adk.consume_context("investigator_agent", ref) is None
    assert "failed authentication (invalid)" in s.adk.last_failure
    seq, sha = _raw_insert(s.db, post, {**forged, "x": 1}, None)            # no signature at all
    assert s.adk.consume_context("investigator_agent", s.ref.model_copy(update={"run_seq": seq, "sha256": sha})) is None
    assert "failed authentication (unsigned)" in s.adk.last_failure
    # The forged rows also make the genuine report no longer the current one: fail closed, nothing is consumed.
    assert s.adk.consume_context("investigator_agent", s.ref) is None and "stale" in s.adk.last_failure
    assert receipts(s) == []


def test_hash_mismatches_are_rejected(s):
    assert s.adk.consume_context("investigator_agent", s.ref.model_copy(update={"sha256": "0" * 64})) is None
    assert "not the expected" in s.adk.last_failure
    with pytest.raises(BlackboardRejected, match="is not a post of report:evidence_agent"):   # wrong version number
        s.board.consume(RUN, CLAIM_ID, FP, "investigator_agent", s.ref.section, 1, s.ref.sha256,
                        writer=load_writer(s.db, "adk"))
    assert receipts(s) == []


def test_retracted_context_is_rejected(s):
    s.board.retract(RUN, CLAIM_ID, s.ref.run_seq, "controller", "orchestrator", "test", writer=load_writer(s.db, "orchestrator"))
    assert s.adk.consume_context("investigator_agent", s.ref) is None
    assert "was retracted" in s.adk.last_failure and receipts(s) == []


def test_stale_and_unfinished_context_is_rejected(s):
    w = load_writer(s.db, "adk")
    s.board.retract(RUN, CLAIM_ID, s.ref.run_seq, "controller", "orchestrator", "superseded", writer=load_writer(s.db, "orchestrator"))
    newer = s.board.post(RUN, CLAIM_ID, "evidence_agent", "adk", "report:evidence_agent",
                         {"report": evidence(confidence=0.9).model_dump(mode="json"),
                          "report_sha256": report_sha256(evidence(confidence=0.9))}, FP, writer=w)
    assert s.adk.consume_context("investigator_agent", s.ref) is None                       # the old version
    new_ref = s.ref.model_copy(update={"run_seq": newer.run_seq, "sha256": newer.payload_sha256})
    assert s.adk.consume_context("investigator_agent", new_ref) == evidence(confidence=0.9)   # the current one
    # A report whose producer has not finished (no COMPLETED status) is not final context.
    t = setup_run(s.db, run_id="RUN-ctx-3", publish=False)
    post = t.board.post("RUN-ctx-3", CLAIM_ID, "evidence_agent", "adk", "report:evidence_agent",
                        {"report": evidence().model_dump(mode="json"), "report_sha256": report_sha256(evidence())},
                        FP, writer=w)
    ref = ContextRef(run_id="RUN-ctx-3", claim_id=CLAIM_ID, section="report:evidence_agent", run_seq=post.run_seq,
                     sha256=post.payload_sha256, fingerprint=FP)
    assert t.adk.consume_context("investigator_agent", ref) is None
    assert "evidence_agent is RUNNING, so its report is not final" in t.adk.last_failure


def test_late_context_is_rejected(s):
    s.adk.agent_started("investigator_agent")
    seal(s)
    assert s.adk.publish_report("investigator_agent", investigation()) is None          # written, flagged late
    late = next(e for e in entries(s) if e.section == "report:investigator_agent")
    assert late.late
    with pytest.raises(BlackboardRejected, match="arrived after the seal"):
        s.board.consume(RUN, CLAIM_ID, FP, "adjudicator", late.section, late.run_seq, late.payload_sha256,
                        writer=load_writer(s.db, "orchestrator"))


def test_context_cannot_be_consumed_after_sealing(s):
    with pytest.raises(BlackboardRejected, match="not sealed"):          # the adjudicator reads only sealed inputs
        s.board.consume(RUN, CLAIM_ID, FP, "adjudicator", s.ref.section, s.ref.run_seq, s.ref.sha256,
                        writer=load_writer(s.db, "orchestrator"))
    seal(s)
    assert s.adk.consume_context("investigator_agent", s.ref) is None
    assert s.crew.consume_context("forensic_agent", s.ref) is None
    assert all("is sealed" in f for f in (s.adk.last_failure, s.crew.last_failure))
    assert receipts(s) == []


CONTEXT_WORKER = r"""
import json, sys, time
from pathlib import Path
from claimsleuth.blackboard import Blackboard, BlackboardRejected, load_writer
db, role, start, n, ref, extra = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]), json.loads(sys.argv[5]), sys.argv[6]
principal, consumer = {"investigator": ("adk", "investigator_agent"), "forensic": ("crewai", "forensic_agent"),
                       "publisher": ("adk", "investigator_agent")}[role]
board, writer = Blackboard(db), load_writer(db, principal)
while not start.exists():
    time.sleep(0.01)
if role == "publisher":
    try:
        entry = board.post(ref["run_id"], ref["claim_id"], "investigator_agent", "adk", "report:investigator_agent",
                           json.loads(extra), ref["fingerprint"], writer=writer)
        print("posted", entry.run_seq)
    except BlackboardRejected as exc:
        print("rejected", exc)
else:
    for _ in range(n):
        board.consume(ref["run_id"], ref["claim_id"], ref["fingerprint"], consumer, ref["section"], ref["run_seq"],
                      ref["sha256"], writer=writer)
    print("consumed", n)
"""


def test_concurrent_publication_and_consumption_are_safe(s, tmp_path):
    start, n = tmp_path / "go", 15
    report = investigation()
    same = json.dumps({"report": report.model_dump(mode="json"), "report_sha256": report_sha256(report)})
    other = json.dumps({"report": investigation(confidence=0.1).model_dump(mode="json"),
                        "report_sha256": report_sha256(investigation(confidence=0.1))})
    jobs = [(sys.executable, "investigator", "")] * 3 + [(sys.executable, "publisher", same)] * 2 + [
        (sys.executable, "publisher", other)]
    if CREW_PYTHON.exists():
        jobs += [(str(CREW_PYTHON), "forensic", "")] * 2
    ref = s.ref.model_dump_json()
    procs = [subprocess.Popen([py, "-c", CONTEXT_WORKER, str(s.db), role, str(start), str(n), ref, extra], cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for py, role, extra in jobs]
    time.sleep(1.5)
    start.touch()
    outputs = [p.communicate(timeout=180)[0] for p in procs]
    assert all(p.returncode == 0 for p in procs), outputs
    all_entries = entries(s)
    assert [e.run_seq for e in all_entries] == list(range(1, len(all_entries) + 1))      # no lost or duplicate rows
    assert {e.auth for e in all_entries} == {"valid"}
    consumers = sum(1 for _, role, _ in jobs if role != "publisher")
    got = receipts(s)
    evidence_post = next(e for e in all_entries if e.section == "report:evidence_agent")
    assert len(got) == consumers * n and {r.ref_seq for r in got} == {evidence_post.seq}
    posts = [e for e in all_entries if e.section == "report:investigator_agent"]
    assert len(posts) == 1                                                                 # one version, not three
    published = [o for o in outputs if o.startswith(("posted", "rejected"))]
    assert len(published) == 3 and len({o.split()[1] for o in published if o.startswith("posted")}) == 1, outputs


# =========================================================================== adjudicator inputs

def _results(inv=None, fo=None, ev=None):
    adk = AgentSystemResult(system="GOOGLE_ADK", run_id=RUN, claim_id=CLAIM_ID, status=ExecutionStatus.COMPLETED,
                            report=inv or investigation(), evidence_report=ev or evidence())
    crew = AgentSystemResult(system="CREWAI", run_id=RUN, claim_id=CLAIM_ID, status=ExecutionStatus.COMPLETED,
                             report=fo or forensics())
    return adk, crew


def _publish_rest(s):
    s.adk.consume_context("investigator_agent", s.ref)
    s.crew.consume_context("forensic_agent", s.ref)
    s.adk.agent_started("investigator_agent")
    s.adk.publish_report("investigator_agent", investigation())
    s.crew.publish_report("forensic_agent", forensics())


def test_the_adjudicator_receives_only_blackboard_verified_reports(s):
    _publish_rest(s)
    adk, crew = _results()
    s.ctl.collect(adk, crew, [])
    adk_in, crew_in, checks = s.ctl.verified_inputs(adk, crew)
    assert {a: c["verified"] for a, c in checks.items()} == {"evidence_agent": True, "investigator_agent": True,
                                                             "forensic_agent": True}
    assert (adk_in.report, adk_in.evidence_report, crew_in.report) == (investigation(), evidence(), forensics())
    assert adk_in.status == crew_in.status == ExecutionStatus.COMPLETED
    adjudicator = [r for r in receipts(s) if r.author == "adjudicator"]
    assert [(r.payload["source_section"], r.principal, r.auth) for r in adjudicator] == [
        ("report:evidence_agent", "orchestrator", "valid"), ("report:investigator_agent", "orchestrator", "valid"),
        ("report:forensic_agent", "orchestrator", "valid")]
    assert all(r.run_seq > s.board.state(RUN, CLAIM_ID).sealed_at for r in adjudicator)


def test_the_adjudicator_ignores_unverified_reports(s):
    _publish_rest(s)
    adk, crew = _results(fo=forensics(recommended_outcome="REJECT"))       # differs from what was published
    seal(s)
    adk_in, crew_in, checks = s.ctl.verified_inputs(adk, crew)
    assert adk_in.status == ExecutionStatus.COMPLETED and checks["investigator_agent"]["verified"]
    assert crew_in.status == ExecutionStatus.FAILED and crew_in.report is None
    assert "differs from the report that was delivered" in checks["forensic_agent"]["reason"]
    assert any("blackboard verification failed" in e for e in crew_in.errors)
    assert crew.status == ExecutionStatus.COMPLETED and crew.report is not None          # what ran is not rewritten
    # A report that was never published is not adjudicated either.
    t = setup_run(s.db, run_id="RUN-ctx-4")
    t.adk.consume_context("investigator_agent", t.ref)
    adk, crew = _results()
    adk, crew = adk.model_copy(update={"run_id": "RUN-ctx-4"}), crew.model_copy(update={"run_id": "RUN-ctx-4"})
    t.ctl.collect(adk, crew, [])
    adk_in, crew_in, checks = t.ctl.verified_inputs(adk, crew)
    assert adk_in.status == crew_in.status == ExecutionStatus.FAILED
    assert checks["investigator_agent"]["reason"].endswith("no live report on the blackboard")
    assert checks["evidence_agent"]["reason"] == "not used: another report of the same system failed verification"


def test_an_unusable_blackboard_hands_the_adjudicator_nothing(tmp_path):
    broken = tmp_path / "dir"
    broken.mkdir()
    ctl = RunController(RUN, CLAIM_ID, path=broken)
    ctl.register(DOCS)
    adk, crew = _results()
    adk_in, crew_in, _ = ctl.verified_inputs(adk, crew)
    assert ctl.requested and not ctl.enabled
    assert adk_in.status == crew_in.status == ExecutionStatus.FAILED


# =========================================================================== ADK: Evidence -> blackboard -> Investigator

@pytest.fixture(scope="module")
def mcp(dataset, tmp_path_factory):
    srv = RunningServer(*dataset, tmp_path_factory.mktemp("ctx_mcp"))
    yield srv
    srv.stop()


def adk_run(mcp, dataset, tmp_path, scripted, run_id):
    db = tmp_path / "bb.sqlite3"
    _, docs = load_claim(ADK_CLAIM, dataset[0])
    RunController(run_id, ADK_CLAIM, path=db).register(docs)
    seen = []
    on_evidence = lambda ref, error: seen.append((ref, error, len(scripted["investigator_agent"].requests)))
    result = asyncio.run(run_adk_investigation_async(ADK_CLAIM, run_id=run_id, model=scripted, mcp_url=mcp.url,
                                                     data_dir=dataset[0], blackboard_path=db, on_evidence=on_evidence))
    return result, Blackboard(db), seen


def test_investigator_receives_the_blackboard_copy_before_it_starts(mcp, dataset, tmp_path):
    scripted = happy_models()
    result, board, seen = adk_run(mcp, dataset, tmp_path, scripted, "RUN-ctx-adk-1")
    assert result.status == ExecutionStatus.COMPLETED, result.errors
    es = board.entries("RUN-ctx-adk-1", ADK_CLAIM)
    post = next(e for e in es if e.section == "report:evidence_agent")
    receipt = next(e for e in es if e.section == RECEIPT_PREFIX + "investigator_agent")
    started = next(e for e in es if e.section == "status:investigator_agent" and e.payload.get("state") == "RUNNING")
    # Published during execution and handed to the orchestrator before the Investigator made any model request.
    (ref, error, investigator_requests), = seen
    assert error is None and investigator_requests == 0
    assert (ref.run_seq, ref.sha256) == (post.run_seq, post.payload_sha256)
    assert post.run_seq < receipt.run_seq < started.run_seq and receipt.ref_seq == post.seq
    assert post.ts <= result.agent_runs[1].started_at
    # The model's instruction contains exactly the verified blackboard copy, delimited as untrusted data.
    copy = EvidenceReport.model_validate(post.payload["report"])
    instruction = scripted["investigator_agent"].system_instructions()[0]
    assert context_block(copy, ref) in instruction
    assert f'<untrusted source="blackboard:report:evidence_agent@{ref.run_seq}">' in instruction
    assert result.evidence_report == copy
    assert [(c.consumer, c.receipt_run_seq) for c in result.consumed_context] == [("investigator_agent", receipt.run_seq)]
    trace = next(t["blackboard_context"] for t in result.trace if "blackboard_context" in t)
    assert trace["supplied_to_investigator_sha256"] == payload_hash(context_block(copy, ref))


def test_a_different_raw_session_state_value_is_ignored(mcp, dataset, tmp_path, monkeypatch):
    sentinel = json.dumps(evidence_report(recommended_decision="REJECT", reasoning_summary="RAW-SESSION-STATE-SENTINEL"))
    original = BlackboardLifecyclePlugin.after_agent_callback

    async def tampering(self, *, agent, callback_context):
        out = await original(self, agent=agent, callback_context=callback_context)
        if agent.name == "evidence_agent":                     # after publication, overwrite the raw state value
            callback_context.state["evidence_report"] = sentinel
        return out

    raw_at_start = {}
    original_before = BlackboardLifecyclePlugin.before_agent_callback

    async def observing(self, *, agent, callback_context):
        if agent.name == "investigator_agent":                 # what session state held as the Investigator started
            raw_at_start["value"] = callback_context.state.get("evidence_report")
        return await original_before(self, agent=agent, callback_context=callback_context)

    monkeypatch.setattr(BlackboardLifecyclePlugin, "after_agent_callback", tampering)
    monkeypatch.setattr(BlackboardLifecyclePlugin, "before_agent_callback", observing)
    scripted = happy_models()
    result, board, _ = adk_run(mcp, dataset, tmp_path, scripted, "RUN-ctx-adk-2")
    assert result.status == ExecutionStatus.COMPLETED, result.errors
    assert raw_at_start["value"] == sentinel                   # the tampered raw value really was in session state
    instruction = scripted["investigator_agent"].system_instructions()[0]
    assert "RAW-SESSION-STATE-SENTINEL" not in instruction
    assert "blackboard:report:evidence_agent@" in instruction
    assert "RAW-SESSION-STATE-SENTINEL" not in result.evidence_report.model_dump_json()
    assert result.evidence_report.recommended_decision.value == "APPROVE"


def test_unsupported_evidence_is_never_published_and_stops_the_investigator(mcp, dataset, tmp_path):
    fabricated = evidence_report(evidence_citations=[{"doc_id": "invoice", "line": 7, "quote": "Purchase Date: 1999-01-01"}])
    scripted = models([call("warranty_policy_search", policy_id="POL-TECH-CARE-01"), call("set_model_response", **fabricated)],
                      [call("set_model_response", **investigation_report())])
    result, board, seen = adk_run(mcp, dataset, tmp_path, scripted, "RUN-ctx-adk-3")
    assert result.status == ExecutionStatus.FAILED and result.evidence_report is None and result.report is None
    (ref, error, _), = seen
    assert ref is None and "not found in the document" in error
    es = board.entries("RUN-ctx-adk-3", ADK_CLAIM)
    assert [e for e in es if e.kind == "post"] == []                                   # nothing was shared
    assert scripted["investigator_agent"].requests == []                               # the Investigator never ran
    assert any("investigator_agent was not started" in e for e in result.errors)
    states = {a: st.state for a, st in board.state("RUN-ctx-adk-3", ADK_CLAIM).agents.items()}
    assert states["evidence_agent"] == states["investigator_agent"] == "FAILED"


# =========================================================================== whole run: ADK + A2A + CrewAI service

@pytest.fixture(scope="module")
def services(dataset, tmp_path_factory):
    if not CREW_PYTHON.exists():
        pytest.skip("needs .venv-crew for the CrewAI A2A service")
    tmp = tmp_path_factory.mktemp("ctx_e2e")
    db = tmp / "blackboard.sqlite3"
    previous = os.environ.get(ENV_VAR)
    os.environ[ENV_VAR] = str(db)                        # this process and the CrewAI service (inherits it)
    mcp = RunningServer(dataset[0], dataset[1], tmp)
    try:
        crew = CrewService(dataset[0], mcp.url, tmp)
    except Exception:
        mcp.stop()
        raise
    try:
        yield SimpleNamespace(db=db, mcp=mcp, crew=crew, data_dir=dataset[0], db_path=dataset[1], tmp=tmp,
                              truth=json.loads((dataset[0] / "ground_truth.json").read_text(encoding="utf-8")))
    finally:
        crew.stop()
        mcp.stop()
        if previous is None:
            os.environ.pop(ENV_VAR, None)
        else:
            os.environ[ENV_VAR] = previous


_bundles: dict = {}


def run(env, claim_id, suffix="ctx", **kw):
    key = (claim_id, suffix)
    if key not in _bundles:
        _bundles[key] = run_claim(claim_id, run_id=f"RUN-{suffix}-{claim_id}", mcp_url=env.mcp.url,
                                  crew_card_url=kw.pop("crew_card_url", env.crew.card), data_dir=env.data_dir,
                                  db_path=env.db_path, **kw)
    return _bundles[key]


def test_forensic_gets_a_context_ref_over_a2a_and_consumes_it_with_its_own_key(services):
    bundle = run(services, "CS-003")
    assert bundle.context_sharing == "blackboard"
    es = Blackboard(services.db).entries(bundle.run_id, "CS-003")
    post = next(e for e in es if e.section == "report:evidence_agent")
    request = bundle.messages[0]
    assert request.message_type == "FORENSIC_REQUEST"
    ref = ContextRef(run_id=bundle.run_id, claim_id="CS-003", section="report:evidence_agent", run_seq=post.run_seq,
                     sha256=post.payload_sha256, fingerprint=post.fingerprint)
    assert request.payload.context == [ref]
    assert bundle.adk.evidence_report.reasoning_summary not in request.model_dump_json()   # a reference, not content
    assert request.timestamp >= post.ts                         # the request waited for the published report
    receipt = next(e for e in es if e.section == RECEIPT_PREFIX + "forensic_agent")
    assert (receipt.principal, receipt.process.environment, receipt.auth, receipt.ref_seq) == (
        "crewai", ".venv-crew", "valid", post.seq)
    assert receipt.process.pid != os.getpid()
    assert [(c.consumer, c.ref, c.receipt_run_seq) for c in bundle.crewai.consumed_context] == [
        ("forensic_agent", ref, receipt.run_seq)]
    # What the CrewAI service added to the claim packet is exactly the blackboard copy, as untrusted data.
    supplied = next(t["blackboard_context"] for t in bundle.crewai.trace if "blackboard_context" in t)
    copy = EvidenceReport.model_validate(post.payload["report"])
    assert supplied["supplied_sha256"] == payload_hash(shared_context([context_facts(copy, ref)]))
    assert bundle.final_report.review_status == "COMPLETE"


def test_forensic_remains_independent_of_the_investigator(services):
    bundle = run(services, "CS-003")
    es = Blackboard(services.db).entries(bundle.run_id, "CS-003")
    forensic_reads = [e.payload["source_section"] for e in es if e.section == RECEIPT_PREFIX + "forensic_agent"]
    assert forensic_reads == ["report:evidence_agent"]
    inv_post = next(e for e in es if e.section == "report:investigator_agent")
    assert all(e.ref_seq != inv_post.seq for e in es if e.author == "forensic_agent")
    assert [r.section for r in bundle.messages[0].payload.context] == ["report:evidence_agent"]
    supplied = next(t["blackboard_context"] for t in bundle.crewai.trace if "blackboard_context" in t)
    assert [c["section"] for c in supplied["consumed"]] == ["report:evidence_agent"]


def test_every_adjudicated_report_was_verified_on_the_sealed_blackboard(services):
    bundle = run(services, "CS-003")
    state = Blackboard(services.db).state(bundle.run_id, "CS-003")
    adjudicator = [m for m in state.monitor if m.section == RECEIPT_PREFIX + "adjudicator"]
    assert sorted(m.payload["source_section"] for m in adjudicator) == sorted(SECTION_SCHEMAS)
    assert all(m.run_seq > state.sealed_at and m.auth == "valid" for m in adjudicator)
    verified = [a for a in bundle.audit if a.event == "blackboard_context" and a.actor == "adjudicator"]
    assert len(verified) == 3
    view = run_monitor(bundle.run_id, path=services.db)                     # the dashboard's view of it
    assert {(c["consumer"], c["source_section"]) for c in view["context"]} == {
        ("investigator_agent", "report:evidence_agent"), ("forensic_agent", "report:evidence_agent"),
        ("adjudicator", "report:evidence_agent"), ("adjudicator", "report:investigator_agent"),
        ("adjudicator", "report:forensic_agent")}


@pytest.mark.parametrize("claim_id", ["CS-001", "CS-003", "CS-006", "CS-001-INJ"])
def test_blackboard_mode_outcomes_match_the_ground_truth(services, claim_id):
    final = run(services, claim_id).final_report
    assert final.final_decision.value == services.truth[claim_id]["expected_decision"]
    assert final.review_status == "COMPLETE"


def test_a_forensic_failure_keeps_the_investigator_and_needs_a_human(services):
    dead = f"http://127.0.0.1:{_free_port()}/.well-known/agent-card.json"
    bundle = run(services, "CS-001", suffix="ctx-noforensic", crew_card_url=dead, a2a_timeout=30)
    final = bundle.final_report
    assert bundle.crewai.status == ExecutionStatus.FAILED and bundle.adk.status == ExecutionStatus.COMPLETED
    assert final.review_status == "PARTIAL" and final.requires_human_approval and final.approval_status == "PENDING"
    assert any("CREWAI review unavailable" in r for r in final.incomplete_reasons)
    checks = {a.data.get("verified") for a in bundle.audit if a.actor == "adjudicator" and a.event == "blackboard_context"}
    assert checks == {True}                                    # the ADK reports were verified and used


def test_an_investigator_failure_after_evidence_publication_is_incomplete(services, monkeypatch):
    from claimsleuth.adk_service import test_double as adk_doubles
    real = adk_doubles.offline_models
    monkeypatch.setattr(adk_doubles, "offline_models", lambda: {**real(), "investigator_agent": ScriptedLlm(script=[])})
    bundle = run(services, "CS-001", suffix="ctx-noinvestigator")
    final = bundle.final_report
    assert bundle.adk.status == ExecutionStatus.FAILED and bundle.crewai.status == ExecutionStatus.COMPLETED
    assert final.review_status == "PARTIAL" and final.requires_human_approval
    state = Blackboard(services.db).state(bundle.run_id, "CS-001")
    # The real history is kept: the Evidence Agent completed and was consumed; the Investigator failed.
    assert (state.agents["evidence_agent"].state, state.agents["investigator_agent"].state) == ("COMPLETED", "FAILED")
    # The valid Evidence report is preserved, signed and live; the adjudicator did not use it (the ADK review failed).
    assert state.retracted == [] and "report:evidence_agent" in {p.section for p in state.live_posts}
    assert bundle.adk.evidence_report is not None and bundle.adk.report is None
    reads = {m.payload["source_section"] for m in state.monitor if m.section == RECEIPT_PREFIX + "forensic_agent"}
    assert reads == {"report:evidence_agent"}


def test_disabled_blackboard_direct_mode_still_works(services, monkeypatch):
    with_board = run(services, "CS-003")
    monkeypatch.setenv(ENV_VAR, "off")
    direct = run(services, "CS-003", suffix="ctx-direct")
    assert direct.context_sharing == "direct"
    assert direct.messages[0].payload.context == [] and direct.adk.consumed_context == []
    assert direct.adk.status == direct.crewai.status == ExecutionStatus.COMPLETED
    pick = lambda f: (f.final_decision, f.decided_by, f.review_status, f.requires_human_approval)
    assert pick(direct.final_report) == pick(with_board.final_report)
    assert Blackboard(services.db).registration(direct.run_id) is None


# =========================================================================== regression: evaluation and dashboard

# The 40-case offline evaluation is run once per session by the `evaluation` fixture in tests/conftest.py.

BASELINE_EVALUATION = ROOT / "runtime" / "evaluation" / "EV-20261102-135347-0a89f7" / "results.json"


def test_the_40_case_offline_evaluation_gives_the_same_final_decisions(evaluation):
    result, tmp = evaluation
    meta, rows = result["meta"], result["rows"]
    assert meta["cases_executed"] == 40 and meta["ledger_requests_added"] == 0
    assert gemini_rate.total_requests(tmp / "ledger.sqlite3") == 0
    assert meta["context_sharing"] == "blackboard"
    # The earlier evaluation (direct mode) decided all 40 cases as the ground truth expects, every review COMPLETE.
    assert {r["case_id"]: r["combined"]["final_decision"] for r in rows} == {r["case_id"]: r["expected_decision"] for r in rows}
    assert {r["combined"]["review_status"] for r in rows} == {"COMPLETE"}
    if BASELINE_EVALUATION.exists():                       # the stored run, when this machine has it
        before = {r["case_id"]: (r["combined"]["final_decision"], r["combined"]["decided_by"])
                  for r in json.loads(BASELINE_EVALUATION.read_text(encoding="utf-8"))["rows"]}
        assert {r["case_id"]: (r["combined"]["final_decision"], r["combined"]["decided_by"]) for r in rows} == before
    # ... and now through the blackboard: both agents consumed the Evidence report in every case.
    board = Blackboard(Path(meta["blackboard"]))
    for row in rows:
        reg = board.registration(row["run_id"])
        consumers = {m.payload["consumer"] for m in board.state(row["run_id"], reg["claim_id"]).monitor
                     if m.section.startswith(RECEIPT_PREFIX)}
        assert consumers == {"investigator_agent", "forensic_agent", "adjudicator"}, row["case_id"]


def test_dashboard_and_evaluation_endpoints_still_work(evaluation, services, tmp_path, dataset):
    from starlette.testclient import TestClient

    from claimsleuth.dashboard.app import create_app
    from claimsleuth.dashboard.services import ServiceConfig
    from claimsleuth.dashboard.workspace import Workspace
    from claimsleuth.integration.run import save_bundle
    result, tmp = evaluation
    eval_id = result["meta"]["evaluation_id"]
    bundle = run(services, "CS-003")
    save_bundle(bundle, tmp_path / "rt")
    ws = Workspace(tmp_path / "dash", source_data=dataset[0])
    app = create_app(ws, ServiceConfig(mcp_port=services.mcp.port, crew_port=services.crew.port), tmp_path / "rt",
                     evaluation_root=tmp / "evaluation")
    with TestClient(app) as client:
        home = client.get("/api/evaluation").json()
        assert home["dataset"]["cases"] == 40 and eval_id in [e["evaluation_id"] for e in home["evaluations"]]
        stored = client.get(f"/api/evaluation/results/{eval_id}").json()
        assert stored["meta"]["cases_executed"] == 40 and len(stored["rows"]) == 40
        assert client.get(f"/api/evaluation/results/{eval_id}/export.csv").status_code == 200
        assert client.get(f"/api/evaluation/results/{eval_id}/export.md").status_code == 200
        view = client.get(f"/api/runs/{bundle.run_id}/blackboard").json()
        assert view["registered"] and view["decision_matches_report"] is True and len(view["context"]) == 5
        assert client.get("/").status_code == 200
