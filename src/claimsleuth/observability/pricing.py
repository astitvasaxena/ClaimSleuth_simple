"""Model cost: only from an explicitly configured, verified price table, and only for usage the provider reported.

ClaimSleuth ships **no** prices. A cost is calculated only when CLAIMSLEUTH_MODEL_PRICING names a JSON file such as

    {"models": {"gemini-3.5-flash": {"input_per_million": 0.0, "output_per_million": 0.0, "currency": "USD",
                                     "source": "https://ai.google.dev/pricing (checked by <name>)",
                                     "verified_on": "2026-11-04"}}}

Every price needs its source and the date it was verified. Without such a file, or without reported token counts,
the cost is None with the reason - never an estimate.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from pydantic import Field, ValidationError

from claimsleuth.schemas import StrictModel

ENV_VAR = "CLAIMSLEUTH_MODEL_PRICING"
OFFLINE_MODELS = frozenset({"test-double", "scripted-test-double", "offline-test-double"})


class ModelPrice(StrictModel):
    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)
    cached_input_per_million: float | None = Field(default=None, ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    source: str = Field(min_length=8, max_length=300)
    verified_on: date


class PricingTable(StrictModel):
    models: dict[str, ModelPrice]


def load_pricing(path: Path | None = None) -> tuple[PricingTable | None, str]:
    """(table, note). No configured file -> (None, reason); an invalid file is rejected, never partly used."""
    raw = path or os.environ.get(ENV_VAR)
    if not raw:
        return None, f"no verified pricing configured ({ENV_VAR} is not set)"
    try:
        return PricingTable.model_validate_json(Path(raw).read_text(encoding="utf-8")), f"pricing from {Path(raw).name}"
    except (OSError, ValidationError, ValueError) as exc:
        return None, f"pricing file not used: {type(exc).__name__}: {str(exc).splitlines()[0][:150]}"


def cost(model: str | None, prompt_tokens: int | None, completion_tokens: int | None, cached_tokens: int | None,
         table: PricingTable | None, note: str) -> tuple[float | None, str | None, str]:
    """(cost, currency, note) for reported token counts. Cached input is priced at cached_input_per_million when the
    table gives it, otherwise as normal input."""
    if model in OFFLINE_MODELS:
        return None, None, "offline test double: no model was billed"
    if table is None:
        return None, None, note
    price = table.models.get(model or "")
    if price is None:
        return None, None, f"no verified price for model {model!r}"
    if prompt_tokens is None or completion_tokens is None:
        return None, None, "token usage was not reported by the provider, so no cost is calculated"
    cached = cached_tokens or 0
    cached_rate = price.cached_input_per_million if price.cached_input_per_million is not None else price.input_per_million
    amount = ((prompt_tokens - cached) * price.input_per_million + cached * cached_rate
              + completion_tokens * price.output_per_million) / 1_000_000
    return round(amount, 8), price.currency, f"from verified pricing ({price.source}, verified {price.verified_on})"
