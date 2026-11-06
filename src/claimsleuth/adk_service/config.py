"""Configuration for the Google ADK investigation (model, MCP endpoint, limits).

Secrets are read from the environment or the project's .env file and never printed.
"""
from __future__ import annotations

import os

from claimsleuth import config, execution_mode

DEFAULT_GEMINI_MODEL = execution_mode.DEFAULT_GEMINI_MODEL
DEFAULT_MCP_URL = "http://127.0.0.1:8102/mcp"
MAX_LLM_CALLS = 30                            # per run, enforced by ADK RunConfig
LIVE_MAX_LLM_CALLS = 10                       # live (Gemini): both agents together; normally 4-5 are needed
RUN_TIMEOUT_SECONDS = 300
LIVE_RUN_TIMEOUT_SECONDS = 600                # live: requests may wait for the shared per-minute Gemini limit
MCP_TIMEOUT_SECONDS = 10.0
GEMINI_RETRY_ATTEMPTS = 1                     # no HTTP-level retry: it would bypass the request ledger. The single
                                              # retry after a 503 is ExecutionRecorder.on_model_error's, through the ledger


def load_env() -> None:
    """Load <project>/.env if present; values already in the environment win."""
    execution_mode.load_env()


def gemini_model() -> str:
    return execution_mode.gemini_model()


def gemini_llm(model: str):
    """ADK's own Gemini model class with no HTTP-level retries (the API key is read from the environment). Each
    request is admitted by the shared request ledger first (ExecutionRecorder.before_request)."""
    from google.adk.models.google_llm import Gemini
    from google.genai import types
    return Gemini(model=model, retry_options=types.HttpRetryOptions(attempts=GEMINI_RETRY_ATTEMPTS, initial_delay=2))


def mcp_url() -> str:
    return os.environ.get("CLAIMSLEUTH_MCP_URL", DEFAULT_MCP_URL)


def llm_config_errors() -> list[str]:
    """Empty when a supported Gemini configuration is present. Never includes secret values."""
    return execution_mode.gemini_config_errors()
