"""Phase 3 tests: trade flow windows against synthetic trade sequences
with known ground truth. See docs/09_tests_plan.md."""
import pytest

from src.orderbook.trade_flow import Trade, TradeFlowTracker


def make_tracker_with_trades(trades):
    tracker = TradeFlowTracker(windows_s=(1, 5, 10))
    tracker.add_trades(trades)
    return tracker


def test_window_stats_basic_buy_sell_split():
    trades = [
        Trade(ts_ms=1000, price=100.0, size=1.0, side="B"),
        Trade(ts_ms=1200, price=100.1, size=2.0, side="B"),
        Trade(ts_ms=1400, price=100.0, size=1.5, side="A"),
    ]
    tracker = make_tracker_with_trades(trades)
    stats = tracker.window_stats(5, now_ms=1400)

    assert stats["buy_volume"] == pytest.approx(3.0)
    assert stats["sell_volume"] == pytest.approx(1.5)
    assert stats["signed_volume"] == pytest.approx(1.5)
    assert stats["trade_imbalance"] == pytest.approx(1.5 / 4.5)
    assert stats["n_trades"] == 3
    assert stats["avg_trade_size"] == pytest.approx(4.5 / 3)


def test_window_stats_excludes_trades_outside_window():
    trades = [
        Trade(ts_ms=0, price=100.0, size=100.0, side="B"),      # far in the past
        Trade(ts_ms=9500, price=100.0, size=1.0, side="B"),     # within last 1s of now=10000
    ]
    tracker = make_tracker_with_trades(trades)
    stats = tracker.window_stats(1, now_ms=10000)
    assert stats["buy_volume"] == pytest.approx(1.0)  # the far-past trade is excluded
    assert stats["n_trades"] == 1


def test_window_stats_empty_window_returns_none_imbalance():
    tracker = TradeFlowTracker(windows_s=(1, 5))
    stats = tracker.window_stats(1, now_ms=1000)
    assert stats["trade_imbalance"] is None
    assert stats["avg_trade_size"] is None
    assert stats["n_trades"] == 0


def test_trade_pruning_drops_trades_older_than_max_window():
    tracker = TradeFlowTracker(windows_s=(1, 5))
    tracker.add_trade(Trade(ts_ms=0, price=1.0, size=1.0, side="B"))
    tracker.add_trade(Trade(ts_ms=10_000, price=1.0, size=1.0, side="B"))  # 10s later, beyond max window (5s)
    assert len(tracker.trades) == 1  # the first trade should have been pruned


def test_large_trade_freq_none_without_enough_samples():
    tracker = TradeFlowTracker(windows_s=(1,), large_trade_min_samples=20)
    tracker.add_trade(Trade(ts_ms=0, price=1.0, size=100.0, side="B"))
    stats = tracker.window_stats(1, now_ms=0)
    assert stats["large_trade_freq"] is None


def test_large_trade_freq_detects_outsized_trade():
    tracker = TradeFlowTracker(windows_s=(10,), large_trade_multiplier=5.0, large_trade_min_samples=5)
    # 20 small trades (size=1) establish a baseline avg size of 1.0
    for i in range(20):
        tracker.add_trade(Trade(ts_ms=i, price=100.0, size=1.0, side="B"))
    # one large trade at size=10 (>= 5x avg) should count
    tracker.add_trade(Trade(ts_ms=20, price=100.0, size=10.0, side="B"))
    stats = tracker.window_stats(10, now_ms=20)
    assert stats["large_trade_freq"] is not None
    assert stats["large_trade_freq"] > 0


def test_volume_acceleration_positive_when_recent_burst():
    tracker = TradeFlowTracker(windows_s=(1, 10))
    # Baseline: 1 unit/sec over the last 10s (10 trades of size 1, spread out)
    for i in range(10):
        tracker.add_trade(Trade(ts_ms=i * 1000, price=100.0, size=1.0, side="B"))
    # Burst in the last second: 5 more units
    tracker.add_trade(Trade(ts_ms=9500, price=100.0, size=5.0, side="B"))

    accel = tracker.volume_acceleration(short_s=1, long_s=10, now_ms=10_000)
    assert accel > 0


def test_volume_acceleration_rejects_short_gte_long():
    tracker = TradeFlowTracker(windows_s=(1, 10))
    with pytest.raises(ValueError):
        tracker.volume_acceleration(short_s=10, long_s=1)


def test_buy_sell_ratio_none_when_no_sell_volume():
    tracker = TradeFlowTracker(windows_s=(5,))
    tracker.add_trade(Trade(ts_ms=0, price=100.0, size=1.0, side="B"))
    stats = tracker.window_stats(5, now_ms=0)
    assert stats["buy_sell_ratio"] is None
