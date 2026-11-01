"""Human review of a finished investigation (Approve / Reject / Request more information, with notes).

A reviewer action is recorded next to the run, in runtime/runs/<run_id>/human_review.jsonl, one JSON line per
action. It never changes the automated result: final_report.json, bundle.json and audit.jsonl are only read.
Each record keeps a copy of the automated decision it was made against, the reviewer's name and a timestamp.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


def load_reviews(run_dir: Path) -> list[HumanReviewRecord]:
    path = run_dir / REVIEW_FILE
    if not path.exists():
        return []
    return [HumanReviewRecord.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def record_review(run_dir: Path, bundle: ClaimRunBundle, review: HumanReviewInput) -> HumanReviewRecord:
    final = bundle.final_report
    if final is None:
        raise ReviewNotAllowed("this run has no automated decision to review")
    if not final.requires_human_approval:
        raise ReviewNotAllowed("the automated decision does not require human approval")
    record = HumanReviewRecord(**review.model_dump(), review_id=uuid.uuid4().hex[:12], ts=utc_now(),
                               run_id=bundle.run_id, claim_id=bundle.claim_id,
                               automated_decision=final.final_decision.value, automated_decided_by=final.decided_by)
    with _LOCK, (run_dir / REVIEW_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.model_dump(mode="json")) + "\n")
    return record
