"""Phase 11 tests: strategy/exit.py — 7 exit candidates checked in
config/strategy.yaml order, first-to-fire wins, each candidate degrades
to "skip" (never "trigger") when its required data is missing.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.strategy.exit import (
    CANDIDATE_ORDER,
    EXIT_FIXED_SL,
    EXIT_FIXED_TP,
    EXIT_LIQUIDITY_REVERSAL,
    EXIT_ORDER_FLOW_REVERSAL,
    EXIT_PROBABILITY_REVERSAL,
    EXIT_TIME_STOP,
    EXIT_VOLATILITY_SL,
    should_exit,
)

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def long_position(**overrides):
    position = {
        "side": "buy",
        "entry_price": 100.0,
        "opened_at": BASE,
        "entry_obi": 0.5,
        "entry_liquidity_imbalance": 0.5,
    }
    position.update(overrides)
    return position


def short_position(**overrides):
    position = {
        "side": "sell",
        "entry_price": 100.0,
        "opened_at": BASE,
        "entry_obi": -0.5,
        "entry_liquidity_imbalance": -0.5,
    }
    position.update(overrides)
    return position


def features(**overrides):
    row = {"price": 100.0, "timestamp": BASE}
    row.update(overrides)
    return row


def test_position_within_all_bounds_does_not_exit():
    assert should_exit(long_position(), features(price=100.05)) is None


def test_fixed_tp_fires_for_long_position_in_profit():
    reason = should_exit(long_position(), features(price=100.20), take_profit_bps=15.0)
    assert reason == EXIT_FIXED_TP


def test_fixed_tp_fires_for_short_position_in_profit():
    reason = should_exit(short_position(), features(price=99.80), take_profit_bps=15.0)
    assert reason == EXIT_FIXED_TP


def test_fixed_sl_fires_for_long_position_at_a_loss():
    reason = should_exit(long_position(), features(price=99.80), take_profit_bps=100.0, stop_loss_bps=15.0)
    assert reason == EXIT_FIXED_SL


def test_fixed_sl_fires_for_short_position_at_a_loss():
    reason = should_exit(short_position(), features(price=100.20), take_profit_bps=100.0, stop_loss_bps=15.0)
    assert reason == EXIT_FIXED_SL


def test_missing_price_never_fires_price_based_exits():
    reason = should_exit(long_position(), features(price=None), take_profit_bps=0.0001, stop_loss_bps=0.0001)
    assert reason is None


def test_volatility_sl_fires_when_loss_exceeds_volatility_scaled_stop():
    # unrealized -30bps; volatility=0.002 (20bps) * multiplier 1.0 = 20bps stop.
    # fixed_sl set loose (100bps) so it doesn't preempt this check.
    reason = should_exit(
        long_position(),
        features(price=99.70, volatility=0.002),
        stop_loss_bps=100.0,
        volatility_stop_multiplier=1.0,
    )
    assert reason == EXIT_VOLATILITY_SL


def test_volatility_sl_skipped_when_volatility_missing():
    reason = should_exit(long_position(), features(price=99.70), stop_loss_bps=100.0, volatility_stop_multiplier=1.0)
    assert reason is None


def test_probability_reversal_fires_when_model_flips_against_position():
    reason = should_exit(
        long_position(),
        features(
            price=100.05,  # not enough to hit TP/SL
            probability_up=0.10,
            probability_down=0.80,
            probability_neutral=0.10,
        ),
        take_profit_bps=100.0,
        stop_loss_bps=100.0,
        reversal_probability_min=0.55,
    )
    assert reason == EXIT_PROBABILITY_REVERSAL


def test_probability_reversal_skipped_when_probabilities_absent():
    reason = should_exit(long_position(), features(price=100.05), take_profit_bps=100.0, stop_loss_bps=100.0)
    assert reason is None


def test_probability_reversal_does_not_fire_when_model_agrees_with_position():
    reason = should_exit(
        long_position(),
        features(price=100.05, probability_up=0.80, probability_down=0.10, probability_neutral=0.10),
        take_profit_bps=100.0,
        stop_loss_bps=100.0,
    )
    assert reason is None


def test_order_flow_reversal_fires_when_obi_flips_against_position():
    reason = should_exit(
        long_position(entry_obi=0.5),
        features(price=100.05, obi_10=-0.6),
        take_profit_bps=100.0,
        stop_loss_bps=100.0,
        order_flow_reversal_min_abs=0.3,
    )
    assert reason == EXIT_ORDER_FLOW_REVERSAL


def test_order_flow_reversal_skipped_when_entry_snapshot_missing():
    position = long_position()
    del position["entry_obi"]
    reason = should_exit(position, features(price=100.05, obi_10=-0.6), take_profit_bps=100.0, stop_loss_bps=100.0)
    assert reason is None


def test_order_flow_reversal_requires_minimum_magnitude():
    reason = should_exit(
        long_position(entry_obi=0.5),
        features(price=100.05, obi_10=-0.05),
        take_profit_bps=100.0,
        stop_loss_bps=100.0,
        order_flow_reversal_min_abs=0.3,
    )
    assert reason is None


def test_liquidity_reversal_fires_when_liquidity_imbalance_flips():
    reason = should_exit(
        long_position(entry_liquidity_imbalance=0.5),
        features(price=100.05, liquidity_imbalance=-0.6),
        take_profit_bps=100.0,
        stop_loss_bps=100.0,
        liquidity_reversal_min_abs=0.3,
    )
    assert reason == EXIT_LIQUIDITY_REVERSAL


def test_liquidity_reversal_skipped_when_entry_snapshot_missing():
    position = long_position()
    del position["entry_liquidity_imbalance"]
    reason = should_exit(
        position, features(price=100.05, liquidity_imbalance=-0.6), take_profit_bps=100.0, stop_loss_bps=100.0
    )
    assert reason is None


def test_time_stop_fires_after_horizon_elapses():
    reason = should_exit(
        long_position(opened_at=BASE),
        features(price=100.05, timestamp=BASE + dt.timedelta(seconds=301)),
        take_profit_bps=100.0,
        stop_loss_bps=100.0,
        time_stop_s=300,
    )
    assert reason == EXIT_TIME_STOP


def test_time_stop_does_not_fire_before_horizon():
    reason = should_exit(
        long_position(opened_at=BASE),
        features(price=100.05, timestamp=BASE + dt.timedelta(seconds=100)),
        take_profit_bps=100.0,
        stop_loss_bps=100.0,
        time_stop_s=300,
    )
    assert reason is None


def test_time_stop_skipped_when_opened_at_missing():
    position = long_position()
    del position["opened_at"]
    reason = should_exit(
        position,
        features(price=100.05, timestamp=BASE + dt.timedelta(seconds=1000)),
        take_profit_bps=100.0,
        stop_loss_bps=100.0,
        time_stop_s=300,
    )
    assert reason is None


def test_candidates_checked_in_config_order_first_match_wins():
    # Rig a position/features pair where BOTH fixed_tp and time_stop would
    # independently fire -- fixed_tp is earlier in CANDIDATE_ORDER and
    # must win.
    reason = should_exit(
        long_position(opened_at=BASE),
        features(price=100.20, timestamp=BASE + dt.timedelta(seconds=1000)),
        take_profit_bps=15.0,
        time_stop_s=300,
    )
    assert reason == EXIT_FIXED_TP
    assert CANDIDATE_ORDER.index(EXIT_FIXED_TP) < CANDIDATE_ORDER.index(EXIT_TIME_STOP)


def test_no_candidate_fires_returns_none():
    assert should_exit(long_position(), features(price=100.02)) is None
