"""Phase 13 tests: monitoring/performance.py — PerformanceTracker windows
accumulated closed-trade dicts through backtest/metrics.py's
compute_metrics() (Phase 10), rather than a second stats implementation.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.monitoring.performance import (
    PERIOD_ALL_TIME,
    PERIOD_DAILY,
    PERIOD_HOURLY,
    PerformanceTracker,
    to_performance_snapshot_row,
)

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def make_trade(**overrides):
    trade = {
        "asset": "BTC", "net_pnl": 10.0, "gross_pnl": 12.0, "fees": 1.0, "slippage": 1.0,
        "exit_reason": "fixed_tp", "opened_at": BASE, "closed_at": BASE,
    }
    trade.update(overrides)
    return trade


def test_empty_tracker_snapshot_has_zero_trades():
    tracker = PerformanceTracker()
    snapshot = tracker.all_time_snapshot()
    assert snapshot["trades"] == 0
    assert snapshot["best_asset"] is None
    assert snapshot["best_setup"] is None
    assert snapshot["worst_setup"] is None


def test_all_time_snapshot_includes_every_recorded_trade():
    tracker = PerformanceTracker()
    tracker.record_trade(make_trade(closed_at=BASE))
    tracker.record_trade(make_trade(closed_at=BASE + dt.timedelta(days=10)))
    snapshot = tracker.all_time_snapshot()
    assert snapshot["trades"] == 2
    assert snapshot["period"] == PERIOD_ALL_TIME


def test_hourly_snapshot_excludes_trades_outside_the_window():
    tracker = PerformanceTracker()
    tracker.record_trade(make_trade(closed_at=BASE))                              # in window
    tracker.record_trade(make_trade(closed_at=BASE - dt.timedelta(hours=2)))       # too old
    now = BASE + dt.timedelta(minutes=30)
    snapshot = tracker.hourly_snapshot(now=now)
    assert snapshot["period"] == PERIOD_HOURLY
    assert snapshot["trades"] == 1


def test_daily_snapshot_excludes_trades_outside_the_window():
    tracker = PerformanceTracker()
    tracker.record_trade(make_trade(closed_at=BASE))
    tracker.record_trade(make_trade(closed_at=BASE - dt.timedelta(days=2)))
    now = BASE + dt.timedelta(hours=12)
    snapshot = tracker.daily_snapshot(now=now)
    assert snapshot["period"] == PERIOD_DAILY
    assert snapshot["trades"] == 1


def test_a_trade_closing_after_now_is_excluded_not_just_ones_before_the_window():
    # Regression: an upper-bound-less filter would have let a
    # future-dated trade leak into an earlier snapshot.
    tracker = PerformanceTracker()
    tracker.record_trade(make_trade(closed_at=BASE))
    tracker.record_trade(make_trade(closed_at=BASE + dt.timedelta(hours=5)))
    now = BASE + dt.timedelta(minutes=10)
    snapshot = tracker.hourly_snapshot(now=now)
    assert snapshot["trades"] == 1


def test_win_rate_and_pnl_match_compute_metrics_directly():
    from src.backtest.metrics import compute_metrics

    trades = [
        make_trade(net_pnl=10.0, gross_pnl=12.0, fees=1.0, slippage=1.0, closed_at=BASE),
        make_trade(net_pnl=-5.0, gross_pnl=-4.0, fees=0.5, slippage=0.5, closed_at=BASE),
    ]
    tracker = PerformanceTracker()
    for t in trades:
        tracker.record_trade(t)
    snapshot = tracker.all_time_snapshot()
    expected = compute_metrics(trades)
    assert snapshot["win_rate"] == pytest.approx(expected["win_rate"])
    assert snapshot["net_pnl"] == pytest.approx(expected["net_pnl"])
    assert snapshot["gross_pnl"] == pytest.approx(expected["gross_pnl"])
    assert snapshot["fees"] == pytest.approx(expected["total_fees"])
    assert snapshot["slippage"] == pytest.approx(expected["total_slippage"])
    assert snapshot["max_drawdown"] == pytest.approx(expected["max_drawdown"])


def test_best_asset_is_the_asset_with_highest_aggregate_net_pnl():
    tracker = PerformanceTracker()
    tracker.record_trade(make_trade(asset="BTC", net_pnl=100.0, closed_at=BASE))
    tracker.record_trade(make_trade(asset="ETH", net_pnl=5.0, closed_at=BASE))
    snapshot = tracker.all_time_snapshot()
    assert snapshot["best_asset"] == "BTC"


def test_best_and_worst_setup_are_exit_reasons_by_aggregate_net_pnl():
    tracker = PerformanceTracker()
    tracker.record_trade(make_trade(exit_reason="fixed_tp", net_pnl=50.0, closed_at=BASE))
    tracker.record_trade(make_trade(exit_reason="fixed_sl", net_pnl=-30.0, closed_at=BASE))
    tracker.record_trade(make_trade(exit_reason="time_stop", net_pnl=1.0, closed_at=BASE))
    snapshot = tracker.all_time_snapshot()
    assert snapshot["best_setup"] == "fixed_tp"
    assert snapshot["worst_setup"] == "fixed_sl"


def test_trades_missing_exit_reason_or_asset_are_excluded_from_that_ranking_only():
    tracker = PerformanceTracker()
    tracker.record_trade(make_trade(exit_reason=None, net_pnl=1000.0, closed_at=BASE))
    tracker.record_trade(make_trade(exit_reason="fixed_tp", net_pnl=1.0, closed_at=BASE))
    snapshot = tracker.all_time_snapshot()
    assert snapshot["trades"] == 2  # both trades still counted in aggregate stats
    assert snapshot["best_setup"] == "fixed_tp"  # the None-exit-reason trade can't win a setup ranking


def test_to_performance_snapshot_row_matches_the_db_column_set():
    tracker = PerformanceTracker()
    tracker.record_trade(make_trade(closed_at=BASE))
    snapshot = tracker.all_time_snapshot()
    row = to_performance_snapshot_row(snapshot, timestamp=BASE)
    expected_keys = {
        "period", "trades", "win_rate", "gross_pnl", "fees", "slippage",
        "net_pnl", "max_drawdown", "best_asset", "best_setup", "worst_setup", "timestamp",
    }
    assert set(row.keys()) == expected_keys
    assert row["timestamp"] == BASE


def test_to_performance_snapshot_row_omits_timestamp_when_not_given():
    tracker = PerformanceTracker()
    tracker.record_trade(make_trade(closed_at=BASE))
    snapshot = tracker.all_time_snapshot()
    row = to_performance_snapshot_row(snapshot)
    assert "timestamp" not in row


def test_trades_since_is_reusable_directly_for_a_custom_window():
    tracker = PerformanceTracker()
    tracker.record_trade(make_trade(closed_at=BASE))
    tracker.record_trade(make_trade(closed_at=BASE + dt.timedelta(seconds=30)))
    result = tracker.trades_since(now=BASE + dt.timedelta(seconds=60), window_s=45)
    assert len(result) == 1


def test_record_trade_accumulates_across_multiple_calls():
    tracker = PerformanceTracker()
    for i in range(5):
        tracker.record_trade(make_trade(closed_at=BASE + dt.timedelta(minutes=i)))
    assert tracker.all_time_snapshot()["trades"] == 5
