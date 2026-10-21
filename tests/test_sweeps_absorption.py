"""Phase 3 tests: sweep detection and absorption scores against synthetic
sequences with known ground truth. See docs/09_tests_plan.md."""
import pytest

from src.orderbook.absorption import compute_absorption_scores
from src.orderbook.sweeps import SweepDetector
from src.orderbook.trade_flow import Trade


def test_sweep_detected_for_fast_same_side_burst():
    detector = SweepDetector(burst_window_ms=2000, min_trades=3)
    trades = [
        Trade(ts_ms=0, price=100.0, size=1.0, side="B"),
        Trade(ts_ms=200, price=100.1, size=1.0, side="B"),
        Trade(ts_ms=400, price=100.2, size=1.0, side="B"),
        Trade(ts_ms=600, price=100.3, size=1.0, side="B"),
    ]
    events = detector.detect(trades)
    assert len(events) == 1
    sweep = events[0]
    assert sweep.side == "B"
    assert sweep.n_trades == 4
    assert sweep.volume == pytest.approx(4.0)
    assert sweep.price_displacement_bps > 0  # price moved up during the buy sweep


def test_no_sweep_below_min_trades():
    detector = SweepDetector(burst_window_ms=2000, min_trades=5)
    trades = [
        Trade(ts_ms=0, price=100.0, size=1.0, side="B"),
        Trade(ts_ms=100, price=100.1, size=1.0, side="B"),
    ]
    assert detector.detect(trades) == []


def test_sweep_burst_broken_by_side_change():
    detector = SweepDetector(burst_window_ms=2000, min_trades=2)
    trades = [
        Trade(ts_ms=0, price=100.0, size=1.0, side="B"),
        Trade(ts_ms=100, price=100.1, size=1.0, side="B"),
        Trade(ts_ms=200, price=100.05, size=1.0, side="A"),  # breaks the buy burst
        Trade(ts_ms=300, price=100.0, size=1.0, side="A"),
    ]
    events = detector.detect(trades)
    assert len(events) == 2
    assert events[0].side == "B"
    assert events[1].side == "A"


def test_sweep_burst_broken_by_time_gap():
    detector = SweepDetector(burst_window_ms=500, min_trades=2)
    trades = [
        Trade(ts_ms=0, price=100.0, size=1.0, side="B"),
        Trade(ts_ms=100, price=100.1, size=1.0, side="B"),
        Trade(ts_ms=5000, price=100.2, size=1.0, side="B"),  # 4.9s gap -> new burst
        Trade(ts_ms=5100, price=100.3, size=1.0, side="B"),
    ]
    events = detector.detect(trades)
    assert len(events) == 2


def test_sweep_min_volume_filters_small_bursts():
    detector = SweepDetector(burst_window_ms=2000, min_trades=2, min_volume=10.0)
    trades = [
        Trade(ts_ms=0, price=100.0, size=1.0, side="B"),
        Trade(ts_ms=100, price=100.1, size=1.0, side="B"),
    ]
    assert detector.detect(trades) == []


def test_levels_crossed_counts_asks_within_burst_range_for_buy_sweep():
    detector = SweepDetector(burst_window_ms=2000, min_trades=2)
    trades = [
        Trade(ts_ms=0, price=100.0, size=1.0, side="B"),
        Trade(ts_ms=100, price=100.3, size=1.0, side="B"),
    ]
    asks = [(100.0, 1.0, 1), (100.1, 1.0, 1), (100.2, 1.0, 1), (100.3, 1.0, 1), (100.4, 1.0, 1)]
    bids = [(99.9, 1.0, 1)]
    events = detector.detect(trades, pre_sweep_book=(bids, asks))
    assert events[0].levels_crossed == 4  # 100.0, 100.1, 100.2, 100.3 fall within [100.0, 100.3]


def test_absorption_scores_high_volume_low_displacement_scores_high():
    result = compute_absorption_scores(buy_volume=100.0, sell_volume=0.0, price_start=100.0, price_end=100.001)
    assert result["buy_absorption_score"] > 0
    assert result["sell_absorption_score"] == 0.0
    assert result["price_displacement_bps"] < 1.0  # tiny move relative to the large volume absorbed


def test_absorption_scores_low_volume_high_displacement_scores_low():
    low_disp = compute_absorption_scores(buy_volume=10.0, sell_volume=0.0, price_start=100.0, price_end=100.001)
    high_disp = compute_absorption_scores(buy_volume=10.0, sell_volume=0.0, price_start=100.0, price_end=101.0)
    assert low_disp["buy_absorption_score"] > high_disp["buy_absorption_score"]


def test_absorption_scores_zero_price_start_does_not_crash():
    result = compute_absorption_scores(buy_volume=1.0, sell_volume=1.0, price_start=0.0, price_end=1.0)
    assert result["price_displacement_bps"] == 0.0
