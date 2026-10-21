"""Order flow / trade flow metrics (section 14, Phase 3).

Tracks a rolling window of trades and computes, per requested window size
(1/5/10/30/60/180s per the master prompt): aggressive buy/sell volume,
buy/sell ratio, trade imbalance, signed volume, trade velocity, average
trade size, large-trade frequency, and volume acceleration (short-window
rate vs. long-window rate).

Side convention (Hyperliquid `trades` channel): side "B" = the taker/
aggressor bought (hit the ask); side "A" = the taker/aggressor sold (hit
the bid). This was cross-checked against a live run during Phase 2 —
a burst of "B" trades coincided with the mid price rising, consistent with
"B" = aggressive buying. Re-verify if Hyperliquid ever changes this.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

BUY_SIDE = "B"
SELL_SIDE = "A"

DEFAULT_WINDOWS_S = (1, 5, 10, 30, 60, 180)


@dataclass(frozen=True)
class Trade:
    ts_ms: int
    price: float
    size: float
    side: str  # "B" or "A"


class TradeFlowTracker:
    """One instance per asset. Feed it trades as they arrive; ask for
    window_stats() at any point using the timestamp of the most recent
    trade (or an explicit `now_ms`, e.g. wall-clock, for use between
    trades)."""

    def __init__(
        self,
        windows_s: tuple[int, ...] = DEFAULT_WINDOWS_S,
        large_trade_multiplier: float = 5.0,
        large_trade_min_samples: int = 20,
    ) -> None:
        self.windows_s = windows_s
        self.large_trade_multiplier = large_trade_multiplier
        self.large_trade_min_samples = large_trade_min_samples
        self._max_window_s = max(windows_s)
        self.trades: deque[Trade] = deque()

    def add_trade(self, trade: Trade) -> None:
        self.trades.append(trade)
        self._prune(trade.ts_ms)

    def add_trades(self, trades: list[Trade]) -> None:
        for t in trades:
            self.add_trade(t)

    def _prune(self, now_ms: int) -> None:
        cutoff = now_ms - self._max_window_s * 1000
        while self.trades and self.trades[0].ts_ms < cutoff:
            self.trades.popleft()

    def _large_trade_threshold(self) -> float | None:
        """Relative threshold (avg trade size x multiplier) rather than a
        hardcoded absolute size, so it adapts naturally across assets with
        very different price/size scales instead of needing a per-asset
        constant in config. Needs enough samples to be meaningful."""
        if len(self.trades) < self.large_trade_min_samples:
            return None
        avg_size = sum(t.size for t in self.trades) / len(self.trades)
        return avg_size * self.large_trade_multiplier

    def _trades_in_window(self, window_s: int, now_ms: int) -> list[Trade]:
        cutoff = now_ms - window_s * 1000
        return [t for t in self.trades if t.ts_ms >= cutoff]

    def window_stats(self, window_s: int, now_ms: int | None = None) -> dict:
        if now_ms is None:
            now_ms = self.trades[-1].ts_ms if self.trades else 0
        window_trades = self._trades_in_window(window_s, now_ms)

        buy_volume = sum(t.size for t in window_trades if t.side == BUY_SIDE)
        sell_volume = sum(t.size for t in window_trades if t.side == SELL_SIDE)
        total_volume = buy_volume + sell_volume
        signed_volume = buy_volume - sell_volume
        trade_imbalance = signed_volume / total_volume if total_volume else None
        buy_sell_ratio = (buy_volume / sell_volume) if sell_volume else None

        n = len(window_trades)
        trade_velocity = n / window_s  # trades per second
        avg_trade_size = (total_volume / n) if n else None

        large_threshold = self._large_trade_threshold()
        large_trade_freq = (
            sum(1 for t in window_trades if t.size >= large_threshold) / window_s
            if large_threshold is not None
            else None
        )

        return {
            "window_s": window_s,
            "n_trades": n,
            "buy_volume": buy_volume,
            "sell_volume": sell_volume,
            "signed_volume": signed_volume,
            "trade_imbalance": trade_imbalance,
            "buy_sell_ratio": buy_sell_ratio,
            "trade_velocity": trade_velocity,
            "avg_trade_size": avg_trade_size,
            "large_trade_freq": large_trade_freq,
        }

    def all_window_stats(self, now_ms: int | None = None) -> dict[str, dict]:
        return {f"{w}s": self.window_stats(w, now_ms) for w in self.windows_s}

    def volume_acceleration(
        self, short_s: int, long_s: int, now_ms: int | None = None
    ) -> float | None:
        """Difference between the short-window and long-window volume
        *rate* (volume per second). Positive means recent activity is
        picking up relative to the longer baseline."""
        if short_s >= long_s:
            raise ValueError("short_s must be < long_s")
        short = self.window_stats(short_s, now_ms)
        long = self.window_stats(long_s, now_ms)
        short_rate = (short["buy_volume"] + short["sell_volume"]) / short_s
        long_rate = (long["buy_volume"] + long["sell_volume"]) / long_s
        return short_rate - long_rate
