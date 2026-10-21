"""Reconstructs a chronological market-event stream from the raw tick
Parquet data Phase 4 already captures (`hyperliquid_l2`, one row per book
level per snapshot; `hyperliquid_trades`, one row per trade), so Phase 7's
`FeatureEngine` can replay history through the exact same
`OrderBookEngine` (Phase 3, already unit-tested) that drives the live
console — no separate/duplicated microstructure math.

Why grouping by `ts_received_ns` reconstructs the original messages:
`main.py: persist_l2book_levels`/`persist_trades` write every row from one
inbound WebSocket message with the *same* `ts_received_ns`
(`time.time_ns()` is called once per message in `_handle_message`, not
once per row) — so grouping raw rows back by that value recovers exactly
which levels/trades arrived together in one envelope. This matters for
trades in particular: `SweepDetector.detect()` (sweeps.py) only looks for
bursts *within a single `on_trades()` call* — replaying trades one-by-one
instead of in their original batches would silently break sweep
detection. Levels within a group are re-sorted best-first by price rather
than trusting row order survived Parquet's multi-file buffering
unchanged — cheap and removes that dependency entirely.

Known, documented approximation: if two distinct messages ever land in
the exact same nanosecond (`time.time_ns()` collision), they'd be merged
into one replayed event. Vanishingly unlikely for a single-process
collector, but not impossible — same spirit as `orderbook_engine.py`'s
own "proxy, not exact replay" note on absorption.
"""
from __future__ import annotations

from collections import defaultdict

from src.orderbook.trade_flow import BUY_SIDE
from src.storage.parquet_reader import read_partitions

Level = tuple[float, float, int | None]


def build_market_event_stream(root, asset: str, dates: "list[str]") -> "list[dict]":
    """Returns a chronologically sorted list of
    `{"kind": "l2book" | "trades", "ts_received_ns": int, "payload": dict}`
    — `payload` is shaped exactly like `collectors.hyperliquid_ws.
    parse_hyperliquid_message()`'s output, so it can be fed straight into
    `OrderBookEngine.on_l2book()`/`on_trades()` unchanged, same as
    `main.py`'s live `market_state_consumer` does."""
    l2_rows = read_partitions(root, "hyperliquid_l2", dates, asset=asset)
    trade_rows = read_partitions(root, "hyperliquid_trades", dates, asset=asset)

    l2_groups: dict[int, dict] = defaultdict(lambda: {"bids": [], "asks": [], "ts_exchange_ms": None})
    for r in l2_rows:
        ts_ns = r.get("ts_received_ns")
        if ts_ns is None:
            continue
        g = l2_groups[ts_ns]
        side_key = "bids" if r.get("side") == "bid" else "asks"
        g[side_key].append((r["price"], r["size"], r.get("n_orders")))
        g["ts_exchange_ms"] = r.get("ts_exchange_ms")

    trade_groups: dict[int, list[dict]] = defaultdict(list)
    for r in trade_rows:
        ts_ns = r.get("ts_received_ns")
        if ts_ns is None:
            continue
        trade_groups[ts_ns].append(
            {
                "coin": asset,
                "side": r.get("side"),
                "price": r.get("price"),
                "size": r.get("size"),
                "ts_exchange_ms": r.get("ts_exchange_ms"),
                "tid": r.get("tid"),
            }
        )

    events: list[dict] = []
    for ts_ns, g in l2_groups.items():
        bids: list[Level] = sorted(g["bids"], key=lambda lvl: -lvl[0])
        asks: list[Level] = sorted(g["asks"], key=lambda lvl: lvl[0])
        events.append(
            {
                "kind": "l2book",
                "ts_received_ns": ts_ns,
                "payload": {
                    "type": "l2Book",
                    "coin": asset,
                    "ts_exchange_ms": g["ts_exchange_ms"],
                    "best_bid": bids[0][0] if bids else None,
                    "best_ask": asks[0][0] if asks else None,
                    "bids": bids,
                    "asks": asks,
                },
            }
        )

    for ts_ns, trades in trade_groups.items():
        # Stable within-batch order: exchange timestamp then trade id —
        # doesn't affect sweep burst *detection* (same-side/same-window
        # grouping is order-independent for that), but keeps
        # `price_start`/`price_end` (first/last trade in the burst)
        # deterministic across runs instead of depending on dict
        # insertion order.
        trades_sorted = sorted(trades, key=lambda t: (t.get("ts_exchange_ms") or 0, t.get("tid") or 0))
        events.append(
            {
                "kind": "trades",
                "ts_received_ns": ts_ns,
                "payload": {"type": "trades", "coin": asset, "trades": trades_sorted},
            }
        )

    events.sort(key=lambda e: e["ts_received_ns"])
    return events


def net_sweep_volume(recent_sweeps, now_ms: int, lookback_s: float) -> float:
    """Signed volume of detected sweeps (sweeps.py: `SweepEvent`) that
    completed within the last `lookback_s` seconds — positive means net
    aggressive buying pressure from *burst* activity specifically, a
    narrower signal than `trade_imbalance` (which counts every trade, not
    just ones that formed a detected burst). Used by `feature_engine.py`
    for the `sweep_score` column.

    `now_ms > s.end_ms` is guaranteed by construction during a forward
    replay (a sweep can only appear in `recent_sweeps` once the trades
    that formed it have already been fed to the engine, i.e. at or before
    `now_ms`) — the explicit bound here is a defensive no-op, not load-
    bearing, kept so this function is safe to call from anywhere, not just
    the replay loop.
    """
    cutoff_ms = now_ms - lookback_s * 1000
    signed = 0.0
    for s in recent_sweeps:
        if s.end_ms < cutoff_ms or s.end_ms > now_ms:
            continue
        signed += s.volume if s.side == BUY_SIDE else -s.volume
    return signed
