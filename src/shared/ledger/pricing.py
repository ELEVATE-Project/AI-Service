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

    def compute_cost(self, provider: str, model: str, tokens_in: int, tokens_out: int, cache_write_tokens: int = 0,
        cache_read_tokens: int = 0) -> CostBlock:

        key = f"{provider}/{model}"
        rates = self._models.get(key)
        if rates is None:
            raise UnknownModelError(f"no pricing for {key}")
        cost = (
            tokens_in * rates["input_rate_per_1k_tokens"] / 1000
            + tokens_out * rates["output_rate_per_1k_tokens"] / 1000
            + cache_write_tokens * rates["cache_write_rate_per_1k_tokens"] / 1000
            + cache_read_tokens * rates["cache_read_rate_per_1k_tokens"] / 1000
        )
        return CostBlock(computed_usd=round(cost, 10), pricing_version=self._version)


pricing_table = PricingTable()