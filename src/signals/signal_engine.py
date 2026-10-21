"""Combines model output + expected value + regime into the final
signal payload (section 33): signal, probabilities, expected_move,
expected_value, confidence, signal_quality, market_regime.

Roadmap: PHASE 11. Formalizes, for live/paper use, what
`backtest/event_backtester.py: decisions_from_model()` did as a
deliberately simple backtest-only stand-in (see that module's docstring)
— both now share the same `direction_from_probabilities()` primitive
(`models/direction.py`) so a model's chosen direction is never computed
two different ways depending on whether it's being backtested or run
live. `SignalEngine` is a genuinely richer payload than
`decisions_from_model()`'s three-key dict: it additionally computes
`signal_quality` (`signals/scoring.py`) and `confidence`
(`signals/confidence.py`), and shapes its output to match
`storage/database.py: Signal`'s exact column set, so a later phase
persisting a signal to that table needs no translation step.
"""
from __future__ import annotations

import datetime as dt

from src.models.direction import direction_from_probabilities
from src.signals.confidence import DEFAULT_MAX_DATA_AGE_S, compute_confidence
from src.signals.scoring import (
    DEFAULT_QUALITY_A_MIN_PROBABILITY,
    DEFAULT_QUALITY_B_MIN_PROBABILITY,
    DEFAULT_QUALITY_C_MIN_PROBABILITY,
    classify_signal_quality,
)

# Defaults mirror config/strategy.yaml's own values (this module has no
# config I/O of its own — same convention as backtest/event_backtester.py).
DEFAULT_TAKER_FEE_BPS = 5              # strategy.yaml: costs.taker_fee_bps
DEFAULT_ASSUMED_SLIPPAGE_BPS = 3       # strategy.yaml: costs.assumed_slippage_bps


class SignalEngine:
    """One instance per `(asset, horizon_s, threshold_bps)` — same
    one-instance-per-question convention as `LogisticDirectionModel`/
    `TreeDirectionModel`/`FeatureEngine`/`RegimeDetector`. Wraps a fitted
    Phase 9 model (anything exposing `predict_proba(rows)` — a plain
    `LogisticDirectionModel`/`TreeDirectionModel` or a
    `CalibratedDirectionModel`) and turns its raw probabilities into the
    full section-33 signal payload."""

    def __init__(
        self,
        model,
        *,
        asset: str,
        horizon_s: int,
        threshold_bps: float,
        model_version: str,
        taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS,
        assumed_slippage_bps: float = DEFAULT_ASSUMED_SLIPPAGE_BPS,
        model_calibration: "dict | None" = None,
        quality_a_min_probability: float = DEFAULT_QUALITY_A_MIN_PROBABILITY,
        quality_b_min_probability: float = DEFAULT_QUALITY_B_MIN_PROBABILITY,
        quality_c_min_probability: float = DEFAULT_QUALITY_C_MIN_PROBABILITY,
        max_data_age_s: float = DEFAULT_MAX_DATA_AGE_S,
        confidence_weights: "dict | None" = None,
    ) -> None:
        self.model = model
        self.asset = asset
        self.horizon_s = horizon_s
        self.threshold_bps = threshold_bps
        self.model_version = model_version
        self.taker_fee_bps = taker_fee_bps
        self.assumed_slippage_bps = assumed_slippage_bps
        # A snapshot of the model's own calibration diagnostics (e.g. from
        # `ModelRegistry.current_promoted()["calibration"]`), attached to
        # every signal this engine builds so `compute_confidence()` can
        # read it without a second registry lookup per row.
        self.model_calibration = model_calibration
        self.quality_a_min_probability = quality_a_min_probability
        self.quality_b_min_probability = quality_b_min_probability
        self.quality_c_min_probability = quality_c_min_probability
        self.max_data_age_s = max_data_age_s
        self.confidence_weights = confidence_weights

    @property
    def round_trip_cost_bps(self) -> float:
        """See `backtest.event_backtester.EventBacktester.round_trip_cost_bps`
        — identical formula, kept as a same-named property on this class
        too so a caller reading either engine's cost assumption doesn't
        need to remember two different attribute names."""
        return 2 * (self.taker_fee_bps + self.assumed_slippage_bps)

    def build_signals(self, rows: "list[dict]", *, now: "dt.datetime | None" = None) -> "list[dict]":
        """One signal payload per row, in the same order. `now` is passed
        through to `compute_confidence()`'s freshness component — leave
        it `None` for offline/backtest use (freshness then degrades to a
        neutral score, per `confidence.py`'s documented behavior) and
        pass the real wall clock for live use."""
        proba_list = self.model.predict_proba(rows)
        signals = []
        for row, proba in zip(rows, proba_list):
            signals.append(self._build_one(row, proba, now=now))
        return signals

    def build_signal(self, row: dict, *, now: "dt.datetime | None" = None) -> dict:
        """Single-row convenience wrapper around `build_signals()` — for
        a live tick-by-tick caller that doesn't have a batch of rows to
        score at once. Prefer `build_signals()` when scoring more than
        one row: batching is what `predict_proba()`'s underlying
        scikit-learn pipeline is actually efficient at."""
        return self.build_signals([row], now=now)[0]

    def _build_one(self, row: dict, proba: dict, *, now: "dt.datetime | None") -> dict:
        direction, probability = direction_from_probabilities(proba)

        # Signed expected move in bps: the full probability-weighted
        # expectation over up/down (neutral contributes 0 either way),
        # independent of which direction ends up chosen — this is a
        # market-move estimate, not a strategy-specific one. Same
        # documented simplification `event_backtester.py:
        # decisions_from_model()` uses (threshold_bps standing in for
        # the assumed move size, since the model was trained to answer
        # "did it clear threshold_bps", not "by how much").
        expected_move_bps = (
            proba["probability_up"] * self.threshold_bps - proba["probability_down"] * self.threshold_bps
        )

        expected_value_bps = None
        if direction != 0:
            # Net of round-trip costs — "expected_value" answers "is this
            # worth trading", not just "which way does the model lean".
            expected_value_bps = abs(expected_move_bps) - self.round_trip_cost_bps

        quality = classify_signal_quality(
            proba,
            quality_a_min_probability=self.quality_a_min_probability,
            quality_b_min_probability=self.quality_b_min_probability,
            quality_c_min_probability=self.quality_c_min_probability,
        )

        signal = {
            "asset": row.get("asset", self.asset),
            "timestamp": row.get("timestamp"),
            "horizon_s": self.horizon_s,
            "threshold_bps": self.threshold_bps,
            "signal": direction,
            "probability_up": proba["probability_up"],
            "probability_down": proba["probability_down"],
            "probability_neutral": proba["probability_neutral"],
            "expected_move": expected_move_bps,
            "expected_value": expected_value_bps,
            "signal_quality": quality,
            "market_regime": row.get("market_regime"),
            "model_version": self.model_version,
            "model_calibration": self.model_calibration,
            "features_snapshot": row,
        }
        signal["confidence"] = compute_confidence(
            signal,
            now=now,
            max_data_age_s=self.max_data_age_s,
            weights=self.confidence_weights,
        )
        return signal
