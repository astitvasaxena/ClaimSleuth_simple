"""Read-only monitoring summary of one run's blackboard, for the dashboard and the command-line viewer.

Never writes and never raises: an unusable database is itself reported (with the monitoring log, which is a
separate file and usually still readable).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from claimsleuth.blackboard.controller import read_monitoring_failures
from claimsleuth.blackboard.models import BoardEntry
from claimsleuth.blackboard.store import Blackboard, BlackboardError, configured_path


def _event(e: BoardEntry) -> dict[str, Any]:
    p = e.payload
    what = (p.get("state") or p.get("event") or p.get("reason") or p.get("final_decision")
            or ("report published" if e.section.startswith("report:") else "") or "")
    return {"run_seq": e.run_seq, "ts": e.ts.isoformat(), "kind": e.kind, "section": e.section, "author": e.author,
            "principal": e.principal, "process": f"{e.process.environment} pid {e.process.pid}", "auth": e.auth,
            "late": e.late, "what": what}


def run_monitor(run_id: str, path: Path | None = None, report_decision: str | None = None,
                recent: int = 12) -> dict[str, Any]:
    path = configured_path() if path is None else path
    if path is None:
        return {"enabled": False, "run_id": run_id}
    out: dict[str, Any] = {"enabled": True, "database": str(path), "run_id": run_id, "registered": False,
                           "monitoring_failures": read_monitoring_failures(path, run_id)}
    if not path.exists():
        out["error"] = f"no blackboard database at {path}"
        return out
    try:
        board = Blackboard(path)
        registration = board.registration(run_id)
        if registration is None:
            return out
        state = board.state(run_id, registration["claim_id"])
        entries = board.entries(run_id, registration["claim_id"])
    except BlackboardError as exc:
        out["error"] = str(exc)
        return out
    summary = next((m.payload for m in state.monitor if m.section == "controller:completion"), None)
    decision = next((p for p in state.live_posts if p.section == "decision:final"), None)
    out.update({
        "registered": True, "claim_id": registration["claim_id"], "fingerprint": registration["fingerprint"],
        "agents": [{"agent": a.agent, "framework": a.framework, "state": a.state, "state_at_seal": a.state_at_seal,
                    "late": a.late} for a in state.agents.values()],
        "sealed_at": state.sealed_at, "entries": state.entries,
        "events": [_event(e) for e in entries[-recent:]],
        "late": [_event(e) for e in state.late],
        "findings": (summary or {}).get("findings", []),
        "authentication": state.authentication,
        "decision": None if decision is None else {**decision.payload, "auth": decision.auth},
        "decision_matches_report": None if decision is None or report_decision is None
        else decision.payload.get("final_decision") == report_decision,
    })
    return out
