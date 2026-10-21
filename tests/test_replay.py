"""Phase 7 tests: features/replay.py — reconstructing a chronological
market-event stream from raw Parquet tick data. Real Parquet I/O against
tmp_path, no network. See docs/09_tests_plan.md."""
import datetime as dt

from src.features.replay import build_market_event_stream, net_sweep_volume
from src.orderbook.sweeps import SweepEvent
from src.orderbook.trade_flow import BUY_SIDE, SELL_SIDE
from src.storage.parquet_store import ParquetStore

DAY = dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc)
DAY_NS = int(DAY.timestamp() * 1_000_000_000)


def write_l2_level(store, asset, ts_received_ns, ts_exchange_ms, side, price, size, n_orders=1):
    store.write_row(
        "hyperliquid_l2", asset,
        {
            "ts_exchange_ms": ts_exchange_ms, "ts_received_ns": ts_received_ns,
            "asset": asset, "side": side, "price": price, "size": size, "n_orders": n_orders,
        },
        ts_ns=ts_received_ns,
    )


def write_trade(store, asset, ts_received_ns, ts_exchange_ms, side, price, size, tid):
    store.write_row(
        "hyperliquid_trades", asset,
        {
            "ts_exchange_ms": ts_exchange_ms, "ts_received_ns": ts_received_ns,
            "asset": asset, "side": side, "price": price, "size": size, "tid": tid,
        },
        ts_ns=ts_received_ns,
    )


def test_build_market_event_stream_reconstructs_l2book_snapshot(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=100)
    ts_ns = DAY_NS + 1000
    write_l2_level(store, "BTC", ts_ns, 1000, "bid", 100.0, 5.0)
    write_l2_level(store, "BTC", ts_ns, 1000, "bid", 99.9, 3.0)
    write_l2_level(store, "BTC", ts_ns, 1000, "ask", 100.2, 4.0)
    write_l2_level(store, "BTC", ts_ns, 1000, "ask", 100.1, 6.0)  # deliberately out of best-first order
    store.flush_all()

    events = build_market_event_stream(tmp_path, "BTC", ["2026-08-18"])

    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "l2book"
    assert ev["ts_received_ns"] == ts_ns
    # Re-sorted best-first regardless of write order: bids desc, asks asc.
    assert ev["payload"]["bids"] == [(100.0, 5.0, 1), (99.9, 3.0, 1)]
    assert ev["payload"]["asks"] == [(100.1, 6.0, 1), (100.2, 4.0, 1)]
    assert ev["payload"]["best_bid"] == 100.0
    assert ev["payload"]["best_ask"] == 100.1


def test_build_market_event_stream_groups_trades_sharing_ts_received_ns(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=100)
    ts_ns = DAY_NS + 2000
    # Same envelope (same ts_received_ns) — three trades, deliberately
    # written out of exchange-time order.
    write_trade(store, "BTC", ts_ns, 300, "B", 100.3, 1.0, tid=3)
    write_trade(store, "BTC", ts_ns, 100, "B", 100.1, 1.0, tid=1)
    write_trade(store, "BTC", ts_ns, 200, "B", 100.2, 1.0, tid=2)
    store.flush_all()

    events = build_market_event_stream(tmp_path, "BTC", ["2026-08-18"])

    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "trades"
    trades = ev["payload"]["trades"]
    assert [t["tid"] for t in trades] == [1, 2, 3]  # re-sorted by ts_exchange_ms


def test_build_market_event_stream_sorts_l2book_and_trades_chronologically(tmp_path):
    store = ParquetStore(root=tmp_path, flush_every=100)
    write_trade(store, "BTC", DAY_NS + 3000, 300, "B", 100.0, 1.0, tid=1)
    write_l2_level(store, "BTC", DAY_NS + 1000, 100, "bid", 100.0, 1.0)
    write_l2_level(store, "BTC", DAY_NS + 1000, 100, "ask", 100.1, 1.0)
    write_l2_level(store, "BTC", DAY_NS + 2000, 200, "bid", 100.0, 1.0)
    write_l2_level(store, "BTC", DAY_NS + 2000, 200, "ask", 100.1, 1.0)
    store.flush_all()

    events = build_market_event_stream(tmp_path, "BTC", ["2026-08-18"])

    assert [e["ts_received_ns"] for e in events] == [DAY_NS + 1000, DAY_NS + 2000, DAY_NS + 3000]
    assert [e["kind"] for e in events] == ["l2book", "l2book", "trades"]


def test_build_market_event_stream_empty_when_nothing_collected(tmp_path):
    events = build_market_event_stream(tmp_path, "BTC", ["2026-08-18"])
    assert events == []


def test_net_sweep_volume_nets_buy_and_sell_sweeps_within_lookback():
    def sweep(side, volume, end_ms):
        return SweepEvent(
            side=side, start_ms=end_ms - 500, end_ms=end_ms, volume=volume, n_trades=3,
            price_start=100.0, price_end=100.1, levels_crossed=None, price_displacement_bps=10.0,
        )

    recent = [
        sweep(BUY_SIDE, 5.0, end_ms=9_000),
        sweep(SELL_SIDE, 2.0, end_ms=9_500),
        sweep(BUY_SIDE, 1.0, end_ms=1_000),  # outside the 5s lookback from now_ms=10_000
    ]

    result = net_sweep_volume(recent, now_ms=10_000, lookback_s=5)

    assert result == 5.0 - 2.0


def test_net_sweep_volume_empty_history_is_zero():
    assert net_sweep_volume([], now_ms=1000, lookback_s=30) == 0.0


def test_net_sweep_volume_ignores_sweeps_after_now_ms():
    def sweep(side, volume, end_ms):
        return SweepEvent(
            side=side, start_ms=end_ms - 100, end_ms=end_ms, volume=volume, n_trades=3,
            price_start=100.0, price_end=100.1, levels_crossed=None, price_displacement_bps=10.0,
        )

    # Defensive case only (shouldn't arise from a real forward replay) —
    # a "future" sweep must not leak into a "now" score.
    result = net_sweep_volume([sweep(BUY_SIDE, 10.0, end_ms=20_000)], now_ms=10_000, lookback_s=30)
    assert result == 0.0
