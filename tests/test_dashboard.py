"""Dashboard tests (main .venv): PDF intake, workspace, code references, security scenarios 1-4, API basics.

The end-to-end dashboard run (real MCP server + CrewAI A2A service) is in test_dashboard_e2e.py.
"""
import asyncio
import io
import json

import pytest
from pypdf import PdfReader, PdfWriter
from starlette.testclient import TestClient

from claimsleuth.dashboard.app import create_app
from claimsleuth.dashboard.pdf_intake import PdfIntakeError, parse_claim_pdf, read_pdf_lines
from claimsleuth.dashboard.sample_pdf import INSTRUCTION_A, INSTRUCTION_B, SAMPLES, build_pdf, write_samples
from claimsleuth.dashboard.security_lab import SecurityLab
from claimsleuth.dashboard.services import ServiceConfig
from claimsleuth.dashboard.workspace import Workspace, WorkspaceError
from claimsleuth.datastore import load_claim
from claimsleuth.schemas import DocumentKind

NORMAL, FIRST, ACCESS, MISSING = SAMPLES        # FIRST carries instruction A, ACCESS instruction B


@pytest.fixture
def ws(dataset, tmp_path):
    workspace = Workspace(tmp_path / "dash", source_data=dataset[0])
    workspace.prepare()
    return workspace


# --------------------------------------------------------------------------- sample PDFs

def test_samples_are_real_pdfs_without_ground_truth(tmp_path):
    paths = write_samples(tmp_path / "samples", tmp_path / "truth.json")
    for sample, path in zip(SAMPLES, paths):
        data = path.read_bytes()
        assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
        reader = PdfReader(str(path))
        text = "\n".join(p.extract_text() for p in reader.pages)
        assert sample.claim["claim_id"] in text and "fictional" in text
        assert sample.expected not in text and "expected" not in text.lower()        # ground truth stays out
        assert "$" not in text and "INR " in text                                      # amounts in rupees
    truth = json.loads((tmp_path / "truth.json").read_text(encoding="utf-8"))
    assert truth["WC-2026-0412"]["expected_decision"] == "ESCALATE"


# --------------------------------------------------------------------------- PDF intake

def test_extraction_keeps_page_and_line_provenance():
    draft = parse_claim_pdf(FIRST.pdf(), FIRST.filename)
    assert draft.can_submit and draft.errors == [] and draft.missing == []
    assert draft.claim.claim_id == "WC-2026-0412" and draft.claim.claimant_id == "USR-2207"
    assert draft.fields["serial_number"].value == "FRL-RF-500300" and (draft.fields["serial_number"].page,
                                                                        draft.fields["serial_number"].line) == (1, 8)
    assert draft.claim.claimant_statement.endswith("service report are attached.")       # wrapped lines joined
    docs = {d.doc_id: d for d in draft.documents}
    assert docs["invoice"].kind == DocumentKind.INVOICE and docs["repair_report"].kind == DocumentKind.DIAGNOSTIC
    line = next(i for i, ln in enumerate(docs["repair_report"].lines, 1) if ln.text.startswith("Serial Number"))
    assert docs["repair_report"].lines[line - 1].page == 3
    assert "Serial Number: FRL-RF-500380" in docs["repair_report"].text
    assert "Estimated Cost: INR 14,500.00" in docs["repair_report"].text
    assert not any("Page 1 of 3" in ln.text for d in draft.documents for ln in d.lines)   # footers ignored


def test_missing_report_is_reported_not_invented():
    draft = parse_claim_pdf(MISSING.pdf(), MISSING.filename)
    assert draft.can_submit and [d.doc_id for d in draft.documents] == ["invoice"]
    assert any("repair report" in m for m in draft.missing)


@pytest.mark.parametrize("sample, instruction, doc_id, page", [(FIRST, INSTRUCTION_A, "repair_report", 3),
                                                                 (ACCESS, INSTRUCTION_B, "invoice", 2)],
                         ids=["instruction_A", "instruction_B"])
def test_malicious_pdf_instructions_are_detected_through_real_extraction(sample, instruction, doc_id, page):
    """The instruction is real PDF text: pypdf extracts it, the screen flags it with its document, page and line,
    and it is kept as evidence (not removed, not obeyed)."""
    assert instruction not in str(sample.claim) and instruction not in sample.description   # only inside the PDF
    draft = parse_claim_pdf(sample.pdf(), sample.filename)
    events = [e for e in draft.guardrail_events if e.kind == "prompt_injection"]
    assert events and all(e.source.startswith(f"doc:{doc_id}#L") and f"PDF p.{page} " in e.source for e in events)
    doc = next(d for d in draft.documents if d.doc_id == doc_id)
    assert instruction in " ".join(ln.text for ln in doc.lines)                       # wrapped, but all there
    assert draft.can_submit and draft.claim.claim_id == sample.claim["claim_id"]     # the claim itself is intact


@pytest.mark.parametrize("sample", [NORMAL, MISSING], ids=["normal", "incomplete"])
def test_clean_pdfs_raise_no_security_event(sample):
    draft = parse_claim_pdf(sample.pdf(), sample.filename)
    assert draft.can_submit and draft.guardrail_events == []


def test_instruction_b_is_classified_as_a_data_access_attempt():
    from claimsleuth.security import DATA_ACCESS_PATTERNS, detect_injection
    assert set(detect_injection(INSTRUCTION_B)) & DATA_ACCESS_PATTERNS
    assert not set(detect_injection(INSTRUCTION_A)) & DATA_ACCESS_PATTERNS


@pytest.mark.parametrize("kind, message", [("not_pdf", "not a PDF"), ("garbage", "could not be read"),
                                           ("too_large", "limit")])
def test_invalid_uploads_are_rejected(kind, message):
    data = {"not_pdf": b"hello, not a pdf", "garbage": b"%PDF-1.4\n garbage",
            "too_large": b"%PDF-" + b"0" * (5 * 1024 * 1024 + 10)}[kind]
    with pytest.raises(PdfIntakeError, match=message):
        read_pdf_lines(data)


def test_pdf_without_text_layer_is_rejected():
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)       # like a scanned page: no text layer
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(PdfIntakeError, match="no text layer"):
        read_pdf_lines(buffer.getvalue())


def test_missing_required_fields_and_future_dates_block_submission():
    bare = build_pdf([[("title", "WARRANTY CLAIM FORM"), ("text", "Claim ID: WC-1"), ("text", "Incident Date: 2099-01-01")]], "x")
    draft = parse_claim_pdf(bare)
    assert not draft.can_submit and any("Claimant Name" in e for e in draft.errors)
    pages = FIRST.pages()
    pages[0] = [(s, "Incident Date: 2099-01-01" if t.startswith("Incident Date") else t) for s, t in pages[0]]
    future = parse_claim_pdf(build_pdf(pages, "future"))
    assert not future.can_submit and any("future" in e for e in future.errors)


# --------------------------------------------------------------------------- workspace

def test_upload_becomes_a_claim_the_services_can_load(ws):
    draft = ws.store_upload(FIRST.pdf(), FIRST.filename)
    claim_id = ws.materialise_upload(draft.upload_id)
    claim, docs = load_claim(claim_id, ws.data_dir)
    assert claim == draft.claim
    assert [d.sha256 for d in docs] == [d.sha256 for d in draft.evidence_documents()]   # A2A hash check holds
    origin = ws.origin(claim_id)
    assert origin["origin"] == "uploaded PDF" and origin["line_map"]["repair_report"][6] == [3, 7]
    assert ws.materialise_upload(draft.upload_id) == claim_id                            # same PDF: reused
    assert not (ws.data_dir / "ground_truth.json").exists()


def test_uploads_cannot_overwrite_other_claims(ws):
    pages = FIRST.pages()
    pages[0] = [(s, "Claim ID: CS-003" if t.startswith("Claim ID") else t) for s, t in pages[0]]
    draft = ws.store_upload(build_pdf(pages, "x"), "x.pdf")
    with pytest.raises(WorkspaceError, match="built-in sample"):
        ws.materialise_upload(draft.upload_id)
    first = ws.store_upload(FIRST.pdf(), FIRST.filename)
    ws.materialise_upload(first.upload_id)
    pages = FIRST.pages()
    pages[1] = [(s, t.replace("INV-77310", "INV-99999")) for s, t in pages[1]]
    other = ws.store_upload(build_pdf(pages, "changed"), "changed.pdf")
    with pytest.raises(WorkspaceError, match="different PDF"):
        ws.materialise_upload(other.upload_id)
    with pytest.raises(WorkspaceError, match="invalid upload id"):
        ws.load_draft("../../etc")


# --------------------------------------------------------------------------- API

def test_api_basics_without_services(ws, tmp_path):
    with TestClient(create_app(ws, ServiceConfig(mcp_port=1, crew_port=2), tmp_path / "rt")) as client:
        page = client.get("/")
        assert page.status_code == 200 and "Content-Security-Policy" in page.headers
        assert client.get(f"/api/samples/{FIRST.filename}").content.startswith(b"%PDF")
        assert client.get("/api/samples/..%2F..%2Fground_truth.json").status_code == 404
        bad = client.post("/api/upload", content=b"hi", headers={"Content-Type": "text/plain", "X-Filename": "a.txt"})
        assert bad.status_code == 415
        ok = client.post("/api/upload", content=FIRST.pdf(), headers={"Content-Type": "application/pdf", "X-Filename": FIRST.filename})
        assert ok.status_code == 200 and ok.json()["can_submit"]
        run = client.post("/api/runs", json={"upload_id": ok.json()["upload_id"]})
        assert run.status_code == 503 and "not running" in run.json()["error"]          # no services: refused
        status = client.get("/api/status").json()
        assert status["services"] == {"mcp": "STOPPED", "crewai": "STOPPED"} and status["adk_mode"] == "test-double"
        assert (status["currency"], status["human_approval_threshold"]) == ("INR", "15000.00")
        assert client.get("/api/runs/RUN-..%2F..").status_code == 404
        for removed in ("/api/code", "/api/overview", "/api/claims", "/api/security/latest"):   # removed pages
            assert client.get(removed).status_code in (404, 405)
        assert client.post("/api/security/run", json={}).status_code in (404, 405)
        html = page.text
        assert "Security &amp; Guardrails" not in html and 'id="view-security"' not in html


# --------------------------------------------------------------------------- security scenarios without services

def test_security_scenarios_without_services_pass(ws):
    """1 normal, 2 direct injection, 3 PDF injection, 6 abusive (4 and 5 need the services: test_dashboard_e2e)."""
    out = asyncio.run(SecurityLab(ws, ServiceConfig()).run([1, 2, 3, 6]))
    by_id = {r["id"]: r for r in out["results"]}
    assert out["summary"] == {"total": 4, "passed": 4, "failed": 0}, [
        (r["id"], [c for c in r["checks"] if c["passed"] is False], r.get("error")) for r in out["results"]]
    assert by_id[1]["guardrails"] == [] and by_id[1]["verdict"] == "Allowed" and by_id[1]["decision"] == "APPROVE"
    assert by_id[2]["verdict"] == "Flagged" and by_id[2]["decision"] == "ESCALATE"
    assert "<untrusted" in by_id[2]["prompt_excerpt"] and "Ignore all previous instructions" in by_id[2]["input"]
    assert by_id[3]["verdict"] == "Flagged" and "Ignore all previous instructions." in by_id[3]["prompt_excerpt"]
    assert "page 3" in by_id[3]["reason"]
    assert by_id[6]["verdict"] == "Allowed" and by_id[6]["variants"] == {"original statement": "APPROVE",
                                                                        "abusive statement": "APPROVE"}
    assert any(g["kind"] == "abusive_language" for g in by_id[6]["guardrails"])


def test_scenario_order_is_unchanged():
    from claimsleuth.dashboard.security_lab import SCENARIOS
    assert [s["key"] for s in SCENARIOS] == ["normal", "prompt_injection", "pdf_injection", "history_access",
                                             "fabricated_output", "abusive"]


# --------------------------------------------------------------------------- progress callback and stages

def test_progress_callback_is_optional_and_cannot_break_a_run():
    import inspect

    from claimsleuth.integration import run as run_module
    assert inspect.signature(run_module.run_claim_async).parameters["progress"].default is None
    seen = []
    run_module._notify(None, "baseline_finished")                     # default off: nothing happens
    run_module._notify(lambda stage, data: seen.append(stage), "adk_finished", status="COMPLETED")
    run_module._notify(lambda stage, data: 1 / 0, "adjudicated")         # a broken observer is ignored
    assert seen == ["adk_finished"]


def test_stages_complete_only_on_real_signals():
    from claimsleuth.dashboard.jobs import Job, stages
    job = Job(run_id="RUN-x", claim_id="WC-1", source={}, crew_mode="test-double", a2a_timeout_s=None)
    state = lambda calls=(), msgs=(): {s["key"]: s["state"] for s in stages(job, list(calls), list(msgs))}
    assert state() == {"extraction": "done", "evidence": "waiting", "investigator": "waiting",
                       "forensic": "waiting", "adjudication": "waiting"}
    job.progress("baseline_finished", {"status": "COMPLETED"})
    assert state()["evidence"] == "running" and state()["investigator"] == "waiting"
    inv = [{"caller": "adk:investigator_agent"}]
    assert state(inv)["evidence"] == "done" and state(inv)["investigator"] == "running"   # sequential ADK agents
    assert state(inv, [{"direction": "received"}])["forensic"] == "running"
    job.progress("adk_finished", {"status": "COMPLETED"})
    job.progress("crewai_finished", {"status": "FAILED"})
    assert state()["investigator"] == "done" and state()["forensic"] == "failed" and state()["adjudication"] == "running"
    job.progress("adjudicated", {})
    assert state()["adjudication"] == "done"


# --------------------------------------------------------------------------- human review

def adjudicated_bundle(ws, claim_id, dataset):
    """A ClaimRunBundle with a real deterministic baseline and final report (no agents) for one workspace claim."""
    from claimsleuth.baseline import investigate
    from claimsleuth.datastore import load_policies
    from claimsleuth.history import lookup_history
    from claimsleuth.integration.adjudicator import adjudicate
    from claimsleuth.schemas import ClaimRunBundle
    claim, docs = ws.load(claim_id)
    policy = load_policies(ws.data_dir)[claim.policy_id]
    baseline = investigate(claim, docs, policy, f"RUN-review-{claim_id}", dataset[1])
    serial = next(r.value for r in baseline.evidence_refs if r.field == "serial_number" and r.doc_id == "invoice")
    history = lookup_history(serial, claim.claimant_id, claim.claim_id, dataset[1]).records
    final = adjudicate(baseline.run_id, claim, docs, policy, history, baseline, None, None, None)
    return ClaimRunBundle(run_id=baseline.run_id, claim_id=claim_id, baseline=baseline, adk=None, crewai=None, mcp=None,
                          messages=[], final_report=final, audit=[], workflow_metadata={})


def test_human_review_is_recorded_separately_and_never_changes_the_decision(ws, dataset, tmp_path):
    from pydantic import ValidationError

    from claimsleuth.dashboard.reviews import HumanReviewInput, ReviewNotAllowed, load_reviews, record_review
    bundle = adjudicated_bundle(ws, "CS-003", dataset)              # ESCALATE: human approval required
    run_dir = tmp_path / bundle.run_id
    run_dir.mkdir()
    final_before = bundle.final_report.model_dump_json()
    assert bundle.final_report.requires_human_approval
    first = record_review(run_dir, bundle, HumanReviewInput(action="APPROVED", reviewer="  A.  Reviewer ", notes="ok"))
    record_review(run_dir, bundle, HumanReviewInput(action="INFO_REQUESTED", reviewer="B. Reviewer"))
    reviews = load_reviews(run_dir)
    assert [r.action for r in reviews] == ["APPROVED", "INFO_REQUESTED"] and reviews[0].reviewer == "A. Reviewer"
    assert first.automated_decision == bundle.final_report.final_decision.value and first.ts is not None
    assert bundle.final_report.model_dump_json() == final_before
    with pytest.raises(ValidationError):
        HumanReviewInput(action="APPROVED", reviewer="   ")
    with pytest.raises(ValidationError):
        HumanReviewInput(action="PAY_OUT", reviewer="X")
    incomplete = adjudicated_bundle(ws, "CS-006", dataset)            # REQUEST_EVIDENCE: no human approval needed
    assert not incomplete.final_report.requires_human_approval
    with pytest.raises(ReviewNotAllowed):
        record_review(tmp_path, incomplete, HumanReviewInput(action="REJECTED", reviewer="A. Reviewer"))


# --------------------------------------------------------------------------- security & document integrity (report)

def uploaded_bundle(ws, sample, dataset):
    draft = ws.store_upload(sample.pdf(), sample.filename)
    claim_id = ws.materialise_upload(draft.upload_id)
    return claim_id, adjudicated_bundle(ws, claim_id, dataset)


@pytest.mark.parametrize("sample, instruction, doc_id, page, lines, kind", [
    (FIRST, INSTRUCTION_A, "repair_report", 3, [12, 13, 14], "Prompt injection"),
    (ACCESS, INSTRUCTION_B, "invoice", 2, [10, 11, 12], "Unauthorized data access attempt")], ids=["A", "B"])
def test_integrity_section_reports_the_instruction_from_backend_records(ws, dataset, sample, instruction, doc_id,
                                                                         page, lines, kind):
    from claimsleuth.dashboard.views import run_view
    claim_id, bundle = uploaded_bundle(ws, sample, dataset)
    claim, docs = ws.load(claim_id)
    sec = run_view(bundle, claim, docs, ws.origin(claim_id))["integrity"]
    assert sec["status"] == "Prompt Injection Detected" and len(sec["findings"]) == 1
    f = sec["findings"][0]
    assert (f["type"], f["doc_id"], f["lines"]) == (kind, doc_id, lines)
    assert {p for p, _ in f["pdf"]} == {page} and sample.filename in f["source"]
    assert " ".join(instruction.split()) in f["text"]                    # the whole instruction, as extracted
    assert f["reported_by"] == []                                        # no agents ran: none credited
    final = bundle.final_report
    assert final.decided_by == "GUARDRAIL_ESCALATION" and final.payout is None    # not followed
    assert final.evidence_references and any("not followed" in r for r in f["response"])
    serial = next(c for c in final.rule_checks if c.name == "serial_consistency")
    assert serial.outcome.value == ("FAIL" if sample is FIRST else "PASS")         # A cannot hide the mismatch
    if sample is FIRST:
        assert "serial consistency" in " ".join(f["response"])


@pytest.mark.parametrize("sample", [NORMAL, MISSING], ids=["normal", "incomplete"])
def test_integrity_section_is_clean_for_clean_pdfs(ws, dataset, sample):
    from claimsleuth.dashboard.views import run_view
    claim_id, bundle = uploaded_bundle(ws, sample, dataset)
    claim, docs = ws.load(claim_id)
    sec = run_view(bundle, claim, docs, ws.origin(claim_id))["integrity"]
    assert sec["status"] == "Clean" and sec["findings"] == []
    assert bundle.final_report.decided_by != "GUARDRAIL_ESCALATION"     # (no agents here; e2e checks the decision)
