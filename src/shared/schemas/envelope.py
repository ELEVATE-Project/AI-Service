from __future__ import annotations
from typing import Optional
from pydantic import BaseModel


class CostBlock(BaseModel):
    computed_usd: float = 0.0
    pricing_version: int = 0
    provider_reported_usd: Optional[float] = None
    currency: str = "USD"


class LatencyBlock(BaseModel):
    total: int = 0
    guardrails_in: Optional[int] = None
    guardrails_out: Optional[int] = None
    upstream: Optional[int] = None
    time_to_first_token: Optional[int] = None


class GuardrailsBlock(BaseModel):
    input_flags: list[str] = []
    output_flags: list[str] = []
    redactions_applied: list[str] = []


class PolicyBlock(BaseModel):
    rate_limit_remaining: Optional[int] = None
    budget_remaining_usd: Optional[float] = None