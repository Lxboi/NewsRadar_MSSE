"""Order Book Imbalance (section 12, Phase 3).

    OBI = (BidVolume - AskVolume) / (BidVolume + AskVolume)

computed at the top 1/5/10/25 levels, plus a distance-weighted variant
that discounts levels further from mid instead of a hard cutoff.

Pure functions operating on `(price, size, n_orders)` level tuples as
already parsed by collectors/hyperliquid_ws.py — no I/O, no state, so
they're trivial to unit-test against hand-built books
(tests/test_orderbook.py).
"""
from __future__ import annotations

import math

Level = tuple[float, float, int | None]  # (price, size, n_orders)

DEFAULT_OBI_LEVELS = (1, 5, 10, 25)


def compute_obi(bids: list[Level], asks: list[Level], levels: int) -> float | None:
    """OBI over the top `levels` price levels on each side. None if both
    sides are empty within that depth (nothing to compare)."""
    bid_vol = sum(sz for _, sz, _ in bids[:levels])
    ask_vol = sum(sz for _, sz, _ in asks[:levels])
    total = bid_vol + ask_vol
    if total == 0:
        return None
    return (bid_vol - ask_vol) / total


def compute_obi_all(
    bids: list[Level], asks: list[Level], levels_list: tuple[int, ...] = DEFAULT_OBI_LEVELS
) -> dict[str, float | None]:
    return {f"obi_{n}": compute_obi(bids, asks, n) for n in levels_list}


def compute_weighted_obi(
    bids: list[Level],
    asks: list[Level],
    mid: float | None,
    levels: int = 25,
    decay_per_bps: float = 0.01,
) -> float | None:
    """Distance-weighted OBI (section 12: "tester également des pondérations
    selon la distance au mid-price"). Each level's size is weighted by
    exp(-decay_per_bps * distance_bps_from_mid), so levels far from mid
    contribute less than an equally-sized level right at the touch, without
    a hard level-count cutoff changing the answer discontinuously.

    Returns None if mid is unknown or both sides are empty.
    """
    if mid is None or mid <= 0:
        return None

    def weighted_volume(levels_side: list[Level]) -> float:
        total = 0.0
        for price, size, _ in levels_side[:levels]:
            distance_bps = abs(price - mid) / mid * 10_000
            weight = math.exp(-decay_per_bps * distance_bps)
            total += size * weight
        return total

    bid_vol = weighted_volume(bids)
    ask_vol = weighted_volume(asks)
    total = bid_vol + ask_vol
    if total == 0:
        return None
    return (bid_vol - ask_vol) / total
