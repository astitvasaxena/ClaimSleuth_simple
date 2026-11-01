"""Dashboard end to end: real MCP server (.venv) + CrewAI A2A service (.venv-crew) processes on a dashboard
workspace; the dashboard app (TestClient) uploads the sample PDF, runs the full investigation and serves the views.
ADK and CrewAI use their labelled test doubles; MCP, A2A, the ADK Runner and Crew.kickoff() are real.
"""
import asyncio
import time

import pytest
from mcp import Client
from starlette.testclient import TestClient

from claimsleuth.dashboard.app import create_app
from claimsleuth.dashboard.sample_pdf import INSTRUCTION_A, INSTRUCTION_B, SAMPLES
from claimsleuth.dashboard.security_lab import SecurityLab
from claimsleuth.dashboard.services import ServiceConfig
from claimsleuth.dashboard.workspace import Workspace
from test_integration_e2e import CREW_PYTHON, CrewService
from test_mcp_server import RunningServer

NORMAL, FIRST, ACCESS, MISSING = SAMPLES        # FIRST carries instruction A, ACCESS instruction B


@pytest.fixture(scope="module")
def stack(dataset, tmp_path_factory):
    if not CREW_PYTHON.exists():
        pytest.skip("needs .venv-crew for the CrewAI A2A service")
    tmp = tmp_path_factory.mktemp("dash_e2e")
    ws = Workspace(tmp / "dash", source_data=dataset[0])
    ws.prepare()
    mcp = RunningServer(ws.data_dir, dataset[1], tmp)
    try:
        crew = CrewService(ws.data_dir, mcp.url, tmp)
    except Exception:
        mcp.stop()
        raise
    cfg = ServiceConfig(mcp_port=mcp.port, crew_port=crew.port)
    with TestClient(create_app(ws, cfg, tmp / "runtime")) as client:
        yield client, ws, tmp / "runtime", cfg
    crew.stop()
    mcp.stop()


def run_pdf(client, sample):
    up = client.post("/api/upload", content=sample.pdf(), headers={"Content-Type": "application/pdf",
                                                                   "X-Filename": sample.filename})
    assert up.status_code == 200, up.text
    started = client.post("/api/runs", json={"upload_id": up.json()["upload_id"]})
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    deadline = time.time() + 240
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{run_id}").json()
        if job["state"] != "running":
            break
        time.sleep(0.5)
    assert job["state"] == "completed", job
    assert [st["state"] for st in job["stages"]] == ["done"] * 5, job["stages"]     # every stage really finished
    assert set(job["events"]) == {"baseline_finished", "adk_finished", "crewai_finished", "adjudicated"}
    return client.get(f"/api/runs/{run_id}").json(), job


@pytest.fixture(scope="module")
def first(stack):
    return run_pdf(stack[0], FIRST)


def test_services_are_running_on_the_workspace(stack):
    status = stack[0].get("/api/status").json()
    assert status["services"] == {"mcp": "RUNNING", "crewai": "RUNNING"} and status["warnings"] == []
    assert status["crew_mode"] == "test-double" and status["adk_mode"] == "test-double"


def test_uploaded_pdf_drives_every_component(first):
    view, job = first
    assert view["claim_id"] == "WC-2026-0412" and view["origin"]["origin"] == "uploaded PDF"
    assert view["mcp"]["calls"] and {m["direction"] for m in view["a2a"]["service_log"]} == {"received", "sent"}
    # The repair-report serial exists only in the uploaded PDF; every system saw it.
    assert "FRL-RF-500380" in " ".join(c["detail"] for c in view["final"]["rule_checks"])
    for card in view["agents"]:
        assert card["status"] == "COMPLETED" and card["model"] == "test-double" and not card["genuine_inference"]
        text = str(card["output"])
        assert "FRL-RF-500380" in text
    forensic = view["agents"][2]
    assert forensic["framework"] == "CrewAI" and forensic["crew"]["process"] == "sequential"


def test_history_comes_from_the_database_via_mcp(first):
    view, _ = first
    history = [c for c in view["mcp"]["calls"] if c["tool"] == "claim_history_lookup"]
    callers = {c["caller"] for c in history}
    assert callers == {"adk:investigator_agent", "crewai:forensic_agent"}
    by_serial = [c for c in history if c["arguments"].get("serial_number") == "FRL-RF-500300"]
    assert by_serial and all([r["claim_id"] for r in c["returned"]["records"]] == ["HST-2026-0115"] for c in by_serial)
    assert all(c["logged_by_server"] and c["claim_id"] == "WC-2026-0412" for c in view["mcp"]["calls"])
    policy = [c for c in view["mcp"]["calls"] if c["tool"] == "warranty_policy_search"]
    assert {c["caller"] for c in policy} == {"adk:evidence_agent", "adk:investigator_agent"}


def test_a2a_and_execution_pattern(first):
    view, _ = first
    assert [m["message_type"] for m in view["a2a"]["messages"]] == ["FORENSIC_REQUEST", "FORENSIC_RESPONSE"]
    assert view["pattern"]["sequential"]["verified"] and view["pattern"]["parallel"]["overlap_s"] > 0
    events = [e["event"] for e in view["timeline"]]
    assert events[0] == "Claim submitted" and "Final deterministic adjudication completed" in events
    assert any(e.startswith("A2A FORENSIC_REQUEST sent") for e in events)


def test_final_decision_is_deterministic(first):
    view, _ = first
    final = view["final"]
    # Instruction A is in this PDF: the existing security rule escalates; the serial mismatch is still reported.
    assert final["final_decision"] == "ESCALATE" and final["decided_by"] == "GUARDRAIL_ESCALATION"
    assert final["payout"] is None and final["requires_human_approval"]
    assert {c["name"]: c["outcome"] for c in final["rule_checks"]}["serial_consistency"] == "FAIL"
    systems = [r["system"] for r in view["comparison"]]
    assert systems == ["Deterministic baseline", "Evidence Agent", "Investigator Agent", "Forensic Agent", "MCP tools",
                       "Final adjudicator"]
    assert view["comparison"][4]["result"] is None


def test_incomplete_pdf_requests_evidence(stack):
    view, _ = run_pdf(stack[0], MISSING)
    assert view["final"]["final_decision"] == "REQUEST_EVIDENCE"
    assert all("diagnostic / repair report" in c["output"]["missing_information"] for c in view["agents"])


def test_security_scenarios_4_and_5_with_live_services(stack):
    """The security lab is no longer a page; its live-service scenarios still run against the real services."""
    out = asyncio.run(SecurityLab(stack[1], stack[3]).run([4, 5]))
    failed = [(r["id"], r.get("error"), [c for c in r["checks"] if c["passed"] is False]) for r in out["results"]]
    assert out["summary"] == {"total": 2, "passed": 2, "failed": 0}, failed
    assert [r["verdict"] for r in out["results"]] == ["Refused", "Refused"]
    harness = next(r for r in out["results"] if r["id"] == 5)["harness"]
    retry = next(s for s in harness["scenarios"] if s["scenario"] == "fabricated_citation_then_valid")
    assert [v["accepted"] for v in retry["guardrail_verdicts"]] == [False, True]


def test_normal_pdf_is_approved_with_a_deterministic_payout(stack):
    view, _ = run_pdf(stack[0], NORMAL)
    final = view["final"]
    assert view["claim_id"] == "WC-2026-0411" and view["origin"]["filename"] == NORMAL.filename
    assert final["final_decision"] == "APPROVE" and final["decided_by"] == "DETERMINISTIC_RULES"
    assert final["payout"]["payout"] == "16400.00" and final["requires_human_approval"]     # >= ₹15,000 needs sign-off
    assert final["payout"]["currency"] == "INR" and "₹18,900.00" in final["payout"]["explanation"]
    assert all(a["status"] == "COMPLETED" for a in view["agents"])
    history = [c for c in view["mcp"]["calls"] if c["tool"] == "claim_history_lookup"]
    assert history and all(c["returned"]["records"] == [] for c in history)                  # no invented history


def test_instruction_a_is_detected_and_not_followed(first):
    view, _ = first
    sec = view["integrity"]
    assert sec["status"] == "Prompt Injection Detected" and len(sec["findings"]) == 1
    f = sec["findings"][0]
    assert (f["type"], f["doc_id"], f["lines"], f["pdf"][0][0]) == ("Prompt injection", "repair_report", [12, 13, 14], 3)
    assert " ".join(INSTRUCTION_A.split()) in f["text"] and FIRST.filename in f["source"]
    # Each agent's own output, not the screen, decides whether it is credited.
    credited = {a["title"] for a in view["agents"] if any(c.get("doc_id") == "repair_report" and c.get("line") in f["lines"]
                                                        for x in a["output"]["findings"] for c in x["citations"])}
    assert set(f["reported_by"]) == credited and credited
    assert all(a["recommendation"] != "APPROVE" for a in view["agents"])       # no agent obeyed it
    assert any("serial consistency" in r for r in f["response"])                # the mismatch was not hidden


def test_instruction_b_is_detected_and_history_access_stays_in_scope(stack):
    client, _, _, cfg = stack
    view, _ = run_pdf(client, ACCESS)
    claim_id, final, sec = view["claim_id"], view["final"], view["integrity"]
    assert sec["status"] == "Prompt Injection Detected" and len(sec["findings"]) == 1
    f = sec["findings"][0]
    assert (f["type"], f["doc_id"], f["lines"], f["pdf"][0][0]) == ("Unauthorized data access attempt", "invoice",
                                                                   [10, 11, 12], 2)
    assert " ".join(INSTRUCTION_B.split()) in f["text"]
    assert final["final_decision"] == "ESCALATE" and final["decided_by"] == "GUARDRAIL_ESCALATION" and final["payout"] is None
    # The MCP server's own log: every history lookup of this run was made under this claim's scope; none returned
    # another customer's records.
    history = [c for c in view["mcp"]["calls"] if c["tool"] == "claim_history_lookup"]
    assert history and all(c["claim_id"] == claim_id and c["logged_by_server"] for c in history)
    assert sec["history_scope"]["lookups"] == sec["history_scope"]["scoped_to_claim"] == len(history)
    assert all(r["claimant_id"] == "USR-3318" for c in history for r in (c["returned"] or {}).get("records", []))

    async def ask(args):          # what instruction B asks for, made directly against the running MCP server
        meta = {"claimsleuth/run_id": "RUN-e2e-scope-b", "claimsleuth/claim_id": claim_id,
                "claimsleuth/caller": "dashboard:scope_test"}
        async with Client(cfg.mcp_url) as mcp:
            return await mcp.call_tool("claim_history_lookup", args, meta=meta)
    for args in ({"claimant_id": "USR-1002"}, {"serial_number": "FRL-RF-500300"}, {}):
        refused = asyncio.run(ask(args))
        assert refused.is_error, args
    own = asyncio.run(ask({"claimant_id": "USR-3318"}))
    assert not own.is_error


def test_same_pdf_gives_the_same_deterministic_decision(stack):
    first_run, _ = run_pdf(stack[0], ACCESS)
    second_run, _ = run_pdf(stack[0], ACCESS)
    pick = lambda v: (v["final"]["final_decision"], v["final"]["decided_by"],
                      [(c["name"], c["outcome"]) for c in v["final"]["rule_checks"]],
                      [(r["field"], r["value"]) for r in v["final"]["evidence_references"]])
    assert first_run["run_id"] != second_run["run_id"] and pick(first_run) == pick(second_run)
    fields = {r["field"] for r in first_run["final"]["evidence_references"]}
    assert {"serial_number", "purchase_date", "repair_cost", "diagnosis"} <= fields     # legitimate evidence used


def test_clean_pdfs_have_no_security_finding(stack):
    for sample in (NORMAL, MISSING):
        view, _ = run_pdf(stack[0], sample)
        assert view["integrity"]["status"] == "Clean" and view["integrity"]["findings"] == []
        assert view["integrity"]["history_scope"]["refused"] == []


def test_human_review_is_recorded_without_touching_the_automated_decision(stack, first):
    client, _, runtime, _ = stack
    view, _ = first
    run_dir = runtime / "runs" / view["run_id"]
    before = {n: (run_dir / n).read_bytes() for n in ("final_report.json", "bundle.json", "audit.jsonl")}
    r = client.post(f"/api/runs/{view['run_id']}/review", json={"action": "APPROVED", "reviewer": "Dr. Test",
                                                                 "notes": "Serial checked with the customer."})
    assert r.status_code == 201 and r.json()["automated_decision"] == "ESCALATE"
    assert client.post(f"/api/runs/{view['run_id']}/review", json={"action": "PAY", "reviewer": "x"}).status_code == 400
    assert client.post(f"/api/runs/{view['run_id']}/review", json={"action": "REJECTED", "reviewer": " "}).status_code == 400
    after = client.get(f"/api/runs/{view['run_id']}").json()
    assert after["final"]["final_decision"] == "ESCALATE" and after["final"]["approval_status"] == "PENDING"
    assert {n: (run_dir / n).read_bytes() for n in before} == before                      # stored report untouched
    human = [a for a in after["audit_trail"] if a["kind"] == "human"]
    assert len(human) == 1 and human[0]["reviewer"] == "Dr. Test" and human[0]["ts"]
    assert human[0]["automated_decision"] == "ESCALATE" and after["human_reviews"][0]["action"] == "APPROVED"
