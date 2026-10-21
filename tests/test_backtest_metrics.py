"""Phase 10 tests: backtest/metrics.py — pure aggregation over
hand-built trade dicts, no I/O, no dependency on the backtester itself
(that's covered separately in test_event_backtester.py).
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.backtest.metrics import (
    bootstrap_confidence_intervals,
    compute_metrics,
    monte_carlo_reshuffle,
    probability_of_ruin,
)

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def trade(net_pnl, *, gross_pnl=None, fees=1.0, slippage=0.5, opened_at=None, closed_at=None):
    opened_at = opened_at or BASE
    closed_at = closed_at or (opened_at + dt.timedelta(seconds=30))
    gross_pnl = gross_pnl if gross_pnl is not None else net_pnl + fees + slippage
    return {
        "net_pnl": net_pnl,
        "gross_pnl": gross_pnl,
        "fees": fees,
        "slippage": slippage,
        "opened_at": opened_at,
        "closed_at": closed_at,
    }


def test_compute_metrics_empty_trades_returns_zeros_not_a_crash():
    m = compute_metrics([])
    assert m["n_trades"] == 0
    assert m["net_pnl"] == 0.0
    assert m["win_rate"] is None
    assert m["sharpe"] is None
    assert m["equity_curve"] == [10_000.0]


def test_compute_metrics_basic_aggregate_sums():
    trades = [trade(10.0, fees=1.0, slippage=0.5), trade(-5.0, fees=1.0, slippage=0.5)]
    m = compute_metrics(trades, initial_equity=1000.0)
    assert m["n_trades"] == 2
    assert m["net_pnl"] == pytest.approx(5.0)
    assert m["total_fees"] == pytest.approx(2.0)
    assert m["total_slippage"] == pytest.approx(1.0)
    assert m["win_rate"] == pytest.approx(0.5)
    assert m["avg_win"] == pytest.approx(10.0)
    assert m["avg_loss"] == pytest.approx(-5.0)
    assert m["expectancy"] == pytest.approx(2.5)
    assert m["equity_curve"] == [1000.0, 1010.0, 1005.0]


def test_win_rate_excludes_exact_breakeven_trades_from_denominator():
    trades = [trade(10.0), trade(-10.0), trade(0.0, fees=0.0, slippage=0.0, gross_pnl=0.0)]
    m = compute_metrics(trades)
    # 1 win, 1 loss, 1 breakeven -> win_rate over decisive trades only (2), not all 3
    assert m["win_rate"] == pytest.approx(0.5)


def test_profit_factor_none_when_no_losing_trades():
    trades = [trade(10.0), trade(5.0)]
    m = compute_metrics(trades)
    assert m["profit_factor"] is None


def test_profit_factor_matches_hand_computed_ratio():
    trades = [trade(20.0), trade(-10.0)]
    m = compute_metrics(trades)
    assert m["profit_factor"] == pytest.approx(2.0)


def test_sharpe_and_sortino_none_with_fewer_than_two_trades():
    m = compute_metrics([trade(10.0)])
    assert m["sharpe"] is None
    assert m["sortino"] is None


def test_sharpe_none_when_all_trades_identical_zero_variance():
    trades = [trade(10.0), trade(10.0), trade(10.0)]
    m = compute_metrics(trades)
    assert m["sharpe"] is None  # std == 0, would otherwise divide by zero


def test_sortino_none_with_fewer_than_two_losing_trades():
    trades = [trade(10.0), trade(20.0), trade(-5.0)]
    m = compute_metrics(trades)
    assert m["sortino"] is None  # only one losing trade — no downside variance to speak of


def test_sortino_is_computed_with_at_least_two_losing_trades():
    trades = [trade(10.0), trade(-5.0), trade(-15.0)]
    m = compute_metrics(trades)
    assert m["sortino"] is not None


def test_max_drawdown_matches_hand_computed_path():
    # equity: 1000 -> 1100 (peak) -> 1050 (dd=50) -> 900 (dd=200, new max) -> 950
    trades = [trade(100.0), trade(-50.0), trade(-150.0), trade(50.0)]
    m = compute_metrics(trades, initial_equity=1000.0)
    assert m["max_drawdown"] == pytest.approx(200.0)
    assert m["max_drawdown_pct"] == pytest.approx(200.0 / 1100.0 * 100.0)


def test_avg_holding_time_matches_hand_computed_seconds():
    trades = [
        trade(1.0, opened_at=BASE, closed_at=BASE + dt.timedelta(seconds=10)),
        trade(1.0, opened_at=BASE, closed_at=BASE + dt.timedelta(seconds=30)),
    ]
    m = compute_metrics(trades)
    assert m["avg_holding_time_s"] == pytest.approx(20.0)


def test_sharpe_annualized_scales_by_sqrt_of_empirical_trade_frequency():
    trades = [
        trade(10.0 if i % 2 == 0 else -8.0, opened_at=BASE + dt.timedelta(seconds=i * 3600))
        for i in range(20)
    ]
    m = compute_metrics(trades)
    assert m["sharpe"] is not None
    assert m["sharpe_annualized"] is not None
    assert m["sharpe_annualized"] != m["sharpe"]


# -- bootstrap / monte carlo / probability of ruin --------------------------


def test_bootstrap_confidence_intervals_raises_on_empty_trades():
    with pytest.raises(ValueError):
        bootstrap_confidence_intervals([])


def test_bootstrap_confidence_intervals_shape_and_mean_near_actual_total():
    trades = [trade(10.0) for _ in range(50)] + [trade(-3.0) for _ in range(50)]
    result = bootstrap_confidence_intervals(trades, n_resamples=300, random_state=0)
    assert result["n_resamples"] == 300
    lo, hi = result["net_pnl_ci"]
    assert lo <= result["net_pnl_mean"] <= hi
    actual_total = sum(t["net_pnl"] for t in trades)
    assert result["net_pnl_mean"] == pytest.approx(actual_total, rel=0.2)


def test_bootstrap_is_deterministic_given_a_random_state():
    trades = [trade(10.0 if i % 3 else -4.0) for i in range(40)]
    r1 = bootstrap_confidence_intervals(trades, n_resamples=200, random_state=42)
    r2 = bootstrap_confidence_intervals(trades, n_resamples=200, random_state=42)
    assert r1["net_pnl_ci"] == r2["net_pnl_ci"]


def test_monte_carlo_reshuffle_raises_on_empty_trades():
    with pytest.raises(ValueError):
        monte_carlo_reshuffle([])


def test_monte_carlo_reshuffle_total_pnl_is_not_in_the_output_since_its_order_invariant():
    trades = [trade(10.0), trade(-30.0), trade(5.0)]
    result = monte_carlo_reshuffle(trades, n_shuffles=100, random_state=0)
    assert "net_pnl" not in result
    assert "total_net_pnl" not in result


def test_monte_carlo_reshuffle_percentile_of_actual_is_between_0_and_100():
    trades = [trade(10.0 if i % 2 == 0 else -12.0) for i in range(30)]
    result = monte_carlo_reshuffle(trades, n_shuffles=200, random_state=0)
    assert 0.0 <= result["percentile_of_actual"] <= 100.0


def test_monte_carlo_reshuffle_worst_case_ordering_has_the_largest_drawdown():
    # All losses first, then all wins: this ordering is provably the
    # worst-case drawdown for this exact multiset of outcomes, so its
    # actual drawdown should sit at or above the shuffled distribution's
    # mean far more often than not.
    trades = [trade(-10.0) for _ in range(10)] + [trade(10.0) for _ in range(10)]
    result = monte_carlo_reshuffle(trades, n_shuffles=300, random_state=0)
    assert result["actual_max_drawdown"] >= result["shuffled_max_drawdown_mean"]


def test_probability_of_ruin_raises_on_empty_trades():
    with pytest.raises(ValueError):
        probability_of_ruin([])


def test_probability_of_ruin_is_zero_when_every_trade_is_a_small_win():
    trades = [trade(1.0) for _ in range(50)]
    result = probability_of_ruin(trades, initial_equity=1000.0, ruin_threshold_pct=50, n_paths=200, random_state=0)
    assert result["probability_of_ruin"] == 0.0


def test_probability_of_ruin_is_high_when_losses_would_breach_threshold_immediately():
    trades = [trade(-600.0)] * 5 + [trade(1.0)] * 5
    result = probability_of_ruin(trades, initial_equity=1000.0, ruin_threshold_pct=50, n_paths=300, random_state=0)
    assert result["probability_of_ruin"] > 0.5
