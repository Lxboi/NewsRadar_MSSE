"""Phase 3 tests: liquidity wall lifecycle, depth, and transient_liquidity_score
against a hand-built snapshot sequence. See docs/09_tests_plan.md."""
import pytest

from src.orderbook.liquidity import LiquidityTracker

NORMAL_LEVEL_SIZE = 1.0


def normal_book(n=10):
    bids = [(100.0 - i * 0.1, NORMAL_LEVEL_SIZE, 1) for i in range(n)]
    asks = [(100.1 + i * 0.1, NORMAL_LEVEL_SIZE, 1) for i in range(n)]
    return bids, asks


def test_no_wall_detected_on_uniform_book():
    tracker = LiquidityTracker(wall_size_multiplier=3.0)
    bids, asks = normal_book()
    result = tracker.update(bids, asks, ts_ms=0, mid=100.05)
    assert result["n_active_walls"] == 0
    assert result["transient_liquidity_score"] == 0.0


def test_wall_detected_when_level_far_exceeds_average():
    tracker = LiquidityTracker(wall_size_multiplier=3.0)
    bids, asks = normal_book()
    # Inject a wall at the best bid: 20x the normal size.
    bids[0] = (bids[0][0], NORMAL_LEVEL_SIZE * 20, 1)
    result = tracker.update(bids, asks, ts_ms=0, mid=100.05)
    assert result["n_active_walls"] == 1


def test_wall_persistence_grows_while_wall_stays():
    tracker = LiquidityTracker(wall_size_multiplier=3.0)
    bids, asks = normal_book()
    bids[0] = (bids[0][0], NORMAL_LEVEL_SIZE * 20, 1)

    tracker.update(bids, asks, ts_ms=0, mid=100.05)
    result = tracker.update(bids, asks, ts_ms=5000, mid=100.05)  # same wall, 5s later

    assert result["n_active_walls"] == 1
    assert result["wall_persistence"] == pytest.approx(5.0)


def test_wall_cancellation_when_level_disappears():
    tracker = LiquidityTracker(wall_size_multiplier=3.0, transient_persistence_s=2.0)
    bids, asks = normal_book()
    bids_with_wall = list(bids)
    bids_with_wall[0] = (bids[0][0], NORMAL_LEVEL_SIZE * 20, 1)

    tracker.update(bids_with_wall, asks, ts_ms=0, mid=100.05)
    # Wall gone after 1s (< transient_persistence_s) -> should count as transient.
    result = tracker.update(bids, asks, ts_ms=1000, mid=100.05)

    assert result["n_active_walls"] == 0
    assert result["n_cancelled_walls"] == 1
    assert result["wall_cancellation_rate"] == pytest.approx(1.0)
    assert result["transient_liquidity_score"] == pytest.approx(1.0)


def test_wall_that_persists_long_is_not_transient():
    tracker = LiquidityTracker(wall_size_multiplier=3.0, transient_persistence_s=2.0)
    bids, asks = normal_book()
    bids_with_wall = list(bids)
    bids_with_wall[0] = (bids[0][0], NORMAL_LEVEL_SIZE * 20, 1)

    tracker.update(bids_with_wall, asks, ts_ms=0, mid=100.05)
    tracker.update(bids_with_wall, asks, ts_ms=10_000, mid=100.05)  # still there at 10s
    result = tracker.update(bids, asks, ts_ms=11_000, mid=100.05)   # gone at 11s (lived 11s, not transient)

    assert result["transient_liquidity_score"] == pytest.approx(0.0)


def test_liquidity_imbalance_favors_heavier_side():
    tracker = LiquidityTracker(near_mid_bps=1000)  # wide enough to capture the whole book
    bids, asks = normal_book(n=5)
    # Double all bid sizes so bid depth > ask depth.
    bids = [(p, sz * 2, n) for p, sz, n in bids]
    result = tracker.update(bids, asks, ts_ms=0, mid=100.05)
    assert result["liquidity_imbalance"] > 0


def test_depth_change_is_none_on_first_snapshot_then_reflects_delta():
    tracker = LiquidityTracker(near_mid_bps=1000)
    bids, asks = normal_book(n=5)
    first = tracker.update(bids, asks, ts_ms=0, mid=100.05)
    assert first["depth_change"] is None

    bigger_bids = [(p, sz * 2, n) for p, sz, n in bids]
    second = tracker.update(bigger_bids, asks, ts_ms=1000, mid=100.05)
    assert second["depth_change"] > 0
