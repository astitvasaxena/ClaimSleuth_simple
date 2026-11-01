"""SQLite claim-history access (the project's single database).

The baseline reads this directly; the MCP claim_history_lookup tool will expose the same function.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from claimsleuth import config
from claimsleuth.rules import normalise_serial
from claimsleuth.schemas import ClaimHistoryLookupInput, ClaimHistoryLookupOutput, HistoryLookupOutput, HistoryRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_history (
    claim_id      TEXT PRIMARY KEY,
    serial_number TEXT NOT NULL,
    claimant_id   TEXT NOT NULL,
    device_model  TEXT NOT NULL,
    claim_date    TEXT NOT NULL,
    outcome       TEXT NOT NULL CHECK (outcome IN ('APPROVED','REJECTED','ESCALATED','PENDING')),
    amount        TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_serial ON claim_history(serial_number);
"""


def connect(db_path: Path = config.DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path, rows: list[HistoryRecord]) -> None:
    """Create the table and replace its contents with the seed rows."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM claim_history")
        conn.executemany(
            "INSERT INTO claim_history VALUES (?,?,?,?,?,?,?)",
            [(r.claim_id, normalise_serial(r.serial_number), r.claimant_id, r.device_model,
              r.claim_date.isoformat(), r.outcome, None if r.amount is None else str(r.amount)) for r in rows],
        )
    conn.close()


def lookup_history(serial_number: str, claimant_id: str | None = None, exclude_claim_id: str | None = None,
                   db_path: Path = config.DB_PATH) -> HistoryLookupOutput:
    """All earlier claims on this serial, newest first, excluding the claim being investigated."""
    if not Path(db_path).exists():
        raise FileNotFoundError(f"claim history database not found: {db_path}")
    serial = normalise_serial(serial_number)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM claim_history WHERE serial_number = ? AND claim_id != ? ORDER BY claim_date DESC",
            (serial, exclude_claim_id or ""),
        ).fetchall()
    conn.close()
    records = [_record(r) for r in rows]
    others = sum(1 for r in records if claimant_id and r.claimant_id != claimant_id)
    return HistoryLookupOutput(
        serial_number=serial, claimant_id=claimant_id, records=records,
        same_serial_count=len(records), other_claimant_count=others,
        source=f"sqlite:{Path(db_path).name}#claim_history",
    )


def _record(row: sqlite3.Row) -> HistoryRecord:
    return HistoryRecord(
        claim_id=row["claim_id"], serial_number=row["serial_number"], claimant_id=row["claimant_id"],
        device_model=row["device_model"], claim_date=row["claim_date"], outcome=row["outcome"],
        amount=None if row["amount"] is None else Decimal(row["amount"]),
    )


def query_history(request: ClaimHistoryLookupInput, db_path: Path = config.DB_PATH) -> ClaimHistoryLookupOutput:
    """Records matching every given filter (claim id, claimant id, serial), newest first, at most `limit`.
    Backs the MCP tool claim_history_lookup."""
    if not Path(db_path).exists():
        raise FileNotFoundError(f"claim history database not found: {Path(db_path).name}")
    filters = {"claim_id": request.claim_id, "claimant_id": request.claimant_id,
               "serial_number": normalise_serial(request.serial_number) or None}
    where = " AND ".join(f"{column} = ?" for column, value in filters.items() if value is not None)
    params = [value for value in filters.values() if value is not None]
    with connect(db_path) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM claim_history WHERE {where}", params).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM claim_history WHERE {where} ORDER BY claim_date DESC, claim_id "
                            "LIMIT ?", [*params, request.limit]).fetchall()
    conn.close()
    records = [_record(r) for r in rows]
    table = f"sqlite:{Path(db_path).name}#claim_history"
    return ClaimHistoryLookupOutput(
        **filters, records=records, returned=len(records), total_matches=total, truncated=total > len(records),
        source=table, record_sources=[f"{table}/claim_id={r.claim_id}" for r in records],
    )
