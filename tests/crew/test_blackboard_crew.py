"""The CrewAI side of the shared coordination blackboard, run inside .venv-crew against a real MCP server."""
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("crewai", reason="CrewAI tests run in .venv-crew")

from claimsleuth.blackboard import (ENV_VAR, Blackboard, RunController, document_fingerprint, load_writer,  # noqa: E402
                                    provision)
from claimsleuth.blackboard.controller import read_monitoring_failures, report_sha256  # noqa: E402
from claimsleuth.crew_service.runner import run_forensic_investigation  # noqa: E402
from claimsleuth.datastore import load_claim  # noqa: E402
from claimsleuth.schemas import ExecutionStatus  # noqa: E402


@pytest.fixture
def board_db(tmp_path, monkeypatch):
    db = tmp_path / "blackboard.sqlite3"
    monkeypatch.setenv(ENV_VAR, str(db))
    provision(db)
    return db


def register(db, run_id, claim_id, fingerprint):
    Blackboard(db).register(run_id, claim_id, fingerprint, writer=load_writer(db, "orchestrator"))


def test_crewai_side_reads_the_registration_and_publishes_its_result(mcp, dataset, board_db):
    _, documents = load_claim("CS-003", dataset[0])
    register(board_db, "RUN-bbcrew-1", "CS-003", document_fingerprint(documents))
    result = run_forensic_investigation("CS-003", run_id="RUN-bbcrew-1", mcp_url=mcp.url, data_dir=dataset[0])
    assert result.status == ExecutionStatus.COMPLETED
    board = Blackboard(board_db)
    entries = [e for e in board.entries("RUN-bbcrew-1", "CS-003") if e.author == "forensic_agent"]
    assert [(e.kind, e.payload.get("state")) for e in entries] == [("status", "RUNNING"), ("post", None),
                                                                   ("status", "COMPLETED")]
    assert {(e.process.pid, e.process.environment, e.framework, e.principal, e.auth) for e in entries} == {
        (os.getpid(), ".venv-crew", "crewai", "crewai", "valid")}
    assert entries[1].payload["report_sha256"] == report_sha256(result.report)
    assert board.state("RUN-bbcrew-1", "CS-003").agents["evidence_agent"].state == "PENDING"   # not this side's
    assert read_monitoring_failures(board_db, "RUN-bbcrew-1") == []


def test_a_rejected_crewai_post_never_changes_the_forensic_result(mcp, dataset, board_db, monkeypatch):
    register(board_db, "RUN-bbcrew-2", "CS-003", "0" * 64)                              # registered with other documents
    with_board = run_forensic_investigation("CS-003", run_id="RUN-bbcrew-2", mcp_url=mcp.url, data_dir=dataset[0])
    monkeypatch.setenv(ENV_VAR, "off")
    without = run_forensic_investigation("CS-003", run_id="RUN-bbcrew-3", mcp_url=mcp.url, data_dir=dataset[0])
    assert with_board.status == without.status == ExecutionStatus.COMPLETED
    assert with_board.report.model_dump() == without.report.model_dump()
    failures = read_monitoring_failures(board_db, "RUN-bbcrew-2")
    assert failures and any("documents" in f["error"] for f in failures)
    assert Blackboard(board_db).state("RUN-bbcrew-2", "CS-003").agents["forensic_agent"].state == "PENDING"


def test_a_late_crewai_report_is_kept_but_marked_and_not_used(mcp, dataset, board_db):
    """The orchestrator gave up on the CrewAI side (an A2A time-out) and fixed the adjudicator's inputs; the real
    CrewAI run then finishes and publishes. Its entries stay on the blackboard, flagged late, never live results."""
    _, documents = load_claim("CS-003", dataset[0])
    ctl = RunController("RUN-bbcrew-late", "CS-003", path=board_db)
    ctl.register(documents)
    failed = SimpleNamespace(status=ExecutionStatus.FAILED, report=None, evidence_report=None, errors=[])
    summary = ctl.collect(failed, failed, ["A2A request timed out after 30 s"])          # seal
    assert "forensic_agent" in summary["unfinished"]                                  # (no ADK side ran here either)
    result = run_forensic_investigation("CS-003", run_id="RUN-bbcrew-late", mcp_url=mcp.url, data_dir=dataset[0])
    assert result.status == ExecutionStatus.COMPLETED                                   # the CrewAI run itself is unchanged
    state = Blackboard(board_db).state("RUN-bbcrew-late", "CS-003")
    assert [(e.kind, e.payload.get("state")) for e in state.late] == [("status", "RUNNING"), ("post", None),
                                                                      ("status", "COMPLETED")]
    assert all(e.process.environment == ".venv-crew" and e.run_seq > state.sealed_at for e in state.late)
    assert "report:forensic_agent" not in [p.section for p in state.live_posts]
    forensic = state.agents["forensic_agent"]
    assert (forensic.state, forensic.late, forensic.state_at_seal) == ("COMPLETED", True, "PENDING")
    late_failures = [f for f in read_monitoring_failures(board_db, "RUN-bbcrew-late") if f["where"].startswith("late")]
    assert late_failures and all(f["environment"] == ".venv-crew" for f in late_failures)
