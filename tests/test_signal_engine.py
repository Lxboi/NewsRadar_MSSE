"""Phase 11 tests: signals/signal_engine.py — SignalEngine wraps a fitted
Phase 9 model and shapes its output into the full section-33 signal
payload, sharing direction_from_probabilities() with
backtest/event_backtester.py.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.signals.scoring import NO_TRADE, QUALITY_A
from src.signals.signal_engine import SignalEngine

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


class FixedProbaModel:
    """Returns the same probability dict for every row -- deterministic,
    so PnL/sign/quality assertions can't flake."""

    def __init__(self, p_up, p_down, p_neutral):
        self.p_up, self.p_down, self.p_neutral = p_up, p_down, p_neutral
        self.received_rows = None

    def predict_proba(self, rows):
        self.received_rows = rows
        return [
            {"probability_up": self.p_up, "probability_down": self.p_down, "probability_neutral": self.p_neutral}
            for _ in rows
        ]


def make_row(**overrides):
    row = {
        "asset": "BTC/USD",
        "timestamp": BASE,
        "price": 100.0,
        "market_regime": "NORMAL_VOL",
        "obi_10": 0.4,
    }
    row.update(overrides)
    return row


def test_build_signals_batches_through_predict_proba_once():
    model = FixedProbaModel(0.9, 0.05, 0.05)
    engine = SignalEngine(model, asset="BTC/USD", horizon_s=60, threshold_bps=25.0, model_version="v1")
    rows = [make_row(), make_row(), make_row()]
    signals = engine.build_signals(rows)
    assert len(signals) == 3
    assert model.received_rows == rows  # one batched call, not three


def test_build_signal_single_row_wrapper_matches_build_signals():
    model = FixedProbaModel(0.9, 0.05, 0.05)
    engine = SignalEngine(model, asset="BTC/USD", horizon_s=60, threshold_bps=25.0, model_version="v1")
    row = make_row()
    single = engine.build_signal(row)
    batched = engine.build_signals([row])[0]
    assert single["signal"] == batched["signal"]
    assert single["expected_value"] == batched["expected_value"]


def test_strong_up_signal_has_positive_direction_and_a_quality():
    model = FixedProbaModel(0.90, 0.05, 0.05)
    engine = SignalEngine(model, asset="BTC/USD", horizon_s=60, threshold_bps=25.0, model_version="v1")
    signal = engine.build_signal(make_row())
    assert signal["signal"] == 1
    assert signal["signal_quality"] == QUALITY_A
    assert signal["probability_up"] == 0.90


def test_neutral_signal_has_no_expected_value_and_no_trade_quality():
    model = FixedProbaModel(0.30, 0.30, 0.40)
    engine = SignalEngine(model, asset="BTC/USD", horizon_s=60, threshold_bps=25.0, model_version="v1")
    signal = engine.build_signal(make_row())
    assert signal["signal"] == 0
    assert signal["expected_value"] is None
    assert signal["signal_quality"] == NO_TRADE


def test_expected_move_sign_matches_directional_lean():
    up_model = FixedProbaModel(0.8, 0.1, 0.1)
    down_model = FixedProbaModel(0.1, 0.8, 0.1)
    up_engine = SignalEngine(up_model, asset="BTC/USD", horizon_s=60, threshold_bps=25.0, model_version="v1")
    down_engine = SignalEngine(down_model, asset="BTC/USD", horizon_s=60, threshold_bps=25.0, model_version="v1")
    up_signal = up_engine.build_signal(make_row())
    down_signal = down_engine.build_signal(make_row())
    assert up_signal["expected_move"] > 0
    assert down_signal["expected_move"] < 0


def test_expected_value_is_net_of_round_trip_cost():
    model = FixedProbaModel(0.9, 0.05, 0.05)
    engine = SignalEngine(
        model,
        asset="BTC/USD",
        horizon_s=60,
        threshold_bps=25.0,
        model_version="v1",
        taker_fee_bps=5,
        assumed_slippage_bps=3,
    )
    signal = engine.build_signal(make_row())
    expected_move_bps = 0.9 * 25.0 - 0.05 * 25.0
    round_trip_cost_bps = 2 * (5 + 3)
    assert signal["expected_value"] == pytest.approx(abs(expected_move_bps) - round_trip_cost_bps)


def test_round_trip_cost_bps_property_matches_event_backtester_formula():
    model = FixedProbaModel(0.9, 0.05, 0.05)
    engine = SignalEngine(
        model, asset="BTC/USD", horizon_s=60, threshold_bps=25.0, model_version="v1",
        taker_fee_bps=7, assumed_slippage_bps=4,
    )
    assert engine.round_trip_cost_bps == 2 * (7 + 4)


def test_confidence_is_populated_and_within_unit_interval():
    model = FixedProbaModel(0.9, 0.05, 0.05)
    engine = SignalEngine(
        model,
        asset="BTC/USD",
        horizon_s=60,
        threshold_bps=25.0,
        model_version="v1",
        model_calibration={"roc_auc_macro": 0.8},
    )
    signal = engine.build_signal(make_row(), now=BASE)
    assert 0.0 <= signal["confidence"] <= 1.0


def test_features_snapshot_carries_the_full_input_row():
    model = FixedProbaModel(0.9, 0.05, 0.05)
    engine = SignalEngine(model, asset="BTC/USD", horizon_s=60, threshold_bps=25.0, model_version="v1")
    row = make_row(spread=2.5)
    signal = engine.build_signal(row)
    assert signal["features_snapshot"] is row
    assert signal["features_snapshot"]["spread"] == 2.5


def test_asset_falls_back_to_engine_default_when_row_omits_it():
    model = FixedProbaModel(0.9, 0.05, 0.05)
    engine = SignalEngine(model, asset="ETH/USD", horizon_s=60, threshold_bps=25.0, model_version="v1")
    row = make_row()
    del row["asset"]
    signal = engine.build_signal(row)
    assert signal["asset"] == "ETH/USD"


def test_model_version_and_horizon_and_threshold_stamped_onto_every_signal():
    model = FixedProbaModel(0.9, 0.05, 0.05)
    engine = SignalEngine(model, asset="BTC/USD", horizon_s=120, threshold_bps=30.0, model_version="v2-calibrated")
    signal = engine.build_signal(make_row())
    assert signal["horizon_s"] == 120
    assert signal["threshold_bps"] == 30.0
    assert signal["model_version"] == "v2-calibrated"
