"""Cost accounting and the USD budget (WP1).

Implements spec 01 R50 (price rows in USD per million tokens for input,
output, 5-minute cache write, 1-hour cache write, cache read; x1.1 for US-only
inference geography; $0.01 per web search; long-prompt tier above a
threshold; unknown models use default pricing; final usage from the closing
delta; 5m/1h cache-creation split preserved) and 01 R8 (``max_budget_usd``:
remaining budget surfaced to the model, exceeding it ends the session with
``error_max_budget_usd``).

Token *estimation* and context anchoring (01 R47/R48) are FINAL in
``harnesslab.tokens``; threshold levels are in ``compaction.thresholds``.

Design choices (spec silent):

* Model lookup: exact id, then the first ``PRICES`` key contained in the id
  (family match), then ``DEFAULT_PRICE``. Rows below are illustrative
  placeholders, not authoritative prices; org policy passes ``prices=``.
* Cache creation not split into 5m/1h by the server is billed at the 5 m rate.
* The long-prompt tier multiplies every token price when the prompt (input +
  cache read + cache creation) exceeds the row's threshold.
* The US-geo multiplier applies to token cost; web searches are a flat fee.
* "Exceeding" the budget means ``total >= max_budget_usd`` (no money left).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harnesslab.types import Usage

US_GEO_MULTIPLIER = 1.1
WEB_SEARCH_USD = 0.01
PER_MILLION = 1_000_000


@dataclass
class PriceRow:
    """USD per million tokens (01 R50)."""

    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    long_prompt_threshold: int | None = None
    long_prompt_multiplier: float = 1.0


#: Placeholder prices for the demo; unknown models use ``DEFAULT_PRICE`` (01 R50).
DEFAULT_PRICE = PriceRow(3.0, 15.0, 3.75, 6.0, 0.30)
PRICES: dict[str, PriceRow] = {
    "haiku": PriceRow(1.0, 5.0, 1.25, 2.0, 0.10),
    "sonnet": PriceRow(3.0, 15.0, 3.75, 6.0, 0.30, long_prompt_threshold=200_000, long_prompt_multiplier=2.0),
    "opus": PriceRow(15.0, 75.0, 18.75, 30.0, 1.50),
}


def price_for(model_id: str, prices: dict[str, PriceRow] | None = None) -> PriceRow:
    table = PRICES if prices is None else prices
    if model_id in table:
        return table[model_id]
    mid = model_id.lower()
    for key, row in table.items():
        if key.lower() in mid:
            return row
    return DEFAULT_PRICE


def cost_usd(usage: Usage, model_id: str, *, us_geo: bool = False, prices: dict[str, PriceRow] | None = None) -> float:
    """``Σ tokens_k · price_k / 1e6`` (x1.1 US geo) + $0.01 per web search (01 R50)."""
    row = price_for(model_id, prices)
    split = usage.cache_creation_5m + usage.cache_creation_1h
    unsplit = max(0, usage.cache_creation_input_tokens - split)
    tokens_cost = (
        usage.input_tokens * row.input
        + usage.output_tokens * row.output
        + (usage.cache_creation_5m + unsplit) * row.cache_write_5m
        + usage.cache_creation_1h * row.cache_write_1h
        + usage.cache_read_input_tokens * row.cache_read
    ) / PER_MILLION
    prompt = usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens
    if row.long_prompt_threshold is not None and prompt > row.long_prompt_threshold:
        tokens_cost *= row.long_prompt_multiplier
    if us_geo:
        tokens_cost *= US_GEO_MULTIPLIER
    return tokens_cost + usage.web_search_requests * WEB_SEARCH_USD


@dataclass
class CostTracker:
    total_usd: float = 0.0
    by_model: dict[str, Usage] = field(default_factory=dict)
    us_geo: bool = False
    prices: dict[str, PriceRow] | None = None

    def add(self, usage: Usage, model_id: str) -> float:
        """Record one response's final usage; return its cost."""
        c = cost_usd(usage, model_id, us_geo=self.us_geo, prices=self.prices)
        self.total_usd += c
        self.by_model[model_id] = self.by_model.get(model_id, Usage()).add(usage)
        return c

    def remaining(self, max_budget_usd: float | None) -> float | None:
        if max_budget_usd is None:
            return None
        return max(0.0, max_budget_usd - self.total_usd)

    def over_budget(self, max_budget_usd: float | None) -> bool:
        return max_budget_usd is not None and self.total_usd >= max_budget_usd
