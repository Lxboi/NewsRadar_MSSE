"""Phase 12 tests: strategy/position_sizing.py — pure risk-based sizing,
scaled down (never up) by confidence/volatility/expected-value, clamped
to an equity ceiling.

Most tests below pass an explicit, very high `max_position_size_pct_equity`
so the multiplier math under test isn't incidentally clipped by the
default 5% equity ceiling (`base_risk_formula`'s own raw size, with the
default 0.1% risk-per-trade and a tight 10bps stop, comfortably exceeds
5% of equity — the ceiling binding is its own dedicated, separately
tested behavior below, not something the multiplier tests want mixed in).
"""
from __future__ import annotations

import pytest

from src.strategy.position_sizing import (
    DEFAULT_FULL_SIZE_EXPECTED_VALUE_BPS,
    DEFAULT_REFERENCE_VOLATILITY,
    size_position,
)

UNCAPPED = {"max_position_size_pct_equity": 1_000.0}


def test_zero_or_missing_account_equity_returns_zero():
    assert size_position({}) == 0.0
    assert size_position({"account_equity": 0.0, "stop_distance_bps": 10}) == 0.0
    assert size_position({"account_equity": -100.0, "stop_distance_bps": 10}) == 0.0


def test_missing_or_zero_stop_distance_returns_zero():
    assert size_position({"account_equity": 10_000.0}) == 0.0
    assert size_position({"account_equity": 10_000.0, "stop_distance_bps": 0}) == 0.0


def test_base_risk_formula_with_no_other_inputs():
    # risk_amount = 10_000 * 0.1/100 = 10; raw_size = 10 / (10/10_000) = 10_000.
    # No confidence/volatility/EV given -> all multipliers neutral (1.0).
    size = size_position({"account_equity": 10_000.0, "risk_per_trade_pct": 0.1, "stop_distance_bps": 10, **UNCAPPED})
    assert size == pytest.approx(10_000.0)


def test_custom_risk_per_trade_pct_scales_linearly():
    base = size_position({"account_equity": 10_000.0, "risk_per_trade_pct": 0.1, "stop_distance_bps": 10, **UNCAPPED})
    doubled = size_position({"account_equity": 10_000.0, "risk_per_trade_pct": 0.2, "stop_distance_bps": 10, **UNCAPPED})
    assert doubled == pytest.approx(base * 2)


def test_wider_stop_distance_reduces_size():
    tight = size_position({"account_equity": 10_000.0, "stop_distance_bps": 10, **UNCAPPED})
    wide = size_position({"account_equity": 10_000.0, "stop_distance_bps": 100, **UNCAPPED})
    assert wide < tight


def test_low_confidence_shrinks_size_proportionally():
    full = size_position({"account_equity": 10_000.0, "stop_distance_bps": 10, "signal_confidence": 1.0, **UNCAPPED})
    half = size_position({"account_equity": 10_000.0, "stop_distance_bps": 10, "signal_confidence": 0.5, **UNCAPPED})
    assert half == pytest.approx(full * 0.5)


def test_confidence_clamped_to_unit_interval():
    over = size_position({"account_equity": 10_000.0, "stop_distance_bps": 10, "signal_confidence": 1.5, **UNCAPPED})
    full = size_position({"account_equity": 10_000.0, "stop_distance_bps": 10, "signal_confidence": 1.0, **UNCAPPED})
    assert over == pytest.approx(full)


def test_missing_confidence_is_neutral_not_zero():
    with_conf = size_position({"account_equity": 10_000.0, "stop_distance_bps": 10, "signal_confidence": 1.0, **UNCAPPED})
    without_conf = size_position({"account_equity": 10_000.0, "stop_distance_bps": 10, **UNCAPPED})
    assert without_conf == pytest.approx(with_conf)


def test_high_volatility_shrinks_size():
    calm = size_position(
        {"account_equity": 10_000.0, "stop_distance_bps": 10, "volatility": DEFAULT_REFERENCE_VOLATILITY, **UNCAPPED}
    )
    volatile = size_position(
        {"account_equity": 10_000.0, "stop_distance_bps": 10, "volatility": DEFAULT_REFERENCE_VOLATILITY * 4, **UNCAPPED}
    )
    assert volatile == pytest.approx(calm * 0.25)


def test_volatility_at_or_below_reference_gets_no_haircut():
    baseline = size_position({"account_equity": 10_000.0, "stop_distance_bps": 10, **UNCAPPED})
    calm = size_position(
        {"account_equity": 10_000.0, "stop_distance_bps": 10, "volatility": DEFAULT_REFERENCE_VOLATILITY / 2, **UNCAPPED}
    )
    assert calm == pytest.approx(baseline)


def test_negative_expected_value_collapses_size_to_zero():
    size = size_position({"account_equity": 10_000.0, "stop_distance_bps": 10, "expected_value_bps": -1.0, **UNCAPPED})
    assert size == 0.0


def test_expected_value_below_full_size_reference_is_fractional():
    half_ev = DEFAULT_FULL_SIZE_EXPECTED_VALUE_BPS / 2
    baseline = size_position({"account_equity": 10_000.0, "stop_distance_bps": 10, **UNCAPPED})
    partial = size_position({"account_equity": 10_000.0, "stop_distance_bps": 10, "expected_value_bps": half_ev, **UNCAPPED})
    assert partial == pytest.approx(baseline * 0.5)


def test_expected_value_at_or_above_reference_gets_no_haircut():
    baseline = size_position({"account_equity": 10_000.0, "stop_distance_bps": 10, **UNCAPPED})
    strong_ev = size_position(
        {
            "account_equity": 10_000.0,
            "stop_distance_bps": 10,
            "expected_value_bps": DEFAULT_FULL_SIZE_EXPECTED_VALUE_BPS * 2,
            **UNCAPPED,
        }
    )
    assert strong_ev == pytest.approx(baseline)


def test_missing_expected_value_is_neutral():
    baseline = size_position(
        {
            "account_equity": 10_000.0,
            "stop_distance_bps": 10,
            "expected_value_bps": DEFAULT_FULL_SIZE_EXPECTED_VALUE_BPS,
            **UNCAPPED,
        }
    )
    without = size_position({"account_equity": 10_000.0, "stop_distance_bps": 10, **UNCAPPED})
    assert without == pytest.approx(baseline)


def test_result_clamped_to_max_position_size_pct_equity():
    size = size_position(
        {
            "account_equity": 10_000.0,
            "risk_per_trade_pct": 50.0,  # deliberately huge risk to force the ceiling to bind
            "stop_distance_bps": 1.0,
            "max_position_size_pct_equity": 5.0,
        }
    )
    assert size == pytest.approx(10_000.0 * 0.05)


def test_custom_max_position_size_pct_equity_overrides_default():
    size = size_position(
        {
            "account_equity": 10_000.0,
            "risk_per_trade_pct": 50.0,
            "stop_distance_bps": 1.0,
            "max_position_size_pct_equity": 2.0,
        }
    )
    assert size == pytest.approx(10_000.0 * 0.02)


def test_default_ceiling_binds_on_the_plain_default_inputs():
    # Documents the interaction the other tests deliberately avoid: with
    # no max_position_size_pct_equity override, the default 5% equity
    # ceiling binds well before the raw risk-based formula would, on
    # ordinary risk_per_trade_pct/stop_distance_bps defaults.
    size = size_position({"account_equity": 10_000.0, "risk_per_trade_pct": 0.1, "stop_distance_bps": 10})
    assert size == pytest.approx(10_000.0 * 0.05)


def test_result_never_negative():
    size = size_position({"account_equity": 10_000.0, "stop_distance_bps": 10, "signal_confidence": 0.0, **UNCAPPED})
    assert size >= 0.0


def test_combined_multipliers_compose_multiplicatively():
    size = size_position(
        {
            "account_equity": 10_000.0,
            "risk_per_trade_pct": 0.1,
            "stop_distance_bps": 10,
            "signal_confidence": 0.5,
            "volatility": DEFAULT_REFERENCE_VOLATILITY * 2,  # 0.5x
            "expected_value_bps": DEFAULT_FULL_SIZE_EXPECTED_VALUE_BPS / 2,  # 0.5x
            **UNCAPPED,
        }
    )
    baseline = size_position({"account_equity": 10_000.0, "risk_per_trade_pct": 0.1, "stop_distance_bps": 10, **UNCAPPED})
    assert size == pytest.approx(baseline * 0.5 * 0.5 * 0.5)
