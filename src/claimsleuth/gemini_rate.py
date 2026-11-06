"""Shared Gemini request ledger and rate limiter for both agent processes (.venv and .venv-crew).

Google's Gemini quotas count requests per project and model: per minute (RPM) and per day (RPD, the day starting at
midnight Pacific time). The ADK agents and the CrewAI agent run in different processes at the same time, so a
limiter inside one process cannot keep the project under the limit. This module keeps one small SQLite ledger
(`runtime/gemini_requests.sqlite3`) that both processes use before every Gemini request:

  * per minute: wait until a slot is free (at most `max_wait_s`), so bursts never trigger a 429;
  * per day: refuse with GeminiQuotaGuard once the configured daily budget is used, instead of sending a request
    that can only fail;
  * every request is recorded with its run id, caller and model, so a run's request count is exact.

Nothing is retried here. The one automatic retry (after an HTTP 503, see UNAVAILABLE_RETRY_DELAY_S) is made by the
callers and takes its own slot here like any other request. Limits come from CLAIMSLEUTH_GEMINI_RPM (default 5) and
CLAIMSLEUTH_GEMINI_RPD (default 20; 0 turns the daily check off), the free-tier values Google reported for this project.
The ledger only knows requests made through ClaimSleuth from this machine.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claimsleuth import config

DEFAULT_RPM, DEFAULT_RPD = 5, 20
WINDOW_S = 60.0
MARGIN_S = 1.0                         # a little slack so the provider's minute has really passed
UNAVAILABLE_RETRY_DELAY_S = 15.0       # wait before the single retry after an HTTP 503 (once per agent per run)


class GeminiQuotaGuard(RuntimeError):
    """A Gemini request was not sent because the configured request budget does not allow it."""


def ledger_path() -> Path:
    return Path(os.environ.get("CLAIMSLEUTH_GEMINI_LEDGER") or config.RUNTIME_DIR / "gemini_requests.sqlite3")


def limits(env: Mapping[str, str] | None = None) -> tuple[int, int]:
    env = os.environ if env is None else env
    try:
        rpm = int(env.get("CLAIMSLEUTH_GEMINI_RPM") or DEFAULT_RPM)
        raw_rpd = env.get("CLAIMSLEUTH_GEMINI_RPD")
        rpd = DEFAULT_RPD if raw_rpd in (None, "") else int(raw_rpd)
    except ValueError as exc:
        raise GeminiQuotaGuard(f"CLAIMSLEUTH_GEMINI_RPM / _RPD must be integers: {exc}") from exc
    if rpm < 1 or rpd < 0:
        raise GeminiQuotaGuard("CLAIMSLEUTH_GEMINI_RPM must be >= 1 and CLAIMSLEUTH_GEMINI_RPD >= 0")
    return rpm, rpd


def _second_sunday_march(year: int) -> datetime:
    d = datetime(year, 3, 8)
    return d + timedelta(days=(6 - d.weekday()) % 7)


def _first_sunday_november(year: int) -> datetime:
    d = datetime(year, 11, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7)


def pacific_day_start(now: float) -> float:
    """Unix time of the most recent midnight in US Pacific time (DST: 2nd Sunday of March to 1st Sunday of
    November), the boundary at which Google resets per-day quotas. Computed without tz data (not in .venv)."""
    utc = datetime.fromtimestamp(now, tz=timezone.utc).replace(tzinfo=None)
    year = utc.year
    dst_start = _second_sunday_march(year) + timedelta(hours=10)      # 02:00 PST  = 10:00 UTC
    dst_end = _first_sunday_november(year) + timedelta(hours=9)        # 02:00 PDT  = 09:00 UTC
    offset = timedelta(hours=-7) if dst_start <= utc < dst_end else timedelta(hours=-8)
    local = utc + offset
    midnight = datetime(local.year, local.month, local.day) - offset
    return midnight.replace(tzinfo=timezone.utc).timestamp()


def _connect(path: Path) -> sqlite3.Connection:
    """Open the ledger, creating it on first use. Setting up a brand-new file (WAL mode, table) can briefly report
    "database is locked" when several processes start at once; that local lock is waited out here (it costs no
    Gemini request)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(100):
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS requests (ts REAL NOT NULL, model TEXT NOT NULL, "
                         "caller TEXT NOT NULL, run_id TEXT, process TEXT)")
            conn.execute("CREATE INDEX IF NOT EXISTS requests_model_ts ON requests (model, ts)")
            return conn
        except sqlite3.OperationalError as exc:
            conn.close()
            if "locked" not in str(exc) or attempt == 99:
                raise
            time.sleep(0.05)
    raise RuntimeError("unreachable")


def usage(model: str, now: float | None = None, path: Path | None = None) -> dict[str, int]:
    """Requests recorded for `model` in the last minute and since the Pacific-time day started."""
    now = time.time() if now is None else now
    path = path or ledger_path()
    if not path.exists():                  # nothing recorded yet; a read never creates the ledger
        return {"last_minute": 0, "today": 0}
    with closing(_connect(path)) as conn:
        minute = conn.execute("SELECT COUNT(*) FROM requests WHERE model=? AND ts>?", (model, now - WINDOW_S)).fetchone()[0]
        day = conn.execute("SELECT COUNT(*) FROM requests WHERE model=? AND ts>=?", (model, pacific_day_start(now))).fetchone()[0]
    return {"last_minute": minute, "today": day}


def run_requests(run_id: str, path: Path | None = None) -> dict[str, int]:
    """Requests recorded for one run, by caller (e.g. {"adk:evidence_agent": 2, "crewai:forensic_agent": 3})."""
    p = path or ledger_path()
    if not p.exists():
        return {}
    with closing(_connect(p)) as conn:
        rows = conn.execute("SELECT caller, COUNT(*) FROM requests WHERE run_id=? GROUP BY caller", (run_id,)).fetchall()
    return dict(rows)


def acquire(model: str, caller: str, run_id: str | None = None, max_wait_s: float = 120.0,
            path: Path | None = None, clock: Callable[[], float] = time.time,
            sleep: Callable[[float], None] = time.sleep, env: Mapping[str, str] | None = None) -> float:
    """Reserve one Gemini request slot, waiting for the per-minute window if needed. Returns the seconds waited.

    Raises GeminiQuotaGuard (nothing is sent) when the daily budget is used up or the wait would exceed
    `max_wait_s`. The reservation is recorded before the request is sent, so concurrent processes never
    over-commit the minute."""
    rpm, rpd = limits(env)
    path = path or ledger_path()
    waited = 0.0
    while True:
        now = clock()
        conn = _connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if rpd:
                today = conn.execute("SELECT COUNT(*) FROM requests WHERE model=? AND ts>=?",
                                     (model, pacific_day_start(now))).fetchone()[0]
                if today >= rpd:
                    conn.execute("ROLLBACK")
                    raise GeminiQuotaGuard(f"daily Gemini request budget used: {today} of {rpd} requests for {model} "
                                           "since midnight Pacific time (CLAIMSLEUTH_GEMINI_RPD); no request was sent")
            recent = [r[0] for r in conn.execute("SELECT ts FROM requests WHERE model=? AND ts>? ORDER BY ts",
                                                 (model, now - WINDOW_S))]
            if len(recent) < rpm:
                conn.execute("INSERT INTO requests (ts, model, caller, run_id, process) VALUES (?, ?, ?, ?, ?)",
                             (now, model, caller, run_id, f"{Path(sys.prefix).name} pid {os.getpid()}"))
                conn.execute("COMMIT")
                return waited
            conn.execute("ROLLBACK")
        finally:
            conn.close()
        delay = recent[len(recent) - rpm] + WINDOW_S + MARGIN_S - now
        if waited + delay > max_wait_s:
            raise GeminiQuotaGuard(f"per-minute Gemini limit ({rpm}/min for {model}) would need a wait of "
                                   f"{waited + delay:.0f} s, more than {max_wait_s:.0f} s; no request was sent")
        sleep(max(delay, 0.05))
        waited += max(delay, 0.05)


def total_requests(path: Path | None = None) -> int:
    """All requests ever recorded in the ledger (0 when it does not exist; a read never creates it)."""
    p = path or ledger_path()
    if not p.exists():
        return 0
    with closing(_connect(p)) as conn:
        return conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
