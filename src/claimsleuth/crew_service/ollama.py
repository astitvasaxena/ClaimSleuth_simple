"""Local Ollama inference for the CrewAI Forensic Agent (live mode "ollama-local").

Uses CrewAI 1.15.33's own Ollama provider (`crewai.llms.providers.openai_compatible`, Ollama's
OpenAI-compatible /v1 endpoint, through the `openai` SDK that ships with CrewAI) -- no extra dependency.

Safety rules enforced before any model call:
  * the endpoint must be http on a loopback host (127.0.0.1, localhost, ::1) with path /v1;
  * the model name must be a plain local tag (no registry host, no "cloud" tag);
  * Ollama must answer /api/version, and /api/tags must list the exact model as a local file
    (non-zero size, no remote_host / remote_model) with the "tools" capability.
Settings come from environment variables with conservative, range-checked defaults.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

import claimsleuth.crew_service  # noqa: F401  (telemetry/tracing off before crewai is imported)
from crewai.llms.providers.openai_compatible.completion import OpenAICompatibleCompletion
from pydantic import PrivateAttr

PROVIDER = "ollama-local"
MODE = "ollama-local"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_MODEL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}(:[a-z0-9][a-z0-9._-]{0,63})?$")

# name -> (env var, default, minimum, maximum)
_INT_SETTINGS = {
    "call_timeout_s": ("CLAIMSLEUTH_OLLAMA_CALL_TIMEOUT", 360, 30, 900),     # one HTTP model call
    "run_budget_s": ("CLAIMSLEUTH_OLLAMA_RUN_BUDGET", 900, 60, 3600),        # checked before every model call
    "max_tokens": ("CLAIMSLEUTH_OLLAMA_MAX_TOKENS", 1200, 256, 4096),        # per completion
    "max_iter": ("CLAIMSLEUTH_OLLAMA_MAX_ITER", 4, 2, 8),                    # CrewAI agent iterations
    "guardrail_retries": ("CLAIMSLEUTH_OLLAMA_GUARDRAIL_RETRIES", 1, 0, 2),
}


class OllamaConfigError(ValueError):
    """Invalid live-mode configuration (reported as NOT_CONFIGURED)."""


class OllamaUnavailableError(RuntimeError):
    """Ollama or the model is not usable right now (reported as FAILED)."""


@dataclass(frozen=True)
class OllamaSettings:
    model: str = "qwen2.5:3b"
    base_url: str = "http://127.0.0.1:11434/v1"
    temperature: float = 0.0
    seed: int = 7
    call_timeout_s: int = 360
    run_budget_s: int = 900
    max_tokens: int = 1200
    max_iter: int = 4
    guardrail_retries: int = 1

    @property
    def server_root(self) -> str:
        parts = urlsplit(self.base_url)
        return f"{parts.scheme}://{parts.netloc}"

    def public(self) -> dict[str, Any]:
        return asdict(self)


def settings_from_env(env: dict[str, str] | None = None) -> OllamaSettings:
    env = os.environ if env is None else env
    values: dict[str, Any] = {"model": env.get("CLAIMSLEUTH_OLLAMA_MODEL", OllamaSettings.model),
                              "base_url": env.get("CLAIMSLEUTH_OLLAMA_URL", OllamaSettings.base_url)}
    for name, (var, default, low, high) in _INT_SETTINGS.items():
        raw = env.get(var)
        try:
            value = default if raw in (None, "") else int(raw)
        except ValueError as exc:
            raise OllamaConfigError(f"{var}={raw!r} is not an integer") from exc
        if not low <= value <= high:
            raise OllamaConfigError(f"{var}={value} is outside the allowed range {low}..{high}")
        values[name] = value
    settings = OllamaSettings(**values)
    validate(settings)
    return settings


def validate(settings: OllamaSettings) -> None:
    parts = urlsplit(settings.base_url)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS:
        raise OllamaConfigError(f"Ollama endpoint must be http on a loopback host, got {settings.base_url!r}")
    if parts.path.rstrip("/") != "/v1" or parts.query or parts.fragment or parts.username:
        raise OllamaConfigError(f"Ollama endpoint must be the OpenAI-compatible /v1 path, got {settings.base_url!r}")
    name = settings.model.lower()
    if not _MODEL_NAME.fullmatch(name) or settings.model != name:
        raise OllamaConfigError(f"model name {settings.model!r} is not a plain local Ollama tag")
    if "cloud" in name:
        raise OllamaConfigError(f"model {settings.model!r} looks cloud-routed; only local models are allowed")
    if settings.run_budget_s < settings.call_timeout_s:
        raise OllamaConfigError("run budget must be at least one call timeout")


def _get_json(url: str, timeout: float = 5.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def preflight(settings: OllamaSettings) -> dict[str, Any]:
    """Prove Ollama is up and the model is a local, tool-capable file. Raises OllamaUnavailableError."""
    validate(settings)
    root = settings.server_root
    try:
        version = _get_json(f"{root}/api/version").get("version")
        tags = _get_json(f"{root}/api/tags").get("models", [])
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise OllamaUnavailableError(f"Ollama is not reachable at {root}: {exc}") from exc
    entry = next((m for m in tags if m.get("name") == settings.model or m.get("model") == settings.model), None)
    if entry is None:
        available = sorted(m.get("name", "?") for m in tags)
        raise OllamaUnavailableError(f"model {settings.model!r} is not in the local Ollama list {available}; "
                                     "it will not be downloaded automatically")
    if entry.get("remote_host") or entry.get("remote_model") or not entry.get("size"):
        raise OllamaUnavailableError(f"model {settings.model!r} is not a local model file (cloud-routed or empty)")
    capabilities = entry.get("capabilities")
    if capabilities is not None and "tools" not in capabilities:
        raise OllamaUnavailableError(f"model {settings.model!r} does not support tool calling: {capabilities}")
    details = entry.get("details") or {}
    return {"ollama_version": version, "model": entry.get("name"), "digest": entry.get("digest"),
            "size_bytes": entry.get("size"), "parameter_size": details.get("parameter_size"),
            "quantization": details.get("quantization_level"), "context_length": details.get("context_length"),
            "capabilities": capabilities}


class LocalOllamaLLM(OpenAICompatibleCompletion):
    """CrewAI's own OpenAI-compatible Ollama provider, plus a run budget checked before every model call.

    Each call is also bounded by the HTTP timeout (`timeout`). A call that starts before the budget runs
    out can overrun it by at most one call timeout; no new call starts after it."""

    _deadline: float | None = PrivateAttr(default=None)

    def start_budget(self, seconds: float) -> None:
        self._deadline = time.monotonic() + seconds

    def call(self, *args, **kwargs):
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise TimeoutError("ollama-local run budget exhausted before the next model call")
        return super().call(*args, **kwargs)


def build_llm(settings: OllamaSettings) -> LocalOllamaLLM:
    validate(settings)
    return LocalOllamaLLM(model=settings.model, provider="ollama", base_url=settings.base_url,
                          temperature=settings.temperature, seed=settings.seed, timeout=settings.call_timeout_s,
                          max_retries=0, max_tokens=settings.max_tokens)
