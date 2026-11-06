"""Gemini quota protection and incomplete-investigation marking, all offline (no Gemini request is ever made).

  * the shared request ledger keeps the project under the per-minute limit across processes and refuses once the
    daily budget is used, without sending anything;
  * the ADK model hook takes a ledger slot before each request, and a refusal stops the request;
  * an investigation without both agent reviews is marked PARTIAL / NONE and always needs a human.
"""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from claimsleuth import gemini_rate
from claimsleuth.adk_service.callbacks import ExecutionRecorder
from claimsleuth.integration import run as integration
from claimsleuth.schemas import AgentRunRecord, Decision, ExecutionStatus, utc_now
from test_adjudicator import adk, judge  # noqa: E402  (tests/ is on sys.path, as for adk_test_double)

MODEL = "gemini-test-model"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "gemini_requests.sqlite3"
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_LEDGER", str(path))
    monkeypatch.delenv("CLAIMSLEUTH_GEMINI_RPM", raising=False)
    monkeypatch.delenv("CLAIMSLEUTH_GEMINI_RPD", raising=False)
    return path


class FakeClock:
    def __init__(self, t: float) -> None:
        self.t, self.slept = t, []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def test_per_minute_limit_waits_instead_of_sending(ledger):
    clock = FakeClock(1_800_000_000.0)
    for i in range(5):
        assert gemini_rate.acquire(MODEL, "adk:evidence_agent", "RUN-a", clock=clock, sleep=clock.sleep) == 0
        clock.t += 1
    waited = gemini_rate.acquire(MODEL, "crewai:forensic_agent", "RUN-a", clock=clock, sleep=clock.sleep)
    assert 56 <= waited <= 58 and len(clock.slept) == 1          # first slot frees 60 s (+1 s margin) after it was used
    assert gemini_rate.run_requests("RUN-a") == {"adk:evidence_agent": 5, "crewai:forensic_agent": 1}


def test_a_wait_longer_than_allowed_is_refused_and_nothing_is_recorded(ledger):
    clock = FakeClock(1_800_000_000.0)
    for _ in range(5):
        gemini_rate.acquire(MODEL, "adk:x", "RUN-b", clock=clock, sleep=clock.sleep)
    with pytest.raises(gemini_rate.GeminiQuotaGuard, match="per-minute"):
        gemini_rate.acquire(MODEL, "adk:x", "RUN-b", max_wait_s=10, clock=clock, sleep=clock.sleep)
    assert clock.slept == [] and gemini_rate.run_requests("RUN-b") == {"adk:x": 5}


def test_daily_budget_is_enforced_before_sending(ledger, monkeypatch):
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_RPD", "3")
    clock = FakeClock(1_800_000_000.0)
    for _ in range(3):
        gemini_rate.acquire(MODEL, "adk:x", "RUN-c", clock=clock, sleep=clock.sleep)
        clock.t += 61
    with pytest.raises(gemini_rate.GeminiQuotaGuard, match="daily Gemini request budget used: 3 of 3"):
        gemini_rate.acquire(MODEL, "adk:x", "RUN-c", clock=clock, sleep=clock.sleep)
    assert gemini_rate.usage(MODEL, now=clock.t)["today"] == 3
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_RPD", "0")                 # 0 turns the daily check off
    gemini_rate.acquire(MODEL, "adk:x", "RUN-c", clock=clock, sleep=clock.sleep)


def test_concurrent_callers_never_over_commit_a_minute(ledger):
    results = []

    def take():
        try:
            gemini_rate.acquire(MODEL, "thread", "RUN-d", max_wait_s=0)
            results.append("ok")
        except gemini_rate.GeminiQuotaGuard:
            results.append("refused")

    threads = [threading.Thread(target=take) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 5 and results.count("refused") == 3


@pytest.mark.parametrize("utc, expected", [
    ("2026-11-02T09:27:00", "2026-11-02T08:00:00"),       # PST (DST ended 2026-11-01): midnight = 08:00 UTC
    ("2026-11-02T07:30:00", "2026-11-01T08:00:00"),       # still the previous Pacific day
    ("2026-07-01T06:00:00", "2026-06-30T07:00:00"),       # PDT: midnight = 07:00 UTC
])
def test_quota_day_starts_at_pacific_midnight(utc, expected):
    from datetime import datetime, timezone
    t = datetime.fromisoformat(utc).replace(tzinfo=timezone.utc).timestamp()
    start = datetime.fromtimestamp(gemini_rate.pacific_day_start(t), tz=timezone.utc).replace(tzinfo=None)
    assert start.isoformat() == expected


def test_adk_model_hook_takes_a_slot_and_a_refusal_stops_the_request():
    seen = []
    recorder = ExecutionRecorder("RUN-e", before_request=seen.append)
    recorder.agent_runs["evidence_agent"] = AgentRunRecord(agent="evidence_agent", framework="adk", started_at=utc_now())
    ctx = SimpleNamespace(agent_name="evidence_agent")
    request = SimpleNamespace(config=SimpleNamespace(tools=[]))
    asyncio.run(recorder.before_model(ctx, request))
    assert seen == ["evidence_agent"] and recorder.agent_runs["evidence_agent"].llm_calls == 1

    def refuse(agent):
        raise gemini_rate.GeminiQuotaGuard("daily budget used")
    recorder.before_request = refuse
    with pytest.raises(gemini_rate.GeminiQuotaGuard):
        asyncio.run(recorder.before_model(ctx, request))
    assert recorder.agent_runs["evidence_agent"].llm_calls == 1        # the refused request was not counted or sent


def test_live_preflight_refuses_when_the_daily_budget_is_too_small(ledger, monkeypatch):
    from claimsleuth import execution_mode as em
    monkeypatch.setattr(em, "load_env", lambda: None)
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-for-tests")
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_RPD", "14")              # 5 used + about 10 needed > 14
    monkeypatch.setattr(integration, "crew_service_llm", lambda url: {"ok": True, "mode": "gemini"})
    for _ in range(5):
        gemini_rate.acquire(em.gemini_model(), "adk:x", "RUN-f", max_wait_s=0)
    with pytest.raises(integration.LiveModeError, match="5 of 14 requests"):
        integration.live_preflight("http://127.0.0.1:1/.well-known/agent-card.json")
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_RPD", "20")
    assert integration.live_preflight("http://127.0.0.1:1/.well-known/agent-card.json") == em.gemini_model()


def test_no_agent_review_marks_the_investigation_incomplete(dataset):
    final = judge(dataset, "CS-006", adk_result=None, crew_result=None)          # rules: REQUEST_EVIDENCE
    assert final.final_decision == Decision.REQUEST_EVIDENCE and final.review_status == "NONE"
    assert final.requires_human_approval and final.approval_status == "PENDING"   # previously decided automatically
    assert len(final.incomplete_reasons) == 2 and any("INCOMPLETE" in r for r in final.rationale)


def test_one_missing_review_is_partial_and_needs_a_human(dataset):
    final = judge(dataset, "CS-001", adk_result=adk("CS-001", status=ExecutionStatus.FAILED))
    assert final.final_decision == Decision.APPROVE and final.review_status == "PARTIAL"
    assert final.requires_human_approval and "GOOGLE_ADK review unavailable" in final.incomplete_reasons[0]
    complete = judge(dataset, "CS-001")
    assert complete.review_status == "COMPLETE" and complete.incomplete_reasons == []


def test_reading_usage_never_creates_the_ledger(tmp_path):
    path = tmp_path / "absent.sqlite3"
    assert gemini_rate.usage(MODEL, path=path) == {"last_minute": 0, "today": 0} and not path.exists()
    assert gemini_rate.run_requests("RUN-x", path=path) == {} and not path.exists()
