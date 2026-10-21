"""Order book engine — Phase 3 orchestrator (sections 12-18).

Hyperliquid's `l2Book` messages are full snapshots (see
docs/03_hyperliquid_ws_architecture.md), so `OrderBookEngine` doesn't need
to apply incremental deltas — it just keeps the latest snapshot plus a
short rolling history, and delegates every derived metric to the
single-purpose modules next to this file: `imbalance.py` (OBI),
`microprice.py`, `trade_flow.py`, `liquidity.py`, `sweeps.py`,
`absorption.py`. This file's job is only to wire raw
collectors.hyperliquid_ws events into those modules and hand back one
combined metrics dict per asset — it computes nothing itself.

One `OrderBookEngine` instance per asset.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from src.orderbook.absorption import compute_absorption_scores
from src.orderbook.imbalance import DEFAULT_OBI_LEVELS, compute_obi_all, compute_weighted_obi
from src.orderbook.liquidity import LiquidityTracker
from src.orderbook.microprice import MicropriceHistory, compute_microprice, compute_mid
from src.orderbook.sweeps import SweepDetector, SweepEvent
from src.orderbook.trade_flow import Trade, TradeFlowTracker

Level = tuple[float, float, int | None]

# Windows used for the absorption score's own volume/price-displacement
# measurement — deliberately short, since absorption is a short-horizon
# microstructure concept (section 18). Distinct from trade_flow's broader
# window set, which features/feature_engine.py (Phase 7) will draw on too.
ABSORPTION_WINDOW_S = 10


@dataclass
class BookSnapshot:
    coin: str
    ts_exchange_ms: int
    ts_received_ns: int
    bids: list[Level]   # sorted best-first (highest price first)
    asks: list[Level]   # sorted best-first (lowest price first)


class OrderBookEngine:
    def __init__(
        self,
        coin: str,
        history_maxlen: int = 500,
        obi_levels: tuple[int, ...] = DEFAULT_OBI_LEVELS,
    ) -> None:
        self.coin = coin
        self.obi_levels = obi_levels

        self.current: BookSnapshot | None = None
        self.history: deque[BookSnapshot] = deque(maxlen=history_maxlen)

        self.microprice_history = MicropriceHistory()
        self.trade_flow = TradeFlowTracker()
        self.liquidity = LiquidityTracker()
        self.sweep_detector = SweepDetector()

        self.recent_sweeps: deque[SweepEvent] = deque(maxlen=20)
        self.last_liquidity_metrics: dict = {}

    # -- ingestion ---------------------------------------------------------

    def on_l2book(self, payload: dict, ts_received_ns: int) -> dict:
        """`payload` is the parsed event from
        collectors.hyperliquid_ws.parse_hyperliquid_message (type == "l2Book").
        Returns the current combined metrics dict (see `metrics()`)."""
        bids = payload.get("bids") or []
        asks = payload.get("asks") or []
        ts_ms = payload.get("ts_exchange_ms") or 0

        pre_sweep_book = (self.current.bids, self.current.asks) if self.current else None

        snapshot = BookSnapshot(
            coin=self.coin, ts_exchange_ms=ts_ms, ts_received_ns=ts_received_ns, bids=bids, asks=asks
        )
        self.current = snapshot
        self.history.append(snapshot)
        self._last_pre_sweep_book = pre_sweep_book

        mid = compute_mid(self.best_bid, self.best_ask)
        microprice = compute_microprice(self.best_bid, self._best_bid_size, self.best_ask, self._best_ask_size)
        self.microprice_history.push(microprice)
        # Bug fix (found while wiring Phase 4 storage, which reads this same
        # dict): update() was called but its result was discarded, so
        # liquidity_imbalance/wall_persistence/transient_liquidity_score
        # never actually reached metrics() or the live display.
        self.last_liquidity_metrics = self.liquidity.update(bids, asks, ts_ms, mid)

        return self.metrics()

    def on_trades(self, payload: dict) -> dict:
        """`payload` is the parsed event (type == "trades") — a batch of one
        or more trades sharing the same envelope."""
        raw_trades = payload.get("trades") or []
        trades = [
            Trade(ts_ms=t["ts_exchange_ms"], price=t["price"], size=t["size"], side=t["side"])
            for t in raw_trades
            if t.get("ts_exchange_ms") is not None
        ]
        self.trade_flow.add_trades(trades)

        pre_sweep_book = getattr(self, "_last_pre_sweep_book", None)
        new_sweeps = self.sweep_detector.detect(trades, pre_sweep_book=pre_sweep_book)
        for sweep in new_sweeps:
            self.recent_sweeps.append(sweep)

        return self.metrics()

    # -- derived top-of-book helpers ----------------------------------------

    @property
    def best_bid(self) -> float | None:
        return self.current.bids[0][0] if self.current and self.current.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.current.asks[0][0] if self.current and self.current.asks else None

    @property
    def _best_bid_size(self) -> float | None:
        return self.current.bids[0][1] if self.current and self.current.bids else None

    @property
    def _best_ask_size(self) -> float | None:
        return self.current.asks[0][1] if self.current and self.current.asks else None

    def spread(self) -> dict:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return {"spread_abs": None, "spread_bps": None}
        mid = (bid + ask) / 2
        spread_abs = ask - bid
        return {
            "spread_abs": spread_abs,
            "spread_bps": (spread_abs / mid * 10_000) if mid else None,
        }

    # -- combined output -----------------------------------------------------

    def metrics(self) -> dict:
        """Everything computed so far for this asset, in one dict — this is
        what main.py's live display (and, later, feature_engine.py) reads."""
        mid = compute_mid(self.best_bid, self.best_ask)
        bids = self.current.bids if self.current else []
        asks = self.current.asks if self.current else []

        absorption = self._absorption_metrics(mid)

        out = {
            "coin": self.coin,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "mid": mid,
            "microprice": self.microprice_history.current,
            "microprice_delta": self.microprice_history.delta,
            "microprice_acceleration": self.microprice_history.acceleration,
            **self.spread(),
            **compute_obi_all(bids, asks, self.obi_levels),
            "obi_weighted": compute_weighted_obi(bids, asks, mid),
            "n_recent_sweeps": len(self.recent_sweeps),
            "last_sweep": self._last_sweep_summary(),
            **absorption,
            "wall_persistence": self.last_liquidity_metrics.get("wall_persistence"),
            "wall_cancellation_rate": self.last_liquidity_metrics.get("wall_cancellation_rate"),
            "depth_change": self.last_liquidity_metrics.get("depth_change"),
            "liquidity_imbalance": self.last_liquidity_metrics.get("liquidity_imbalance"),
            "transient_liquidity_score": self.last_liquidity_metrics.get("transient_liquidity_score"),
        }
        return out

    def _absorption_metrics(self, mid: float | None) -> dict:
        stats = self.trade_flow.window_stats(ABSORPTION_WINDOW_S)
        # Approximate the window's price displacement using the earliest
        # trade still inside the window vs. the current mid — a proxy, not
        # an exact replay, since we don't retain historical mids indexed by
        # time here (feature_engine.py, Phase 7, has the full time series).
        window_trades = self.trade_flow._trades_in_window(  # noqa: SLF001 (internal, same package)
            ABSORPTION_WINDOW_S, self.trade_flow.trades[-1].ts_ms if self.trade_flow.trades else 0
        )
        if not window_trades or mid is None:
            return {"buy_absorption_score": None, "sell_absorption_score": None}
        price_start = window_trades[0].price
        return compute_absorption_scores(
            buy_volume=stats["buy_volume"],
            sell_volume=stats["sell_volume"],
            price_start=price_start,
            price_end=mid,
        )

    def _last_sweep_summary(self) -> dict | None:
        if not self.recent_sweeps:
            return None
        s = self.recent_sweeps[-1]
        return {
            "side": s.side,
            "volume": s.volume,
            "n_trades": s.n_trades,
            "duration_s": s.duration_s,
            "price_displacement_bps": s.price_displacement_bps,
            "levels_crossed": s.levels_crossed,
        }
