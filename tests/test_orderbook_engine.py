"""Phase 3 tests: OrderBookEngine wiring (on_l2book/on_trades -> metrics()).
See docs/09_tests_plan.md."""
import pytest

from src.orderbook.orderbook_engine import OrderBookEngine


def l2book_payload(bids, asks, ts_ms):
    return {
        "type": "l2Book",
        "coin": "BTC",
        "ts_exchange_ms": ts_ms,
        "best_bid": bids[0][0] if bids else None,
        "best_ask": asks[0][0] if asks else None,
        "bids": bids,
        "asks": asks,
    }


def trades_payload(trades):
    return {"type": "trades", "coin": "BTC", "trades": trades}


def test_engine_starts_empty():
    engine = OrderBookEngine(coin="BTC")
    m = engine.metrics()
    assert m["best_bid"] is None
    assert m["mid"] is None
    assert m["n_recent_sweeps"] == 0


def test_engine_on_l2book_updates_top_of_book_and_obi():
    engine = OrderBookEngine(coin="BTC")
    bids = [(100.0, 10.0, 1), (99.9, 5.0, 1)]
    asks = [(100.1, 5.0, 1), (100.2, 5.0, 1)]
    m = engine.on_l2book(l2book_payload(bids, asks, ts_ms=1000), ts_received_ns=1)

    assert m["best_bid"] == 100.0
    assert m["best_ask"] == 100.1
    assert m["mid"] == pytest.approx(100.05)
    assert m["obi_1"] == pytest.approx((10.0 - 5.0) / 15.0)


def test_engine_microprice_delta_after_two_snapshots():
    engine = OrderBookEngine(coin="BTC")
    engine.on_l2book(l2book_payload([(100.0, 10.0, 1)], [(100.1, 10.0, 1)], ts_ms=0), ts_received_ns=1)
    m = engine.on_l2book(l2book_payload([(100.5, 10.0, 1)], [(100.6, 10.0, 1)], ts_ms=1000), ts_received_ns=2)
    assert m["microprice_delta"] == pytest.approx(0.5)


def test_engine_on_trades_feeds_trade_flow_and_sweeps():
    engine = OrderBookEngine(coin="BTC")
    engine.on_l2book(
        l2book_payload([(100.0, 10.0, 1)], [(100.1, 10.0, 1)], ts_ms=0), ts_received_ns=1
    )
    trades = [
        {"coin": "BTC", "side": "B", "price": 100.1, "size": 1.0, "ts_exchange_ms": 100, "tid": 1},
        {"coin": "BTC", "side": "B", "price": 100.2, "size": 1.0, "ts_exchange_ms": 200, "tid": 2},
        {"coin": "BTC", "side": "B", "price": 100.3, "size": 1.0, "ts_exchange_ms": 300, "tid": 3},
    ]
    m = engine.on_trades(trades_payload(trades))

    assert m["n_recent_sweeps"] == 1
    assert m["last_sweep"]["side"] == "B"
    assert m["last_sweep"]["n_trades"] == 3


def test_engine_metrics_include_all_expected_keys():
    engine = OrderBookEngine(coin="BTC")
    engine.on_l2book(l2book_payload([(100.0, 10.0, 1)], [(100.1, 10.0, 1)], ts_ms=0), ts_received_ns=1)
    m = engine.metrics()
    expected_keys = {
        "coin", "best_bid", "best_ask", "mid", "microprice", "microprice_delta",
        "microprice_acceleration", "spread_abs", "spread_bps", "obi_1", "obi_5",
        "obi_10", "obi_25", "obi_weighted", "n_recent_sweeps", "last_sweep",
        "buy_absorption_score", "sell_absorption_score",
        "wall_persistence", "wall_cancellation_rate", "depth_change",
        "liquidity_imbalance", "transient_liquidity_score",
    }
    assert expected_keys.issubset(m.keys())


def test_engine_metrics_liquidity_fields_actually_populated():
    """Regression test for a bug caught while wiring Phase 4 storage:
    LiquidityTracker.update()'s return value was computed but discarded,
    so these fields silently stayed None forever."""
    engine = OrderBookEngine(coin="BTC")
    bids, asks = [(100.0, 10.0, 1)], [(100.1, 10.0, 1)]
    m = engine.on_l2book(l2book_payload(bids, asks, ts_ms=0), ts_received_ns=1)
    assert m["liquidity_imbalance"] is not None
    assert m["wall_cancellation_rate"] is not None
    assert m["transient_liquidity_score"] == 0.0  # no wall history yet, but not None
