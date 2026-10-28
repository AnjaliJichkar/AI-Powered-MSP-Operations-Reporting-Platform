"""What a run cost, when anybody has said what a token is worth.

Prices are settings with no default, for the same reason the review's price
book has none: a figure computed from a rate nobody agreed to is worse than no
figure, and it is worse in the specific way that it looks like an answer. So
`price` returns `None` when the two per-million-token settings are unset, every
caller reports "unpriced" rather than zero, and the cap that always bites is
the one on tokens.

Nothing here reads a live price list. If the model's price changes, two
settings change.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.settings import Settings

#: A million. Prices are quoted per million tokens because that is how the
#: vendor quotes them, and dividing at the last moment keeps the arithmetic
#: readable.
MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class Budget:
    """The caps a run stops at, and what it has spent.

    `tokens` is the backstop and is always set. `usd` is enforceable only once
    both prices are configured, which is why `priced` is asked rather than
    assumed.
    """

    max_tokens: int
    max_usd: float | None
    input_per_mtok: float | None
    output_per_mtok: float | None
    cache_read_per_mtok: float | None = None
    """What a million cached input tokens costs. Unset, cached reads are priced
    at the input rate — which overstates, and overstating is the direction to
    err in for a cap."""

    @classmethod
    def from_settings(cls, settings: Settings) -> Budget:
        return cls(
            max_tokens=settings.autopilot_max_tokens_per_run,
            max_usd=settings.autopilot_cost_cap_usd,
            input_per_mtok=settings.autopilot_input_cost_per_mtok,
            output_per_mtok=settings.autopilot_output_cost_per_mtok,
            cache_read_per_mtok=settings.autopilot_cache_read_cost_per_mtok,
        )

    @property
    def priced(self) -> bool:
        """Has an operator said what a token costs?"""
        return self.input_per_mtok is not None and self.output_per_mtok is not None

    def price(
        self, *, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0
    ) -> float | None:
        """The cost of those tokens, or `None` when unpriced.

        Cached reads are priced apart where a rate is set and at the input rate
        where it is not: the two are billed differently and a replay reads the
        same instructions once per ticket, so folding them together is the
        difference between a cost and a guess.
        """
        if self.input_per_mtok is None or self.output_per_mtok is None:
            return None
        cached_rate = (
            self.cache_read_per_mtok
            if self.cache_read_per_mtok is not None
            else self.input_per_mtok
        )
        return (
            input_tokens * self.input_per_mtok
            + output_tokens * self.output_per_mtok
            + cache_read_tokens * cached_rate
        ) / MILLION

    def exceeded(
        self, *, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0
    ) -> str | None:
        """Which cap has been passed, if either. Checked after each batch, so a
        run stops a batch over rather than halfway through one."""
        if input_tokens + output_tokens + cache_read_tokens >= self.max_tokens:
            return "token_cap"
        spent = self.price(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        if self.max_usd is not None and spent is not None and spent >= self.max_usd:
            return "cost_cap"
        return None
