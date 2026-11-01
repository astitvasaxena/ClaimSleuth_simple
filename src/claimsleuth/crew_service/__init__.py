"""CREWAI forensic investigation (runs in .venv-crew: crewai needs mcp<2 and opentelemetry-api>=1.45.1).

Importing this package first switches off CrewAI's anonymous telemetry, trace upload and its
interactive first-run tracing prompt, before `crewai` itself is imported anywhere.
"""
import os

os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_DISABLE_TRACKING", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("CREWAI_TESTING", "true")      # makes CrewAI skip its interactive first-run tracing prompt

import warnings

# CrewAI warns that bound-method callbacks are dropped from crew checkpoints; this project does not use
# checkpoint/restore, so the per-run recorder's methods are fine.
warnings.filterwarnings("ignore", message=r"A (method|function) callback cannot be saved in a checkpoint")
