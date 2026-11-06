"""Controlled adversarial test of evidence integrity: a published report is altered, and the real pathways must refuse it.

Everything here runs on disposable databases in a marked temporary folder (assert_disposable). The production
blackboard, the request ledger, the sample PDFs, .env and the claim data are only read or hashed, and no check in the
system knows this file exists: the verification under test is the production code, unchanged.

What is real and what is not:
  REAL       Blackboard, SHA-256, HMAC-SHA256, append-only triggers, the InvestigationManager, the ADK Runner, the CrewAI
             A2A service as its own process, MCP, the adjudicator, the PDF parser.
  TEST DOUBLE the agents' reasoning (the labelled offline test doubles). No model request is sent; the run uses a
             temporary request ledger, which must stay empty.
  ATTACKER   mutate() below, and TamperingManager, which only chooses the moment of the attack.

Claims: "CLM-002" is the name this test was specified under; no file or configuration uses that id. It is taken to be
the second of the four sample claims, SAMPLES[1] (WC-2026-0412). That claim escalates anyway (prompt injection in its
repair report), so escalation alone would prove nothing there: SAMPLES[0] (WC-2026-0411), which the rules approve, is
attacked as well, and there the tamper must turn an approval into a human review.

Run:  python -m claimsleuth.dashboard.evidence_integrity_lab [--out runtime/security/evidence_integrity]
The result file holds outcomes only: never a hash, a signature, a key or document text.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from claimsleuth import config, contract, execution_mode, gemini_rate
from claimsleuth.adk_service.forensic_a2a import request_forensics
from claimsleuth.blackboard import (ENV_VAR, AgentParticipant, Blackboard, ContextRef, RunController, Writer,
                                    document_fingerprint, load_writer)
from claimsleuth.blackboard.auth import key_dir
from claimsleuth.blackboard.controller import TEST_MODE_ENV, report_sha256
from claimsleuth.blackboard.monitor import run_monitor
from claimsleuth.dashboard.sample_pdf import SAMPLES, SAMPLES_DIR
from claimsleuth.dashboard.services import MAIN_PYTHON
from claimsleuth.dashboard.workspace import Workspace
from claimsleuth.datastore import load_claim, load_policies
from claimsleuth.documents import extract_fields
from claimsleuth.evaluation.runner import EvalServices
from claimsleuth.history import lookup_history
from claimsleuth.integration.adjudicator import adjudicate
from claimsleuth.orchestration import plan as P
from claimsleuth.orchestration.manager import InvestigationManager
from claimsleuth.rules import normalise_serial
from claimsleuth.schemas import ClaimRunBundle, Decision, DocumentKind, EvidenceReport, ExecutionStatus, utc_now

EVIDENCE, INVESTIGATOR, FORENSIC = "evidence_agent", "investigator_agent", "forensic_agent"
SECTION = "report:evidence_agent"
MARKER = "CLAIMSLEUTH_DISPOSABLE"
OUT_DIR = config.RUNTIME_DIR / "security" / "evidence_integrity"
TARGET, CONTROL = SAMPLES[1], SAMPLES[0]
TAMPER_FACTOR = Decimal(10)          # the tampered test amount is the report's real amount times this
SECRETS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")
CHECK_FAILED = 3                     # exit code: the test ran and a check failed (1 would be a crash)


class LabError(RuntimeError):
    """The test refused to run: a path was not disposable, or the workflow did not give what the test needs."""


# =========================================================================== disposable storage and the attacker

def disposable_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="claimsleuth-tamper-"))
    (root / MARKER).write_text("disposable adversarial test data\n", encoding="utf-8")
    return root


def assert_disposable(db: Path) -> Path:
    """Only a database in a folder this test created may be touched: inside the system temporary directory, outside
    the project, marked, and not the configured blackboard."""
    path = Path(db).resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    if (not path.is_relative_to(temporary) or path.is_relative_to(config.PROJECT_ROOT.resolve())
            or not (path.parent / MARKER).exists()):
        raise LabError(f"refused: {path} is not a disposable test database")
    return path


def mutate(db: Path, change: Callable[[sqlite3.Connection], None]) -> None:
    """TEST ONLY, disposable databases only. The attacker: a raw edit behind the store's back. The update trigger is
    lifted and restored inside one transaction on this file; the production schema and triggers are never touched."""
    conn = sqlite3.connect(assert_disposable(db), isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'entries_no_update'"
                               ).fetchone()[0]
        conn.execute("DROP TRIGGER entries_no_update")
        change(conn)
        conn.execute(trigger)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def isolate(db: Path, name: str) -> Path:
    """An independent copy of a disposable database and its keys, so one attack never affects another."""
    source, target = assert_disposable(db), Path(db).parent / name
    src, dst = sqlite3.connect(source), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    shutil.copytree(key_dir(source), key_dir(target))
    return target


def stored(db: Path, run_id: str, run_seq: int) -> dict[str, Any]:
    conn = sqlite3.connect(assert_disposable(db))
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM entries WHERE run_id = ? AND run_seq = ?", (run_id, run_seq)).fetchone())
    finally:
        conn.close()


def edit_payload(db: Path, run_id: str, run_seq: int, edit: Callable[[dict[str, Any]], Any], rehash: bool = False) -> None:
    """Change the stored payload. The stored hash and the signature stay as published, unless rehash also replaces the
    hash (which a signature made over the old hash cannot follow)."""
    text = json.dumps(edit(json.loads(stored(db, run_id, run_seq)["payload"])), sort_keys=True, default=str)
    set_columns(db, run_id, run_seq, payload=text,
                **({"payload_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()} if rehash else {}))


def set_columns(db: Path, run_id: str, run_seq: int, **columns: Any) -> None:
    assignments = ", ".join(f"{name} = ?" for name in columns)
    mutate(db, lambda conn: conn.execute(f"UPDATE entries SET {assignments} WHERE run_id = ? AND run_seq = ?",
                                         (*columns.values(), run_id, run_seq)))


def repair_fact(payload: dict[str, Any]) -> dict[str, Any]:
    return next(f for f in payload["report"]["facts"] if f["field"] == "repair_cost")


def tampered_amount(amount: str) -> str:
    """The real amount from the report, multiplied. Nothing here knows the claim's amount in advance."""
    currency, _, number = amount.strip().rpartition(" ")
    return f"{currency} {Decimal(number.replace(',', '')) * TAMPER_FACTOR:,.2f}".strip()


def change_amount(payload: dict[str, Any]) -> dict[str, Any]:
    fact = repair_fact(payload)
    fact["value"] = tampered_amount(fact["value"])
    return payload


def difference(before: Any, after: Any, path: str = "") -> list[str]:
    """Every place two JSON values differ, as paths."""
    if isinstance(before, dict) and isinstance(after, dict):
        return [p for k in sorted(set(before) | set(after))
                for p in difference(before.get(k), after.get(k), f"{path}.{k}" if path else k)]
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [p for i, (a, b) in enumerate(zip(before, after)) for p in difference(a, b, f"{path}[{i}]")]
    return [] if before == after else [path]


class TamperingManager(InvestigationManager):
    """The real manager. The only addition is the moment the attacker strikes: right after the Evidence report is
    published (before anyone has read it), or after the agents finished (before the blackboard is sealed)."""

    def __init__(self, *args: Any, attack: Callable[[ContextRef], None], moment: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.attack, self.moment = attack, moment

    def on_evidence(self, ref: ContextRef | None, error: str | None) -> None:
        if self.moment == "after_publication" and ref is not None:
            self.attack(ref)
        super().on_evidence(ref, error)

    def _settle_before_seal(self, adk, crewai) -> None:
        if self.moment == "after_consumption":
            self.attack(P.task(self.plan, "evidence").result_ref)
        super()._settle_before_seal(adk, crewai)


# =========================================================================== the published base every attack copies

@dataclass
class Published:
    """A clean run's real reports, published to a disposable database through the normal API."""
    db: Path
    bundle: ClaimRunBundle
    claim: Any
    documents: list
    policy: Any
    history: list
    ref: ContextRef
    investigator_ref: ContextRef
    hash_at_publication: str = ""
    signature_at_publication: str = field(default="", repr=False)

    @property
    def run_id(self) -> str:
        return self.bundle.run_id

    @property
    def claim_id(self) -> str:
        return self.bundle.claim_id


def publish(db: Path, bundle: ClaimRunBundle, data_dir: Path, db_path: Path) -> Published:
    claim, documents = load_claim(bundle.claim_id, data_dir)
    policy = load_policies(data_dir).get(claim.policy_id)
    serial = next((extract_fields(d).value("serial_number") for d in documents if d.kind == DocumentKind.INVOICE), None)
    serial = normalise_serial(serial or claim.serial_number)
    history = lookup_history(serial, claim.claimant_id, claim.claim_id, db_path).records if serial else []
    RunController(bundle.run_id, bundle.claim_id, path=db).register(documents)
    adk = AgentParticipant(bundle.run_id, bundle.claim_id, "adk", (EVIDENCE, INVESTIGATOR), path=db)
    crew = AgentParticipant(bundle.run_id, bundle.claim_id, "crewai", (FORENSIC,), path=db)
    adk.running(documents)
    crew.running(documents)
    ref = adk.publish_report(EVIDENCE, bundle.adk.evidence_report)
    investigator_ref = adk.publish_report(INVESTIGATOR, bundle.adk.report)
    crew.publish_report(FORENSIC, bundle.crewai.report)
    if ref is None or investigator_ref is None:
        raise LabError(f"the reports could not be published to the test database: {adk.last_failure}")
    row = stored(db, bundle.run_id, ref.run_seq)
    return Published(db, bundle, claim, documents, policy, history, ref, investigator_ref, row["payload_sha256"],
                     row["signature"])


def participants(base: Published, db: Path) -> tuple[AgentParticipant, AgentParticipant]:
    adk = AgentParticipant(base.run_id, base.claim_id, "adk", (EVIDENCE, INVESTIGATOR), path=db)
    crew = AgentParticipant(base.run_id, base.claim_id, "crewai", (FORENSIC,), path=db)
    adk.running(base.documents, mark_running=False)
    crew.running(base.documents, mark_running=False)
    return adk, crew


def normal_pathway(base: Published, db: Path, ref: ContextRef) -> dict[str, Any]:
    """Both agents read the report as in a run, then the run is sealed, verified and adjudicated: only production code."""
    adk, crew = participants(base, db)
    investigator = adk.consume_context(INVESTIGATOR, ref)
    forensic = crew.consume_context(FORENSIC, ref)
    ctl = RunController(base.run_id, base.claim_id, path=db)
    ctl.register(base.documents)
    ctl.collect(base.bundle.adk, base.bundle.crewai, [])
    adk_in, crew_in, checks = ctl.verified_inputs(base.bundle.adk, base.bundle.crewai)
    final = adjudicate(base.run_id, base.claim, base.documents, base.policy, base.history, base.bundle.baseline,
                       adk_in, crew_in, base.bundle.mcp)
    events = adk.security_events + crew.security_events + ctl.security_events
    return {"investigator": investigator, "forensic": forensic, "adk_input": adk_in, "crew_input": crew_in,
            "checks": checks, "final": final, "events": events, "receipts": len(adk.consumed) + len(crew.consumed)}


def refused_reads(base: Published, db: Path, ref: ContextRef) -> dict[str, Any]:
    """Only the two agents' reads: for attacks on the reference, where the stored report itself is sound."""
    adk, crew = participants(base, db)
    return {"investigator": adk.consume_context(INVESTIGATOR, ref), "forensic": crew.consume_context(FORENSIC, ref),
            "events": adk.security_events + crew.security_events, "receipts": len(adk.consumed) + len(crew.consumed)}


# =========================================================================== attacks A-J

def _verification(events: list) -> dict[str, str]:
    """What the checks found, from the first event that ran them."""
    for e in events:
        if e.verification_result.signature != "NOT_CHECKED":
            return e.verification_result.model_dump()
    return {"payload_integrity": "NOT_CHECKED", "signature": "NOT_CHECKED"}


def _row(letter: str, name: str, injected: str, outcome: dict[str, Any], expected: set[str], compromised: bool) -> dict[str, Any]:
    """One attack's result. passed is computed from what the pathway returned, never assumed."""
    events, final = outcome["events"], outcome.get("final")
    detected = sorted({e.attack_type for e in events})
    read = outcome["investigator"] is not None or outcome["forensic"] is not None
    checks = [not read, outcome["receipts"] == 0, bool(expected & set(detected))]
    row = {"attack": letter, "name": name, "injected": injected, "expected_detection": sorted(expected),
           "detected": detected, **_verification(events), "investigator_received": outcome["investigator"] is not None,
           "forensic_received": outcome["forensic"] is not None, "receipts": outcome["receipts"],
           "events": [e.model_dump(mode="json") for e in events]}
    if final is not None:
        reviewed = final.review_status
        row.update({"final_decision": final.final_decision.value, "decided_by": final.decided_by,
                    "review_status": reviewed, "human_review_required": final.requires_human_approval,
                    "payout": str(final.payout.payout) if final.payout else None})
        checks += [final.requires_human_approval, reviewed != "COMPLETE"]
        if compromised:
            checks += [outcome["adk_input"].status == ExecutionStatus.FAILED,
                       outcome["crew_input"].status == ExecutionStatus.FAILED, final.payout is None]
    row["passed"] = all(checks)
    return row


def _case(base: Published, key: str, letter: str, name: str, injected: str, attack: Callable[[Path], ContextRef],
          expected: set[str], compromised: bool = True, reads_only: bool = False) -> dict[str, Any]:
    db = isolate(base.db, f"attack-{key}.sqlite3")
    ref = attack(db)
    outcome = (refused_reads if reads_only else normal_pathway)(base, db, ref)
    return _row(letter, name, injected, outcome, expected, compromised)


def attack_a(base: Published) -> dict[str, Any]:
    def attack(db: Path) -> ContextRef:
        edit_payload(db, base.run_id, base.ref.run_seq, change_amount)
        return base.ref
    return _case(base, "a", "A", "Repair amount changed after publication",
                 "Only the repair amount; stored hash and signature kept", attack, {"PAYLOAD_TAMPERING"})


def attack_b(base: Published) -> dict[str, Any]:
    def edit(payload: dict[str, Any]) -> dict[str, Any]:
        report = payload["report"]
        report["recommended_decision"] = "REJECT" if report["recommended_decision"] == "APPROVE" else "APPROVE"
        return payload

    def attack(db: Path) -> ContextRef:
        edit_payload(db, base.run_id, base.ref.run_seq, edit)
        return base.ref
    return _case(base, "b", "B", "Payload changed, original signature kept",
                 "The report's recommended decision; stored hash and signature kept", attack, {"PAYLOAD_TAMPERING"})


def attack_c(base: Published) -> list[dict[str, Any]]:
    def forged(db: Path) -> ContextRef:
        attacker_key = os.urandom(32)        # the signature is real HMAC-SHA256, made with a key that is not the writer's
        forged_signature = hmac.new(attacker_key, base.hash_at_publication.encode("ascii"), hashlib.sha256).hexdigest()
        set_columns(db, base.run_id, base.ref.run_seq, signature=forged_signature)
        return base.ref

    def replaced(db: Path) -> ContextRef:
        other = stored(db, base.run_id, base.investigator_ref.run_seq)["signature"]   # genuine, from another entry
        set_columns(db, base.run_id, base.ref.run_seq, signature=other)
        return base.ref
    return [_case(base, "c1", "C", "Forged signature", "HMAC made with an attacker's key", forged, {"SIGNATURE_FORGERY"}),
            _case(base, "c2", "C", "Replaced signature", "Genuine signature of another entry", replaced,
                  {"SIGNATURE_FORGERY"})]


def attack_d(base: Published, other: Published) -> list[dict[str, Any]]:
    def swapped_row(db: Path) -> ContextRef:
        source = stored(other.db, other.run_id, other.ref.run_seq)      # a genuinely signed report of another claim
        set_columns(db, base.run_id, base.ref.run_seq, payload=source["payload"],
                    payload_sha256=source["payload_sha256"], signature=source["signature"])
        return base.ref

    def signed_by_real_key(db: Path) -> ContextRef:
        board = Blackboard(db)
        board.retract(base.run_id, base.claim_id, base.ref.run_seq, "controller", "orchestrator", "test",
                      writer=load_writer(db, "orchestrator"))
        report = other.bundle.adk.evidence_report
        post = board.post(base.run_id, base.claim_id, EVIDENCE, "adk", SECTION,
                          {"report": report.model_dump(mode="json"), "report_sha256": report_sha256(report)},
                          document_fingerprint(base.documents), writer=load_writer(db, "adk"))
        return base.ref.model_copy(update={"run_seq": post.run_seq, "sha256": post.payload_sha256})

    return [_case(base, "d1", "D", "Another claim's signed report put in this entry", "Payload, hash and signature of "
                  "another claim's row", swapped_row, {"HASH_AND_PAYLOAD_TAMPERING", "SIGNATURE_FORGERY"}),
            _case(base, "d2", "D", "Another claim's report posted with a real key", "Signed by the adk key, wrong claim",
                  signed_by_real_key, {"CROSS_CLAIM_SUBSTITUTION"}, compromised=False),
            _case(base, "d3", "D", "Reference to another claim's report", "ContextRef of another claim",
                  lambda db: other.ref, {"CROSS_CLAIM_SUBSTITUTION"}, compromised=False, reads_only=True)]


def attack_e(base: Published, earlier: Published) -> list[dict[str, Any]]:
    def superseded(db: Path) -> ContextRef:
        board = Blackboard(db)
        board.retract(base.run_id, base.claim_id, base.ref.run_seq, "controller", "orchestrator", "corrected",
                      writer=load_writer(db, "orchestrator"))
        report = base.bundle.adk.evidence_report.model_copy(update={"confidence": 0.5})
        board.post(base.run_id, base.claim_id, EVIDENCE, "adk", SECTION,
                   {"report": report.model_dump(mode="json"), "report_sha256": report_sha256(report)},
                   document_fingerprint(base.documents), writer=load_writer(db, "adk"))
        return base.ref                      # the old, once valid version

    def row_copied_forward(db: Path) -> ContextRef:
        columns = ("run_id", "claim_id", "ts", "kind", "section", "author", "framework", "process", "fingerprint",
                   "payload", "payload_sha256", "principal", "signature")
        row = stored(db, base.run_id, base.ref.run_seq)
        conn = sqlite3.connect(assert_disposable(db))
        try:
            run_seq = conn.execute("SELECT MAX(run_seq) + 1 FROM entries WHERE run_id = ?", (base.run_id,)).fetchone()[0]
            conn.execute(f"INSERT INTO entries (run_seq, {', '.join(columns)}) VALUES (?{', ?' * len(columns)})",
                         (run_seq, *(row[c] for c in columns)))
            conn.commit()
        finally:
            conn.close()
        return base.ref.model_copy(update={"run_seq": run_seq})

    return [_case(base, "e1", "E", "Superseded report replayed", "Reference to a retracted, replaced version", superseded,
                  {"REPLAY"}, compromised=False, reads_only=True),
            _case(base, "e2", "E", "Report of an earlier run replayed", "Valid reference from a previous run of this claim",
                  lambda db: earlier.ref, {"REPLAY"}, compromised=False, reads_only=True),
            _case(base, "e3", "E", "Signed row copied forward", "The old row re-inserted as a newer entry",
                  row_copied_forward, {"SIGNATURE_FORGERY"})]


def attack_f(base: Published) -> list[dict[str, Any]]:
    def attempt(key: str, name: str, injected: str, read: Callable[[AgentParticipant, AgentParticipant], Any],
                participant: str) -> dict[str, Any]:
        db = isolate(base.db, f"attack-{key}.sqlite3")
        adk, crew = participants(base, db)
        got = read(adk, crew)
        events = (crew if participant == "crew" else adk).security_events
        outcome = {"investigator": got if participant == "adk" else None, "forensic": got if participant == "crew" else None,
                   "events": events, "receipts": len(adk.consumed) + len(crew.consumed)}
        return _row("F", name, injected, outcome, {"UNAUTHORISED_ACCESS"}, False)

    def wrong_key(adk: AgentParticipant, crew: AgentParticipant) -> Any:
        adk._writer = Writer("adk", b"\0" * 32)
        return adk.consume_context(INVESTIGATOR, base.ref)

    return [attempt("f1", "Forensic Agent reads the Investigator's report", "Section it may not read",
                    lambda a, c: c.consume_context(FORENSIC, base.investigator_ref), "crew"),
            attempt("f2", "CrewAI principal acts as the Investigator", "Identity of another framework's agent",
                    lambda a, c: c.consume_context(INVESTIGATOR, base.ref), "crew"),
            attempt("f3", "Unknown reader", "A consumer with no read permission",
                    lambda a, c: a.consume_context("claimant_portal", base.ref), "adk"),
            attempt("f4", "Wrong writer key", "A key that is not the principal's", wrong_key, "adk")]


def attack_g(base: Published) -> list[dict[str, Any]]:
    """The triggers stay in place here: production protection, not the mutation fixture."""
    db = isolate(base.db, "attack-g.sqlite3")
    before = stored(db, base.run_id, base.ref.run_seq)
    rows = []
    for name, sql, args in (
            ("Update of a published entry", "UPDATE entries SET payload = '{}' WHERE run_id = ? AND run_seq = ?",
             (base.run_id, base.ref.run_seq)),
            ("Delete of a published entry", "DELETE FROM entries WHERE run_id = ? AND run_seq = ?",
             (base.run_id, base.ref.run_seq)),
            ("Update of the run registration", "UPDATE runs SET claim_id = 'X' WHERE run_id = ?", (base.run_id,)),
            ("Delete of the run registration", "DELETE FROM runs WHERE run_id = ?", (base.run_id,))):
        conn = sqlite3.connect(assert_disposable(db))
        try:
            conn.execute(sql, args)
            conn.commit()
            refused = False
        except sqlite3.DatabaseError as exc:
            refused = "append-only" in str(exc)
        finally:
            conn.close()
        rows.append({"attack": "G", "name": name, "injected": "Raw SQL with the triggers in place", "detected":
                     ["APPEND_ONLY_TRIGGER"] if refused else [], "expected_detection": ["APPEND_ONLY_TRIGGER"],
                     "payload_integrity": "NOT_CHECKED", "signature": "NOT_CHECKED", "investigator_received": False,
                     "forensic_received": False, "receipts": 0, "events": [],
                     "passed": refused and stored(db, base.run_id, base.ref.run_seq) == before})
    return rows


def attack_h(base: Published) -> list[dict[str, Any]]:
    def invalid_json(db: Path) -> ContextRef:
        set_columns(db, base.run_id, base.ref.run_seq, payload="{not json")
        return base.ref

    def wrong_shape(db: Path) -> ContextRef:
        edit_payload(db, base.run_id, base.ref.run_seq, lambda p: {**p, "report": {"claim_id": base.claim_id}})
        return base.ref

    def signed_but_malformed(db: Path) -> ContextRef:
        board = Blackboard(db)
        board.retract(base.run_id, base.claim_id, base.ref.run_seq, "controller", "orchestrator", "test",
                      writer=load_writer(db, "orchestrator"))
        post = board.post(base.run_id, base.claim_id, EVIDENCE, "adk", SECTION,
                          {"report": {"claim_id": base.claim_id}, "report_sha256": "x"},
                          document_fingerprint(base.documents), writer=load_writer(db, "adk"))
        return base.ref.model_copy(update={"run_seq": post.run_seq, "sha256": post.payload_sha256})

    return [_case(base, "h1", "H", "Payload is not valid JSON", "Unparsable text in the entry", invalid_json,
                  {"PAYLOAD_TAMPERING"}),
            _case(base, "h2", "H", "Report without its required fields", "Most of the report removed", wrong_shape,
                  {"PAYLOAD_TAMPERING"}),
            _case(base, "h3", "H", "Malformed report signed with a real key", "Valid HMAC, invalid EvidenceReport",
                  signed_but_malformed, {"MALFORMED_REPORT"}, compromised=False)]


def attack_i(base: Published) -> list[dict[str, Any]]:
    def rehashed(db: Path) -> ContextRef:
        edit_payload(db, base.run_id, base.ref.run_seq, change_amount, rehash=True)
        return base.ref

    def rehashed_with_matching_reference(db: Path) -> ContextRef:
        rehashed(db)
        return base.ref.model_copy(update={"sha256": stored(db, base.run_id, base.ref.run_seq)["payload_sha256"]})

    return [_case(base, "i1", "I", "Payload and SHA-256 changed", "Hash recomputed; no key to sign it", rehashed,
                  {"HASH_AND_PAYLOAD_TAMPERING"}),
            _case(base, "i2", "I", "Payload, SHA-256 and reference changed", "The attacker also controls the ContextRef",
                  rehashed_with_matching_reference, {"SIGNATURE_FORGERY"})]


def attack_j(base: Published) -> dict[str, Any]:
    db = isolate(base.db, "attack-j.sqlite3")
    outcome = normal_pathway(base, db, base.ref)
    final = outcome["final"]
    same = (final.final_decision, final.decided_by) == (base.bundle.final_report.final_decision,
                                                        base.bundle.final_report.decided_by)
    return {"attack": "J", "name": "Clean report accepted", "injected": "Nothing: control", "detected": [],
            "expected_detection": [], "payload_integrity": "MATCH", "signature": "VALID",
            "investigator_received": outcome["investigator"] == base.bundle.adk.evidence_report,
            "forensic_received": outcome["forensic"] == base.bundle.adk.evidence_report,
            "receipts": outcome["receipts"], "events": [], "final_decision": final.final_decision.value,
            "decided_by": final.decided_by, "review_status": final.review_status,
            "human_review_required": final.requires_human_approval,
            "payout": str(final.payout.payout) if final.payout else None,
            "passed": (outcome["investigator"] == outcome["forensic"] == base.bundle.adk.evidence_report and same
                       and not outcome["events"] and outcome["receipts"] == 2 and final.review_status == "COMPLETE")}


def run_attacks(base: Published, other: Published, earlier: Published) -> list[dict[str, Any]]:
    return [attack_a(base), attack_b(base), *attack_c(base), *attack_d(base, other), *attack_e(base, earlier),
            *attack_f(base), *attack_g(base), *attack_h(base), *attack_i(base), attack_j(base)]


# =========================================================================== whole investigations

def repair_amount(report: EvidenceReport | None) -> str:
    fact = next((f for f in (report.facts if report else []) if f.field == "repair_cost"), None)
    if fact is None:
        raise LabError("the Evidence report holds no repair amount")
    return fact.value


def run_summary(bundle: ClaimRunBundle, label: str) -> dict[str, Any]:
    final = bundle.final_report
    return {"label": label, "run_id": bundle.run_id, "claim_id": bundle.claim_id,
            "final_decision": final.final_decision.value, "deterministic_decision": final.deterministic_decision.value,
            "decided_by": final.decided_by, "review_status": final.review_status,
            "human_review_required": final.requires_human_approval, "approval_status": final.approval_status,
            "payout": str(final.payout.payout) if final.payout else None,
            "tasks": {t.task_id: t.status.value for t in bundle.execution_plan.tasks},
            "adk_status": bundle.adk.status.value, "crewai_status": bundle.crewai.status.value,
            "investigator_received": any(c.consumer == INVESTIGATOR for c in bundle.adk.consumed_context),
            "forensic_received": any(c.consumer == FORENSIC for c in bundle.crewai.consumed_context),
            "forensic_agent_ran": any(r.agent == FORENSIC for r in bundle.crewai.agent_runs),
            "agent_reports_adjudicated": bundle.outcome.adjudicated_reports,
            "events": [e.model_dump(mode="json") for e in bundle.security_events]}


async def a2a_bypass(run_id: str, claim: Any, documents: list, crew_card: str, ref: ContextRef) -> list[dict[str, Any]]:
    """The two ways around the blackboard over A2A: no ContextRef at all (to the real service), or the report inside
    the message (refused by the message contract the service parses with)."""
    empty = await request_forensics(claim, documents, run_id, crew_card, timeout=60.0, context=[])
    message = contract.forensic_request(run_id, claim.claim_id, documents, [ref]).model_dump(mode="json")
    message["payload"]["report"] = {"claim_id": claim.claim_id}
    try:
        contract.parse(json.dumps(message))
        inline = "accepted"
    except contract.ContractError as exc:
        inline = exc.code
    no_context = next((c for c in ("CONTEXT_REJECTED",) if any(c in e for e in empty.errors)), None)
    return [{"check": "Request without a ContextRef", "result": no_context or "accepted", "passed": no_context is not None},
            {"check": "Report sent inside the A2A message", "result": inline, "passed": inline == "INVALID_MESSAGE"}]


# =========================================================================== the whole test

def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def protected_files() -> dict[str, Path]:
    return {"blackboard": config.RUNTIME_DIR / "blackboard.sqlite3", "ledger": config.RUNTIME_DIR / "gemini_requests.sqlite3",
            "env": config.PROJECT_ROOT / ".env", "claims_database": config.DB_PATH,
            **{s.filename: SAMPLES_DIR / s.filename for s in SAMPLES}}


@contextmanager
def isolated_environment(root: Path) -> Iterator[None]:
    """This process and the services it starts: offline, no key, a temporary ledger, the disposable blackboard."""
    saved = dict(os.environ)
    for name in SECRETS:
        os.environ.pop(name, None)
    os.environ.update({execution_mode.ENV_VAR: execution_mode.OFFLINE, "CLAIMSLEUTH_GEMINI_LEDGER": str(root / "ledger.sqlite3"),
                       ENV_VAR: str(root / "workflow.sqlite3"), TEST_MODE_ENV: "1"})
    try:
        with execution_mode.forced(execution_mode.OFFLINE):
            yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def run_lab(out_dir: Path | None = OUT_DIR) -> dict[str, Any]:
    root = disposable_root()
    before = {name: _digest(path) for name, path in protected_files().items()}
    pdf_path = SAMPLES_DIR / TARGET.filename
    pdf, control_pdf = pdf_path.read_bytes(), (SAMPLES_DIR / CONTROL.filename).read_bytes()
    fingerprint = hashlib.sha256(pdf).hexdigest()
    lab_id = f"EI-{utc_now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    try:
        with isolated_environment(root):
            ws = Workspace(root / "workspace", config.DATA_DIR)
            ws.prepare()
            claim_id = ws.materialise_upload(ws.store_upload(pdf, TARGET.filename).upload_id)
            control_id = ws.materialise_upload(ws.store_upload(control_pdf, CONTROL.filename).upload_id)
            db_path = ws.data_dir / "claimsleuth.db"
            shutil.copyfile(config.DB_PATH, db_path)
            workflow = root / "workflow.sqlite3"
            services = EvalServices(ws.data_dir, root / "services", execution_mode.OFFLINE, _free_port(), _free_port(),
                                    blackboard=workflow)
            await asyncio.to_thread(services.start)

            async def investigate(claim: str, tag: str, attack: Callable[[ContextRef], None] | None = None,
                                  moment: str | None = None) -> ClaimRunBundle:
                run_id, cls = f"RUN-lab-{tag}-{uuid.uuid4().hex[:8]}", InvestigationManager
                extra: dict[str, Any] = {} if attack is None else {"attack": attack, "moment": moment}
                if attack is not None:
                    cls = TamperingManager
                return await cls(claim, run_id=run_id, mcp_url=services.mcp_url, crew_card_url=services.crew_card,
                                 data_dir=ws.data_dir, db_path=db_path, adk_llm_mode="test-double",
                                 blackboard_path=workflow, **extra).run()

            def amount_attack(ref: ContextRef) -> None:
                edit_payload(workflow, ref.run_id, ref.run_seq, change_amount)

            try:
                clean = await investigate(claim_id, "clean")
                earlier = await investigate(claim_id, "earlier")
                control_clean = await investigate(control_id, "control-clean")
                tampered = await investigate(claim_id, "tampered", amount_attack, "after_publication")
                control_tampered = await investigate(control_id, "control-tampered", amount_attack, "after_publication")
                control_late = await investigate(control_id, "control-late", amount_attack, "after_consumption")
                monitor = run_monitor(tampered.run_id, workflow)
                claim, documents = load_claim(claim_id, ws.data_dir)
                bypass = await a2a_bypass(tampered.run_id, claim, documents, services.crew_card,
                                          P.task(tampered.execution_plan, "evidence").result_ref)
            finally:
                services.stop()

            original = repair_amount(clean.adk.evidence_report)
            base = publish(root / "published.sqlite3", clean, ws.data_dir, db_path)
            other = publish(isolate(base.db, "other.sqlite3"), control_clean, ws.data_dir, db_path)
            previous = publish(root / "earlier.sqlite3", earlier, ws.data_dir, db_path)
            attacks = run_attacks(base, other, previous)

            copy = isolate(base.db, "demonstration.sqlite3")
            untouched = stored(copy, base.run_id, base.ref.run_seq)
            edit_payload(copy, base.run_id, base.ref.run_seq, change_amount)
            altered = stored(copy, base.run_id, base.ref.run_seq)
            base_row = stored(base.db, base.run_id, base.ref.run_seq)
            changed = difference(json.loads(untouched["payload"]), json.loads(altered["payload"]))
            requests = gemini_rate.total_requests()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    after = {name: _digest(path) for name, path in protected_files().items()}
    tampered_view = run_summary(tampered, "Tampered after publication")
    result = {
        "lab_id": lab_id, "ran_at": utc_now().isoformat(), "kind": "CONTROLLED_ADVERSARIAL_TEST", "test_mode": True,
        "execution": "offline: labelled test doubles for the agents' reasoning; real blackboard, HMAC, A2A, MCP and adjudicator",
        "claim_alias": "CLM-002", "claim_id": claim_id, "control_claim_id": control_id,
        "source_pdf": TARGET.filename, "pdf_fingerprint": fingerprint[:12],
        "pdf_fingerprint_unchanged": _digest(pdf_path) == fingerprint,
        "original_repair_amount": original, "tampered_repair_amount": tampered_amount(original),
        "tamper": {"changed_fields": changed, "hash_preserved": altered["payload_sha256"] == base.hash_at_publication,
                   "signature_preserved": altered["signature"] == base.signature_at_publication,
                   "published_entry_unchanged": base_row == untouched},
        "clean_run": run_summary(clean, "Clean"), "tampered_run": tampered_view,
        "control_clean_run": run_summary(control_clean, "Clean (approves)"),
        "control_tampered_run": run_summary(control_tampered, "Tampered after publication"),
        "control_late_run": run_summary(control_late, "Tampered after the agents read it"),
        "original_report_kept": repair_amount(tampered.adk.evidence_report) == original,
        "monitoring_events": len(monitor.get("security_events", [])),
        "a2a_bypass": bypass, "attacks": attacks,
        "summary": {"total": len(attacks), "passed": sum(a["passed"] for a in attacks),
                    "failed": sum(not a["passed"] for a in attacks)},
        "gemini_requests": requests, "protected_files_unchanged": before == after}
    result["passed"] = lab_passed(result)
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{lab_id}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def lab_passed(r: dict[str, Any]) -> bool:
    bad, clean = r["tampered_run"], r["clean_run"]
    control_bad, control_late, control_clean = r["control_tampered_run"], r["control_late_run"], r["control_clean_run"]
    return bool(r["summary"]["failed"] == 0 and r["pdf_fingerprint_unchanged"] and r["protected_files_unchanged"]
                and r["gemini_requests"] == 0 and r["original_report_kept"] and r["tamper"]["hash_preserved"]
                and r["tamper"]["signature_preserved"] and r["tamper"]["published_entry_unchanged"]
                and all(c["passed"] for c in r["a2a_bypass"]) and clean["investigator_received"]
                and clean["forensic_received"] and not clean["events"] and bad["events"]
                and not bad["investigator_received"] and not bad["forensic_received"] and not bad["forensic_agent_ran"]
                and bad["human_review_required"] and control_clean["final_decision"] == Decision.APPROVE.value
                and not control_clean["events"] and control_bad["final_decision"] != Decision.APPROVE.value
                and control_bad["payout"] is None and control_bad["human_review_required"]
                and control_late["final_decision"] != Decision.APPROVE.value and control_late["payout"] is None
                and control_late["human_review_required"] and control_late["events"])


# =========================================================================== from the dashboard

def latest_result(out_dir: Path = OUT_DIR) -> dict[str, Any] | None:
    files = sorted(out_dir.glob("*.json"), key=lambda p: p.stat().st_mtime) if out_dir.exists() else []
    for path in reversed(files):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue                        # an unreadable file is skipped, never guessed
    return None


class LabJob:
    """One test at a time, as its own process: whatever mode the dashboard runs in, the test stays offline."""

    def __init__(self, out_dir: Path = OUT_DIR, module: str = "claimsleuth.dashboard.evidence_integrity_lab") -> None:
        self.out_dir, self.module = out_dir, module
        self.state: dict[str, Any] | None = None
        self._task: asyncio.Task | None = None

    def busy(self) -> bool:
        return self.state is not None and self.state["state"] == "running"

    def start(self) -> None:
        self.state = {"state": "running", "started_at": utc_now().isoformat(), "error": None}

        async def run() -> None:
            env = {k: v for k, v in os.environ.items() if k not in SECRETS}
            error = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    str(MAIN_PYTHON), "-m", self.module, "--out", str(self.out_dir),
                    cwd=str(config.PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                    env={**env, "PYTHONIOENCODING": "utf-8", execution_mode.ENV_VAR: execution_mode.OFFLINE})
                _, err = await proc.communicate()
                if proc.returncode not in (0, CHECK_FAILED):   # a failed check is in the result file; this is a crash
                    lines = err.decode("utf-8", "replace").strip().splitlines()
                    error = (lines[-1] if lines else f"exit {proc.returncode}")[:300]
            except OSError as exc:
                error = f"{type(exc).__name__}: the test could not start"
            self.state.update({"state": "failed" if error else "completed", "finished_at": utc_now().isoformat(),
                               "error": error})

        self._task = asyncio.create_task(run())


def main() -> int:
    parser = argparse.ArgumentParser(description="Controlled evidence tampering test (offline, disposable databases)")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args()
    result = asyncio.run(run_lab(args.out))
    print(json.dumps({k: result[k] for k in ("lab_id", "claim_id", "original_repair_amount", "tampered_repair_amount",
                                             "summary", "passed")}, indent=2))
    return 0 if result["passed"] else CHECK_FAILED


if __name__ == "__main__":
    sys.exit(main())
