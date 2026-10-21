"""Phase 8 tests: models/baseline_model.py — the section-31 non-ML
baselines. Pure computation, no I/O, no network. See docs/09_tests_plan.md.
"""
import pytest

from src.models.baseline_model import BASELINE_KINDS, baseline_signal


def test_all_seven_kinds_are_declared():
    assert set(BASELINE_KINDS) == {
        "momentum", "obi", "trade_imbalance", "news",
        "volatility_regime", "order_flow_momentum", "news_order_flow",
    }


def test_momentum_reads_the_configured_horizon():
    assert baseline_signal({"returns_30s": 0.01}, "momentum") == pytest.approx(0.01)
    assert baseline_signal({"returns_5s": 0.02}, "momentum", momentum_horizon_s=5) == pytest.approx(0.02)


def test_momentum_missing_value_is_zero_not_a_crash():
    assert baseline_signal({}, "momentum") == 0.0


def test_obi_reads_the_configured_level():
    assert baseline_signal({"obi_10": 0.3}, "obi") == pytest.approx(0.3)
    assert baseline_signal({"obi_5": -0.1}, "obi", obi_level=5) == pytest.approx(-0.1)


def test_obi_missing_value_is_zero():
    assert baseline_signal({}, "obi") == 0.0


def test_trade_imbalance_passthrough():
    assert baseline_signal({"trade_imbalance": -0.4}, "trade_imbalance") == pytest.approx(-0.4)
    assert baseline_signal({}, "trade_imbalance") == 0.0


def test_news_passthrough():
    assert baseline_signal({"news_score": 0.2}, "news") == pytest.approx(0.2)
    assert baseline_signal({}, "news") == 0.0


def test_volatility_regime_passes_through_momentum_in_low_and_normal_vol():
    for regime in ("LOW_VOL", "NORMAL_VOL"):
        row = {"returns_30s": 0.02, "market_regime": regime}
        assert baseline_signal(row, "volatility_regime") == pytest.approx(0.02)


def test_volatility_regime_flips_momentum_sign_in_high_and_extreme_vol():
    for regime in ("HIGH_VOL", "EXTREME_VOL"):
        row = {"returns_30s": 0.02, "market_regime": regime}
        assert baseline_signal(row, "volatility_regime") == pytest.approx(-0.02)


def test_volatility_regime_missing_regime_defaults_to_trend_following():
    row = {"returns_30s": 0.02}  # no market_regime key at all
    assert baseline_signal(row, "volatility_regime") == pytest.approx(0.02)


def test_order_flow_momentum_is_unweighted_sum_of_order_flow_average_and_momentum():
    row = {"obi_10": 0.2, "trade_imbalance": 0.4, "returns_30s": 0.1}
    order_flow = (0.2 + 0.4) / 2
    assert baseline_signal(row, "order_flow_momentum") == pytest.approx(order_flow + 0.1)


def test_news_order_flow_is_unweighted_sum_of_order_flow_average_and_news():
    row = {"obi_10": 0.2, "trade_imbalance": 0.4, "news_score": -0.5}
    order_flow = (0.2 + 0.4) / 2
    assert baseline_signal(row, "news_order_flow") == pytest.approx(order_flow - 0.5)


def test_combination_baselines_tolerate_fully_missing_row():
    assert baseline_signal({}, "order_flow_momentum") == 0.0
    assert baseline_signal({}, "news_order_flow") == 0.0


def test_unknown_kind_raises_value_error():
    with pytest.raises(ValueError):
        baseline_signal({"returns_30s": 0.01}, "not_a_real_kind")
