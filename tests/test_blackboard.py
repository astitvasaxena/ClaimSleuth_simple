"""Shared coordination blackboard: store guarantees, writer authentication, late entries, concurrency across real
processes, the controller, the ADK lifecycle plugin, the monitoring view and the dashboard's blackboard settings.
The end-to-end run with the real ADK side and the real CrewAI A2A service is in test_blackboard_e2e.py."""
import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from claimsleuth.blackboard import (AgentParticipant, Blackboard, BlackboardError, BlackboardRejected, RunController,
                                    configured_path, document_fingerprint, load_writer, provision, run_monitor)
from claimsleuth.blackboard.auth import Writer, key_dir
from claimsleuth.blackboard.controller import monitoring_log, read_monitoring_failures, report_sha256
from claimsleuth.schemas import ExecutionStatus

ROOT = Path(__file__).resolve().parents[1]
CREW_PYTHON = ROOT / ".venv-crew" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
FP, OTHER_FP = "a" * 64, "b" * 64


@pytest.fixture
def db(tmp_path):
    return tmp_path / "bb.sqlite3"


@pytest.fixture
def board(db):
    provision(db)
    b = Blackboard(db)
    b.register("RUN-1", "CS-001", FP, writer=load_writer(db, "orchestrator"))
    return b


@pytest.fixture
def w(db):
    return lambda principal: load_writer(db, principal)


# --------------------------------------------------------------------------- configuration

@pytest.mark.parametrize("value", [None, "", "off", "0", "false", "OFF", "disabled"])
def test_disabled_values(value):
    assert configured_path({} if value is None else {"CLAIMSLEUTH_BLACKBOARD_DB": value}) is None


def test_disabled_participants_do_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAIMSLEUTH_BLACKBOARD_DB", raising=False)
    p = AgentParticipant("RUN-x", "CS-001", "adk", ("evidence_agent",))
    p.running([])
    p.finished(SimpleNamespace(status=ExecutionStatus.COMPLETED))
    assert not p.enabled and p.failures == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("flag, env, expected", [
    (False, {}, "DEFAULT"),                                              # default path
    (False, {"CLAIMSLEUTH_BLACKBOARD_DB": "D:/explicit/bb.sqlite3"}, "D:/explicit/bb.sqlite3"),   # explicit path
    (False, {"CLAIMSLEUTH_BLACKBOARD_DB": "off"}, "off"),               # explicitly disabled
    (False, {"CLAIMSLEUTH_BLACKBOARD_DB": ""}, ""),                     # explicitly empty = disabled
    (True, {}, "off"),                                                  # --no-blackboard
    (True, {"CLAIMSLEUTH_BLACKBOARD_DB": "D:/explicit/bb.sqlite3"}, "off"),
])
def test_dashboard_blackboard_setting(flag, env, expected):
    from claimsleuth.dashboard.__main__ import DEFAULT_BLACKBOARD, blackboard_setting
    assert blackboard_setting(flag, env) == (str(DEFAULT_BLACKBOARD) if expected == "DEFAULT" else expected)
    assert DEFAULT_BLACKBOARD.name == "blackboard.sqlite3" and DEFAULT_BLACKBOARD.parent.name == "runtime"


@pytest.mark.parametrize("argv, preset, expected", [
    ([], None, "DEFAULT"), ([], "D:/explicit/bb.sqlite3", "D:/explicit/bb.sqlite3"),
    (["--no-blackboard"], None, "off"), (["--no-blackboard"], "D:/explicit/bb.sqlite3", "off")])
def test_dashboard_sets_the_blackboard_before_starting_the_services(monkeypatch, argv, preset, expected):
    import claimsleuth.dashboard.__main__ as launcher
    seen = {}

    class Services:                                       # records what the CrewAI service would inherit
        def __init__(self, *a): pass
        def start(self, log): seen["at_start"] = launcher.os.environ.get("CLAIMSLEUTH_BLACKBOARD_DB")
        def stop(self): pass

    class FakeWorkspace:
        data_dir = "workspace"
        def prepare(self): pass

    monkeypatch.setattr(launcher, "ServiceLauncher", Services)
    monkeypatch.setattr(launcher, "Workspace", FakeWorkspace)
    monkeypatch.setattr(launcher.uvicorn, "run", lambda *a, **k: None)
    # main() writes os.environ directly; registering the variable with monkeypatch first guarantees it is restored
    # after the test, so later tests never see the dashboard's default (the real runtime database).
    monkeypatch.setenv("CLAIMSLEUTH_BLACKBOARD_DB", "registered-for-restore")
    if preset is None:
        monkeypatch.delenv("CLAIMSLEUTH_BLACKBOARD_DB", raising=False)
    else:
        monkeypatch.setenv("CLAIMSLEUTH_BLACKBOARD_DB", preset)
    assert launcher.main(argv) == 0
    want = str(launcher.DEFAULT_BLACKBOARD) if expected == "DEFAULT" else expected
    assert seen["at_start"] == want == launcher.os.environ["CLAIMSLEUTH_BLACKBOARD_DB"]


# --------------------------------------------------------------------------- registration, isolation, validation

def test_registration_binds_run_to_one_claim_and_documents(board, w):
    assert board.register("RUN-1", "CS-001", FP, writer=w("orchestrator")) == []        # same details: no-op
    with pytest.raises(BlackboardRejected, match="already registered"):
        board.register("RUN-1", "CS-002", FP, writer=w("orchestrator"))
    with pytest.raises(BlackboardRejected, match="already registered"):
        board.register("RUN-1", "CS-001", OTHER_FP, writer=w("orchestrator"))
    state = board.state("RUN-1", "CS-001")
    assert {a: s.state for a, s in state.agents.items()} == {
        "evidence_agent": "PENDING", "investigator_agent": "PENDING", "forensic_agent": "PENDING"}
    assert {s.framework for s in state.agents.values()} == {"adk", "crewai"}


def test_runs_and_claims_are_isolated(board, w):
    board.register("RUN-2", "CS-002", OTHER_FP, writer=w("orchestrator"))
    board.post("RUN-1", "CS-001", "evidence_agent", "adk", "report:evidence_agent", {"x": 1}, FP, writer=w("adk"))
    assert [e.section for e in board.entries("RUN-2", "CS-002") if e.kind == "post"] == []
    with pytest.raises(BlackboardRejected, match="belongs to claim CS-001"):
        board.entries("RUN-1", "CS-002")
    with pytest.raises(BlackboardRejected, match="belongs to claim"):
        board.post("RUN-1", "CS-002", "evidence_agent", "adk", "report:evidence_agent", {"x": 1}, FP, writer=w("adk"))
    with pytest.raises(BlackboardRejected, match="not registered"):
        board.post("RUN-9", "CS-001", "evidence_agent", "adk", "report:evidence_agent", {"x": 1}, FP, writer=w("adk"))
    assert all(e.run_id == "RUN-2" and e.claim_id == "CS-002" for e in board.entries("RUN-2", "CS-002"))


def test_document_fingerprints_are_checked(board, w):
    with pytest.raises(BlackboardRejected, match="fingerprint"):
        board.post("RUN-1", "CS-001", "forensic_agent", "crewai", "report:forensic_agent", {"x": 1}, OTHER_FP,
                   writer=w("crewai"))
    with pytest.raises(BlackboardRejected, match="fingerprint is required"):
        board.set_status("RUN-1", "CS-001", "forensic_agent", "RUNNING", "forensic_agent", "crewai", None,
                         writer=w("crewai"))
    # A failure before the documents were loaded may be reported without a fingerprint.
    assert board.set_status("RUN-1", "CS-001", "forensic_agent", "NOT_CONFIGURED", "forensic_agent", "crewai",
                            None, writer=w("crewai")).payload["state"] == "NOT_CONFIGURED"


def test_fingerprint_is_order_independent():
    docs = [SimpleNamespace(doc_id="invoice", sha256="1" * 64), SimpleNamespace(doc_id="repair_report", sha256="2" * 64)]
    assert document_fingerprint(docs) == document_fingerprint(list(reversed(docs)))
    assert document_fingerprint(docs) != document_fingerprint(docs[:1])


# --------------------------------------------------------------------------- transitions and idempotency

def test_status_transitions_are_enforced(board, w):
    s = lambda state: board.set_status("RUN-1", "CS-001", "evidence_agent", state, "evidence_agent", "adk", FP,
                                       writer=w("adk"))
    first = s("RUNNING")
    assert s("RUNNING").seq == first.seq                                              # idempotent repeat
    done = s("COMPLETED")
    assert s("COMPLETED").seq == done.seq
    for bad in ("RUNNING", "FAILED", "PENDING"):
        with pytest.raises(BlackboardRejected, match="cannot move from COMPLETED"):
            s(bad)


def test_duplicate_and_contradictory_posts(board, w):
    post = lambda payload: board.post("RUN-1", "CS-001", "forensic_agent", "crewai", "report:forensic_agent",
                                      payload, FP, writer=w("crewai"))
    first = post({"finding": "serial mismatch"})
    count = board.state("RUN-1", "CS-001").entries
    assert post({"finding": "serial mismatch"}).seq == first.seq                       # duplicate submission
    assert board.state("RUN-1", "CS-001").entries == count
    with pytest.raises(BlackboardRejected, match="already holds different content"):
        post({"finding": "something else"})
    retract = lambda reason: board.retract("RUN-1", "CS-001", first.run_seq, "controller", "orchestrator", reason,
                                           writer=w("orchestrator"))
    r1 = retract("superseded")
    assert retract("again").seq == r1.seq
    second = post({"finding": "something else"})                                       # allowed after retraction
    state = board.state("RUN-1", "CS-001")
    assert [p.seq for p in state.live_posts] == [second.seq] and [p.seq for p in state.retracted] == [first.seq]
    assert state.retractions[0].ref_seq == first.seq and state.retractions[0].payload["reason"] == "superseded"


def test_no_result_after_a_failure(board, w):
    board.set_status("RUN-1", "CS-001", "forensic_agent", "FAILED", "forensic_agent", "crewai", FP, writer=w("crewai"))
    with pytest.raises(BlackboardRejected, match="cannot publish a result"):
        board.post("RUN-1", "CS-001", "forensic_agent", "crewai", "report:forensic_agent", {"x": 1}, FP,
                   writer=w("crewai"))


def test_history_is_append_only_in_the_database(board, db, w):
    board.post("RUN-1", "CS-001", "evidence_agent", "adk", "report:evidence_agent", {"x": 1}, FP, writer=w("adk"))
    conn = sqlite3.connect(db)
    for sql in ("UPDATE entries SET payload = '{}'", "DELETE FROM entries", "UPDATE runs SET claim_id = 'X'",
                "DELETE FROM runs"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(sql)
    conn.close()


def test_persistence_after_reopening(board, db, w):
    board.set_status("RUN-1", "CS-001", "evidence_agent", "RUNNING", "evidence_agent", "adk", FP, writer=w("adk"))
    before = board.entries("RUN-1", "CS-001")
    reopened = Blackboard(db)
    assert reopened.entries("RUN-1", "CS-001") == before
    nxt = reopened.post("RUN-1", "CS-001", "evidence_agent", "adk", "report:evidence_agent", {"x": 1}, FP,
                        writer=w("adk"))
    assert nxt.run_seq == before[-1].run_seq + 1 and nxt.auth == "valid"
    assert reopened.state("RUN-1", "CS-001").agents["evidence_agent"].state == "RUNNING"


# --------------------------------------------------------------------------- writer authentication

def test_writers_must_authenticate_and_keep_to_their_identity(board, db, w):
    post = lambda author, writer: board.post("RUN-1", "CS-001", author, "x", f"report:{author}", {"x": 1}, FP,
                                             writer=writer)
    with pytest.raises(BlackboardRejected, match="authentication failed"):
        post("forensic_agent", Writer("crewai", b"\x00" * 32))                        # wrong key
    with pytest.raises(BlackboardRejected, match="may not write as 'evidence_agent'"):
        post("evidence_agent", w("crewai"))                                            # CrewAI posing as ADK
    with pytest.raises(BlackboardRejected, match="may not write as 'adjudicator'"):
        post("adjudicator", w("adk"))                                                  # an agent posing as adjudicator
    with pytest.raises(BlackboardRejected, match="only the orchestrator may seal"):
        board.monitor("RUN-1", "CS-001", "evidence_agent", "adk", "controller:seal", {}, writer=w("adk"))
    with pytest.raises(BlackboardRejected, match="authenticated writer is required"):
        board.post("RUN-1", "CS-001", "forensic_agent", "crewai", "report:forensic_agent", {"x": 1}, FP, writer=None)
    ok = post("forensic_agent", w("crewai"))
    assert (ok.principal, ok.auth) == ("crewai", "valid") and ok.signature
    assert {e.auth for e in board.entries("RUN-1", "CS-001")} == {"valid"}
    assert sorted(p.name for p in key_dir(db).iterdir()) == ["adk.key", "crewai.key", "orchestrator.key"]


def test_unprovisioned_writers_are_refused(tmp_path):
    db = tmp_path / "new.sqlite3"
    Blackboard(db)                                                                     # database but no keys
    with pytest.raises(Exception, match="no credentials provisioned"):
        load_writer(db, "crewai")


def test_forged_and_tampered_entries_are_detected(board, db, w):
    real = board.post("RUN-1", "CS-001", "forensic_agent", "crewai", "report:forensic_agent", {"finding": "f"}, FP,
                      writer=w("crewai"))
    conn = sqlite3.connect(db)
    # A row inserted with raw SQL (no key): stored, but reported as unsigned.
    conn.execute("INSERT INTO entries (run_id, run_seq, claim_id, ts, kind, section, author, framework, process, "
                 "payload, payload_sha256) VALUES ('RUN-1', 99, 'CS-001', '2026-11-01T00:00:00+00:00', 'post', "
                 "'report:evidence_agent', 'evidence_agent', 'adk', '{\"pid\": 1, \"environment\": \"x\"}', "
                 "'{\"forged\": true}', 'x')")
    # A signed row edited behind the triggers' back: its signature no longer matches.
    conn.execute("DROP TRIGGER entries_no_update")
    conn.execute("UPDATE entries SET payload = '{\"finding\": \"changed\"}' WHERE seq = ?", (real.seq,))
    conn.commit()
    conn.close()
    by_seq = {e.run_seq: e for e in board.entries("RUN-1", "CS-001")}
    assert by_seq[99].auth == "unsigned" and by_seq[real.run_seq].auth == "invalid"
    assert board.state("RUN-1", "CS-001").authentication == {"valid": real.run_seq - 1, "invalid": 1, "unsigned": 1}


def test_databases_from_before_authentication_are_migrated_without_losing_entries(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE entries (seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(run_id),
            run_seq INTEGER NOT NULL, claim_id TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL, section TEXT NOT NULL,
            author TEXT NOT NULL, framework TEXT NOT NULL, process TEXT NOT NULL, fingerprint TEXT, payload TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL, ref_seq INTEGER, UNIQUE (run_id, run_seq));
        INSERT INTO runs VALUES ('RUN-old', 'CS-001', 'ff', '2026-11-01T00:00:00+00:00');
        INSERT INTO entries (run_id, run_seq, claim_id, ts, kind, section, author, framework, process, payload,
            payload_sha256) VALUES ('RUN-old', 1, 'CS-001', '2026-11-01T00:00:00+00:00', 'register', 'run',
            'controller', 'orchestrator', '{"pid": 1, "environment": ".venv"}', '{}', 'x');
    """)
    conn.close()
    board = Blackboard(db)
    old = board.entries("RUN-old", "CS-001")
    assert len(old) == 1 and old[0].auth == "unsigned" and old[0].principal is None
    provision(db)
    board.register("RUN-new", "CS-001", FP, writer=load_writer(db, "orchestrator"))
    assert {e.auth for e in board.entries("RUN-new", "CS-001")} == {"valid"}


# --------------------------------------------------------------------------- real concurrent processes

WRITER = r"""
import sys, time
from pathlib import Path
from claimsleuth.blackboard import Blackboard, BlackboardRejected, load_writer
db, name, start, n, principal = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
board = Blackboard(db)
writer = load_writer(db, principal)
author = "forensic_agent" if principal == "crewai" else "controller"
while not start.exists():
    time.sleep(0.01)
for i in range(n):
    board.monitor("RUN-1", "CS-001", author, "test", f"tick:{name}", {"i": i}, writer=writer)
crew = load_writer(db, "crewai")
try:
    board.set_status("RUN-1", "CS-001", "forensic_agent", "RUNNING", "forensic_agent", "test", "a" * 64, writer=crew)
    final = "COMPLETED" if name.endswith("0") else "FAILED"
    board.set_status("RUN-1", "CS-001", "forensic_agent", final, "forensic_agent", "test", "a" * 64, writer=crew)
    print("won", final)
except BlackboardRejected as exc:
    print("rejected", exc)
"""


def test_concurrent_writers_in_separate_processes(board, db, tmp_path):
    start, n = tmp_path / "go", 25
    pythons = [(sys.executable, "orchestrator")] * 4 + ([(str(CREW_PYTHON), "crewai")] if CREW_PYTHON.exists() else [])
    procs = [subprocess.Popen([py, "-c", WRITER, str(db), f"writer{i}", str(start), str(n), principal], cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
             for i, (py, principal) in enumerate(pythons)]
    time.sleep(1.5)                     # let every process import and open the database
    start.touch()
    outputs = [p.communicate(timeout=180)[0] for p in procs]
    assert all(p.returncode == 0 for p in procs), outputs
    entries = board.entries("RUN-1", "CS-001")
    ticks = [e for e in entries if e.kind == "monitor"]
    assert len(ticks) == n * len(pythons)                                             # no lost writes
    assert [e.run_seq for e in entries] == list(range(1, len(entries) + 1))           # contiguous, no duplicates
    assert len({e.process.pid for e in ticks}) == len(pythons)
    assert {e.auth for e in entries} == {"valid"}
    if CREW_PYTHON.exists():
        assert {e.process.environment for e in ticks} == {".venv", ".venv-crew"}
    finals = [e for e in entries if e.section == "status:forensic_agent" and e.payload["state"] in ("COMPLETED", "FAILED")]
    assert len(finals) == 1                                                           # one final state only
    assert sum(o.count("won") for o in outputs) == 1, outputs
    assert board.state("RUN-1", "CS-001").agents["forensic_agent"].state == finals[0].payload["state"]


CRASH = r"""
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("BEGIN IMMEDIATE")
conn.execute("INSERT INTO entries (run_id, run_seq, claim_id, ts, kind, section, author, framework, process, "
             "payload, payload_sha256) VALUES ('RUN-1', 999, 'CS-001', 'now', 'post', 'half', 'x', 'x', '{}', '{}', 'x')")
os._exit(1)        # the process dies inside the transaction, before COMMIT
"""


def test_a_process_dying_mid_write_leaves_no_partial_entry(board, db, w):
    proc = subprocess.run([sys.executable, "-c", CRASH, str(db)], cwd=ROOT, timeout=60)
    assert proc.returncode == 1
    assert [e for e in board.entries("RUN-1", "CS-001") if e.section == "half"] == []
    assert board.post("RUN-1", "CS-001", "evidence_agent", "adk", "report:evidence_agent", {"x": 1}, FP,
                      writer=w("adk")).run_seq                                         # lock released


HOLD = r"""
import sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("BEGIN IMMEDIATE")
print("locked", flush=True)
time.sleep(float(sys.argv[2]))
conn.execute("ROLLBACK")
"""


def _hold_lock(db, seconds):
    proc = subprocess.Popen([sys.executable, "-c", HOLD, str(db), str(seconds)], cwd=ROOT, stdout=subprocess.PIPE,
                            text=True)
    assert proc.stdout.readline().strip() == "locked"
    return proc


def test_contention_waits_for_a_short_lock(board, db, w):
    holder = _hold_lock(db, 1.0)
    t0 = time.monotonic()
    entry = board.post("RUN-1", "CS-001", "evidence_agent", "adk", "report:evidence_agent", {"x": 1}, FP,
                       writer=w("adk"))
    assert entry.run_seq and time.monotonic() - t0 >= 0.5                       # waited, then succeeded
    holder.wait(timeout=30)


def test_contention_beyond_the_busy_timeout_is_an_explicit_error(board, db, w):
    holder = _hold_lock(db, 3.0)
    impatient = Blackboard(db, busy_timeout_s=0.3)
    with pytest.raises(BlackboardError, match="locked"):
        impatient.post("RUN-1", "CS-001", "evidence_agent", "adk", "report:evidence_agent", {"x": 1}, FP,
                       writer=w("adk"))
    holder.wait(timeout=30)
    assert [e for e in board.entries("RUN-1", "CS-001") if e.kind == "post"] == []


# --------------------------------------------------------------------------- participants and controller

class Report(BaseModel):
    claim_id: str
    finding: str


def result(status, report=None, evidence=None, errors=()):
    return SimpleNamespace(status=status, report=report, evidence_report=evidence, errors=list(errors), agent_runs=[])


DOCS = [SimpleNamespace(doc_id="invoice", sha256="1" * 64)]


def participants(db):
    return (AgentParticipant("RUN-c", "CS-001", "adk", ("evidence_agent", "investigator_agent"), path=db),
            AgentParticipant("RUN-c", "CS-001", "crewai", ("forensic_agent",), path=db))


FINAL = SimpleNamespace(final_decision=ExecutionStatus.COMPLETED, decided_by="DETERMINISTIC_RULES",
                        deterministic_decision=None, requires_human_approval=False, approval_status="NOT_REQUIRED",
                        payout=None, model_dump_json=lambda: "{}")


def test_controller_tracks_completion_and_publishes_the_decision(db):
    ctl = RunController("RUN-c", "CS-001", path=db)
    ctl.register(DOCS)
    adk_side, crew_side = participants(db)
    ev, inv, fo = Report(claim_id="CS-001", finding="e"), Report(claim_id="CS-001", finding="i"), Report(claim_id="CS-001", finding="f")
    adk_side.running(DOCS), crew_side.running(DOCS)
    adk_res, crew_res = result(ExecutionStatus.COMPLETED, inv, ev), result(ExecutionStatus.COMPLETED, fo)
    adk_side.finished(adk_res), crew_side.finished(crew_res)
    crew_side.finished(crew_res)                                                       # a retry: idempotent
    summary = ctl.collect(adk_res, crew_res, [])
    assert summary["completed"] == ["evidence_agent", "investigator_agent", "forensic_agent"]
    assert summary["findings"] == [] and summary["failed"] == summary["unfinished"] == []
    assert all(r["matches_received"] for r in summary["agents"]) and set(summary["authentication"]) == {"valid"}
    ctl.publish_decision(FINAL)
    ctl.publish_decision(FINAL)                                                        # idempotent
    state = Blackboard(db).state("RUN-c", "CS-001")
    assert [p.section for p in state.live_posts] == ["report:evidence_agent", "report:investigator_agent",
                                                     "report:forensic_agent", "decision:final"]
    assert state.live_posts[2].payload["report_sha256"] == report_sha256(fo)
    assert [m.section for m in state.monitor] == ["controller:seal", "controller:completion"]
    assert state.sealed_at == state.monitor[0].run_seq and state.late == []
    assert adk_side.failures == crew_side.failures == ctl.failures == []


def test_report_not_received_by_the_adjudicator_is_retracted(db):
    ctl = RunController("RUN-c", "CS-001", path=db)
    ctl.register(DOCS)
    adk_side, crew_side = participants(db)
    adk_side.running(DOCS), crew_side.running(DOCS)
    fo = Report(claim_id="CS-001", finding="f")
    crew_side.finished(result(ExecutionStatus.COMPLETED, fo))                          # the CrewAI process finished ...
    adk_res = result(ExecutionStatus.COMPLETED, Report(claim_id="CS-001", finding="i"), Report(claim_id="CS-001", finding="e"))
    adk_side.finished(adk_res)
    received_crew = result(ExecutionStatus.FAILED, errors=["A2A request timed out"])   # ... but A2A failed
    summary = ctl.collect(adk_res, received_crew, ["A2A request timed out after 30 s"])
    assert any("forensic_agent: published report retracted" in f and "timed out" in f for f in summary["findings"])
    state = Blackboard(db).state("RUN-c", "CS-001")
    assert [p.section for p in state.retracted] == ["report:forensic_agent"]
    assert state.agents["forensic_agent"].state == "COMPLETED"                         # history is kept as it was


def test_late_crewai_report_is_kept_but_cannot_change_the_completed_adjudication(db):
    ctl = RunController("RUN-c", "CS-001", path=db)
    ctl.register(DOCS)
    adk_side, crew_side = participants(db)
    adk_side.running(DOCS), crew_side.running(DOCS)
    adk_res = result(ExecutionStatus.COMPLETED, Report(claim_id="CS-001", finding="i"), Report(claim_id="CS-001", finding="e"))
    adk_side.finished(adk_res)
    timed_out = result(ExecutionStatus.FAILED, errors=["A2A timeout"])
    summary = ctl.collect(adk_res, timed_out, ["A2A request timed out after 30 s"])     # seal: inputs fixed
    assert summary["unfinished"] == ["forensic_agent"]
    ctl.publish_decision(FINAL)
    crew_side.finished(result(ExecutionStatus.COMPLETED, Report(claim_id="CS-001", finding="late")))  # arrives late
    state = Blackboard(db).state("RUN-c", "CS-001")
    late = [(e.kind, e.section) for e in state.late]
    assert late == [("post", "report:forensic_agent"), ("status", "status:forensic_agent")]
    assert "report:forensic_agent" not in [p.section for p in state.live_posts]       # never a live result
    forensic = state.agents["forensic_agent"]
    assert (forensic.state, forensic.late, forensic.state_at_seal) == ("COMPLETED", True, "RUNNING")
    decision = next(p for p in state.live_posts if p.section == "decision:final")
    assert decision.run_seq < min(e.run_seq for e in state.late)                       # the decision came first
    assert [f for f in crew_side.failures if f.startswith("late")]                    # reported explicitly
    assert any(r["where"].startswith("late") for r in read_monitoring_failures(db, "RUN-c"))


def test_failures_and_missing_participants_are_tracked(db):
    ctl = RunController("RUN-c", "CS-001", path=db)
    ctl.register(DOCS)
    adk_side, _ = participants(db)
    adk_side.running(DOCS)
    adk_res = result(ExecutionStatus.FAILED, errors=["investigator_agent produced no structured report"])
    adk_side.finished(adk_res)
    # The CrewAI process never took part (died, or ran without the blackboard), yet A2A delivered a report.
    crew_res = result(ExecutionStatus.COMPLETED, Report(claim_id="CS-001", finding="f"))
    summary = ctl.collect(adk_res, crew_res, [])
    assert summary["failed"] == ["evidence_agent", "investigator_agent"] and summary["unfinished"] == ["forensic_agent"]
    assert any("forensic_agent: no final status" in f for f in summary["findings"])
    assert any("did not publish" in f for f in summary["findings"])
    failed = [e for e in Blackboard(db).entries("RUN-c", "CS-001") if e.payload.get("state") == "FAILED"]
    assert failed and failed[0].payload["errors"] == ["investigator_agent produced no structured report"]
    assert Blackboard(db).state("RUN-c", "CS-001").agents["forensic_agent"].state == "PENDING"


def test_wrong_documents_are_rejected_and_reported_as_monitoring_failures(db, capsys):
    ctl = RunController("RUN-c", "CS-001", path=db)
    ctl.register(DOCS)
    crew_side = AgentParticipant("RUN-c", "CS-001", "crewai", ("forensic_agent",), path=db)
    other_docs = [SimpleNamespace(doc_id="invoice", sha256="9" * 64)]
    crew_side.running(other_docs)                                                      # never raises
    crew_side.finished(result(ExecutionStatus.COMPLETED, Report(claim_id="CS-001", finding="f")))
    assert any("documents" in f for f in crew_side.failures)
    assert "BLACKBOARD MONITORING FAILURE" in capsys.readouterr().err
    assert Blackboard(db).state("RUN-c", "CS-001").agents["forensic_agent"].state == "PENDING"   # nothing accepted
    recorded = read_monitoring_failures(db, "RUN-c")
    assert recorded and all(r["run_id"] == "RUN-c" for r in recorded)
    summary = ctl.collect(result(ExecutionStatus.FAILED), result(ExecutionStatus.FAILED), [])
    assert len(summary["monitoring_failures"]) == len(recorded)


def test_an_unusable_database_never_breaks_the_caller(tmp_path, capsys):
    blocked = tmp_path / "not_a_db"
    blocked.mkdir()                                                                    # a directory, not a file
    side = AgentParticipant("RUN-c", "CS-001", "adk", ("evidence_agent",), path=blocked)
    side.running(DOCS)
    side.finished(result(ExecutionStatus.COMPLETED, Report(claim_id="CS-001", finding="e")))
    assert side.failures and side.failures[0].startswith("open:")
    assert "BLACKBOARD MONITORING FAILURE [open]" in capsys.readouterr().err
    ctl = RunController("RUN-c", "CS-001", path=blocked)
    ctl.register(DOCS)
    assert ctl.collect(result(ExecutionStatus.FAILED), result(ExecutionStatus.FAILED), []) is None
    assert ctl.failures
    logged = [json.loads(x) for x in monitoring_log(blocked).read_text(encoding="utf-8").splitlines()]
    assert [r["where"] for r in logged][:1] == ["open"] and all(r["run_id"] == "RUN-c" for r in logged)
    view = run_monitor("RUN-c", path=blocked)                                          # visible on the dashboard
    assert view["enabled"] and view["error"] and len(view["monitoring_failures"]) == len(logged)


# --------------------------------------------------------------------------- ADK lifecycle plugin

def test_adk_plugin_publishes_each_agents_own_start_and_end(db):
    from claimsleuth.blackboard.adk_plugin import BlackboardLifecyclePlugin
    ctl = RunController("RUN-c", "CS-001", path=db)
    ctl.register(DOCS)
    adk_side, _ = participants(db)
    adk_side.running(DOCS, mark_running=False)
    plugin = BlackboardLifecyclePlugin(adk_side)
    ctx = SimpleNamespace(invocation_id="inv-1")
    call = lambda hook, name: asyncio.run(getattr(plugin, hook)(agent=SimpleNamespace(name=name), callback_context=ctx))
    assert call("before_agent_callback", "claimsleuth_adk_investigation") is None     # the workflow itself: ignored
    assert call("before_agent_callback", "evidence_agent") is None                    # never changes execution
    state = Blackboard(db).state("RUN-c", "CS-001")
    assert state.agents["evidence_agent"].state == "RUNNING" and state.agents["investigator_agent"].state == "PENDING"
    assert call("after_agent_callback", "evidence_agent") is None
    assert call("before_agent_callback", "investigator_agent") is None
    entries = Blackboard(db).entries("RUN-c", "CS-001")
    events = [(e.author, e.payload.get("event")) for e in entries if e.payload.get("event")]
    assert events == [("evidence_agent", "agent_started"), ("evidence_agent", "agent_finished"),
                      ("investigator_agent", "agent_started")]
    assert all(e.principal == "adk" and e.auth == "valid" for e in entries if e.payload.get("event"))


# --------------------------------------------------------------------------- monitoring view

def test_monitoring_view(db, monkeypatch):
    monkeypatch.delenv("CLAIMSLEUTH_BLACKBOARD_DB", raising=False)
    assert run_monitor("RUN-c") == {"enabled": False, "run_id": "RUN-c"}
    ctl = RunController("RUN-c", "CS-001", path=db)
    ctl.register(DOCS)
    adk_side, crew_side = participants(db)
    adk_side.running(DOCS), crew_side.running(DOCS)
    adk_res = result(ExecutionStatus.COMPLETED, Report(claim_id="CS-001", finding="i"), Report(claim_id="CS-001", finding="e"))
    crew_res = result(ExecutionStatus.COMPLETED, Report(claim_id="CS-001", finding="f"))
    adk_side.finished(adk_res), crew_side.finished(crew_res)
    ctl.collect(adk_res, crew_res, [])
    ctl.publish_decision(FINAL)
    view = run_monitor("RUN-c", path=db, report_decision=FINAL.final_decision.value)
    assert (view["run_id"], view["claim_id"], view["registered"]) == ("RUN-c", "CS-001", True)
    assert {a["agent"]: a["state"] for a in view["agents"]} == {"evidence_agent": "COMPLETED",
                                                                 "investigator_agent": "COMPLETED",
                                                                 "forensic_agent": "COMPLETED"}
    assert view["decision"]["final_decision"] == "COMPLETED" and view["decision_matches_report"] is True
    assert view["events"][-1]["section"] == "decision:final" and view["authentication"] == {"valid": view["entries"]}
    assert view["monitoring_failures"] == [] and view["late"] == []
    assert run_monitor("RUN-c", path=db, report_decision="REJECT")["decision_matches_report"] is False
    assert run_monitor("RUN-unknown", path=db)["registered"] is False


def test_dashboard_route_is_read_only_and_follows_the_setting(db, monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    from claimsleuth.dashboard.app import create_app
    from claimsleuth.dashboard.services import ServiceConfig
    from claimsleuth.dashboard.workspace import Workspace
    ws = Workspace(tmp_path / "dash", source_data=ROOT / "data")
    monkeypatch.delenv("CLAIMSLEUTH_BLACKBOARD_DB", raising=False)
    with TestClient(create_app(ws, ServiceConfig(mcp_port=1, crew_port=2), tmp_path / "rt")) as client:
        assert client.get("/api/runs/RUN-..%2F..%2Fx/blackboard").status_code == 404
        assert client.get("/api/runs/RUN-none-1/blackboard").json() == {"enabled": False, "run_id": "RUN-none-1"}
        RunController("RUN-c-1", "CS-001", path=db).register(DOCS)
        monkeypatch.setenv("CLAIMSLEUTH_BLACKBOARD_DB", str(db))
        before = db.stat().st_size
        view = client.get("/api/runs/RUN-c-1/blackboard").json()
        assert view["registered"] and view["claim_id"] == "CS-001" and db.stat().st_size == before
