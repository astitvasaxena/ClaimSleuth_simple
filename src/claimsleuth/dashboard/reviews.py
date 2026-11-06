"""Human review of a finished investigation (Approve / Reject / Request more information, with notes).

A reviewer action is recorded next to the run, in runtime/runs/<run_id>/human_review.jsonl, one JSON line per
action. It never changes the automated result: final_report.json, bundle.json and audit.jsonl are only read.
Each record keeps a copy of the automated decision it was made against, the reviewer's name and a timestamp.

The first APPROVED or REJECTED stands. The same action again changes nothing and is recorded as REPEATED; a different one is
refused and recorded as REFUSED, as is an approval of a run whose integrity checks failed. More information can always be
requested. Nothing here authorises a payout: the automated decision and its payout are the adjudicator's.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from claimsleuth.dashboard.recovery import final_review
from claimsleuth.schemas import ClaimRunBundle, utc_now

REVIEW_FILE = "human_review.jsonl"
ACTION_LABELS = {"APPROVED": "Approved", "REJECTED": "Rejected", "INFO_REQUESTED": "More information requested"}
_LOCK = threading.Lock()


class ReviewNotAllowed(ValueError):
    pass


class HumanReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["APPROVED", "REJECTED", "INFO_REQUESTED"]
    reviewer: str = Field(min_length=1, max_length=80)
    notes: str = Field(default="", max_length=2000)

    @field_validator("reviewer")
    @classmethod
    def _reviewer(cls, value: str) -> str:
        name = " ".join(value.split())            # one line, no control characters
        if not name:
            raise ValueError("enter the reviewer's name")
        return name

    @field_validator("notes")
    @classmethod
    def _notes(cls, value: str) -> str:
        return value.replace("\x00", "").strip()


class HumanReviewRecord(HumanReviewInput):
    review_id: str
    ts: datetime
    run_id: str
    claim_id: str
    automated_decision: str
    automated_decided_by: str
    effect: Literal["FINAL", "INFO", "REPEATED", "REFUSED", "RECORDED"] = "RECORDED"    # RECORDED: stored before effects existed


class Reviews(list):
    """The readable records, in order. `unreadable`: lines that could not be read (a write cut short). They are reported,
    never guessed at, and never stop the other records being read."""

    unreadable = 0

    @property
    def records(self) -> list[HumanReviewRecord]:
        return list(self)


def load_reviews(run_dir: Path) -> Reviews:
    out = Reviews()
    path = run_dir / REVIEW_FILE
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(HumanReviewRecord.model_validate_json(line))
            except ValueError:
                out.unreadable += 1
    return out


def _effect(review: HumanReviewInput, stands: HumanReviewRecord | None, problems: list[str]) -> tuple[str, str | None]:
    """What this action does, and why it is refused when it is."""
    if review.action == "INFO_REQUESTED":
        return "INFO", None
    if review.action == "APPROVED" and problems:
        return "REFUSED", f"an approval cannot override a failed integrity check: {'; '.join(problems)[:300]}"
    if stands is None:
        return "FINAL", None
    if stands.action == review.action:
        return "REPEATED", None
    return "REFUSED", f"this run was already {ACTION_LABELS[stands.action].lower()} by {stands.reviewer}"


def record_review(run_dir: Path, bundle: ClaimRunBundle, review: HumanReviewInput,
                  integrity: list[str] | None = None) -> HumanReviewRecord:
    """Every action is recorded, a refused one too: the audit shows what was attempted. `integrity`: problems found outside
    the stored report (the signed decision, the blackboard). Only an approval is stopped by them."""
    final = bundle.final_report
    if final is None:
        raise ReviewNotAllowed("this run has no automated decision to review")
    if not final.requires_human_approval:
        raise ReviewNotAllowed("the automated decision does not require human approval")
    if bundle.run_id != run_dir.name:
        raise ReviewNotAllowed("this report belongs to another run")
    problems = sorted({e.attack_type for e in bundle.security_events}) + list(integrity or [])
    path = run_dir / REVIEW_FILE
    with _LOCK:
        effect, refusal = _effect(review, final_review(load_reviews(run_dir)), problems)
        record = HumanReviewRecord(**review.model_dump(), review_id=uuid.uuid4().hex[:12], ts=utc_now(), run_id=bundle.run_id,
                                   claim_id=bundle.claim_id, automated_decision=final.final_decision.value,
                                   automated_decided_by=final.decided_by, effect=effect)
        complete = not path.exists() or path.read_bytes()[-1:] in (b"", b"\n")     # an earlier write may have been cut short
        with path.open("a", encoding="utf-8") as fh:
            fh.write(("" if complete else "\n") + json.dumps(record.model_dump(mode="json")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    if refusal:
        raise ReviewNotAllowed(refusal)
    return record
