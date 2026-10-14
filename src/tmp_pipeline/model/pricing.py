"""Model call cost from token usage (plan 6.1 operations: cost per question).

Prices are data (``data/model_prices.toml``, USD per 1M tokens); ``TMP_MODEL_PRICES`` points at another file of the
same shape. A model without a price gives no cost (usage is still traced).
"""
from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from importlib import resources
from pathlib import Path

PRICES_FILE = "model_prices.toml"


@lru_cache(maxsize=4)
def load_prices(path: str = "") -> dict[str, dict[str, float]]:
    path = path or os.environ.get("TMP_MODEL_PRICES", "")
    text = (Path(path).read_text(encoding="utf-8") if path
            else resources.files("tmp_pipeline").joinpath("data", PRICES_FILE).read_text(encoding="utf-8"))
    return {name: {k: float(v) for k, v in p.items()} for name, p in tomllib.loads(text).get("models", {}).items()}


def cost(model: str, input_tokens: int | None, output_tokens: int | None = 0) -> dict[str, float] | None:
    """{"input", "output", "total"} in USD, or None when the model has no price or there is no usage."""
    price = load_prices().get(model)
    if price is None or input_tokens is None:
        return None
    cin = (input_tokens or 0) * price.get("input", 0.0) / 1e6
    cout = (output_tokens or 0) * price.get("output", 0.0) / 1e6
    return {"input": round(cin, 8), "output": round(cout, 8), "total": round(cin + cout, 8)}
