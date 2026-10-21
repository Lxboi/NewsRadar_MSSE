"""Liquidity walls, depth, and transient liquidity (sections 15-16, Phase 3).

Never labels a large resting order as a signal by itself (section 15) —
this module only measures, it doesn't interpret. In particular it never
claims to detect spoofing with certainty (section 16); it exposes
`transient_liquidity_score`, a purely descriptive measure of how often
recently-seen "walls" turn out to be short-lived, which downstream research
(Phase 7+) can correlate with outcomes if it wants to.

Wall and depth thresholds here are relative to the book's own recent
statistics (average level size, distance from mid in bps) rather than
hardcoded per-asset constants, so the same code works across BTC/ETH/SOL
and whatever gets added later (section 6) without per-asset tuning. The
exact multiplier/threshold values are Phase-1-style starting points —
expect to recalibrate them empirically once real data accumulates
(consistent with section 4: never assume a default is "correct" just
because it runs).
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field

Level = tuple[float, float, int | None]  # (price, size, n_orders)


@dataclass
class WallEvent:
    side: str          # "bid" | "ask"
    price: float
    size: float
    first_seen_ms: int
    last_seen_ms: int

    @property
    def persistence_s(self) -> float:
        return (self.last_seen_ms - self.first_seen_ms) / 1000


@dataclass
class LiquidityTracker:
    """One instance per asset. Call `update()` on every l2Book snapshot."""

    wall_size_multiplier: float = 3.0     # a level >= multiplier x avg level size counts as a "wall"
    near_mid_bps: float = 50.0            # depth is summed within this distance of mid
    transient_window: int = 20            # how many recently-completed walls feed transient_liquidity_score
    transient_persistence_s: float = 2.0  # a completed wall living less than this counts as "transient"
    history_maxlen: int = 500

    active_walls: dict[tuple[str, float], WallEvent] = field(default_factory=dict)
    completed_walls: deque[WallEvent] = field(default_factory=lambda: deque(maxlen=20))
    depth_history: deque[tuple[int, float, float]] = field(default_factory=lambda: deque(maxlen=500))

    def __post_init__(self) -> None:
        # dataclass field defaults above use fixed maxlen; re-create with the
        # configured values so a non-default history_maxlen/transient_window
        # actually takes effect.
        self.completed_walls = deque(maxlen=self.transient_window)
        self.depth_history = deque(maxlen=self.history_maxlen)

    def update(self, bids: list[Level], asks: list[Level], ts_ms: int, mid: float | None) -> dict:
        wall_threshold = self._wall_threshold(bids, asks)

        seen_keys: set[tuple[str, float]] = set()
        if wall_threshold is not None and wall_threshold > 0:
            for side, levels in (("bid", bids), ("ask", asks)):
                for price, size, _n in levels:
                    if size < wall_threshold:
                        continue
                    key = (side, round(price, 8))
                    seen_keys.add(key)
                    existing = self.active_walls.get(key)
                    if existing is not None:
                        existing.last_seen_ms = ts_ms
                        existing.size = size
                    else:
                        self.active_walls[key] = WallEvent(
                            side=side, price=price, size=size,
                            first_seen_ms=ts_ms, last_seen_ms=ts_ms,
                        )

        cancelled = []
        for key in list(self.active_walls.keys()):
            if key not in seen_keys:
                wall = self.active_walls.pop(key)
                self.completed_walls.append(wall)
                cancelled.append(wall)

        bid_depth = self._depth_near_mid(bids, mid)
        ask_depth = self._depth_near_mid(asks, mid)
        total_depth = bid_depth + ask_depth
        liquidity_imbalance = (bid_depth - ask_depth) / total_depth if total_depth else None

        depth_change = None
        if self.depth_history:
            _, prev_bid, prev_ask = self.depth_history[-1]
            depth_change = total_depth - (prev_bid + prev_ask)
        self.depth_history.append((ts_ms, bid_depth, ask_depth))

        n_walls_considered = len(seen_keys) + len(cancelled)
        wall_cancellation_rate = (len(cancelled) / n_walls_considered) if n_walls_considered else 0.0
        wall_persistence = (
            statistics.mean(w.persistence_s for w in self.active_walls.values())
            if self.active_walls
            else 0.0
        )

        return {
            "wall_persistence": wall_persistence,
            "wall_cancellation_rate": wall_cancellation_rate,
            "n_active_walls": len(self.active_walls),
            "n_cancelled_walls": len(cancelled),
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "depth_change": depth_change,
            "liquidity_imbalance": liquidity_imbalance,
            "transient_liquidity_score": self.transient_liquidity_score(),
        }

    def _wall_threshold(self, bids: list[Level], asks: list[Level]) -> float | None:
        sizes = [sz for _, sz, _ in bids] + [sz for _, sz, _ in asks]
        if not sizes:
            return None
        return statistics.mean(sizes) * self.wall_size_multiplier

    def _depth_near_mid(self, levels: list[Level], mid: float | None) -> float:
        if mid is None or mid <= 0:
            return sum(sz for _, sz, _ in levels)
        total = 0.0
        for price, size, _n in levels:
            distance_bps = abs(price - mid) / mid * 10_000
            if distance_bps <= self.near_mid_bps:
                total += size
        return total

    def transient_liquidity_score(self) -> float:
        """Fraction of recently-*completed* walls (out of the last
        `transient_window`) that persisted for less than
        `transient_persistence_s` before disappearing — i.e. liquidity that
        showed up, looked large, and vanished quickly. Purely descriptive
        (section 16); 0.0 with no history yet."""
        if not self.completed_walls:
            return 0.0
        short_lived = sum(1 for w in self.completed_walls if w.persistence_s < self.transient_persistence_s)
        return short_lived / len(self.completed_walls)
