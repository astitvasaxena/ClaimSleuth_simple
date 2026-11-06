"""Service failures during an investigation. "Killed" means the real local process is terminated mid-run. "Injected" means a
function is replaced to raise. Agents are labelled test doubles and no model is used, so these prove how this code behaves
at each boundary, not how a real provider outage behaves.
"""
import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from claimsleuth import contract
from claimsleuth.blackboard import ENV_VAR, Blackboard, BlackboardError
from claimsleuth.dashboard import jobs as jobs_module
from claimsleuth.dashboard import recovery
from claimsleuth.dashboard.services import ServiceConfig
from claimsleuth.observability.outcome import outcome_of
from claimsleuth.orchestration.manager import InvestigationManager
from claimsleuth.schemas import Decision, ExecutionStatus, RunStatus, TaskStatus
from test_dashboard import adjudicated_bundle, ws  # noqa: F401  (fixture)
from test_hierarchical_orchestration import env, manage  # noqa: F401  (fixture)
from test_integration_e2e import CREW_PYTHON, CrewService
from test_mcp_server import RunningServer

CLEAN = "CS-001"                                   # the rules alone would approve this claim


def assert_safe(bundle):
    """A human has the last word, and nothing unverified reached a payout."""
    final = bundle.final_report
    assert final.requires_human_approval and final.review_status != "COMPLETE"
    assert outcome_of(bundle).run_status != RunStatus.COMPLETED


@pytest.fixture
def own(dataset, tmp_path, monkeypatch):
    """Services this test may kill, with their own blackboard."""
    if not CREW_PYTHON.exists():
        pytest.skip("needs .venv-crew for the CrewAI A2A service")
    db = tmp_path / "blackboard.sqlite3"
    monkeypatch.setenv(ENV_VAR, str(db))
    mcp = RunningServer(dataset[0], dataset[1], tmp_path)
    crew = CrewService(dataset[0], mcp.url, tmp_path)
    yield SimpleNamespace(db=db, mcp=mcp, crew=crew, data_dir=dataset[0], db_path=dataset[1])
    for service in (crew, mcp):
        try:
            service.stop()
        except Exception:
            pass


def when(condition, action, seconds=60):
    """Runs `action` the moment `condition` holds, from another thread, while the run is in progress."""
    def watch():
        end = time.time() + seconds
        while time.time() < end:
            try:
                if condition():
                    action()
                    return
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                pass
            time.sleep(0.02)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread


def mcp_calls(service, run_id):
    with urllib.request.urlopen(f"{service.base}/tool-calls?run_id={run_id}&limit=50", timeout=2) as resp:
        return json.loads(resp.read())["calls"]


# =========================================================================== killed processes

def test_the_crewai_process_dies_after_receiving_the_request(own):
    run_id = "RUN-fault-crewkilled"
    watcher = when(lambda: any(m["direction"] == "received" for m in own.crew.messages(run_id)), own.crew.proc.kill)
    bundle = manage(own, CLEAN, run_id, a2a_timeout=30)
    watcher.join(1)
    assert own.crew.proc.poll() is not None                          # it really died
    assert bundle.crewai.status == ExecutionStatus.FAILED and bundle.crewai.report is None
    assert bundle.adk.status == ExecutionStatus.COMPLETED and bundle.adk.report is not None      # valid reports are kept
    assert_safe(bundle)
    assert bundle.final_report.review_status == "PARTIAL"
    assert P_status(bundle, "forensics") in (TaskStatus.FAILED, TaskStatus.TIMED_OUT)


def test_the_mcp_process_dies_during_the_agents_work(own):
    run_id = "RUN-fault-mcpkilled"
    watcher = when(lambda: mcp_calls(own.mcp, run_id), own.mcp.proc.kill)
    bundle = manage(own, CLEAN, run_id, a2a_timeout=30, deadlines={"mcp_records": 5})
    watcher.join(1)
    assert own.mcp.proc.poll() is not None
    assert bundle.mcp.status == ExecutionStatus.FAILED
    assert_safe(bundle)


def P_status(bundle, task_id):
    return next(t for t in bundle.execution_plan.tasks if t.task_id == task_id).status


# =========================================================================== injected at a boundary

def test_a_malformed_a2a_response_gives_no_forensic_result(env, monkeypatch):  # noqa: F811
    def malformed(text):
        raise contract.ContractError("INVALID_MESSAGE", "not a workflow message")

    monkeypatch.setattr("claimsleuth.adk_service.forensic_a2a.contract.parse", malformed)
    bundle = manage(env, CLEAN, "RUN-fault-a2amalformed")
    assert bundle.crewai.status == ExecutionStatus.FAILED and bundle.crewai.report is None
    assert any("invalid A2A response" in e for e in bundle.crewai.errors)
    assert_safe(bundle)


def test_an_evidence_report_that_cannot_be_published_stops_what_depends_on_it(env, monkeypatch):  # noqa: F811
    real = Blackboard.post

    def refuses(self, run_id, claim_id, author, framework, section, payload, fingerprint, *, writer):
        if section == "report:evidence_agent":
            raise BlackboardError("disk I/O error")
        return real(self, run_id, claim_id, author, framework, section, payload, fingerprint, writer=writer)

    monkeypatch.setattr(Blackboard, "post", refuses)
    bundle = manage(env, CLEAN, "RUN-fault-nopublish")
    assert P_status(bundle, "evidence") == TaskStatus.FAILED
    assert P_status(bundle, "investigation") == TaskStatus.BLOCKED == P_status(bundle, "forensics")
    assert not any(m.message_type == "FORENSIC_REQUEST" for m in bundle.messages)      # no model time spent on it
    assert_safe(bundle)
    assert bundle.final_report.final_decision != Decision.APPROVE and bundle.final_report.payout is None


def test_a_decision_that_cannot_be_recorded_on_the_blackboard_is_visible(env, monkeypatch):  # noqa: F811
    real = Blackboard.post

    def refuses(self, run_id, claim_id, author, framework, section, payload, fingerprint, *, writer):
        if section == "decision:final":
            raise BlackboardError("disk I/O error")
        return real(self, run_id, claim_id, author, framework, section, payload, fingerprint, writer=writer)

    monkeypatch.setattr(Blackboard, "post", refuses)
    bundle = manage(env, CLEAN, "RUN-fault-nodecision")
    assert bundle.final_report is not None
    assert not [e for e in Blackboard(env.db).entries("RUN-fault-nodecision", CLEAN) if e.section == "decision:final"]
    from claimsleuth.blackboard.monitor import run_monitor
    board = run_monitor("RUN-fault-nodecision", env.db)
    assert board["decision"] is None and any("publish decision" in f["where"] for f in board["monitoring_failures"])


def test_a_report_that_cannot_be_stored_leaves_a_failed_run_for_a_human(ws, dataset, tmp_path, monkeypatch):  # noqa: F811
    async def run(claim_id, run_id, **kwargs):
        return adjudicated_bundle(ws, claim_id, dataset).model_copy(update={"run_id": run_id})

    def disk_full(bundle, runtime):
        raise OSError("no space left on device")

    monkeypatch.setattr(jobs_module, "run_claim_async", run)
    monkeypatch.setattr(jobs_module, "save_bundle", disk_full)
    runtime = tmp_path / "rt"

    async def main():
        manager = jobs_module.JobManager(ws, ServiceConfig(mcp_port=1, crew_port=2), runtime)
        job = manager.start("CS-003", {"kind": "test", "label": "t"}, None)
        while job.state == "running":
            await asyncio.sleep(0.01)
        return job

    job = asyncio.run(main())
    assert job.state == "failed" and "no space" in job.error
    row = recovery.unfinished_runs(runtime / "runs", set(), lambda r: None, None)[0]
    assert (row["state"], row["human_review_required"]) == ("FAILED", True)


def test_a_forensic_failure_is_not_retried(own):
    """One A2A request whatever happens: no model call is repeated behind the budget's back."""
    run_id = "RUN-fault-noretry"
    own.crew.proc.kill()
    own.crew.proc.wait(10)
    bundle = asyncio.run(InvestigationManager(CLEAN, run_id=run_id, mcp_url=own.mcp.url, crew_card_url=own.crew.card,
                                              data_dir=own.data_dir, db_path=own.db_path, a2a_timeout=30).run())
    assert bundle.crewai.status == ExecutionStatus.FAILED
    assert sum(m.message_type == "FORENSIC_REQUEST" for m in bundle.messages) <= 1
    assert_safe(bundle)
