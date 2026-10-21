"""Phase 11 tests: strategy/entry.py — live counterpart to
backtest/event_backtester.py: EventBacktester._passes_entry_filter(),
plus the freshness check the backtester structurally can't perform.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.strategy.entry import should_enter

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def make_signal(**overrides):
    signal = {
        "probability_up": 0.70,
        "probability_down": 0.15,
        "probability_neutral": 0.15,
        "expected_value": 10.0,
        "market_regime": "NORMAL_VOL",
        "timestamp": BASE,
        "features_snapshot": {"spread": 2.0},
    }
    signal.update(overrides)
    return signal


def test_passes_all_checks_by_default():
    assert should_enter(make_signal(), {}) is True


def test_neutral_signal_never_enters_even_with_risk_state_and_ev_favorable():
    signal = make_signal(probability_up=0.30, probability_down=0.30, probability_neutral=0.40)
    assert should_enter(signal, {}) is False


def test_blocked_risk_state_prevents_entry():
    assert should_enter(make_signal(), {"blocked": True, "reason": "daily loss limit"}) is False


def test_missing_blocked_key_treated_as_not_blocked():
    assert should_enter(make_signal(), {}) is True


def test_probability_below_minimum_blocks_entry():
    signal = make_signal(probability_up=0.55)
    assert should_enter(signal, {}, probability_min=0.62) is False


def test_probability_at_minimum_passes():
    signal = make_signal(probability_up=0.62, probability_down=0.10, probability_neutral=0.28)
    assert should_enter(signal, {}, probability_min=0.62) is True


def test_negative_expected_value_blocks_entry():
    signal = make_signal(expected_value=-1.0)
    assert should_enter(signal, {}, min_expected_value=0.0) is False


def test_missing_expected_value_does_not_block():
    signal = make_signal(expected_value=None)
    assert should_enter(signal, {}, min_expected_value=0.0) is True


def test_spread_over_ceiling_blocks_entry():
    signal = make_signal(features_snapshot={"spread": 10.0})
    assert should_enter(signal, {}, max_spread_bps=5) is False


def test_missing_spread_does_not_block():
    signal = make_signal(features_snapshot={})
    assert should_enter(signal, {}, max_spread_bps=5) is True


def test_disallowed_regime_blocks_entry():
    signal = make_signal(market_regime="HIGH_VOL")
    assert should_enter(signal, {}, allowed_regimes=("LOW_VOL", "NORMAL_VOL")) is False


def test_missing_regime_does_not_block():
    signal = make_signal(market_regime=None)
    assert should_enter(signal, {}, allowed_regimes=("LOW_VOL", "NORMAL_VOL")) is True


def test_stale_signal_blocks_entry_when_now_given():
    signal = make_signal(timestamp=BASE)
    now = BASE + dt.timedelta(seconds=10)
    assert should_enter(signal, {}, max_data_age_s=3, now=now) is False


def test_fresh_signal_passes_when_now_given():
    signal = make_signal(timestamp=BASE)
    now = BASE + dt.timedelta(seconds=1)
    assert should_enter(signal, {}, max_data_age_s=3, now=now) is True


def test_freshness_check_skipped_entirely_when_now_not_given():
    # Backtest/offline replay has no live wall clock -- staleness is
    # unanswerable, not automatically a violation.
    signal = make_signal(timestamp=BASE)
    assert should_enter(signal, {}, max_data_age_s=3, now=None) is True


def test_none_thresholds_disable_their_respective_checks():
    signal = make_signal(probability_up=0.01, probability_down=0.01, probability_neutral=0.0)
    # probability_up + probability_down + probability_neutral must reflect
    # a directional argmax; use a low-but-nonzero directional probability
    # with probability_min disabled entirely.
    signal = make_signal(probability_up=0.51, probability_down=0.30, probability_neutral=0.19)
    assert should_enter(signal, {}, probability_min=None) is True


def test_short_direction_also_evaluated_correctly():
    signal = make_signal(probability_up=0.10, probability_down=0.75, probability_neutral=0.15)
    assert should_enter(signal, {}) is True
