"""Gemini inference for the CrewAI Forensic Agent (CrewAI LLM mode "gemini", used by EXECUTION_MODE=live).

Uses CrewAI 1.15.33's own native Gemini provider (`crewai.llms.providers.gemini.completion.GeminiCompletion`,
installed with the `crewai[google-genai]` extra, google-genai 1.65). The API key comes from GOOGLE_API_KEY (or
GEMINI_API_KEY) in the environment or the project's .env; it is never logged or returned.

Cost and safety bounds, all checked before a model call is made:
  * at most `max_calls` model calls per run and a wall-clock `run_budget_s`. These are the real limits. The agent's
    `max_iter` and the guardrail retry multiply each other, and a normal run needs only 2 calls (request the two
    lookups, write the report). The default of 4 leaves room for one guardrail retry and one 503 retry.
    The run takes at most run_budget_s + call_timeout_s: no call starts after the budget, and each call has a timeout;
  * every request first takes a slot in the shared Gemini request ledger (claimsleuth.gemini_rate), which keeps the
    project under the per-minute limit together with the ADK process and refuses once the daily budget is used;
  * every HTTP call has a timeout and a single attempt, and CrewAI's own rate-limit retry wrapper (3 attempts with
    1-8 s back-off, which ignores the provider's retry delay) is bypassed. The one retry is made here, after an HTTP
    503 and once per run: it waits, then takes its own ledger slot and counts against both budgets like any request;
  * the run executes inside the network guard with exactly one extra host allowed: the Gemini API.
A failure is reported as FAILED with the provider's error; it is never replaced by the test double.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

import claimsleuth.crew_service  # noqa: F401  (telemetry/tracing off before crewai is imported)
from pydantic import PrivateAttr

from claimsleuth import execution_mode, gemini_rate
from claimsleuth.observability.provider_errors import retry_record, should_retry
from claimsleuth.schemas import ProviderRetry

MODE = execution_mode.GEMINI
PROVIDER = execution_mode.PROVIDER_GEMINI
API_HOSTS = ("generativelanguage.googleapis.com",)
RETRY_ATTEMPTS = 1                                      # one HTTP attempt: an HTTP-level retry would bypass the ledger
CALLER = "crewai:forensic_agent"
AGENT = "forensic_agent"

# name -> (env var, default, minimum, maximum)
_INT_SETTINGS = {
    "call_timeout_s": ("CLAIMSLEUTH_GEMINI_CALL_TIMEOUT", 120, 15, 600),      # one HTTP model call
    "run_budget_s": ("CLAIMSLEUTH_GEMINI_RUN_BUDGET", 300, 60, 1800),         # checked before every model call
    "max_calls": ("CLAIMSLEUTH_GEMINI_MAX_CALLS", 4, 2, 30),                  # model calls per run, retries included
    "max_output_tokens": ("CLAIMSLEUTH_GEMINI_MAX_OUTPUT_TOKENS", 8192, 1024, 32768),
    "max_iter": ("CLAIMSLEUTH_GEMINI_MAX_ITER", 2, 2, 10),                    # CrewAI agent iterations per attempt
    "guardrail_retries": ("CLAIMSLEUTH_GEMINI_GUARDRAIL_RETRIES", 1, 0, 2),
}


class GeminiConfigError(ValueError):
    """Invalid or missing live configuration (reported as NOT_CONFIGURED)."""


class GeminiBudgetExceeded(RuntimeError):
    """The run's call or time budget is used up; no further model call is made."""


class GeminiRunTimeout(GeminiBudgetExceeded, TimeoutError):
    """The run's time budget is used up: a time limit of this application, not a provider failure."""


@dataclass(frozen=True)
class GeminiSettings:
    model: str = execution_mode.DEFAULT_GEMINI_MODEL
    call_timeout_s: int = 120
    run_budget_s: int = 300
    max_calls: int = 4
    max_output_tokens: int = 8192
    max_iter: int = 2
    guardrail_retries: int = 1

    def public(self) -> dict[str, Any]:            # no secrets in here
        return {**asdict(self), "provider": PROVIDER, "retry_attempts": RETRY_ATTEMPTS, "api_hosts": list(API_HOSTS)}


def settings_from_env(env: dict[str, str] | None = None) -> GeminiSettings:
    env = os.environ if env is None else env
    problems = execution_mode.gemini_config_errors(env)
    if problems:
        raise GeminiConfigError(problems[0])
    if env.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true", "yes"):
        raise GeminiConfigError("the CrewAI live mode uses the Gemini API key; Vertex AI is not supported here")
    values: dict[str, Any] = {"model": execution_mode.gemini_model(env)}
    if not values["model"].startswith("gemini-"):
        raise GeminiConfigError(f"model {values['model']!r} is not a Gemini model name")
    for name, (var, default, low, high) in _INT_SETTINGS.items():
        raw = env.get(var)
        try:
            value = default if raw in (None, "") else int(raw)
        except ValueError as exc:
            raise GeminiConfigError(f"{var}={raw!r} is not an integer") from exc
        if not low <= value <= high:
            raise GeminiConfigError(f"{var}={value} is outside the allowed range {low}..{high}")
        values[name] = value
    settings = GeminiSettings(**values)
    if settings.run_budget_s < settings.call_timeout_s:
        raise GeminiConfigError("run budget must be at least one call timeout")
    return settings


def _llm_class():
    try:
        from crewai.llms.base_llm import BaseLLM
        from crewai.llms.providers.gemini.completion import GeminiCompletion
    except ImportError as exc:      # google-genai missing from .venv-crew
        raise GeminiConfigError("CrewAI's Gemini provider needs google-genai in .venv-crew "
                                "(pip install -r requirements/crew.txt)") from exc

    # CrewAI wraps every subclass's call/acall in run_with_rate_limit_retry (llms/retry.py: 3 attempts on a 429).
    # The unwrapped originals are used here, so each model call is exactly one request.
    unwrapped_call = getattr(GeminiCompletion.call, "__wrapped__", GeminiCompletion.call)
    unwrapped_acall = getattr(GeminiCompletion.acall, "__wrapped__", GeminiCompletion.acall)

    class BudgetedGeminiLLM(GeminiCompletion):
        """CrewAI's native Gemini provider plus a per-run call cap and time budget, checked before each call, and
        the shared request ledger. One retry after an HTTP 503, under the same limits; no retries otherwise."""

        _deadline: float | None = PrivateAttr(default=None)
        _max_calls: int | None = PrivateAttr(default=None)
        _calls: int = PrivateAttr(default=0)
        _run_id: str | None = PrivateAttr(default=None)
        _retries: list[ProviderRetry] = PrivateAttr(default_factory=list)

        def start_budget(self, seconds: float, max_calls: int, run_id: str | None = None) -> None:
            self._deadline, self._max_calls, self._calls = time.monotonic() + seconds, max_calls, 0
            self._run_id = run_id
            self._retries = []

        @property
        def provider_retries(self) -> list[ProviderRetry]:
            return list(self._retries)

        @property
        def calls_made(self) -> int:
            return self._calls

        def _spend(self) -> None:
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise GeminiRunTimeout("Gemini run budget (time) exhausted before the next model call")
            if self._max_calls is not None and self._calls >= self._max_calls:
                raise GeminiBudgetExceeded(f"Gemini call budget of {self._max_calls} calls exhausted")
            remaining = max(self._deadline - time.monotonic(), 1.0) if self._deadline is not None else 120.0
            gemini_rate.acquire(self.model, CALLER, self._run_id, max_wait_s=min(remaining, 120.0))   # may wait
            self._calls += 1

        def _may_retry(self, error: Exception) -> bool:
            """After the run's first HTTP 503: wait, then take a slot for the second request. False: not retried."""
            if self._retries or not should_retry(error):
                return False
            delay = gemini_rate.UNAVAILABLE_RETRY_DELAY_S
            time.sleep(delay)
            try:
                self._spend()
            except (GeminiBudgetExceeded, gemini_rate.GeminiQuotaGuard):   # no second request was sent
                self._retries.append(retry_record(error, AGENT, "refused", delay))
                return False
            return True

        def _retried(self, error: Exception, outcome: str) -> None:
            self._retries.append(retry_record(error, AGENT, outcome, gemini_rate.UNAVAILABLE_RETRY_DELAY_S))

        def get_input_tokens_count(self, messages, tools=None, response_model=None) -> int:
            """CrewAI's local estimate. GeminiCompletion would ask Google's countTokens endpoint, a request outside the
            ledger and the call budget."""
            return BaseLLM.get_input_tokens_count(self, messages, tools, response_model)

        def call(self, *args, **kwargs):
            self._spend()
            try:
                return unwrapped_call(self, *args, **kwargs)
            except Exception as error:
                if not self._may_retry(error):
                    raise
                try:
                    response = unwrapped_call(self, *args, **kwargs)
                except Exception:
                    self._retried(error, "failed")
                    raise
                self._retried(error, "recovered")
                return response

        async def acall(self, *args, **kwargs):
            self._spend()
            try:
                return await unwrapped_acall(self, *args, **kwargs)
            except Exception as error:
                if not await asyncio.to_thread(self._may_retry, error):
                    raise
                try:
                    response = await unwrapped_acall(self, *args, **kwargs)
                except Exception:
                    self._retried(error, "failed")
                    raise
                self._retried(error, "recovered")
                return response

        call._crewai_rate_limit_wrapped = True      # tells BaseLLM.__init_subclass__ not to add its retry wrapper
        acall._crewai_rate_limit_wrapped = True

    return BudgetedGeminiLLM


def build_llm(settings: GeminiSettings):
    from google.genai import types
    http = types.HttpOptions(timeout=settings.call_timeout_s * 1000,
                             retry_options=types.HttpRetryOptions(attempts=RETRY_ATTEMPTS, initial_delay=2))
    return _llm_class()(model=settings.model, max_output_tokens=settings.max_output_tokens,
                        client_params={"http_options": http})


def is_gemini_llm(llm: Any) -> bool:
    return getattr(llm, "llm_type", None) == "gemini"
