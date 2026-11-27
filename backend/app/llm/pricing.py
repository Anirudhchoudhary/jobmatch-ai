"""Model price table and LLM cost arithmetic."""

from collections.abc import Mapping

import structlog
from pydantic import BaseModel

log = structlog.get_logger()

_warned_models: set[str] = set()


class ModelPrice(BaseModel):
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float


def llm_cost_usd(
    prices: Mapping[str, ModelPrice],
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
) -> float:
    """Cost of one call. ``input_tokens`` is LangChain's total and includes cache reads and
    writes, so the uncached part is what remains after subtracting them."""
    price = prices.get(model)
    if price is None:
        if model not in _warned_models:
            _warned_models.add(model)
            log.warning("unpriced_model", model=model)
        return 0.0
    uncached = max(input_tokens - cache_read - cache_write, 0)
    return (
        uncached * price.input
        + cache_read * price.cache_read
        + cache_write * price.cache_write
        + output_tokens * price.output
    ) / 1_000_000
