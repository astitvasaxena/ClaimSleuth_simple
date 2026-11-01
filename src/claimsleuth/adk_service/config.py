"""Configuration for the Google ADK investigation (model, MCP endpoint, limits).

Secrets are read from the environment or the project's .env file and never printed.
"""
from __future__ import annotations

import os

from claimsleuth import config

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"     # google-adk 2.13.0's LlmAgent.DEFAULT_MODEL
DEFAULT_MCP_URL = "http://127.0.0.1:8102/mcp"
MAX_LLM_CALLS = 30                            # per run, enforced by ADK RunConfig
RUN_TIMEOUT_SECONDS = 300
MCP_TIMEOUT_SECONDS = 10.0


def load_env() -> None:
    """Load D:\\Claimsleuth_simple\\.env if present; values already in the environment win."""
    from dotenv import load_dotenv
    load_dotenv(config.PROJECT_ROOT / ".env", override=False)


def gemini_model() -> str:
    return os.environ.get("CLAIMSLEUTH_GEMINI_MODEL") or os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL


def mcp_url() -> str:
    return os.environ.get("CLAIMSLEUTH_MCP_URL", DEFAULT_MCP_URL)


def llm_config_errors() -> list[str]:
    """Empty when a supported Gemini configuration is present. Never includes secret values."""
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true", "yes"):
        missing = [v for v in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION") if not os.environ.get(v)]
        return [f"Vertex AI selected but {', '.join(missing)} not set."] if missing else []
    if os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"):
        return []
    return ["No Gemini credentials configured: set GOOGLE_API_KEY (or GEMINI_API_KEY) in the environment or in "
            f"{config.PROJECT_ROOT / '.env'}. The ADK investigation does not run without a real model."]
