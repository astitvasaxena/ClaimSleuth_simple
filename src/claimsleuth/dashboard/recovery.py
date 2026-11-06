"""What became of runs that stored no usable report, and protection against repeated requests.

A run is recorded on disk (dashboard_meta.json) before any work starts, so a process that dies leaves evidence. This module
only reads that evidence, plus the blackboard, and classifies:

    INTERRUPTED  started, no report stored, and not running in this process
    FAILED       the job ended with an error and produced no report
    DAMAGED      a report is stored but cannot be read
    RECOVERED    was INTERRUPTED, FAILED or DAMAGED, and a later investigation requested for it stored a report

Nothing is resumed. An agent report from an unfinished run is never reused: recovery is a new, explicit investigation with
its own run id, its own verification and its own decision. Original records are never changed. A run in any of these states
has no decision a person can act on, so it needs a human.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from claimsleuth.blackboard.monitor import run_monitor
from claimsleuth.blackboard.store import Blackboard, BlackboardError

KEY = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
PENDING_S = 60.0                      # a reservation with no run after this long was left by a crash
DECISION_FIELDS = ("final_decision", "decided_by", "requires_human_approval")   # present in every signed decision
TERMINAL_ACTIONS = ("APPROVED", "REJECTED")


class InvalidKey(ValueError):
    pass


def final_review(records: list) -> Any | None:
    """The reviewer's action that stands: the first APPROVED or REJECTED that was not refused."""
    return next((r for r in records if r.action in TERMINAL_ACTIONS and r.effect in ("FINAL", "RECORDED")), None)


def decision_problems(bundle: Any, signed: dict[str, Any] | None) -> list[str]:
    """Where the stored report differs from the decision signed on the blackboard. Fields, not the report hash: reports
    stored before later schema fields were added serialise differently, without having been changed."""
    final = bundle.final_report
    if signed is None or final is None:
        return []
    stored = {"final_decision": final.final_decision.value, "decided_by": final.decided_by,
              "requires_human_approval": final.requires_human_approval}
    problems = [f"{name}: stored {stored[name]!r}, signed {signed[name]!r}" for name in DECISION_FIELDS
                if name in signed and signed[name] != stored[name]]
    payout = str(final.payout.payout) if final.payout else None
    if "payout" in signed and signed["payout"] != payout:
        problems.append(f"payout: stored {payout!r}, signed {signed['payout']!r}")
    return problems


def _stage(run_id: str, path: Path | None) -> str:
    """How far a run got, from the blackboard alone. Read only."""
    if path is None:
        return "started"
    board = run_monitor(run_id, path, recent=500)
    if not board.get("registered"):
        return "started"
    if board.get("decision"):
        return "decided_not_stored"
    if board.get("sealed_at") is not None:
        return "sealed"
    if any(e["section"].startswith("report:") for e in board.get("events", [])):
        return "report_published"
    return "agents_running"


def _metas(runs_dir: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for path in runs_dir.glob("*/dashboard_meta.json") if runs_dir.exists() else []:
        try:
            out[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[path.parent.name] = {"state": "unreadable"}
    return out


def _row(run_id: str, runs_dir: Path, meta: dict[str, Any], recovered_by: str | None, board_path: Path | None) -> dict[str, Any]:
    damaged = (runs_dir / run_id / "bundle.json").exists() or meta.get("state") == "completed"
    state = ("RECOVERED" if recovered_by else "DAMAGED" if damaged else
             "FAILED" if meta.get("state") == "failed" else "INTERRUPTED")
    return {"run_id": run_id, "claim_id": meta.get("claim_id"), "state": state, "stage": _stage(run_id, board_path),
            "recovered_by": recovered_by, "submitted_at": meta.get("submitted_at"), "error": meta.get("error"),
            "human_review_required": state != "RECOVERED",
            "actual": run_id.startswith("RUN-dash-")}          # other prefixes come from scripts and demonstrations


def unfinished_runs(runs_dir: Path, active: set[str], load: Callable[[str], Any], board_path: Path | None) -> list[dict[str, Any]]:
    """Every run without a usable report, newest first. `active`: runs of this process. `load`: a run's report, or None."""
    metas = _metas(runs_dir)
    recovered_by = {m["recovers"]: rid for rid, m in metas.items() if m.get("recovers") and load(rid) is not None}
    candidates = dict.fromkeys(metas)
    candidates.update(dict.fromkeys(p.parent.name for p in runs_dir.glob("*/bundle.json")) if runs_dir.exists() else {})
    if board_path is not None and board_path.exists():
        try:
            candidates.update(dict.fromkeys(r["run_id"] for r in Blackboard(board_path).runs(limit=200)))
        except BlackboardError:
            pass
    rows = [_row(run_id, runs_dir, metas.get(run_id, {}), recovered_by.get(run_id), board_path)
            for run_id in candidates if run_id not in active and load(run_id) is None]
    return sorted(rows, key=lambda r: r["submitted_at"] or "", reverse=True)


class IdempotencyStore:
    """One file per request key, created atomically, so a retry finds the first request's run after a restart and two
    workers sharing a runtime folder cannot both start. The key comes from the client and names one user action; a request
    without a key is always a new investigation."""

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def _path(self, key: str) -> Path:
        if not KEY.fullmatch(key):
            raise InvalidKey("Idempotency-Key must be 8 to 80 letters, digits, '-' or '_'")
        return self.folder / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"

    def lookup(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        for _ in range(20):                # a reservation being written by another worker
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (ValueError, PermissionError):
                time.sleep(0.01)
        return {"subject": None, "run_id": None, "reserved_at": 0.0}

    def reserve(self, key: str, subject: str, now: float | None = None) -> dict[str, Any] | None:
        """None: this caller owns the key and may start. Otherwise what the key already holds."""
        path, now = self._path(key), time.time() if now is None else now
        self.folder.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = self.lookup(key)
            if held is not None and held["run_id"] is None and now - held["reserved_at"] > PENDING_S:
                path.unlink(missing_ok=True)
                return self.reserve(key, subject, now)
            return held
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"subject": subject, "run_id": None, "reserved_at": now}, fh)
        return None

    def bind(self, key: str, run_id: str) -> None:
        from claimsleuth.integration.run import atomic_write
        held = self.lookup(key) or {}
        atomic_write(self._path(key), json.dumps({**held, "run_id": run_id}))

    def release(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def note_duplicate(self, run_id: str) -> None:
        with (self.folder / "duplicates.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{run_id}\n")

    def duplicates(self) -> int:
        path = self.folder / "duplicates.log"
        return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0

