"""Evidence tampering: a published report is altered, and the real pathways must refuse it (Phase 4.1).

Two layers. The first section tests each protection directly on disposable databases, without services. The second runs
the whole controlled test once (dashboard/evidence_integrity_lab.py): real manager, ADK Runner, CrewAI A2A service as
its own process, MCP and adjudicator; only the agents' reasoning is the labelled offline test doubles. No model request
is sent, and neither layer can reach the production blackboard (the mutation fixture refuses it).
"""
import asyncio
import hashlib
import json
import re
import sqlite3
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from claimsleuth import config, contract
from claimsleuth.blackboard import (Blackboard, BlackboardRejected, BlackboardSecurityError, ContextRef, Writer,
                                    load_writer, run_monitor, security_event)
from claimsleuth.blackboard.controller import TEST_MODE_ENV, read_monitoring_failures, report_sha256
from claimsleuth.dashboard import evidence_integrity_lab as lab
from claimsleuth.dashboard.app import create_app
from claimsleuth.dashboard.sample_pdf import SAMPLES, SAMPLES_DIR
from claimsleuth.dashboard.services import ServiceConfig
from claimsleuth.dashboard.workspace import Workspace
from claimsleuth.observability.outcome import integrity_explanation
from claimsleuth.observability.trace import TraceBuilder
from claimsleuth.schemas import AgentSystemResult, ExecutionStatus, RunTrace, utc_now
from test_blackboard_context import CLAIM_ID, FP, RUN, evidence, forensics, investigation, setup_run
from test_integration_e2e import CREW_PYTHON

HEX_RUN = re.compile(r"\b[0-9a-f]{32,}\b")       # a full hash, signature or key anywhere in monitoring output


def results():
    adk = AgentSystemResult(system="GOOGLE_ADK", run_id=RUN, claim_id=CLAIM_ID, status=ExecutionStatus.COMPLETED,
                            report=investigation(), evidence_report=evidence())
    crew = AgentSystemResult(system="CREWAI", run_id=RUN, claim_id=CLAIM_ID, status=ExecutionStatus.COMPLETED,
                             report=forensics())
    return adk, crew


@pytest.fixture
def run():
    """A published run on a disposable database: the three reports, the agents finished, nothing sealed yet."""
    root = lab.disposable_root()
    s = setup_run(root / "board.sqlite3")
    s.adk.agent_started("investigator_agent")
    s.inv_ref = s.adk.publish_report("investigator_agent", investigation())
    s.crew.publish_report("forensic_agent", forensics())
    return s


def row_of(s, ref=None) -> dict:
    return lab.stored(s.db, s.run_id, (ref or s.ref).run_seq)


# =========================================================================== the fixture cannot reach production data

@pytest.mark.parametrize("path", [config.RUNTIME_DIR / "blackboard.sqlite3", config.PROJECT_ROOT / "copy.sqlite3",
                                  config.DATA_DIR / "claimsleuth.db"])
def test_the_mutation_fixture_refuses_the_production_blackboard_and_project_files(path):
    with pytest.raises(lab.LabError, match="not a disposable"):
        lab.mutate(path, lambda conn: None)
    with pytest.raises(lab.LabError, match="not a disposable"):
        lab.isolate(path, "x.sqlite3")
    with pytest.raises(lab.LabError, match="not a disposable"):
        lab.stored(path, "RUN-x", 1)


def test_the_mutation_fixture_refuses_an_unmarked_temporary_database(tmp_path):
    with pytest.raises(lab.LabError, match="not a disposable"):
        lab.assert_disposable(tmp_path / "board.sqlite3")


def test_lifting_the_trigger_is_confined_to_one_transaction_on_the_disposable_file(run):
    lab.edit_payload(run.db, RUN, run.ref.run_seq, lambda p: {**p, "x": 1})
    conn = sqlite3.connect(run.db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    conn.close()
    assert names == {"entries_no_update", "entries_no_delete", "runs_no_update", "runs_no_delete"}
    conn = sqlite3.connect(run.db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):        # the protection is back at once
        conn.execute("UPDATE entries SET payload = '{}' WHERE run_id = ?", (RUN,))
    conn.close()


def test_a_failed_mutation_leaves_the_trigger_in_place(run):
    def fail(conn):
        conn.execute("UPDATE entries SET payload = '{}' WHERE run_id = ?", (RUN,))
        raise ValueError("stop")
    with pytest.raises(ValueError):
        lab.mutate(run.db, fail)
    assert row_of(run)["payload"] != "{}" and Blackboard(run.db).state(RUN, CLAIM_ID).unverified == []
    conn = sqlite3.connect(run.db)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'entries_no_update'").fetchone()
    conn.close()


# =========================================================================== what each check reports on its own

def test_tampering_keeps_the_hmac_valid_so_the_hash_check_must_catch_it(run):
    """The signature covers the stored hash, and the attacker kept both: it is the payload-hash check that fails."""
    lab.edit_payload(run.db, RUN, run.ref.run_seq, lambda p: {**p, "x": 1})
    assert run.adk.consume_context("investigator_agent", run.ref) is None
    e = run.adk.security_events[0]
    assert (e.attack_type, e.verification_result.payload_integrity, e.verification_result.signature) == (
        "PAYLOAD_TAMPERING", "MISMATCH", "VALID")


def test_changing_the_hash_too_is_caught_by_the_hmac(run):
    lab.edit_payload(run.db, RUN, run.ref.run_seq, lambda p: {**p, "x": 1}, rehash=True)
    assert run.adk.consume_context("investigator_agent", run.ref) is None
    e = run.adk.security_events[0]
    assert (e.attack_type, e.verification_result.payload_integrity, e.verification_result.signature) == (
        "HASH_AND_PAYLOAD_TAMPERING", "MATCH", "INVALID")


def test_a_sound_entry_read_with_a_wrong_hash_is_not_called_tampering(run):
    bad = run.ref.model_copy(update={"sha256": "0" * 64})
    assert run.adk.consume_context("investigator_agent", bad) is None
    e = run.adk.security_events[0]
    assert (e.attack_type, e.verification_result.payload_integrity, e.verification_result.signature) == (
        "REFERENCE_MISMATCH", "MATCH", "VALID")


# =========================================================================== gaps closed in Phase 4.1

def _forged_status(db, source: dict, state: str) -> None:
    """An unsigned status row added with raw SQL, as anyone with file access could."""
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO entries (run_id, run_seq, claim_id, ts, kind, section, author, framework, process, "
                 "fingerprint, payload, payload_sha256) VALUES (?, 999, ?, ?, 'status', ?, ?, ?, ?, ?, ?, 'x')",
                 (RUN, CLAIM_ID, source["ts"], "status:evidence_agent", "evidence_agent", "adk", source["process"], FP,
                  json.dumps({"state": state, "agent": "evidence_agent"})))
    conn.commit()
    conn.close()


def test_a_forged_status_row_cannot_make_an_unfinished_report_consumable():
    root = lab.disposable_root()
    s = setup_run(root / "board.sqlite3", publish=False)
    post = s.board.post(RUN, CLAIM_ID, "evidence_agent", "adk", "report:evidence_agent",
                        {"report": evidence().model_dump(mode="json"), "report_sha256": report_sha256(evidence())}, FP,
                        writer=load_writer(s.db, "adk"))
    ref = ContextRef(run_id=RUN, claim_id=CLAIM_ID, section="report:evidence_agent", run_seq=post.run_seq,
                     sha256=post.payload_sha256, fingerprint=FP)
    assert s.adk.consume_context("investigator_agent", ref) is None and "is RUNNING" in s.adk.last_failure
    _forged_status(s.db, lab.stored(s.db, RUN, post.run_seq), "COMPLETED")
    assert s.adk.consume_context("investigator_agent", ref) is None
    assert "status entry of evidence_agent failed authentication" in s.adk.last_failure


def test_an_unreadable_payload_does_not_stop_the_run_being_read(run):
    lab.set_columns(run.db, RUN, run.ref.run_seq, payload="{not json")
    board = Blackboard(run.db)
    by_seq = {e.run_seq: e for e in board.entries(RUN, CLAIM_ID)}
    assert by_seq[run.ref.run_seq].payload == {} and by_seq[run.ref.run_seq].auth == "invalid"
    assert board.state(RUN, CLAIM_ID).unverified == [f"report:evidence_agent@{run.ref.run_seq}"]
    view = run_monitor(RUN, run.db)
    assert view["registered"] and "error" not in view
    assert run.adk.consume_context("investigator_agent", run.ref) is None


def test_a_damaged_status_row_does_not_stop_the_run_being_read(run):
    status = next(e for e in Blackboard(run.db).entries(RUN, CLAIM_ID) if e.kind == "status")
    lab.set_columns(run.db, RUN, status.run_seq, payload="[]")
    assert Blackboard(run.db).state(RUN, CLAIM_ID).agents


def test_a_report_altered_after_the_agents_read_it_withholds_every_report(run):
    """Before Phase 4.1 only the altered report's own system was withheld, and the other could still carry an approval."""
    assert run.adk.consume_context("investigator_agent", run.ref) and run.crew.consume_context("forensic_agent", run.ref)
    lab.edit_payload(run.db, RUN, run.ref.run_seq, lambda p: {**p, "x": 1})
    adk, crew = results()
    run.ctl.collect(adk, crew, [])
    adk_in, crew_in, checks = run.ctl.verified_inputs(adk, crew)
    assert adk_in.status == crew_in.status == ExecutionStatus.FAILED and adk_in.report is crew_in.report is None
    assert not any(c["verified"] for c in checks.values())
    assert "failed integrity verification" in checks["forensic_agent"]["reason"]
    assert crew.status == ExecutionStatus.COMPLETED and crew.report is not None      # what ran is not rewritten


def test_a_tampered_entry_that_is_not_a_report_also_withholds_every_report(run):
    """A status row, which nobody reads as context, is still part of the record the seal fixes."""
    status = next(e for e in Blackboard(run.db).entries(RUN, CLAIM_ID) if e.section == "status:forensic_agent")
    lab.edit_payload(run.db, RUN, status.run_seq, lambda p: {**p, "x": 1})
    adk, crew = results()
    run.ctl.collect(adk, crew, [])
    adk_in, crew_in, _ = run.ctl.verified_inputs(adk, crew)
    assert adk_in.status == crew_in.status == ExecutionStatus.FAILED
    e, = run.ctl.security_events
    assert (e.attack_type, e.verification_result.payload_integrity) == ("PAYLOAD_TAMPERING", "MISMATCH")
    assert e.affected_entry_reference == f"status:forensic_agent@{status.run_seq}"


def test_a_sound_run_is_not_affected_by_a_delivered_report_that_differs(run):
    """Unchanged behaviour: no stored entry failed, so only the report that differs is withheld."""
    adk, _ = results()
    _, crew = results()
    crew = crew.model_copy(update={"report": forensics(recommended_outcome="REJECT")})
    run.ctl.collect(adk, crew, [])
    adk_in, crew_in, _ = run.ctl.verified_inputs(adk, crew)
    assert adk_in.status == ExecutionStatus.COMPLETED and crew_in.status == ExecutionStatus.FAILED


def test_a2a_needs_a_context_reference_for_a_run_on_the_blackboard():
    claim_docs = [SimpleNamespace(doc_id="invoice", sha256="1" * 64, filename="invoice.txt", kind="INVOICE")]
    request = SimpleNamespace(run_id=RUN, payload=SimpleNamespace(context=[]))
    with pytest.raises(contract.ContractError) as refused:
        contract.check_context_required(request, {"run_id": RUN})
    assert refused.value.code == "CONTEXT_REJECTED"
    contract.check_context_required(request, None)            # direct mode: no blackboard, no ContextRef expected
    contract.check_context_required(SimpleNamespace(run_id=RUN, payload=SimpleNamespace(context=claim_docs)), {"run_id": RUN})


# =========================================================================== the security event

def test_a_refused_read_writes_nothing_on_the_blackboard_and_reaches_the_monitoring_log(run):
    before = Blackboard(run.db).state(RUN, CLAIM_ID).entries
    lab.edit_payload(run.db, RUN, run.ref.run_seq, lambda p: {**p, "x": 1})
    assert run.adk.consume_context("investigator_agent", run.ref) is None
    assert Blackboard(run.db).state(RUN, CLAIM_ID).entries == before
    logged = [f["security_event"] for f in read_monitoring_failures(run.db, RUN) if "security_event" in f]
    assert [e["event_id"] for e in logged] == [run.adk.security_events[0].event_id]
    assert [e["event_id"] for e in run_monitor(RUN, run.db)["security_events"]] == [logged[0]["event_id"]]


def test_the_event_carries_the_contract_fields_and_no_secret(run):
    lab.edit_payload(run.db, RUN, run.ref.run_seq, lambda p: {**p, "x": 1})
    assert run.crew.consume_context("forensic_agent", run.ref) is None
    e = run.crew.security_events[0]
    data = e.model_dump(mode="json")
    assert set(data) >= {"event_id", "run_id", "claim_id", "event_type", "attack_type", "severity",
                         "affected_entry_reference", "verification_result", "rejection_reason", "downstream_blocked",
                         "escalation_status", "timestamp", "test_mode"}
    assert (e.run_id, e.claim_id, e.consumer) == (RUN, CLAIM_ID, "forensic_agent")
    assert e.affected_entry_reference == f"report:evidence_agent@{run.ref.run_seq}"
    assert e.event_type == "EVIDENCE_INTEGRITY_VIOLATION" and e.severity == "CRITICAL" and e.downstream_blocked
    assert e.escalation_status == "PENDING" and e.test_mode is False
    assert not HEX_RUN.search(json.dumps(data).replace(e.event_id, ""))
    row = row_of(run)
    for secret in (row["signature"], row["payload_sha256"], load_writer(run.db, "adk").key.hex()):
        assert secret not in json.dumps(data)


def test_an_event_takes_nothing_from_the_exception_text():
    error = BlackboardSecurityError("secret text from a stored entry", "PAYLOAD_TAMPERING", "report:evidence_agent", 3)
    assert "secret" not in security_event(RUN, CLAIM_ID, error, "investigator_agent").model_dump_json()


def test_test_mode_labels_the_event_and_changes_no_verification(monkeypatch):
    outcomes = {}
    for flag in ("0", "1"):
        monkeypatch.setenv(TEST_MODE_ENV, flag)
        root = lab.disposable_root()
        s = setup_run(root / "board.sqlite3")
        lab.edit_payload(s.db, RUN, s.ref.run_seq, lambda p: {**p, "x": 1})
        received = s.adk.consume_context("investigator_agent", s.ref)
        e = s.adk.security_events[0]
        outcomes[flag] = (received, e.attack_type, e.verification_result, s.adk.last_failure[:60])
        assert e.test_mode is (flag == "1")
    assert outcomes["0"] == outcomes["1"]


def test_the_trace_records_a_refused_read_in_its_own_category(run):
    lab.edit_payload(run.db, RUN, run.ref.run_seq, lambda p: {**p, "x": 1})
    run.crew.consume_context("forensic_agent", run.ref)
    b = TraceBuilder(RUN, CLAIM_ID)
    b.security(SimpleNamespace(security_events=run.crew.security_events))
    e, = b.events
    assert b.errors == [] and (e.event_type, e.status, e.error_code, e.error_category) == (
        "security.event", "rejected", "PAYLOAD_TAMPERING", "security")
    assert (e.agent_name, e.task_id, e.metadata["payload_integrity"], e.metadata["signature_check"]) == (
        "forensic_agent", "forensics", "MISMATCH", "VALID")


def test_a_wrong_key_is_refused_and_reported(run):
    run.adk._writer = Writer("adk", b"\0" * 32)
    assert run.adk.consume_context("investigator_agent", run.ref) is None
    assert run.adk.security_events[0].event_type == "UNAUTHORISED_ACCESS"
    assert run.adk.security_events[0].consumer == "investigator_agent"


def test_the_explanation_says_only_what_happened():
    assert "no agent report went on" in integrity_explanation([], True) and "A human must" in integrity_explanation([], True)
    partial = integrity_explanation(["forensic_agent"], True)
    assert "only forensic_agent went on" in partial and "no agent report" not in partial
    assert integrity_explanation(["forensic_agent"], False).endswith("No human review was required.")


def test_a_specific_failure_reason_survives_withholding_the_run(run):
    lab.edit_payload(run.db, RUN, run.ref.run_seq, lambda p: {**p, "x": 1})
    adk, crew = results()
    run.ctl.collect(adk, crew, [])
    _, _, checks = run.ctl.verified_inputs(adk, crew)
    assert "failed authentication" in checks["evidence_agent"]["reason"]
    assert "failed integrity verification" in checks["forensic_agent"]["reason"]


def test_blackboard_security_error_is_still_a_rejection():
    assert issubclass(BlackboardSecurityError, BlackboardRejected)


# =========================================================================== the whole controlled test, once

@pytest.fixture(scope="module")
def result():
    if not CREW_PYTHON.exists():
        pytest.skip("needs .venv-crew for the CrewAI A2A service")
    return asyncio.run(lab.run_lab(None))


def attacks(result, letter):
    return [a for a in result["attacks"] if a["attack"] == letter]


def test_the_whole_controlled_test_passes(result):
    assert result["passed"] and result["summary"]["failed"] == 0
    assert {a["attack"] for a in result["attacks"]} == set("ABCDEFGHIJ")


def test_the_target_claim_and_amounts_come_from_the_sample_configuration(result):
    assert (result["claim_id"], result["source_pdf"]) == ("WC-2026-0412", "sample_claim_WC-2026-0412.pdf")
    assert result["claim_id"] == SAMPLES[1].claim["claim_id"]
    assert result["original_repair_amount"] == SAMPLES[1].report["Estimated Cost"]      # from the report, as in the PDF
    assert result["tampered_repair_amount"] == "INR 145,000.00"


def test_only_the_repair_amount_changed_and_the_hash_and_signature_were_kept(result):
    t = result["tamper"]
    assert len(t["changed_fields"]) == 1 and re.fullmatch(r"report\.facts\[\d+\]\.value", t["changed_fields"][0])
    assert t["hash_preserved"] and t["signature_preserved"] and t["published_entry_unchanged"]


def test_a_amount_tampering_is_caught_by_the_payload_hash(result):
    a, = attacks(result, "A")
    assert (a["detected"], a["payload_integrity"], a["signature"]) == (["PAYLOAD_TAMPERING"], "MISMATCH", "VALID")
    assert not a["investigator_received"] and not a["forensic_received"] and a["receipts"] == 0


def test_b_payload_with_original_signature(result):
    assert attacks(result, "B")[0]["detected"] == ["PAYLOAD_TAMPERING"]


def test_c_forged_and_replaced_signatures(result):
    for a in attacks(result, "C"):
        assert (a["detected"], a["payload_integrity"], a["signature"]) == (["SIGNATURE_FORGERY"], "MATCH", "INVALID")


def test_d_cross_claim_substitution(result):
    swapped, signed, reference = attacks(result, "D")
    assert "HASH_AND_PAYLOAD_TAMPERING" in swapped["detected"]
    assert signed["detected"] == reference["detected"] == ["CROSS_CLAIM_SUBSTITUTION"]
    assert signed["signature"] == "VALID"                 # a real key cannot make another claim's report acceptable


def test_e_replay(result):
    superseded, earlier, copied = attacks(result, "E")
    assert superseded["detected"] == earlier["detected"] == ["REPLAY"]
    assert copied["detected"] == ["SIGNATURE_FORGERY"]    # run_seq is signed, so a row cannot be moved forward


def test_f_unauthorised_principals(result):
    rows = attacks(result, "F")
    assert len(rows) == 4 and all(a["detected"] == ["UNAUTHORISED_ACCESS"] for a in rows)


def test_g_published_entries_cannot_be_updated_or_deleted(result):
    rows = attacks(result, "G")
    assert len(rows) == 4 and all(a["detected"] == ["APPEND_ONLY_TRIGGER"] for a in rows)


def test_h_malformed_reports(result):
    invalid_json, wrong_shape, signed = attacks(result, "H")
    assert "PAYLOAD_TAMPERING" in invalid_json["detected"] and wrong_shape["detected"] == ["PAYLOAD_TAMPERING"]
    assert signed["detected"] == ["MALFORMED_REPORT"] and signed["signature"] == "VALID"


def test_i_payload_and_hash_changed_without_a_valid_hmac(result):
    with_old_ref, with_new_ref = attacks(result, "I")
    assert "HASH_AND_PAYLOAD_TAMPERING" in with_old_ref["detected"]
    assert with_new_ref["detected"] == ["SIGNATURE_FORGERY"]         # even the reference could not help
    assert with_new_ref["payload_integrity"] == "MATCH" and with_new_ref["signature"] == "INVALID"


def test_j_a_clean_report_is_accepted_and_adjudicated_as_before(result):
    a, = attacks(result, "J")
    assert a["investigator_received"] and a["forensic_received"] and a["receipts"] == 2 and a["detected"] == []
    assert (a["final_decision"], a["decided_by"]) == (result["clean_run"]["final_decision"], result["clean_run"]["decided_by"])


def test_every_read_of_a_compromised_report_was_refused_in_every_attack(result):
    for a in result["attacks"]:
        if a["attack"] != "J":
            assert not a["investigator_received"] and not a["forensic_received"] and a["receipts"] == 0, a["name"]


def test_clean_run_uses_every_review(result):
    r = result["clean_run"]
    assert set(r["tasks"].values()) == {"COMPLETED"} and r["review_status"] == "COMPLETE" and r["events"] == []
    assert r["investigator_received"] and r["forensic_received"] and r["forensic_agent_ran"]
    assert r["agent_reports_adjudicated"] == ["evidence_agent", "investigator_agent", "forensic_agent"]


def test_tampered_run_blocks_both_agents_and_records_the_failure(result):
    r = result["tampered_run"]
    assert (r["tasks"]["evidence"], r["tasks"]["investigation"], r["tasks"]["forensics"]) == ("COMPLETED", "FAILED", "FAILED")
    assert not r["investigator_received"] and not r["forensic_received"]
    assert not r["forensic_agent_ran"]                   # the crew never started: no model call could have been made
    assert r["review_status"] == "NONE" and r["agent_reports_adjudicated"] == [] and r["human_review_required"]
    assert r["payout"] is None
    assert {e["consumer"] for e in r["events"]} >= {"investigator_agent", "forensic_agent"}
    assert {e["escalation_status"] for e in r["events"]} == {"HUMAN_REVIEW_REQUIRED"}
    assert all(e["test_mode"] and e["downstream_blocked"] for e in r["events"])
    assert result["monitoring_events"] == len(r["events"])    # the CrewAI process's refusal reached the shared log


def test_the_tamper_is_what_stops_an_approval(result):
    """WC-2026-0412 escalates anyway; WC-2026-0411 shows the effect: the same claim, approved when clean."""
    clean, tampered = result["control_clean_run"], result["control_tampered_run"]
    assert (clean["final_decision"], clean["deterministic_decision"], clean["payout"]) == ("APPROVE", "APPROVE", "16400.00")
    assert (tampered["final_decision"], tampered["decided_by"], tampered["payout"]) == ("ESCALATE", "INSUFFICIENT_REVIEW", None)
    assert tampered["human_review_required"] and tampered["deterministic_decision"] == "APPROVE"


def test_tampering_after_the_agents_read_the_report_still_stops_the_approval(result):
    r = result["control_late_run"]
    assert r["investigator_received"] and r["forensic_received"]            # both read it while it was still sound
    assert (r["final_decision"], r["decided_by"], r["review_status"], r["payout"]) == (
        "ESCALATE", "INSUFFICIENT_REVIEW", "NONE", None)
    assert r["human_review_required"] and r["agent_reports_adjudicated"] == []
    assert any(e["consumer"] == "adjudicator" for e in r["events"])


def test_a2a_cannot_go_around_the_blackboard(result):
    assert [(c["check"], c["result"]) for c in result["a2a_bypass"]] == [
        ("Request without a ContextRef", "CONTEXT_REJECTED"), ("Report sent inside the A2A message", "INVALID_MESSAGE")]


def test_the_original_evidence_is_preserved(result):
    assert result["original_report_kept"] and result["pdf_fingerprint_unchanged"]


def test_nothing_protected_changed_and_no_model_request_was_made(result):
    assert result["gemini_requests"] == 0 and result["protected_files_unchanged"]


def test_the_result_file_holds_no_hash_signature_or_key(result):
    text = json.dumps(result)
    assert not HEX_RUN.search(text)
    assert "signature_at_publication" not in text and "hash_at_publication" not in text


# =========================================================================== dashboard

def stored_trace(run_id: str, test_mode: bool) -> RunTrace:
    b = TraceBuilder(run_id, CLAIM_ID)
    event = security_event(run_id, CLAIM_ID, BlackboardSecurityError("x", "PAYLOAD_TAMPERING", "report:evidence_agent", 5,
                                                                     "MISMATCH", "VALID"), "forensic_agent")
    b.security(SimpleNamespace(security_events=[event.model_copy(update={
        "test_mode": test_mode, "escalation_status": "HUMAN_REVIEW_REQUIRED"})]))
    return RunTrace(run_id=run_id, claim_id=CLAIM_ID, correlation_id=f"trace-{run_id}", collected_at=utc_now(), events=b.events)


@pytest.fixture
def site(dataset, tmp_path):
    ws = Workspace(tmp_path / "dash", source_data=dataset[0])
    ws.prepare()
    runtime = tmp_path / "rt"
    with TestClient(create_app(ws, ServiceConfig(mcp_port=1, crew_port=2), runtime)) as client:
        yield SimpleNamespace(client=client, runtime=runtime)


def test_the_section_shows_nothing_until_a_test_has_run_and_invents_no_events(site):
    body = site.client.get("/api/evidence-integrity").json()
    assert body["controlled_test"] is None and body["job"] is None
    assert body["real_investigations"] == {"runs_checked": 0, "events": []}


def test_real_investigations_and_controlled_tests_stay_apart(site):
    for run_id, test_mode in (("RUN-real-001", False), ("RUN-lab-002", True)):
        folder = site.runtime / "runs" / run_id
        folder.mkdir(parents=True)
        (folder / "trace.json").write_text(stored_trace(run_id, test_mode).model_dump_json(), encoding="utf-8")
    real = site.client.get("/api/evidence-integrity").json()["real_investigations"]
    assert real["runs_checked"] == 2 and [e["run_id"] for e in real["events"]] == ["RUN-real-001"]
    e = real["events"][0]
    assert (e["attack_type"], e["payload_integrity"], e["signature"], e["consumer"]) == (
        "PAYLOAD_TAMPERING", "MISMATCH", "VALID", "forensic_agent")


def test_the_latest_stored_test_is_served_as_stored(site, result):
    folder = site.runtime / "security" / "evidence_integrity"
    folder.mkdir(parents=True)
    (folder / "EI-1.json").write_text(json.dumps(result), encoding="utf-8")
    (folder / "EI-0.json").write_text("{broken", encoding="utf-8")
    response = site.client.get("/api/evidence-integrity")
    assert response.json()["controlled_test"]["lab_id"] == result["lab_id"]
    assert not HEX_RUN.search(response.text)


@pytest.mark.parametrize("code,state", [(0, "completed"), (lab.CHECK_FAILED, "completed"), (1, "failed")])
def test_a_crash_is_not_reported_as_a_finished_test(tmp_path, monkeypatch, code, state):
    class Proc:
        returncode = code

        async def communicate(self):
            return b"", b"BLACKBOARD MONITORING FAILURE earlier line\nRuntimeError: last line\n"

    async def spawn(*args, **kwargs):
        return Proc()

    async def go():
        job = lab.LabJob(tmp_path)
        monkeypatch.setattr(lab.asyncio, "create_subprocess_exec", spawn)
        job.start()
        await job._task
        return job.state

    out = asyncio.run(go())
    assert out["state"] == state and out["error"] == ("RuntimeError: last line" if state == "failed" else None)


def test_the_test_cannot_start_while_something_else_runs(site, monkeypatch):
    started = []
    monkeypatch.setattr(lab.LabJob, "start", lambda self: started.append(1))
    monkeypatch.setattr(lab.LabJob, "busy", lambda self: True)
    assert site.client.post("/api/evidence-integrity/run", json={}).status_code == 409 and not started


def test_the_page_has_the_section_and_the_removed_pages_stay_removed(site):
    html = site.client.get("/").text
    assert 'id="integrity-details"' in html and "Security &amp; Trust" in html
    assert "Security &amp; Guardrails" not in html and 'id="view-security"' not in html
    assert site.client.get("/api/security/latest").status_code in (404, 405)
    assert site.client.post("/api/security/run", json={}).status_code in (404, 405)
    assert "Agent Evaluation" in html and 'id="trace-details"' in html and 'id="bb-details"' in html


def test_the_sample_pdf_fingerprint_is_the_recorded_one(result):
    data = (SAMPLES_DIR / "sample_claim_WC-2026-0412.pdf").read_bytes()
    assert hashlib.sha256(data).hexdigest() == "2c3e7758adce663e50145d7c59e7a5a0ddedba6849ecb70e65395fb5aa57ce10"
