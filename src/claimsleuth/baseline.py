"""DETERMINISTIC BASELINE: the original ClaimSleuth rules in ordinary Python, with no LLM.

Decision precedence (fail closed):
  1. prompt injection in any untrusted text         -> ESCALATE
  2. evidence conflict (serial, policy, claimant, dates, claim history) or unknown policy -> ESCALATE
  3. deterministic ineligibility (before purchase, expired, excluded cause) -> REJECT
  4. required document or field missing             -> REQUEST_EVIDENCE
  5. otherwise                                      -> APPROVE with a computed settlement
REJECT, ESCALATE and payouts at or above the threshold need a human approval.

Run:  python -m claimsleuth.baseline CS-001 [--save] [--json]
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

from claimsleuth import config
from claimsleuth.datastore import load_claim, load_policies
from claimsleuth.documents import ExtractedFields, extract_fields
from claimsleuth.history import lookup_history
from claimsleuth.rules import (compute_settlement, coverage_window, find_exclusion, is_valid_serial_format,
                               normalise_serial, parse_iso_date, parse_money)
from claimsleuth.schemas import (BaselineResult, CheckOutcome, ClaimInput, Decision, DocumentKind,
                                 EvidenceDocument, EvidenceRef, ExecutionStatus, GuardrailEvent,
                                 HistoryRecord, PolicyTerms, RuleCheck, make_evidence_id, utc_now)
from claimsleuth.security import screen_text

# Checks whose failure means conflicting evidence (escalate) vs. a policy rule (reject).
CONFLICT_CHECKS = ("policy_known", "serial_format", "serial_consistency", "policy_consistency",
                   "claimant_consistency", "document_consistency", "inspection_after_incident", "claim_history")
INELIGIBILITY_CHECKS = ("incident_after_purchase", "coverage_window", "exclusions")


def new_run_id() -> str:
    return f"RUN-{uuid.uuid4().hex[:12]}"


class _Checks:
    def __init__(self) -> None:
        self.items: list[RuleCheck] = []

    def add(self, name: str, outcome: CheckOutcome, detail: str, *refs: EvidenceRef | None) -> None:
        self.items.append(RuleCheck(name=name, outcome=outcome, detail=detail,
                                    evidence_ids=[r.evidence_id for r in refs if r is not None]))

    def failed(self, names: tuple[str, ...]) -> list[RuleCheck]:
        return [c for c in self.items if c.name in names and c.outcome == CheckOutcome.FAIL]


def _first(docs: list[ExtractedFields], kind: DocumentKind) -> ExtractedFields | None:
    return next((d for d in docs if d.doc.kind == kind), None)


def evaluate(claim: ClaimInput, documents: list[EvidenceDocument], policy: PolicyTerms | None,
             history: list[HistoryRecord], run_id: str) -> BaselineResult:
    """Pure function of its inputs (apart from timestamps)."""
    started_at, t0 = utc_now(), time.perf_counter()
    checks = _Checks()
    missing: list[str] = []
    guardrails: list[GuardrailEvent] = []

    # 1. Screen every piece of untrusted text. Nothing here changes a value used by a rule.
    guardrails += screen_text(claim.claimant_statement, "claim.claimant_statement", run_id)
    guardrails += screen_text(claim.reported_issue, "claim.reported_issue", run_id)
    for doc in documents:
        guardrails += screen_text(doc.text, f"doc:{doc.doc_id}", run_id)
    injection = [g for g in guardrails if g.kind == "prompt_injection"]

    # 2. Policy and documents.
    if policy is None:
        checks.add("policy_known", CheckOutcome.FAIL, f"Policy {claim.policy_id} is not in the policy register.")
    else:
        checks.add("policy_known", CheckOutcome.PASS, f"Policy {policy.policy_id}: {policy.name}.")

    extracted = [extract_fields(d) for d in documents]
    invoice = _first(extracted, DocumentKind.INVOICE)
    diag = _first(extracted, DocumentKind.DIAGNOSTIC)
    evidence = [ref for e in extracted for ref in e.values.values()]

    needs_receipt = policy.requires_original_receipt if policy else True
    needs_diag = policy.requires_diagnostic_report if policy else True
    if needs_receipt and invoice is None:
        missing.append("purchase invoice")
    if needs_diag and diag is None:
        missing.append("diagnostic / repair report")
    checks.add("required_documents", CheckOutcome.FAIL if missing else CheckOutcome.PASS,
               "Missing: " + ", ".join(missing) if missing else "Invoice and diagnostic report present.")

    def field(doc: ExtractedFields | None, name: str, label: str) -> EvidenceRef | None:
        ref = doc.values.get(name) if doc else None
        if doc is not None and ref is None:
            missing.append(f"{label} ({doc.doc.doc_id})")
        return ref

    purchase_ref = field(invoice, "purchase_date", "purchase date")
    inv_serial_ref = field(invoice, "serial_number", "invoice serial number")
    diag_serial_ref = field(diag, "serial_number", "diagnostic serial number")
    diagnosis_ref = field(diag, "diagnosis", "technician diagnosis")
    cost_ref = field(diag, "repair_cost", "repair cost")
    inspection_ref = diag.values.get("inspection_date") if diag else None

    conflicts = [c for e in extracted for c in e.conflicts]
    checks.add("document_consistency", CheckOutcome.FAIL if conflicts else CheckOutcome.PASS,
               "; ".join(f"{a.field} differs within {a.doc_id}: '{a.value}' vs '{b.value}'" for a, b in conflicts)
               or "No field is stated twice with different values.",
               *[r for pair in conflicts for r in pair])

    # 3. Serial numbers: format and cross-document consistency.
    serials = {"claim form": normalise_serial(claim.serial_number),
               "invoice": normalise_serial(inv_serial_ref.value if inv_serial_ref else None),
               "diagnostic report": normalise_serial(diag_serial_ref.value if diag_serial_ref else None)}
    present = {k: v for k, v in serials.items() if v}
    bad_format = [f"{k} '{v}'" for k, v in present.items() if not is_valid_serial_format(v)]
    if not present:
        checks.add("serial_format", CheckOutcome.NOT_EVALUABLE, "No serial number available.")
    else:
        checks.add("serial_format", CheckOutcome.FAIL if bad_format else CheckOutcome.PASS,
                   "Invalid format: " + ", ".join(bad_format) if bad_format else "All serials well formed.",
                   inv_serial_ref, diag_serial_ref)
    if len(present) < 2:
        checks.add("serial_consistency", CheckOutcome.NOT_EVALUABLE, "Fewer than two serial numbers to compare.")
    elif len(set(present.values())) > 1:
        checks.add("serial_consistency", CheckOutcome.FAIL,
                   "Serial mismatch: " + ", ".join(f"{k} '{v}'" for k, v in present.items()),
                   inv_serial_ref, diag_serial_ref)
    else:
        checks.add("serial_consistency", CheckOutcome.PASS, f"Serial {next(iter(present.values()))} consistent.",
                   inv_serial_ref, diag_serial_ref)
    serial = serials["invoice"] or serials["claim form"] or serials["diagnostic report"]

    # 4. Policy and claimant named on the documents must match the claim.
    inv_policy = invoice.values.get("policy_id") if invoice else None
    if inv_policy is None:
        checks.add("policy_consistency", CheckOutcome.NOT_EVALUABLE, "Invoice names no policy.")
    elif inv_policy.value.strip().upper() != claim.policy_id:
        checks.add("policy_consistency", CheckOutcome.FAIL,
                   f"Invoice names policy '{inv_policy.value}', claim names '{claim.policy_id}'.", inv_policy)
    else:
        checks.add("policy_consistency", CheckOutcome.PASS, "Invoice policy matches the claim.", inv_policy)

    name_refs = [e.values["customer_name"] for e in extracted if "customer_name" in e.values]
    wrong_names = [r for r in name_refs if r.value.strip().casefold() != claim.claimant_name.strip().casefold()]
    if not name_refs:
        checks.add("claimant_consistency", CheckOutcome.NOT_EVALUABLE, "No customer name on the documents.")
    else:
        checks.add("claimant_consistency", CheckOutcome.FAIL if wrong_names else CheckOutcome.PASS,
                   "; ".join(f"{r.doc_id} names '{r.value}'" for r in wrong_names) or "Claimant name matches.",
                   *name_refs)

    # 5. Dates: purchase <= incident <= inspection, and the coverage window.
    purchase = parse_iso_date(purchase_ref.value) if purchase_ref else None
    if purchase_ref is not None and purchase is None:
        missing.append(f"readable purchase date ({purchase_ref.doc_id})")
    inspection = parse_iso_date(inspection_ref.value) if inspection_ref else None
    if inspection is None:
        checks.add("inspection_after_incident", CheckOutcome.NOT_EVALUABLE, "No readable inspection date.")
    elif inspection < claim.incident_date:
        checks.add("inspection_after_incident", CheckOutcome.FAIL,
                   f"Inspection {inspection} is before the reported incident {claim.incident_date}.", inspection_ref)
    else:
        checks.add("inspection_after_incident", CheckOutcome.PASS,
                   f"Inspection {inspection} is on/after incident {claim.incident_date}.", inspection_ref)

    if purchase is None or policy is None:
        checks.add("incident_after_purchase", CheckOutcome.NOT_EVALUABLE, "Purchase date or policy unavailable.")
        checks.add("coverage_window", CheckOutcome.NOT_EVALUABLE, "Purchase date or policy unavailable.")
    else:
        window = coverage_window(purchase, claim.incident_date, policy.duration_months)
        if window["incident_before_purchase"]:
            checks.add("incident_after_purchase", CheckOutcome.FAIL,
                       f"Incident {claim.incident_date} precedes purchase {purchase}.", purchase_ref)
            checks.add("coverage_window", CheckOutcome.NOT_EVALUABLE, "Incident precedes purchase.")
        else:
            checks.add("incident_after_purchase", CheckOutcome.PASS,
                       f"Purchase {purchase} precedes incident {claim.incident_date}.", purchase_ref)
            checks.add("coverage_window", CheckOutcome.PASS if window["within_window"] else CheckOutcome.FAIL,
                       f"Coverage {purchase} to {window['expiry_date']} ({policy.duration_months} months); "
                       f"incident {claim.incident_date}"
                       + ("" if window["within_window"] else f" is {-window['days_from_expiry']} days after expiry")
                       + ".", purchase_ref)

    # 6. Exclusions, matched against the technician's diagnosis (higher authority than customer text).
    if diagnosis_ref is None or policy is None:
        checks.add("exclusions", CheckOutcome.NOT_EVALUABLE, "No technician diagnosis or policy.")
    else:
        hit = find_exclusion(diagnosis_ref.value, policy.exclusions)
        checks.add("exclusions", CheckOutcome.FAIL if hit else CheckOutcome.PASS,
                   f"Diagnosis matches exclusion '{hit}'." if hit else "No policy exclusion in the diagnosis.",
                   diagnosis_ref)

    # 7. Claim history from SQLite: serial used by another claimant, or repeated claims.
    window_start = claim.incident_date - timedelta(days=config.REPEAT_CLAIM_WINDOW_DAYS)
    other_claimant = [h for h in history if h.claimant_id != claim.claimant_id]
    recent = [h for h in history if window_start <= h.claim_date <= claim.incident_date]
    history_refs = [EvidenceRef(evidence_id=make_evidence_id("claim_history", None, h.claim_id),
                                doc_id="claim_history", line=None,
                                field="prior_claim", value=f"{h.claim_id} {h.claim_date} {h.outcome}",
                                quote=f"{h.claim_id} | {h.serial_number} | {h.claimant_id} | {h.claim_date} | {h.outcome}",
                                authority="HISTORY_DB") for h in history]
    evidence += history_refs
    if not serial:
        checks.add("claim_history", CheckOutcome.NOT_EVALUABLE, "No serial number to look up.")
    elif other_claimant:
        checks.add("claim_history", CheckOutcome.FAIL,
                   f"Serial {serial} was claimed before by another claimant: "
                   + ", ".join(f"{h.claim_id} ({h.claimant_id}, {h.claim_date})" for h in other_claimant), *history_refs)
    elif len(recent) >= config.REPEAT_CLAIM_LIMIT:
        checks.add("claim_history", CheckOutcome.FAIL,
                   f"{len(recent)} earlier claims on serial {serial} within {config.REPEAT_CLAIM_WINDOW_DAYS} days.",
                   *history_refs)
    else:
        checks.add("claim_history", CheckOutcome.PASS,
                   f"{len(history)} earlier claim(s) on serial {serial}; no repeat pattern.", *history_refs)

    # 8. Settlement inputs.
    repair_cost = parse_money(cost_ref.value) if cost_ref else None
    if cost_ref is not None and repair_cost is None:
        missing.append(f"readable repair cost ({cost_ref.doc_id})")

    # ---- decision, in precedence order
    reasons: list[str] = []
    settlement = None
    conflict_fails = checks.failed(CONFLICT_CHECKS)
    ineligible = checks.failed(INELIGIBILITY_CHECKS)
    if injection:
        decision = Decision.ESCALATE
        reasons.append("Prompt-injection text found in " + ", ".join(g.source for g in injection)
                       + "; the claim needs a human reviewer.")
    elif conflict_fails:
        decision = Decision.ESCALATE
        reasons += [f"{c.name}: {c.detail}" for c in conflict_fails]
    elif ineligible:
        decision = Decision.REJECT
        reasons += [f"{c.name}: {c.detail}" for c in ineligible]
    elif missing:
        decision = Decision.REQUEST_EVIDENCE
        reasons.append("Missing: " + "; ".join(missing))
    else:
        decision = Decision.APPROVE
        settlement = compute_settlement(repair_cost, policy.deductible, policy.max_coverage_limit,
                                        policy.depreciation_rate)
        reasons.append("All deterministic checks passed. " + settlement.explanation)
    checks.add("settlement", CheckOutcome.PASS if settlement else CheckOutcome.NOT_EVALUABLE,
               settlement.explanation if settlement else "Not computed: the claim is not approvable.", cost_ref)

    if decision in (Decision.REJECT, Decision.ESCALATE):
        needs_human = True
    elif decision == Decision.APPROVE:
        needs_human = settlement.payout >= config.HUMAN_APPROVAL_THRESHOLD
    else:
        needs_human = False

    return BaselineResult(
        run_id=run_id, claim_id=claim.claim_id, status=ExecutionStatus.COMPLETED, decision=decision,
        decision_reasons=reasons, checks=checks.items, evidence_refs=evidence, missing_information=missing,
        settlement=settlement, requires_human_approval=needs_human, guardrail_events=guardrails,
        started_at=started_at, finished_at=utc_now(), execution_ms=round((time.perf_counter() - t0) * 1000, 3),
    )


def run_baseline(claim_id: str, run_id: str | None = None, data_dir: Path = config.DATA_DIR,
                 db_path: Path = config.DB_PATH) -> BaselineResult:
    """Load a stored claim and evaluate it. Any loading error becomes a FAILED result, never a guess."""
    run_id = run_id or new_run_id()
    started_at, t0 = utc_now(), time.perf_counter()
    try:
        claim, documents = load_claim(claim_id, data_dir)
        policy = load_policies(data_dir).get(claim.policy_id)
        return investigate(claim, documents, policy, run_id, db_path)
    except Exception as exc:  # reported as a failed run, with the error text
        return BaselineResult(run_id=run_id, claim_id=claim_id, status=ExecutionStatus.FAILED,
                              started_at=started_at, finished_at=utc_now(),
                              execution_ms=round((time.perf_counter() - t0) * 1000, 3),
                              errors=[f"{type(exc).__name__}: {exc}"])


def investigate(claim: ClaimInput, documents: list[EvidenceDocument], policy: PolicyTerms | None,
                run_id: str, db_path: Path = config.DB_PATH) -> BaselineResult:
    """Evaluate an already-validated claim, reading its serial's history from SQLite."""
    serial = ""
    for doc in documents:
        if doc.kind == DocumentKind.INVOICE:
            serial = extract_fields(doc).value("serial_number") or ""
    serial = normalise_serial(serial or claim.serial_number)
    history = lookup_history(serial, claim.claimant_id, claim.claim_id, db_path).records if serial else []
    return evaluate(claim, documents, policy, history, run_id)


def save_result(result: BaselineResult, runtime_dir: Path = config.RUNTIME_DIR) -> Path:
    from claimsleuth.render import baseline_markdown
    out = runtime_dir / "reports" / result.run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "baseline.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    (out / "baseline.md").write_text(baseline_markdown(result), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    from claimsleuth.render import baseline_markdown
    parser = argparse.ArgumentParser(description="Run the DETERMINISTIC BASELINE on one stored claim.")
    parser.add_argument("claim_id")
    parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    parser.add_argument("--save", action="store_true", help="write JSON + Markdown under runtime/reports/<run_id>/")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    result = run_baseline(args.claim_id)
    print(result.model_dump_json(indent=2) if args.json else baseline_markdown(result))
    if args.save:
        print(f"\nSaved to {save_result(result)}", file=sys.stderr)
    return 0 if result.status == ExecutionStatus.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
