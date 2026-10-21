"""Mid price, microprice, and their delta/acceleration (section 13, Phase 3).

    mid_price  = (best_bid + best_ask) / 2
    microprice = (best_bid * best_ask_size + best_ask * best_bid_size)
                 / (best_bid_size + best_ask_size)

Microprice weights each side's price by the *opposite* side's size: heavy
size resting on the ask (about to be executed against) pulls the microprice
toward the bid side's price less than a naive mid would suggest reflects —
concretely, more size on the ask than the bid pulls microprice toward the
ask, which is the standard convention (it approximates where the price
"should" be if the imbalance resolves via trades hitting the thinner side
first). Comparing its predictive power against last_price/mid_price across
horizons (section 13) is exploratory-analysis work for notebooks/, not
something this module decides on its own.
"""
from __future__ import annotations

from collections import deque


def compute_mid(best_bid: float | None, best_ask: float | None) -> float | None:
    if best_bid is None or best_ask is None:
        return None
    return (best_bid + best_ask) / 2


def compute_microprice(
    best_bid: float | None,
    best_bid_size: float | None,
    best_ask: float | None,
    best_ask_size: float | None,
) -> float | None:
    if best_bid is None or best_ask is None or best_bid_size is None or best_ask_size is None:
        return None
    total_size = best_bid_size + best_ask_size
    if total_size == 0:
        return None
    return (best_bid * best_ask_size + best_ask * best_bid_size) / total_size


class MicropriceHistory:
    """Keeps a short rolling history of microprice values so delta and
    acceleration (second difference) can be computed as new snapshots
    arrive. One instance per asset."""

    def __init__(self, maxlen: int = 64) -> None:
        self._values: deque[float] = deque(maxlen=maxlen)

    def push(self, microprice: float | None) -> None:
        if microprice is not None:
            self._values.append(microprice)

    @property
    def current(self) -> float | None:
        return self._values[-1] if self._values else None

    @property
    def delta(self) -> float | None:
        """Change since the previous observation."""
        if len(self._values) < 2:
            return None
        return self._values[-1] - self._values[-2]

    @property
    def acceleration(self) -> float | None:
        """Change of the change — second difference over the last three
        observations."""
        if len(self._values) < 3:
            return None
        d1 = self._values[-1] - self._values[-2]
        d2 = self._values[-2] - self._values[-3]
        return d1 - d2
