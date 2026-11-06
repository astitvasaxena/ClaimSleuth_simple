"""Structured classification of a failed model request, shared by the ADK and CrewAI sides.

A temporary provider failure must be distinguishable from an application bug. classify() turns an exception into a
ProviderFailure from what the exception actually carries: the HTTP status only from its structured fields (google-genai
APIError.code, an HTTP response's status_code), never from guessing at its text; otherwise from the exception's type.
Nothing here retries, waits, switches model or changes a task. `retryable` says whether the same request could succeed
later; the only automatic retry is the single one after an HTTP 503 (should_retry), made by the agents' model layer.

Nothing unsafe is kept. A provider's response body can echo the request, so it is dropped: failure_text() gives
"ServerError: 503 UNAVAILABLE", which still carries the status the evaluation's stop markers look for. redact() removes
credentials from any other error text.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from claimsleuth.schemas import ProviderFailure, ProviderRetry, utc_now

AGENT_TASK = {"evidence_agent": "evidence", "investigator_agent": "investigation", "forensic_agent": "forensics"}

PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
PROVIDER_SERVER_ERROR = "PROVIDER_SERVER_ERROR"
AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
INVALID_REQUEST = "INVALID_REQUEST"
NETWORK_ERROR = "NETWORK_ERROR"
APPLICATION_TIMEOUT = "APPLICATION_TIMEOUT"
STRUCTURED_OUTPUT_INVALID = "STRUCTURED_OUTPUT_INVALID"
LOCAL_BUDGET_EXCEEDED = "LOCAL_BUDGET_EXCEEDED"
UNCLASSIFIED = "UNCLASSIFIED"

# error code -> (failure category, retryable, trace error_category, what happened)
CODES: dict[str, tuple[str, bool | None, str, str]] = {
    PROVIDER_UNAVAILABLE: ("availability", True, "provider", "the model provider is temporarily unavailable"),
    PROVIDER_RATE_LIMITED: ("quota", True, "provider", "the model provider refused the request: rate limit or quota"),
    PROVIDER_SERVER_ERROR: ("server", True, "provider", "the model provider returned a server error"),
    AUTHENTICATION_FAILED: ("authentication", False, "provider", "the model provider refused the credentials or permissions"),
    INVALID_REQUEST: ("invalid_request", False, "provider", "the model provider rejected the request as invalid"),
    NETWORK_ERROR: ("network", True, "provider", "the model provider could not be reached (network or DNS)"),
    APPLICATION_TIMEOUT: ("timeout", True, "agent", "a time limit of this application passed"),
    STRUCTURED_OUTPUT_INVALID: ("output", False, "agent", "the agent's structured output was missing or invalid"),
    LOCAL_BUDGET_EXCEEDED: ("local_budget", False, "business", "the local request budget refused the request; nothing was sent"),
    UNCLASSIFIED: ("unknown", None, "agent", "the failure could not be classified"),
}

_LOCAL_BUDGET = {"GeminiQuotaGuard", "GeminiBudgetExceeded"}
_TIMEOUT = {"TimeoutError", "TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout"}
_NETWORK = {"ConnectionError", "gaierror", "NetworkError", "TransportError", "ProtocolError", "ClientConnectionError",
            "ClientConnectorError", "ClientOSError", "ServerDisconnectedError"}
_OUTPUT = {"ValidationError", "JSONDecodeError", "ConverterError"}
_SECRET_VARS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")
_SECRET_PATTERNS = (
    (re.compile(r"AIza[0-9A-Za-z_\-]{10,}"), "[redacted]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [redacted]"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|password|secret)[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,}]+"), r"\1[redacted]"),
    (re.compile(r"(?i)([?&](?:key|token)=)[^\s&\"']+"), r"\1[redacted]"),
)
_BODY = re.compile(r"\s*[{\[].*$", re.S)
_STATUS_PREFIX = re.compile(r"^(.*?\b[45]\d{2}\b(?: [A-Z][A-Z_]{2,})?)(?=[\s.:,-]|$)", re.S)
_PROVIDER_STATUS = re.compile(r"^[A-Z][A-Z_]{1,40}$")


def redact(text: Any, limit: int = 500) -> str:
    """Error text without credentials, cut to `limit` characters."""
    out = str(text)
    for name in _SECRET_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            out = out.replace(value, "[redacted]")
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out[:limit]


def secret_matches(text: str) -> list[str]:
    """The credentials redact() would remove from this text: the configured keys, and anything shaped like one."""
    values = [v for name in _SECRET_VARS if (v := os.environ.get(name)) and len(v) >= 8 and v in text]
    return values + [m.group(0) for pattern, _ in _SECRET_PATTERNS for m in pattern.finditer(text)]


def provider_text(text: Any, limit: int = 200) -> str:
    """Error text that may carry a provider's response: everything after the status is dropped ("503 UNAVAILABLE")."""
    head = _BODY.sub("", str(text)).strip()
    found = _STATUS_PREFIX.match(head)
    return redact(found.group(1) if found else head, limit)


def _chain(error: BaseException) -> Iterator[BaseException]:
    """The exception and whatever caused it (frameworks wrap provider errors)."""
    seen: set[int] = set()
    pending = [error]
    while pending:
        exc = pending.pop(0)
        if exc is None or id(exc) in seen:
            continue
        seen.add(id(exc))
        yield exc
        pending += [exc.__cause__, exc.__context__, *getattr(exc, "exceptions", ())]


def http_status(error: BaseException) -> int | None:
    """The HTTP status the exception structurally carries. None when it has none: a status is never inferred."""
    for exc in _chain(error):
        for value in (getattr(exc, "code", None), getattr(exc, "status_code", None),
                      getattr(getattr(exc, "response", None), "status_code", None)):
            if isinstance(value, int) and not isinstance(value, bool) and 400 <= value <= 599:
                return value
    return None


def provider_status(error: BaseException) -> str | None:
    """The provider's own status word (for example UNAVAILABLE), only when it is a plain status word."""
    for exc in _chain(error):
        value = getattr(exc, "status", None)
        if isinstance(value, str) and _PROVIDER_STATUS.match(value):
            return value
    return None


def _names(error: BaseException) -> set[str]:
    return {cls.__name__ for exc in _chain(error) for cls in type(exc).__mro__}


def _code(error: BaseException, status: int | None, output_failure: bool) -> str:
    names = _names(error)
    if "GeminiRunTimeout" in names:
        return APPLICATION_TIMEOUT
    if names & _LOCAL_BUDGET:
        return LOCAL_BUDGET_EXCEEDED
    if status == 503:
        return PROVIDER_UNAVAILABLE
    if status == 429:
        return PROVIDER_RATE_LIMITED
    if status in (401, 403):
        return AUTHENTICATION_FAILED
    if status is not None:
        if status >= 500:
            return PROVIDER_SERVER_ERROR
        # Gemini answers an invalid API key with HTTP 400 and this reason in its structured error.
        return AUTHENTICATION_FAILED if status == 400 and "API_KEY_INVALID" in str(error) else INVALID_REQUEST
    if names & _TIMEOUT:
        return APPLICATION_TIMEOUT
    if names & _NETWORK:
        return NETWORK_ERROR
    if output_failure or names & _OUTPUT:
        return STRUCTURED_OUTPUT_INVALID
    return UNCLASSIFIED


def should_retry(error: BaseException) -> bool:
    """Only an HTTP 503 is retried: a temporary provider condition. A 429 or an authentication error would only spend
    another request, and the status comes from the exception's structured fields, never from its text."""
    return http_status(error) == 503


def retry_record(error: BaseException, agent: str, outcome: str, delay_s: float) -> ProviderRetry:
    return ProviderRetry(agent=agent, task_id=AGENT_TASK.get(agent), occurred_at=utc_now(), http_status=503,
                         error_code=_code(error, http_status(error), False), delay_s=delay_s, outcome=outcome)


def failure_text(error: BaseException, limit: int = 500) -> str:
    """One safe line for the run's error lists: the status of a provider error (its body is dropped), otherwise the
    exception's own message without credentials."""
    name = type(error).__name__
    status = http_status(error)
    if status is None:
        return f"{name}: {redact(error, limit)}"
    word = provider_status(error)
    return f"{name}: {status} {word}" if word else f"{name}: {status}"


def classify(error: BaseException, *, run_id: str, agent: str | None = None, model: str | None = None,
             provider: str | None = None, output_failure: bool = False,
             occurred_at: datetime | None = None) -> ProviderFailure:
    """A ProviderFailure for this exception. output_failure: the caller knows the agent's output was the problem."""
    status = http_status(error)
    code = _code(error, status, output_failure)
    category, retryable, _, what = CODES[code]
    word = provider_status(error)
    reason = "high demand" if code == PROVIDER_UNAVAILABLE and "high demand" in str(error).lower() else None
    summary = what + (f" ({reason})" if reason else "")
    if status is None and code in (LOCAL_BUDGET_EXCEEDED, UNCLASSIFIED, APPLICATION_TIMEOUT):
        summary = f"{summary}: {redact(error, 180)}"
    return ProviderFailure(provider=provider, model=model, http_status=status, provider_status=word,
                           provider_reason=reason, exception_type=type(error).__name__[:80], error_code=code,
                           category=category, retryable=retryable, occurred_at=occurred_at or utc_now(), agent=agent,
                           task_id=AGENT_TASK.get(agent or ""), correlation_id=f"trace-{run_id}", summary=summary[:300])


def trace_labels(code: str | None, default_code: str, default_category: str) -> tuple[str, str]:
    """(error_code, error_category) for a trace event: the classified failure, or the event's own default when the
    failure could not be classified."""
    if code is None or code == UNCLASSIFIED or code not in CODES:
        return default_code, default_category
    return code, CODES[code][2]


def agent_outcome(failure: ProviderFailure | None) -> str:
    """How an agent's run ended, for AgentRunRecord.outcome."""
    if failure is None or failure.category in ("unknown", "output"):
        return "exception"
    return "timeout" if failure.category == "timeout" else "provider_error"


def explanation(failure: ProviderFailure, preserved: list[str], human_review: bool) -> str:
    """What a person should know about this failure. Only states what the run recorded."""
    gemini = (failure.provider or "") == "google-gemini"
    name = "Gemini" if gemini else "The model provider"
    status = f" (HTTP {failure.http_status})" if failure.http_status else ""
    if failure.error_code == PROVIDER_UNAVAILABLE:
        first = (f"{name} is temporarily unavailable due to high demand." if failure.provider_reason == "high demand"
                 else f"{name} is temporarily unavailable.")
    else:
        first = {
            PROVIDER_RATE_LIMITED: f"{name} refused the request because a rate limit or quota was reached{status}.",
            PROVIDER_SERVER_ERROR: f"{name} returned a server error{status}.",
            AUTHENTICATION_FAILED: f"{name} refused the configured credentials or permissions{status}.",
            INVALID_REQUEST: f"{name} rejected the request as invalid{status}.",
            NETWORK_ERROR: f"{name} could not be reached (network or DNS error).",
            APPLICATION_TIMEOUT: "A time limit of this application passed before the agent finished.",
            STRUCTURED_OUTPUT_INVALID: "The agent's structured output was missing or invalid.",
            LOCAL_BUDGET_EXCEEDED: "The local Gemini request budget refused the request; nothing was sent to Gemini.",
        }.get(failure.error_code, "The agent failed with an error that could not be classified.")
    kept = "Existing verified results have been preserved" if preserved else "No verified report existed yet"
    closing = f"{kept}, and the investigation requires human review." if human_review else f"{kept}."
    retried = "The automatic retry also failed. " if failure.retried else ""
    return f"{first} {retried}The affected agent could not complete. {closing}"
