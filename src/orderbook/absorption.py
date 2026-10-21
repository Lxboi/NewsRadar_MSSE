"""Absorption scores (section 18, Phase 3).

Absorption: aggressive volume on one side is high, but price displacement
stays small — resting liquidity is "absorbing" the aggression instead of
being pushed through. Whether that predicts continuation, exhaustion, or
reversal is an open empirical question (section 18 is explicit: "les
données doivent décider") — this module only computes the score, it does
not interpret it. That interpretation is Phase 8+ (baselines) / Phase 9
(models) work, done with statistics against forward returns.
"""
from __future__ import annotations


def compute_absorption_scores(
    buy_volume: float,
    sell_volume: float,
    price_start: float,
    price_end: float,
    epsilon: float = 1e-9,
) -> dict:
    """Higher score = more volume absorbed per unit of price movement.

    `price_start`/`price_end` should bracket the same window the volumes
    were measured over (e.g. a trade_flow window, or a sweep's duration).
    """
    if price_start <= 0:
        displacement_bps = 0.0
    else:
        displacement_bps = abs(price_end - price_start) / price_start * 10_000

    denom = displacement_bps + epsilon
    return {
        "buy_absorption_score": buy_volume / denom,
        "sell_absorption_score": sell_volume / denom,
        "price_displacement_bps": displacement_bps,
    }
