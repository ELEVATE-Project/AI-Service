from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.shared.schemas.envelope import CostBlock

_DEFAULT_YAML = Path(__file__).parents[3] / "pricing" / "models.yaml"


class UnknownModelError(Exception):
    pass


class PricingTable:
    def __init__(self, yaml_path: Path = _DEFAULT_YAML) -> None:
        with yaml_path.open() as fh:
            raw: dict[str, Any] = yaml.safe_load(fh)
        self._version: int = raw["pricing_version"]
        self._models: dict[str, dict[str, Any]] = raw["models"]

    @property
    def version(self) -> int:
        return self._version

    def has_model(self, provider: str, model: str) -> bool:
        return f"{provider}/{model}" in self._models

    def _rates_for_prompt(self, provider: str, model: str, *, prompt_tokens: int) -> dict[str, Any]:
        """Select rates using the full prompt size, including cache tokens."""
        key = f"{provider}/{model}"
        rates = self._models.get(key)
        if rates is None:
            raise UnknownModelError(f"no pricing for {key}")
        tier = rates.get("long_prompt_tier")
        if tier is not None and prompt_tokens > tier["threshold_tokens"]:
            return tier
        return rates

    def compute_cost(
        self, provider: str, model: str, tokens_in: int, tokens_out: int,
        cache_write_tokens: int = 0, cache_read_tokens: int = 0, *,
        cache_write_5m_tokens: int | None = None,
        cache_write_1h_tokens: int | None = None,
        cache_ttl: str | None = None,
    ) -> CostBlock:
        counts = (tokens_in, tokens_out, cache_write_tokens, cache_read_tokens,
                  cache_write_5m_tokens, cache_write_1h_tokens)
        if any(v is not None and v < 0 for v in counts):
            raise ValueError("token counts must be non-negative")
        rates = self._rates_for_prompt(provider, model, prompt_tokens=tokens_in)

        # Response split wins; any writes without a reported TTL use the request
        # TTL, or 5m when omitted. Keep the aggregate token count unchanged.
        write_5m = cache_write_5m_tokens or 0
        write_1h = cache_write_1h_tokens or 0
        # Pricing runs after the provider has billed the call, so an inconsistent
        # split must not fail the request: trust the split when it is larger.
        cache_write_tokens = max(cache_write_tokens, write_5m + write_1h)
        remaining_writes = cache_write_tokens - write_5m - write_1h
        if cache_ttl == "1h":
            write_1h += remaining_writes
        else:
            write_5m += remaining_writes

        # Evidence for inclusive prompt totals (LiteLLM 1.83.0):
        # Anthropic direct: llms/anthropic/chat/transformation.py:1595-1619
        # adds cache_creation_input_tokens and cache_read_input_tokens to input.
        # Bedrock: llms/bedrock/chat/converse_transformation.py:1534-1555
        # adds cacheReadInputTokens/cacheWriteInputTokens before setting prompt_tokens.
        # OpenRouter passes inclusive OpenAI-shaped usage through LiteLLM; its docs
        # show prompt_tokens=10339 with cached_tokens=10318 as a subset:
        # https://openrouter.ai/docs/guides/best-practices/prompt-caching#usage-object-fields
        # Subtract only for pricing; API usage and ledger tokens_in stay inclusive.
        uncached_input = max(0, tokens_in - cache_read_tokens - cache_write_tokens)
        write_5m_rate = rates["cache_write_rate_per_1k_tokens"]
        write_1h_rate = rates.get("cache_write_1h_rate_per_1k_tokens", write_5m_rate)
        cost = (
            uncached_input * rates["input_rate_per_1k_tokens"] / 1000
            + tokens_out * rates["output_rate_per_1k_tokens"] / 1000
            + write_5m * write_5m_rate / 1000
            + write_1h * write_1h_rate / 1000
            + cache_read_tokens * rates["cache_read_rate_per_1k_tokens"] / 1000
        )
        return CostBlock(computed_usd=round(cost, 10), pricing_version=self._version)


pricing_table = PricingTable()
