"""Shared primitive: turn a model's three-class probability output into
a single directional call. New file, not a Phase 1 stub — extracted in
Phase 11 so `backtest/event_backtester.py`'s `decisions_from_model()`
(Phase 10) and `signals/signal_engine.py`'s `SignalEngine` (Phase 11)
can never independently decide "which direction did the model actually
pick" in two different ways. This is the same DRY discipline the project
has followed since Phase 8/9 (`models/feature_matrix.py` reusing
`evaluation.actual_direction()`, `backtest/event_backtester.py` reusing
`features.feature_engine.find_forward_indices()`) applied one link
further down the chain.
"""
from __future__ import annotations

CLASS_ORDER = (1, -1, 0)  # up, down, neutral — argmax tie-break favors up, then down, then neutral


def direction_from_probabilities(proba: dict) -> "tuple[int, float]":
    """`proba`: a `{"probability_up", "probability_down", "probability_neutral"}`
    dict (any Phase 9 model's `predict_proba()` output, one row). Returns
    `(direction, probability)` where `direction` is `1`/`-1`/`0` (up/down/
    neutral) for whichever class has the highest probability, and
    `probability` is that class's own probability value. Ties are broken
    up > down > neutral, an arbitrary but deterministic and documented
    choice — exact probability ties are vanishingly rare from a real
    fitted model and never affect anything except which of two equal
    numbers gets reported."""
    candidates = (
        (1, proba["probability_up"]),
        (-1, proba["probability_down"]),
        (0, proba["probability_neutral"]),
    )
    return max(candidates, key=lambda c: c[1])
