"""Phase 3 tests: OBI and microprice against hand-built order books.
See docs/09_tests_plan.md."""
import pytest

from src.orderbook.imbalance import compute_obi, compute_obi_all, compute_weighted_obi
from src.orderbook.microprice import MicropriceHistory, compute_mid, compute_microprice

# (price, size, n_orders)
BIDS = [(100.0, 10.0, 1), (99.9, 5.0, 1), (99.8, 5.0, 1)]
ASKS = [(100.1, 5.0, 1), (100.2, 5.0, 1), (100.3, 10.0, 1)]


def test_obi_top_1_favors_bid():
    # top-1: bid 10 vs ask 5 -> imbalance toward bid (positive)
    obi = compute_obi(BIDS, ASKS, levels=1)
    assert obi == pytest.approx((10.0 - 5.0) / (10.0 + 5.0))


def test_obi_top_3_is_balanced():
    # totals: bid 10+5+5=20, ask 5+5+10=20 -> perfectly balanced
    obi = compute_obi(BIDS, ASKS, levels=3)
    assert obi == pytest.approx(0.0)


def test_obi_empty_book_is_none():
    assert compute_obi([], [], levels=5) is None


def test_obi_all_returns_all_configured_levels():
    result = compute_obi_all(BIDS, ASKS, levels_list=(1, 2, 3))
    assert set(result.keys()) == {"obi_1", "obi_2", "obi_3"}


def test_weighted_obi_none_without_mid():
    assert compute_weighted_obi(BIDS, ASKS, mid=None) is None


def test_weighted_obi_matches_sign_of_unweighted():
    mid = 100.05
    weighted = compute_weighted_obi(BIDS, ASKS, mid=mid, levels=3)
    unweighted = compute_obi(BIDS, ASKS, levels=3)
    # Both measure the same directional imbalance; weighting shouldn't flip sign
    # on a book this simple.
    assert (weighted >= 0) == (unweighted >= 0)


def test_compute_mid():
    assert compute_mid(100.0, 100.1) == pytest.approx(100.05)
    assert compute_mid(None, 100.1) is None


def test_compute_microprice_weights_toward_thinner_opposite_size():
    # More size on the bid (20) than the ask (5) pulls microprice toward
    # the ask price (since microprice weights each side's price by the
    # OPPOSITE side's size).
    microprice = compute_microprice(best_bid=100.0, best_bid_size=20.0, best_ask=100.1, best_ask_size=5.0)
    mid = compute_mid(100.0, 100.1)
    assert microprice > mid  # pulled toward the ask, away from the heavier bid side


def test_compute_microprice_symmetric_sizes_equals_mid():
    microprice = compute_microprice(best_bid=100.0, best_bid_size=10.0, best_ask=100.1, best_ask_size=10.0)
    assert microprice == pytest.approx(compute_mid(100.0, 100.1))


def test_microprice_history_delta_and_acceleration():
    hist = MicropriceHistory()
    assert hist.delta is None
    assert hist.acceleration is None

    hist.push(100.0)
    assert hist.delta is None  # need 2 points

    hist.push(100.5)
    assert hist.delta == pytest.approx(0.5)
    assert hist.acceleration is None  # need 3 points

    hist.push(101.5)  # delta now 1.0, previous delta 0.5 -> acceleration 0.5
    assert hist.delta == pytest.approx(1.0)
    assert hist.acceleration == pytest.approx(0.5)


def test_microprice_history_ignores_none():
    hist = MicropriceHistory()
    hist.push(100.0)
    hist.push(None)
    hist.push(101.0)
    assert hist.current == 101.0
    assert hist.delta == pytest.approx(1.0)
