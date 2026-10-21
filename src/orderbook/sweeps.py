"""Sweep detection (section 17, Phase 3).

A "sweep" here is a burst of same-side aggressive trades arriving in quick
succession — a heuristic proxy for an order (or coordinated set of orders)
that consumed multiple price levels rapidly. Hyperliquid's public feed
gives us trades and book *snapshots*, not a guaranteed pre/post-trade delta
pair, so `levels_crossed` is computed by comparing the burst's price range
against whatever book snapshot is passed in as "before" — pass the most
recent snapshot prior to the burst if you have one; it's optional.

Statistically validating what happens *after* a detected sweep ("réaction
post-sweep", section 17) is Phase 8+ research work (needs forward returns,
which this module has no business computing) — this module only detects
and describes the event itself.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.orderbook.trade_flow import BUY_SIDE, Trade

Level = tuple[float, float, int | None]


@dataclass(frozen=True)
class SweepEvent:
    side: str            # "B" (aggressive buying swept the ask side) | "A" (aggressive selling swept the bid side)
    start_ms: int
    end_ms: int
    volume: float
    n_trades: int
    price_start: float
    price_end: float
    levels_crossed: int | None
    price_displacement_bps: float

    @property
    def duration_s(self) -> float:
        return (self.end_ms - self.start_ms) / 1000


class SweepDetector:
    def __init__(
        self,
        burst_window_ms: int = 2000,
        min_trades: int = 3,
        min_volume: float | None = None,
    ) -> None:
        self.burst_window_ms = burst_window_ms
        self.min_trades = min_trades
        self.min_volume = min_volume

    def detect(
        self, trades: list[Trade], pre_sweep_book: tuple[list[Level], list[Level]] | None = None
    ) -> list[SweepEvent]:
        """`trades` should be in chronological order. `pre_sweep_book`, if
        given, is `(bids, asks)` from just before the trades — used only to
        estimate how many price levels the burst crossed."""
        events: list[SweepEvent] = []
        n = len(trades)
        i = 0
        while i < n:
            side = trades[i].side
            j = i
            while (
                j + 1 < n
                and trades[j + 1].side == side
                and (trades[j + 1].ts_ms - trades[i].ts_ms) <= self.burst_window_ms
            ):
                j += 1
            burst = trades[i : j + 1]
            if len(burst) >= self.min_trades:
                volume = sum(t.size for t in burst)
                if self.min_volume is None or volume >= self.min_volume:
                    price_start = burst[0].price
                    price_end = burst[-1].price
                    levels_crossed = (
                        self._count_levels_crossed(side, price_start, price_end, pre_sweep_book)
                        if pre_sweep_book is not None
                        else None
                    )
                    displacement_bps = (
                        (price_end - price_start) / price_start * 10_000 if price_start else 0.0
                    )
                    events.append(
                        SweepEvent(
                            side=side,
                            start_ms=burst[0].ts_ms,
                            end_ms=burst[-1].ts_ms,
                            volume=volume,
                            n_trades=len(burst),
                            price_start=price_start,
                            price_end=price_end,
                            levels_crossed=levels_crossed,
                            price_displacement_bps=displacement_bps,
                        )
                    )
            i = j + 1
        return events

    @staticmethod
    def _count_levels_crossed(
        side: str, price_start: float, price_end: float, book: tuple[list[Level], list[Level]]
    ) -> int:
        bids, asks = book
        # A buy sweep (side == BUY_SIDE) consumes ask-side levels; a sell
        # sweep consumes bid-side levels.
        levels = asks if side == BUY_SIDE else bids
        lo, hi = sorted((price_start, price_end))
        return sum(1 for price, _sz, _n in levels if lo <= price <= hi)
