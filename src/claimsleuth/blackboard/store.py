"""SQLite store of the shared coordination blackboard.

One database file is opened independently by the ADK side (main process, .venv) and by the CrewAI A2A service
(separate process, .venv-crew); both only need the standard-library sqlite3 module.

Guarantees:
- Append-only: rows are never updated or deleted (enforced by triggers in the database itself).
- Isolation: every row carries its run id and claim id; a run is bound to exactly one claim and one document
  fingerprint when it is registered, and every write is checked against that registration.
- Authentication: every write needs an authenticated writer (auth.py) whose principal may write as the author;
  every entry is signed and verified again whenever it is read.
- Concurrency: WAL journal, a busy timeout, and BEGIN IMMEDIATE transactions. Each write reads the current state
  and appends inside one transaction, so concurrent writers and retries cannot both succeed with contradictory
  results (for example COMPLETED and FAILED for the same agent).
- Idempotency: repeating a status, a post with identical content, or a registration with identical details
  returns the existing entry and writes nothing.
- Late entries: once the controller has sealed a run (the adjudicator's inputs are fixed), agent entries are still
  accepted for traceability but flagged "late" on every read and never counted as live results.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from claimsleuth.blackboard.auth import (PRINCIPALS, BlackboardAuthError, Writer, authenticate, load_keyring,
                                         provision_keys, sign, verify)
from claimsleuth.blackboard.models import (EXPECTED_AGENTS, TERMINAL, TRANSITIONS, AgentStatus, BoardEntry,
                                           ProcessInfo, RunState, payload_hash)
from claimsleuth.schemas import utc_now

ENV_VAR = "CLAIMSLEUTH_BLACKBOARD_DB"
DISABLED_VALUES = frozenset({"", "0", "off", "false", "no", "none", "disabled"})
BUSY_TIMEOUT_S = 5.0
SEAL_SECTION = "controller:seal"
AGENT_AUTHORS = PRINCIPALS["adk"] | PRINCIPALS["crewai"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    claim_id    TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL REFERENCES runs(run_id),
    run_seq        INTEGER NOT NULL,
    claim_id       TEXT NOT NULL,
    ts             TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('register', 'status', 'post', 'retract', 'monitor')),
    section        TEXT NOT NULL,
    author         TEXT NOT NULL,
    framework      TEXT NOT NULL,
    process        TEXT NOT NULL,
    fingerprint    TEXT,
    payload        TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    ref_seq        INTEGER REFERENCES entries(seq),
    principal      TEXT,
    signature      TEXT,
    UNIQUE (run_id, run_seq)
);
CREATE INDEX IF NOT EXISTS entries_run ON entries (run_id, run_seq);
CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
    BEGIN SELECT RAISE(ABORT, 'blackboard entries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
    BEGIN SELECT RAISE(ABORT, 'blackboard entries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS runs_no_update BEFORE UPDATE ON runs
    BEGIN SELECT RAISE(ABORT, 'blackboard runs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS runs_no_delete BEFORE DELETE ON runs
    BEGIN SELECT RAISE(ABORT, 'blackboard runs are append-only'); END;
"""


class BlackboardError(RuntimeError):
    """The blackboard could not be used (database unavailable, locked too long, corrupt ...)."""


class BlackboardRejected(BlackboardError):
    """A write was refused: unknown run, wrong claim, wrong document fingerprint, invalid or contradictory change,
    or an unauthenticated writer."""


def configured_path(env: dict[str, str] | None = None) -> Path | None:
    """The database path from CLAIMSLEUTH_BLACKBOARD_DB, or None when unset or disabled ("off", "0", ...)."""
    value = (os.environ if env is None else env).get(ENV_VAR, "")
    return None if value.strip().lower() in DISABLED_VALUES else Path(value)


def _process() -> ProcessInfo:
    return ProcessInfo(pid=os.getpid(), environment=Path(sys.prefix).name)


class Blackboard:
    def __init__(self, path: Path, busy_timeout_s: float = BUSY_TIMEOUT_S) -> None:
        self.path, self.busy_timeout_s = Path(path), busy_timeout_s
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
                self._migrate(conn)
        except sqlite3.Error as exc:
            raise BlackboardError(f"blackboard database {self.path} unavailable: {exc}") from exc

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Databases created before writer authentication get the two new columns; existing rows are kept as they
        are and read back as 'unsigned'. Adding a column does not touch any row. The write lock is taken only
        when a column is actually missing, so opening an up-to-date database never waits for other writers."""
        if {"principal", "signature"} <= {r["name"] for r in conn.execute("PRAGMA table_info(entries)")}:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(entries)")}
            for column in ("principal", "signature"):
                if column not in columns:
                    conn.execute(f"ALTER TABLE entries ADD COLUMN {column} TEXT")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ connections and transactions
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_s, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_s * 1000)}")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self, writer: Writer, author: str) -> Iterator[sqlite3.Connection]:
        """Authenticate the writer, then one BEGIN IMMEDIATE transaction (write lock taken before any read)."""
        try:
            authenticate(self.path, writer, author)
        except BlackboardAuthError as exc:
            raise BlackboardRejected(str(exc)) from exc
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
        except BlackboardRejected:
            raise
        except sqlite3.Error as exc:
            raise BlackboardError(f"blackboard write failed ({self.path}): {exc}") from exc

    def _append(self, conn: sqlite3.Connection, writer: Writer, run_id: str, claim_id: str, kind: str, section: str,
                author: str, framework: str, payload: dict[str, Any], fingerprint: str | None = None,
                ref_seq: int | None = None) -> BoardEntry:
        run_seq = conn.execute("SELECT COALESCE(MAX(run_seq), 0) + 1 FROM entries WHERE run_id = ?",
                               (run_id,)).fetchone()[0]
        text = json.dumps(payload, sort_keys=True, default=str)
        fields = {"run_id": run_id, "run_seq": run_seq, "claim_id": claim_id, "ts": utc_now().isoformat(),
                  "kind": kind, "section": section, "author": author, "framework": framework,
                  "process": _process().model_dump_json(), "fingerprint": fingerprint,
                  "payload_sha256": payload_hash(text), "ref_seq": ref_seq, "principal": writer.principal}
        cur = conn.execute(
            "INSERT INTO entries (run_id, run_seq, claim_id, ts, kind, section, author, framework, process, "
            "fingerprint, payload, payload_sha256, ref_seq, principal, signature) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*(fields[f] for f in ("run_id", "run_seq", "claim_id", "ts", "kind", "section", "author", "framework",
                                   "process", "fingerprint")), text, fields["payload_sha256"], ref_seq,
             writer.principal, sign(writer, fields)))
        row = conn.execute("SELECT * FROM entries WHERE seq = ?", (cur.lastrowid,)).fetchone()
        return self._entry(row, load_keyring(self.path), self._seal(conn, run_id))

    @staticmethod
    def _entry(row: sqlite3.Row, keyring: dict[str, bytes], seal: int | None) -> BoardEntry:
        fields = {f: row[f] for f in ("run_id", "run_seq", "claim_id", "ts", "kind", "section", "author", "framework",
                                      "process", "fingerprint", "payload_sha256", "ref_seq", "principal")}
        return BoardEntry(seq=row["seq"], run_seq=row["run_seq"], run_id=row["run_id"], claim_id=row["claim_id"],
                          ts=row["ts"], kind=row["kind"], section=row["section"], author=row["author"],
                          framework=row["framework"], process=ProcessInfo.model_validate_json(row["process"]),
                          fingerprint=row["fingerprint"], payload=json.loads(row["payload"]),
                          payload_sha256=row["payload_sha256"], ref_seq=row["ref_seq"], principal=row["principal"],
                          signature=row["signature"], auth=verify(fields, row["payload"], row["signature"], keyring),
                          late=seal is not None and row["run_seq"] > seal and row["author"] in AGENT_AUTHORS)

    # ------------------------------------------------------------------ validation
    @staticmethod
    def _check_run(conn: sqlite3.Connection, run_id: str, claim_id: str, fingerprint: str | None,
                   require_fingerprint: bool) -> None:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise BlackboardRejected(f"run {run_id} is not registered on the blackboard")
        if run["claim_id"] != claim_id:
            raise BlackboardRejected(f"run {run_id} belongs to claim {run['claim_id']}, not {claim_id}")
        if fingerprint is None:
            if require_fingerprint:
                raise BlackboardRejected(f"run {run_id}: a document fingerprint is required for this write")
        elif fingerprint != run["fingerprint"]:
            raise BlackboardRejected(f"run {run_id}: document fingerprint {fingerprint[:12]}... does not match the "
                                     f"registered documents ({run['fingerprint'][:12]}...)")

    @staticmethod
    def _status(conn: sqlite3.Connection, run_id: str, agent: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM entries WHERE run_id = ? AND kind = 'status' AND section = ? "
                            "ORDER BY run_seq DESC LIMIT 1", (run_id, f"status:{agent}")).fetchone()

    @staticmethod
    def _seal(conn: sqlite3.Connection, run_id: str) -> int | None:
        row = conn.execute("SELECT MIN(run_seq) FROM entries WHERE run_id = ? AND kind = 'monitor' AND section = ?",
                           (run_id, SEAL_SECTION)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ writes
    def register(self, run_id: str, claim_id: str, fingerprint: str, *, writer: Writer, author: str = "controller",
                 agents: dict[str, str] | None = None) -> list[BoardEntry]:
        """Bind the run to one claim and one document fingerprint, and mark the expected agents PENDING.
        Registering again with the same details is a no-op; different details are rejected."""
        agents = EXPECTED_AGENTS if agents is None else agents
        with self._write(writer, author) as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is not None:
                if (run["claim_id"], run["fingerprint"]) != (claim_id, fingerprint):
                    raise BlackboardRejected(f"run {run_id} is already registered for claim {run['claim_id']} with "
                                             "other documents")
                return []
            conn.execute("INSERT INTO runs (run_id, claim_id, fingerprint, created_at) VALUES (?, ?, ?, ?)",
                         (run_id, claim_id, fingerprint, utc_now().isoformat()))
            out = [self._append(conn, writer, run_id, claim_id, "register", "run", author, "orchestrator",
                                {"expected_agents": agents}, fingerprint)]
            out += [self._append(conn, writer, run_id, claim_id, "status", f"status:{agent}", author, "orchestrator",
                                 {"state": "PENDING", "agent": agent, "agent_framework": framework}, fingerprint)
                    for agent, framework in agents.items()]
            return out

    def set_status(self, run_id: str, claim_id: str, agent: str, state: str, author: str, framework: str,
                   fingerprint: str | None, detail: dict[str, Any] | None = None, *, writer: Writer) -> BoardEntry:
        """Move an agent to a new state. Repeating the current state returns the existing entry (idempotent).
        A fingerprint is required except for a failure reported before the documents were loaded."""
        with self._write(writer, author) as conn:
            self._check_run(conn, run_id, claim_id, fingerprint,
                            require_fingerprint=state not in ("FAILED", "NOT_CONFIGURED"))
            current = self._status(conn, run_id, agent)
            if current is None:
                raise BlackboardRejected(f"run {run_id}: agent {agent} was not registered for this run")
            now = json.loads(current["payload"])["state"]
            if now == state:
                return self._entry(current, load_keyring(self.path), self._seal(conn, run_id))
            if state not in TRANSITIONS[now]:
                raise BlackboardRejected(f"run {run_id}: {agent} cannot move from {now} to {state}")
            return self._append(conn, writer, run_id, claim_id, "status", f"status:{agent}", author, framework,
                                {"state": state, "agent": agent, "previous": now, **(detail or {})}, fingerprint)

    def post(self, run_id: str, claim_id: str, author: str, framework: str, section: str, payload: dict[str, Any],
             fingerprint: str, *, writer: Writer) -> BoardEntry:
        """Publish content. An identical live post is returned unchanged (idempotent); different content for a
        section that already has a live post must be retracted first. An agent cannot post after it failed."""
        text = json.dumps(payload, sort_keys=True, default=str)
        with self._write(writer, author) as conn:
            self._check_run(conn, run_id, claim_id, fingerprint, require_fingerprint=True)
            status = self._status(conn, run_id, author)
            if status is not None and json.loads(status["payload"])["state"] in TERMINAL - {"COMPLETED"}:
                raise BlackboardRejected(f"run {run_id}: {author} is {json.loads(status['payload'])['state']}; "
                                         "it cannot publish a result")
            for row in self._live_posts(conn, run_id, section):
                if row["payload_sha256"] == payload_hash(text):
                    return self._entry(row, load_keyring(self.path), self._seal(conn, run_id))
                raise BlackboardRejected(f"run {run_id}: section {section} already holds different content "
                                         f"(entry {row['run_seq']}); retract it first")
            return self._append(conn, writer, run_id, claim_id, "post", section, author, framework, payload,
                                fingerprint)

    def retract(self, run_id: str, claim_id: str, target_run_seq: int, author: str, framework: str,
                reason: str, *, writer: Writer) -> BoardEntry:
        """Retract a post by appending a retraction that points at it; the post itself is kept."""
        with self._write(writer, author) as conn:
            self._check_run(conn, run_id, claim_id, None, require_fingerprint=False)
            target = conn.execute("SELECT * FROM entries WHERE run_id = ? AND run_seq = ?",
                                  (run_id, target_run_seq)).fetchone()
            if target is None or target["kind"] != "post":
                raise BlackboardRejected(f"run {run_id}: entry {target_run_seq} is not a post of this run")
            done = conn.execute("SELECT * FROM entries WHERE run_id = ? AND kind = 'retract' AND ref_seq = ?",
                                (run_id, target["seq"])).fetchone()
            if done is not None:
                return self._entry(done, load_keyring(self.path), self._seal(conn, run_id))
            return self._append(conn, writer, run_id, claim_id, "retract", target["section"], author, framework,
                                {"reason": reason, "retracted_run_seq": target_run_seq}, ref_seq=target["seq"])

    def monitor(self, run_id: str, claim_id: str, author: str, framework: str, section: str,
                payload: dict[str, Any], *, writer: Writer) -> BoardEntry:
        """Record a monitoring finding or event (lifecycle events, completion summary, the seal ...)."""
        with self._write(writer, author) as conn:
            self._check_run(conn, run_id, claim_id, None, require_fingerprint=False)
            if section == SEAL_SECTION:
                if writer.principal != "orchestrator":
                    raise BlackboardRejected("only the orchestrator may seal a run")
                existing = self._seal(conn, run_id)
                if existing is not None:              # idempotent: a run is sealed once
                    row = conn.execute("SELECT * FROM entries WHERE run_id = ? AND run_seq = ?",
                                       (run_id, existing)).fetchone()
                    return self._entry(row, load_keyring(self.path), existing)
            return self._append(conn, writer, run_id, claim_id, "monitor", section, author, framework, payload)

    # ------------------------------------------------------------------ reads
    @staticmethod
    def _live_posts(conn: sqlite3.Connection, run_id: str, section: str | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT * FROM entries e WHERE run_id = ? AND kind = 'post' AND NOT EXISTS "
               "(SELECT 1 FROM entries r WHERE r.kind = 'retract' AND r.ref_seq = e.seq)")
        args: tuple = (run_id,)
        if section is not None:
            sql, args = sql + " AND section = ?", (run_id, section)
        return conn.execute(sql + " ORDER BY run_seq", args).fetchall()

    def entries(self, run_id: str, claim_id: str) -> list[BoardEntry]:
        """Every entry of one run, in order, verified. Reading a run under another claim id is refused."""
        try:
            with self._connect() as conn:
                self._check_run(conn, run_id, claim_id, None, require_fingerprint=False)
                rows = conn.execute("SELECT * FROM entries WHERE run_id = ? AND claim_id = ? ORDER BY run_seq",
                                    (run_id, claim_id)).fetchall()
                seal = self._seal(conn, run_id)
        except sqlite3.Error as exc:
            raise BlackboardError(f"blackboard read failed ({self.path}): {exc}") from exc
        keyring = load_keyring(self.path)
        return [self._entry(r, keyring, seal) for r in rows]

    def registration(self, run_id: str) -> dict[str, str] | None:
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        except sqlite3.Error as exc:
            raise BlackboardError(f"blackboard read failed ({self.path}): {exc}") from exc
        return dict(row) if row else None

    def state(self, run_id: str, claim_id: str) -> RunState:
        entries = self.entries(run_id, claim_id)
        sealed_at = next((e.run_seq for e in entries if e.kind == "monitor" and e.section == SEAL_SECTION), None)
        retracted_seqs = {e.ref_seq for e in entries if e.kind == "retract"}
        expected = next((e.payload["expected_agents"] for e in entries if e.kind == "register"), {})
        agents: dict[str, AgentStatus] = {}
        at_seal: dict[str, str] = {}
        for e in entries:
            if e.kind == "status":
                agent = e.section.split(":", 1)[1]
                if not e.late:
                    at_seal[agent] = e.payload["state"]
                agents[agent] = AgentStatus(agent=agent, framework=expected.get(agent, e.framework),
                                            state=e.payload["state"], seq=e.seq, late=e.late)
        for agent, status in agents.items():
            status.state_at_seal = at_seal.get(agent) if sealed_at is not None else None
        counts: dict[str, int] = {}
        for e in entries:
            counts[e.auth] = counts.get(e.auth, 0) + 1
        reg = self.registration(run_id) or {}
        return RunState(run_id=run_id, claim_id=claim_id, fingerprint=reg.get("fingerprint", ""), agents=agents,
                        live_posts=[e for e in entries if e.kind == "post" and e.seq not in retracted_seqs
                                    and not e.late],
                        retracted=[e for e in entries if e.kind == "post" and e.seq in retracted_seqs],
                        retractions=[e for e in entries if e.kind == "retract"],
                        monitor=[e for e in entries if e.kind == "monitor"], entries=len(entries),
                        sealed_at=sealed_at, late=[e for e in entries if e.late], authentication=counts)

    def runs(self, limit: int = 50) -> list[dict[str, str]]:
        try:
            with self._connect() as conn:
                return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))]
        except sqlite3.Error as exc:
            raise BlackboardError(f"blackboard read failed ({self.path}): {exc}") from exc


def provision(path: Path) -> None:
    """Create the database and the writer keys (done by the orchestrator)."""
    Blackboard(path)
    provision_keys(path)
