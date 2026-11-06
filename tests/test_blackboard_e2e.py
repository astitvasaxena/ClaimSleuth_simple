"""Blackboard end to end: this process (ADK side, .venv) and the real CrewAI A2A service process (.venv-crew) share
one persistent SQLite blackboard, selected only by CLAIMSLEUTH_BLACKBOARD_DB (inherited by the service process).
The deterministic adjudicator's outcome is compared with the ground truth and with the same run without a blackboard.
"""
import json
import os
from types import SimpleNamespace

import pytest

from claimsleuth.blackboard import ENV_VAR, Blackboard
from claimsleuth.blackboard import run_monitor
from claimsleuth.blackboard.controller import read_monitoring_failures, report_sha256
from claimsleuth.integration.run import run_claim
from test_integration_e2e import CREW_PYTHON, CrewService
from test_mcp_server import RunningServer


@pytest.fixture(scope="module")
def shared(dataset, tmp_path_factory):
    if not CREW_PYTHON.exists():
        pytest.skip("needs .venv-crew for the CrewAI A2A service")
    tmp = tmp_path_factory.mktemp("bb_e2e")
    db = tmp / "blackboard.sqlite3"
    previous = os.environ.get(ENV_VAR)
    os.environ[ENV_VAR] = str(db)                       # this process and the CrewAI service (inherits it)
    mcp = RunningServer(dataset[0], dataset[1], tmp)
    try:
        crew = CrewService(dataset[0], mcp.url, tmp)
    except Exception:
        mcp.stop()
        raise
    try:
        yield SimpleNamespace(db=db, mcp=mcp, crew=crew, data_dir=dataset[0], db_path=dataset[1],
                              truth=json.loads((dataset[0] / "ground_truth.json").read_text(encoding="utf-8")))
    finally:
        crew.stop()
        mcp.stop()
        if previous is None:
            os.environ.pop(ENV_VAR, None)
        else:
            os.environ[ENV_VAR] = previous


_runs: dict = {}


def run(env, claim_id, suffix="bb"):
    key = (claim_id, suffix)
    if key not in _runs:
        _runs[key] = run_claim(claim_id, run_id=f"RUN-{suffix}-{claim_id}", mcp_url=env.mcp.url,
                               crew_card_url=env.crew.card, data_dir=env.data_dir, db_path=env.db_path)
    return _runs[key]


def test_adk_and_crewai_processes_share_one_persistent_blackboard(shared):
    bundle = run(shared, "CS-003")
    board = Blackboard(shared.db)                       # a fresh connection: everything was persisted
    entries = board.entries(bundle.run_id, "CS-003")
    by_author = {}
    for e in entries:
        by_author.setdefault(e.author, set()).add((e.process.pid, e.process.environment, e.framework))
    assert by_author["evidence_agent"] == by_author["investigator_agent"] == {(os.getpid(), ".venv", "adk")}
    (crew_pid, crew_env, crew_fw), = by_author["forensic_agent"]
    assert crew_env == ".venv-crew" and crew_fw == "crewai" and crew_pid != os.getpid()
    state = board.state(bundle.run_id, "CS-003")
    assert {a: s.state for a, s in state.agents.items()} == {
        "evidence_agent": "COMPLETED", "investigator_agent": "COMPLETED", "forensic_agent": "COMPLETED"}
    # Each side read the registration and checked its documents: every write carries the registered fingerprint.
    assert {e.fingerprint for e in entries if e.kind in ("status", "post")} == {state.fingerprint}
    assert read_monitoring_failures(shared.db, bundle.run_id) == []
    # The report the CrewAI process published is byte-for-byte the one A2A delivered to the adjudicator.
    forensic = next(p for p in state.live_posts if p.section == "report:forensic_agent")
    assert forensic.payload["report_sha256"] == report_sha256(bundle.crewai.report)
    assert state.retracted == [] and state.late == []
    # Every writer authenticated: the ADK and CrewAI processes each signed with their own key.
    assert {e.auth for e in entries} == {"valid"}
    assert {(e.author, e.principal) for e in entries if e.author.endswith("_agent")} == {
        ("evidence_agent", "adk"), ("investigator_agent", "adk"), ("forensic_agent", "crewai")}


def test_each_adk_agent_publishes_its_own_start_and_end_during_the_workflow(shared):
    bundle = run(shared, "CS-003")
    entries = Blackboard(shared.db).entries(bundle.run_id, "CS-003")
    events = [(e.author, e.kind, e.payload.get("event") or e.payload.get("state")) for e in entries
              if e.framework == "adk"]
    # The Evidence report is published as soon as the Evidence Agent finishes, and the Investigator consumes it from
    # the blackboard (signed receipt) before it starts - not after the whole ADK workflow, as in direct mode.
    assert events == [("evidence_agent", "status", "agent_started"), ("evidence_agent", "monitor", "agent_finished"),
                      ("evidence_agent", "post", None), ("evidence_agent", "status", "report_published"),
                      ("investigator_agent", "monitor", "context_consumed"),
                      ("investigator_agent", "status", "agent_started"),
                      ("investigator_agent", "monitor", "agent_finished"),
                      ("investigator_agent", "post", None), ("investigator_agent", "status", "COMPLETED")]
    started = {e.author: e for e in entries if e.payload.get("event") == "agent_started"}
    finished = {e.author: e for e in entries if e.payload.get("event") == "agent_finished"}
    # Sequential workflow: the Investigator starts only after the Evidence Agent has finished.
    assert started["evidence_agent"].ts < finished["evidence_agent"].ts <= started["investigator_agent"].ts
    assert started["investigator_agent"].payload["source"] == "adk plugin before_agent_callback"
    runs = {r.agent: r for r in bundle.adk.agent_runs}           # the existing callbacks still recorded everything
    assert set(runs) == {"evidence_agent", "investigator_agent"} and all(r.latency_ms for r in runs.values())


def test_controller_tracks_completion_and_records_the_adjudicators_decision(shared):
    bundle = run(shared, "CS-003")
    state = Blackboard(shared.db).state(bundle.run_id, "CS-003")
    summary = next(m for m in state.monitor if m.section == "controller:completion").payload
    assert summary["completed"] == ["evidence_agent", "investigator_agent", "forensic_agent"]
    assert summary["failed"] == summary["unfinished"] == summary["findings"] == []
    assert all(r["published"] and r["matches_received"] for r in summary["agents"])
    decision = next(p for p in state.live_posts if p.section == "decision:final")
    assert decision.author == "adjudicator" and decision.process.pid == os.getpid()
    assert decision.payload["final_decision"] == bundle.final_report.final_decision.value == "ESCALATE"
    assert decision.payload["decided_by"] == bundle.final_report.decided_by
    assert decision.payload["final_report_sha256"] == report_sha256(bundle.final_report)
    sections = [e.section for e in Blackboard(shared.db).entries(bundle.run_id, "CS-003")]
    assert sections.index("controller:completion") < sections.index("decision:final")   # tracked, then decided


@pytest.mark.parametrize("claim_id", ["CS-001", "CS-003", "CS-006", "CS-001-INJ"])
def test_adjudication_outcomes_match_the_ground_truth(shared, claim_id):
    final = run(shared, claim_id).final_report
    truth = shared.truth[claim_id]
    assert final.final_decision.value == truth["expected_decision"]
    assert (str(final.payout.payout) if final.payout else None) == truth["expected_payout"]


@pytest.mark.parametrize("claim_id", ["CS-001", "CS-003"])
def test_the_blackboard_does_not_change_the_adjudication(shared, claim_id, monkeypatch):
    with_board = run(shared, claim_id)
    monkeypatch.setenv(ENV_VAR, "off")                  # this side without a blackboard; the CrewAI service keeps it
    without = run(shared, claim_id, suffix="nobb")
    pick = lambda f: (f.final_decision, f.decided_by, f.deterministic_decision, f.requires_human_approval,
                      f.payout.payout if f.payout else None, [(c.name, c.outcome, c.detail) for c in f.rule_checks])
    assert pick(with_board.final_report) == pick(without.final_report)
    # The CrewAI side could not post for an unregistered run: reported as a monitoring failure, decision untouched.
    assert Blackboard(shared.db).registration(without.run_id) is None
    failures = read_monitoring_failures(shared.db, without.run_id)
    assert failures and {f["environment"] for f in failures} == {".venv-crew"}
    assert all("not registered" in f["error"] for f in failures)


def test_an_unusable_blackboard_is_reported_and_fails_closed(shared, monkeypatch, tmp_path, capfd):
    broken = tmp_path / "not_a_database"
    broken.mkdir()                                       # this side's blackboard cannot be opened at all
    monkeypatch.setenv(ENV_VAR, str(broken))
    bundle = run(shared, "CS-001", suffix="broken")     # a claim the deterministic rules approve
    reference = run(shared, "CS-001")
    assert reference.final_report.final_decision.value == "APPROVE"
    final = bundle.final_report
    # Blackboard mode was requested but nothing could be shared or verified: no agent context, no adjudicated
    # agent review, so the approval is not given silently - the investigation is escalated to a human.
    assert bundle.context_sharing == "blackboard"
    assert final.review_status == "NONE" and final.requires_human_approval
    assert (final.final_decision.value, final.decided_by) == ("ESCALATE", "INSUFFICIENT_REVIEW") and final.payout is None
    assert [(c.name, c.outcome) for c in final.rule_checks] == [(c.name, c.outcome) for c in reference.final_report.rule_checks]
    assert bundle.messages == [] and any("Forensic Agent not started" in e for e in bundle.crewai.errors)
    assert any("investigator_agent was not started" in e for e in bundle.adk.errors)
    assert "BLACKBOARD MONITORING FAILURE [open]" in capfd.readouterr().err
    view = run_monitor(bundle.run_id, path=broken)                # what the dashboard shows
    assert view["enabled"] and "unavailable" in view["error"]
    assert {f["where"] for f in view["monitoring_failures"]} >= {"open"}


def test_dashboard_monitoring_view_of_a_real_run(shared, tmp_path, dataset):
    from starlette.testclient import TestClient

    from claimsleuth.dashboard.app import create_app
    from claimsleuth.dashboard.services import ServiceConfig
    from claimsleuth.dashboard.workspace import Workspace
    from claimsleuth.integration.run import save_bundle
    bundle = run(shared, "CS-003")
    save_bundle(bundle, tmp_path / "rt")
    ws = Workspace(tmp_path / "dash", source_data=dataset[0])
    with TestClient(create_app(ws, ServiceConfig(mcp_port=shared.mcp.port, crew_port=shared.crew.port),
                               tmp_path / "rt")) as client:
        view = client.get(f"/api/runs/{bundle.run_id}/blackboard").json()
    assert (view["run_id"], view["claim_id"], view["registered"]) == (bundle.run_id, "CS-003", True)
    assert {a["agent"]: a["state"] for a in view["agents"]} == {
        "evidence_agent": "COMPLETED", "investigator_agent": "COMPLETED", "forensic_agent": "COMPLETED"}
    assert view["decision"]["final_decision"] == "ESCALATE" and view["decision_matches_report"] is True
    assert view["events"] and view["events"][-1]["section"] == "decision:final"
    assert set(view["authentication"]) == {"valid"} and view["monitoring_failures"] == [] and view["late"] == []
