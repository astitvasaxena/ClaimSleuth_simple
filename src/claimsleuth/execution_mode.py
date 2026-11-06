"""One switch for how the three agents run: EXECUTION_MODE=live or EXECUTION_MODE=offline.

  live     the Evidence and Investigator Agents (Google ADK) and the Forensic Agent (CrewAI) all use genuine
           Gemini inference. A missing key, a failed call or a conflicting setting is an explicit error;
           nothing falls back to a test double.
  offline  all three agents use the labelled offline test doubles (no language model, no key, no network).

When EXECUTION_MODE is unset, the older per-component settings decide on their own (CLAIMSLEUTH_ADK_LLM,
CLAIMSLEUTH_CREW_LLM), so existing commands and tests keep their behaviour. When it is set, a per-component
setting that contradicts it is refused instead of silently winning or losing.

Plain Python: imported by both virtual environments (.venv and .venv-crew). Never reads or prints secret values
beyond checking that a key is present.
"""
from __future__ import annotations

import contextvars
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from claimsleuth import config

ENV_VAR = "EXECUTION_MODE"
LIVE, OFFLINE = "live", "offline"
GEMINI, TEST_DOUBLE = "gemini", "test-double"
PROVIDER_GEMINI = "google-gemini"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"     # google-adk 2.13.0's LlmAgent.DEFAULT_MODEL; verified with one call


class ExecutionModeError(ValueError):
    """The configured modes are invalid or contradict each other (reported as NOT_CONFIGURED)."""


# A mode fixed for one piece of work (one asyncio task / thread context), e.g. an OFFLINE evaluation started from a
# dashboard that runs in LIVE mode. It never changes the process environment, so other work is unaffected.
_forced: contextvars.ContextVar[str | None] = contextvars.ContextVar("claimsleuth_forced_execution_mode", default=None)


@contextmanager
def forced(mode: str) -> Iterator[None]:
    """Run the enclosed work in `mode` ("live" or "offline") regardless of EXECUTION_MODE in the environment."""
    if mode not in (LIVE, OFFLINE):
        raise ExecutionModeError(f"forced execution mode must be 'live' or 'offline', not {mode!r}")
    token = _forced.set(mode)
    try:
        yield
    finally:
        _forced.reset(token)


def load_env() -> None:
    """Load <project>/.env if present; values already in the environment win."""
    try:
        from dotenv import load_dotenv
    except ImportError:          # python-dotenv ships with google-adk and crewai; never required for offline use
        return
    load_dotenv(config.PROJECT_ROOT / ".env", override=False)


def execution_mode(env: Mapping[str, str] | None = None) -> str | None:
    """"live", "offline", or None when unset. Raises ExecutionModeError for any other value.
    With no explicit `env`, a mode fixed by `forced()` for the current context takes precedence."""
    if env is None and _forced.get() is not None:
        return _forced.get()
    raw = (os.environ if env is None else env).get(ENV_VAR, "").strip().lower()
    if not raw:
        return None
    if raw not in (LIVE, OFFLINE):
        raise ExecutionModeError(f"{ENV_VAR}={raw!r} is not valid; use 'live' or 'offline'")
    return raw


def _resolve(component: str, chosen: str | None, mode: str | None, default: str) -> str:
    expected = {LIVE: GEMINI, OFFLINE: TEST_DOUBLE}.get(mode)
    value = chosen or expected or default
    if expected is not None and value != expected:
        raise ExecutionModeError(f"{ENV_VAR}={mode} requires the {component} to use {expected!r}, but it is "
                                 f"configured as {value!r}; nothing was run and no fallback was used")
    return value


def adk_llm_mode(explicit: str | None = None, default: str = GEMINI, env: Mapping[str, str] | None = None) -> str:
    """ADK model mode: an explicit argument, else CLAIMSLEUTH_ADK_LLM, else EXECUTION_MODE, else `default`.
    Inside `forced()`, the forced mode decides and per-component environment settings are ignored."""
    if env is None and _forced.get() is not None:
        return _resolve("Google ADK agents", explicit, _forced.get(), default)
    env = os.environ if env is None else env
    return _resolve("Google ADK agents", explicit or env.get("CLAIMSLEUTH_ADK_LLM"), execution_mode(env), default)


def crew_llm_mode(env: Mapping[str, str] | None = None) -> str:
    """CrewAI model mode: CLAIMSLEUTH_CREW_LLM, else EXECUTION_MODE, else the offline test double."""
    env = os.environ if env is None else env
    return _resolve("CrewAI Forensic Agent", env.get("CLAIMSLEUTH_CREW_LLM"), execution_mode(env), TEST_DOUBLE)


def gemini_model(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return env.get("CLAIMSLEUTH_GEMINI_MODEL") or env.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL


def gemini_config_errors(env: Mapping[str, str] | None = None) -> list[str]:
    """Empty when Gemini credentials are configured. Never includes secret values."""
    env = os.environ if env is None else env
    if env.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true", "yes"):
        missing = [v for v in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION") if not env.get(v)]
        return [f"Vertex AI selected but {', '.join(missing)} not set."] if missing else []
    if (env.get("GOOGLE_API_KEY") or env.get("GEMINI_API_KEY") or "").strip():
        return []
    return ["No Gemini credentials configured: set GOOGLE_API_KEY (or GEMINI_API_KEY) in the environment or in "
            f"{config.PROJECT_ROOT / '.env'}. Live mode does not run without a real model."]
