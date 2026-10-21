"""Builds the synchronized feature table described in
docs/06_features_v1.md (section 27) from raw tick market data (via
`features/replay.py`) and scored news events (via
`storage/database.py: list_canonical_news_events`), strictly respecting
`ingestion_time` (never `event_time`) to avoid look-ahead bias (section 29).

Roadmap: PHASE 7. One `FeatureEngine` instance per asset — mirrors
`OrderBookEngine`'s one-per-asset convention, and in fact owns one
internally, replaying raw ticks through it exactly the way `main.py`'s
live path does (see `replay.py`'s module docstring) rather than
recomputing microstructure math a second way.

## Grain (docs/06_features_v1.md, "one row per (asset, timestamp)")

The spec's own example ("e.g. every 1s") is what this implements: a fixed
UTC-aligned sampling clock, default 1s, walking forward through the raw
tick stream — not the coarser 5s cadence Phase 4 happens to snapshot to
SQLite for the live display. That distinction matters for `returns_1s` in
particular: at anything coarser than 1s sampling, a 1s return isn't an
independently-observed value, it's a relabeled longer return. See
`_compute_returns` below for how a horizon shorter than the configured
sampling interval is handled (returned as `None`, not a fabricated
number).

## Look-ahead discipline (section 29), mechanically

Every column in `build_row()` is computed from state accumulated by
feeding `market_events`/`news_events` *up to and including* the current
sample's timestamp, in one forward pass — nothing here ever looks at
`market_events[j]` or `news_events[j]` for a `j` whose timestamp is after
the row being built. `compute_targets()` (the one place forward-looking
data is used, deliberately) is a **separate function** called only after
every feature row already exists, and it only ever writes to new
`target_*` keys — it never mutates a `price`/feature column, so feature
values for row `i` are structurally incapable of depending on row `i`'s
own or any other row's future. `tests/test_feature_engine.py` has a
property test asserting exactly this (shuffling/removing future rows
never changes an earlier row's feature columns).

## Incremental API (Phase 15): the same code path for offline batch and live

`on_market_event()`/`on_news_event()`/`build_row()` are public, callable
one event or one row at a time — `build_rows()` (the original Phase 7
offline/batch entrypoint, still unchanged in its own external behavior)
is now a thin loop over exactly these three methods, not a second
implementation. This split exists so `src/live/trading_loop.py` (Phase
15) can drive a `FeatureEngine` tick-by-tick off `main.py`'s live
Hyperliquid/news feed — feeding it the same `on_l2book`/`on_trades`-
shaped events `market_state_consumer` already receives, and the same
scored news dicts `news_consumer` already produces — and get back
*exactly* the feature computation `scripts/build_feature_table.py`'s
offline replay would have produced for the same tick. This is the same
"backtest and live share logic" discipline `direction_from_probabilities()`
(Phase 11) and `simulate_fill_costs()` (Phase 10-11) already established,
extended one layer further back, to feature computation itself: a live
paper-trading run and an offline `build_feature_table.py` run over the
same historical window are structurally guaranteed to agree, since
there is only one code path computing a row, not two that could quietly
drift apart. Verified directly by
`tests/test_feature_engine.py::test_incremental_api_matches_build_rows_exactly`
— the same synthetic event stream driven through both entrypoints
produces byte-identical rows.
"""
from __future__ import annotations

import bisect
import datetime as dt
import statistics
from dataclasses import dataclass, field

from src.news.event_detector import DEFAULT_HALF_LIFE_S, apply_decay
from src.orderbook.absorption import compute_absorption_scores
from src.orderbook.imbalance import DEFAULT_OBI_LEVELS
from src.orderbook.orderbook_engine import OrderBookEngine
from src.features.replay import net_sweep_volume
from src.signals.regime_detector import RegimeDetector

# Spec lists returns_1s..returns_60s without pinning a single canonical
# window for trade_imbalance/buy_volume/sell_volume/trade_velocity/
# sweep_score/absorption_score (those are single columns, not one per
# window, unlike trade_features' storage table which keeps every
# window). 10s is the documented choice: short enough to be a live
# short-horizon signal, long enough to have a non-degenerate trade count
# at typical activity levels, and it matches
# `orderbook_engine.py: ABSORPTION_WINDOW_S` so the two absorption
# calculations (engine's live approximation vs. this module's precise
# version) are at least measuring the same window.
DEFAULT_TRADE_FLOW_WINDOW_S = 10
DEFAULT_SWEEP_LOOKBACK_S = 30
DEFAULT_VOLATILITY_WINDOW_S = 60
DEFAULT_RETURN_HORIZONS_S = (1, 5, 10, 30, 60)

DEFAULT_TARGET_HORIZONS_S = (30, 60, 180, 300)
# Phase 15 addendum 5, 2026-08-26: 100/150/200/250/300 added after
# discovering real Kraken Pro Tier 1 taker fees (0.80%, not the old
# 5bps placeholder) put round_trip_cost_bps at ~166 -- no threshold at
# or below 50bps can ever clear EventBacktester/entry.py's EV-after-
# costs filter, regardless of model confidence (see strategy.yaml's
# costs: block and docs/21_paper_trading_methodology.md section 7c).
# The <=50bps thresholds are kept so a rebuilt features_v1 table still
# supports direct before/after comparison against the original targets.
DEFAULT_TARGET_THRESHOLDS_BPS = (5, 10, 15, 25, 50, 100, 150, 200, 250, 300)


@dataclass
class _PriceHistory:
    """Append-only, timestamp-ascending (price, ts_ns) series with
    nearest-at-or-before lookup — the data structure `returns_*`,
    `volatility`, and the precise absorption calc all share. A plain
    bisect over a parallel-array list rather than anything fancier: this
    runs once per research build, not on a hot live path."""

    ts_ns: list[int] = field(default_factory=list)
    price: list[float | None] = field(default_factory=list)

    def append(self, ts_ns: int, price: float | None) -> None:
        self.ts_ns.append(ts_ns)
        self.price.append(price)

    def at_or_before(self, target_ns: int) -> float | None:
        """Price of the latest observation with `ts_ns <= target_ns`, or
        `None` if there isn't one yet (start of history)."""
        idx = bisect.bisect_right(self.ts_ns, target_ns) - 1
        if idx < 0:
            return None
        return self.price[idx]

    def window(self, start_ns: int, end_ns: int) -> "list[float]":
        """Prices with `start_ns <= ts_ns <= end_ns`, in order."""
        lo = bisect.bisect_left(self.ts_ns, start_ns)
        hi = bisect.bisect_right(self.ts_ns, end_ns)
        return [p for p in self.price[lo:hi] if p is not None]


class FeatureEngine:
    """One instance per asset. Call `build_rows()` once with the full raw
    event stream for the period you want features for."""

    def __init__(
        self,
        asset: str,
        sampling_interval_s: float = 1.0,
        obi_levels: "tuple[int, ...]" = DEFAULT_OBI_LEVELS,
        trade_flow_window_s: int = DEFAULT_TRADE_FLOW_WINDOW_S,
        sweep_lookback_s: float = DEFAULT_SWEEP_LOOKBACK_S,
        volatility_window_s: float = DEFAULT_VOLATILITY_WINDOW_S,
        return_horizons_s: "tuple[int, ...]" = DEFAULT_RETURN_HORIZONS_S,
        news_half_life_s: float = DEFAULT_HALF_LIFE_S,
        regime_detector: "RegimeDetector | None" = None,
    ) -> None:
        self.asset = asset
        self.sampling_interval_s = sampling_interval_s
        self.trade_flow_window_s = trade_flow_window_s
        self.sweep_lookback_s = sweep_lookback_s
        self.volatility_window_s = volatility_window_s
        self.return_horizons_s = return_horizons_s
        self.news_half_life_s = news_half_life_s

        self.engine = OrderBookEngine(coin=asset, obi_levels=obi_levels)
        self.regime = regime_detector or RegimeDetector()
        self._prices = _PriceHistory()
        self._last_relevant_news: "dict | None" = None

    # -- public API -----------------------------------------------------

    def build_rows(
        self,
        market_events: "list[dict]",
        news_events: "list[dict]",
        start_ts_ns: int,
        end_ts_ns: int,
    ) -> "list[dict]":
        """`market_events`: chronological list from
        `replay.build_market_event_stream()`. `news_events`: chronological
        list from `database.list_canonical_news_events()` (any asset —
        filtered internally per row against `self.asset`). Produces one
        row per `sampling_interval_s` tick from `start_ts_ns` to
        `end_ts_ns` inclusive (both epoch nanoseconds, UTC). A thin loop
        over `on_market_event()`/`on_news_event()`/`build_row()` — see the
        module docstring's "Incremental API" section for why."""
        market_events = sorted(market_events, key=lambda e: e["ts_received_ns"])
        news_events = sorted(news_events, key=lambda e: e["ingestion_time"])

        interval_ns = int(self.sampling_interval_s * 1_000_000_000)
        if interval_ns <= 0:
            raise ValueError("sampling_interval_s must be > 0")

        me_ptr = 0
        news_ptr = 0
        rows: list[dict] = []

        t_ns = start_ts_ns
        while t_ns <= end_ts_ns:
            while me_ptr < len(market_events) and market_events[me_ptr]["ts_received_ns"] <= t_ns:
                ev = market_events[me_ptr]
                self.on_market_event(ev["kind"], ev["payload"], ev["ts_received_ns"])
                me_ptr += 1

            t_dt = dt.datetime.fromtimestamp(t_ns / 1_000_000_000, tz=dt.timezone.utc)
            while news_ptr < len(news_events) and news_events[news_ptr]["ingestion_time"] <= t_dt:
                self.on_news_event(news_events[news_ptr])
                news_ptr += 1

            rows.append(self.build_row(t_ns, t_dt))
            t_ns += interval_ns

        return rows

    def on_market_event(self, kind: str, payload: dict, ts_received_ns: int) -> None:
        """Feeds one raw market event (`kind` `"l2book"` or `"trades"`,
        `payload` shaped exactly like
        `collectors.hyperliquid_ws.parse_hyperliquid_message()`'s output)
        into this engine's internal `OrderBookEngine` — the live
        counterpart to `build_rows()`'s inner market-event loop. A live
        caller (`src/live/trading_loop.py`, Phase 15) calls this once per
        message, the same messages `main.py: market_state_consumer`
        already receives, immediately after (not instead of) feeding its
        own separate per-asset `OrderBookEngine` used for the console
        display — the two engines' internal state is expected to diverge
        slightly in bookkeeping detail (two independent instances), the
        same "expected to diverge, not reconciled" call already made for
        `PaperExecutor`'s ledger vs. `StrategyEngine`'s (Phase 13)."""
        if kind == "l2book":
            self.engine.on_l2book(payload, ts_received_ns)
        else:
            self.engine.on_trades(payload)

    def on_news_event(self, event: dict) -> None:
        """Feeds one scored news event (the same dict shape
        `news/pipeline.py: NewsPipeline.process()` already produces) —
        the live counterpart to `build_rows()`'s inner news-event loop.
        Updates `_last_relevant_news` only if `_is_relevant()` agrees;
        an irrelevant event is accepted (no error) and simply ignored,
        matching `build_rows()`'s existing behavior exactly."""
        if self._is_relevant(event):
            self._last_relevant_news = event

    def build_row(self, t_ns: int, t_dt: "dt.datetime | None" = None) -> dict:
        """Builds one feature row from state accumulated so far (via
        `on_market_event()`/`on_news_event()`) as of `t_ns` — the public
        live entrypoint `build_rows()` itself now calls internally. `t_dt`
        is derived from `t_ns` when not given (both must agree when both
        are given — a live caller already has its own `dt.datetime.now(
        dt.timezone.utc)` and the matching epoch-ns value, so this avoids
        a redundant reconversion on every live tick)."""
        if t_dt is None:
            t_dt = dt.datetime.fromtimestamp(t_ns / 1_000_000_000, tz=dt.timezone.utc)
        return self._build_row(t_ns, t_dt)

    # -- per-row computation ----------------------------------------------

    def _is_relevant(self, news_event: dict) -> bool:
        affected = news_event.get("affected_assets") or []
        if self.asset in affected:
            return True
        # No specific asset tagged but still scored relevant (e.g. a Fed/
        # macro item, section 24's MACRO/REGULATION categories) — treated
        # as broad market-wide impact rather than silently dropped, since
        # a Fed rate decision moves BTC/ETH/SOL together. Documented
        # decision, see docs/12_feature_engine.md.
        if not affected and (news_event.get("market_relevance") or 0.0) > 0.0:
            return True
        return False

    def _build_row(self, t_ns: int, t_dt: dt.datetime) -> dict:
        metrics = self.engine.metrics()
        price = metrics.get("mid")
        self._prices.append(t_ns, price)

        returns = self._compute_returns(t_ns, price)
        volatility = self._compute_volatility(t_ns)
        regime = self.regime.update(volatility)

        now_ms = t_ns // 1_000_000
        trade_stats = self.engine.trade_flow.window_stats(self.trade_flow_window_s, now_ms=now_ms)
        sweep_score = net_sweep_volume(self.engine.recent_sweeps, now_ms=now_ms, lookback_s=self.sweep_lookback_s)
        absorption_score = self._compute_absorption(t_ns, price, trade_stats)

        row = {
            "asset": self.asset,
            "timestamp": t_dt,
            "price": price,
            **returns,
            "volatility": volatility,
            "spread": metrics.get("spread_bps"),
            "obi_1": metrics.get("obi_1"),
            "obi_5": metrics.get("obi_5"),
            "obi_10": metrics.get("obi_10"),
            "obi_25": metrics.get("obi_25"),
            "microprice_delta": metrics.get("microprice_delta"),
            "trade_imbalance": trade_stats.get("trade_imbalance"),
            "buy_volume": trade_stats.get("buy_volume"),
            "sell_volume": trade_stats.get("sell_volume"),
            "trade_velocity": trade_stats.get("trade_velocity"),
            "sweep_score": sweep_score,
            "absorption_score": absorption_score,
            "liquidity_change": metrics.get("depth_change"),
            # Not in docs/06_features_v1.md's original column list, added
            # here (Phase 15): `strategy/strategy_engine.py`'s
            # `_open_position()` (Phase 11) and `strategy/exit.py`'s
            # `_check_liquidity_reversal()` both already read
            # `row.get("liquidity_imbalance")`/`features.get(
            # "liquidity_imbalance")`, but no feature-table column ever
            # populated it — a real, previously-silent integration gap
            # (never crashed anything, since both callers' missing-data
            # convention degrades a missing key to "skip," but it meant
            # `liquidity_reversal` has been a permanently inert exit
            # candidate on every feature row ever built). The value is
            # already computed for free by `OrderBookEngine.metrics()`
            # (the same live engine `wall_persistence`/
            # `transient_liquidity_score` above come from) — simply never
            # copied into this row before. Flagged, not silently patched
            # without a note: see docs/20_live_paper_trading.md.
            "liquidity_imbalance": metrics.get("liquidity_imbalance"),
            "market_regime": regime,
        }
        row.update(self._news_columns(t_dt))
        return row

    def _compute_returns(self, t_ns: int, price: float | None) -> dict:
        out = {}
        for h in self.return_horizons_s:
            key = f"returns_{h}s"
            if h < self.sampling_interval_s:
                # Can't be independently observed at this grain — see the
                # module docstring. None, not a relabeled longer return.
                out[key] = None
                continue
            if price is None:
                out[key] = None
                continue
            past_price = self._prices.at_or_before(t_ns - int(h * 1_000_000_000))
            if past_price is None or past_price == 0:
                out[key] = None
            else:
                out[key] = (price - past_price) / past_price
        return out

    def _compute_volatility(self, t_ns: int) -> float | None:
        window_ns = int(self.volatility_window_s * 1_000_000_000)
        prices = self._prices.window(t_ns - window_ns, t_ns)
        if len(prices) < 3:
            return None
        sample_returns = [
            (prices[i] - prices[i - 1]) / prices[i - 1]
            for i in range(1, len(prices))
            if prices[i - 1]
        ]
        if len(sample_returns) < 2:
            return None
        return statistics.pstdev(sample_returns)

    def _compute_absorption(self, t_ns: int, price: float | None, trade_stats: dict) -> float | None:
        if price is None:
            return None
        window_ns = int(self.trade_flow_window_s * 1_000_000_000)
        price_start = self._prices.at_or_before(t_ns - window_ns)
        if price_start is None:
            return None
        scores = compute_absorption_scores(
            buy_volume=trade_stats.get("buy_volume") or 0.0,
            sell_volume=trade_stats.get("sell_volume") or 0.0,
            price_start=price_start,
            price_end=price,
        )
        return scores["buy_absorption_score"] - scores["sell_absorption_score"]

    def _news_columns(self, t_dt: dt.datetime) -> dict:
        event = self._last_relevant_news
        if event is None:
            return {
                "news_score": 0.0,
                "news_importance": 0.0,
                "news_novelty": 0.0,
                "news_age": None,
                "news_credibility": 0.0,
            }
        # initial_impact/half_life_s are pipeline.py-only ephemeral fields
        # (src/news/pipeline.py) never persisted to the news_events table —
        # reconstructed here with the exact same formula
        # (importance * sentiment) rather than adding storage columns for
        # values that are cheap to recompute and only ever needed here.
        initial_impact = (event.get("importance") or 0.0) * (event.get("sentiment") or 0.0)
        decayed = apply_decay(
            {
                "ingestion_time": event["ingestion_time"],
                "half_life_s": self.news_half_life_s,
                "initial_impact": initial_impact,
            },
            now=t_dt,
        )
        return {
            "news_score": decayed["current_impact"],
            "news_importance": event.get("importance") or 0.0,
            "news_novelty": event.get("novelty") or 0.0,
            "news_age": decayed["age_s"],
            "news_credibility": event.get("credibility") or 0.0,
        }


def find_forward_indices(rows: "list[dict]", horizon_s: float) -> "list[int | None]":
    """For every row `i`, the index of the first row at or after
    `rows[i]["timestamp"] + horizon_s` seconds — the no-look-ahead-safe
    forward lookup `compute_targets()` uses internally to build its
    up/down/flat labels. This is the primitive both `find_forward_prices()`
    (below) and `backtest/event_backtester.py` (Phase 10, which also needs
    the matching row's `timestamp` for trade bookkeeping, not just its
    price) build on, so there is exactly one implementation of "find the
    row h seconds forward" anywhere in the codebase — reusing it rather
    than re-deriving the same two-pointer scan a second time is what keeps
    label computation and backtest exit simulation from silently drifting
    apart. `None` at the tail of `rows` where nothing exists that far in
    the future yet.

    `rows` must be sorted ascending by `timestamp`, matching
    `FeatureEngine.build_rows()`'s output order. O(n) per call (single
    forward-advancing pointer), not O(n^2).
    """
    n = len(rows)
    out: "list[int | None]" = [None] * n
    j = 0
    for i in range(n):
        target_ts = rows[i]["timestamp"] + dt.timedelta(seconds=horizon_s)
        if j < i + 1:
            j = i + 1
        while j < n and rows[j]["timestamp"] < target_ts:
            j += 1
        out[i] = j if j < n else None
    return out


def find_forward_prices(rows: "list[dict]", horizon_s: float) -> "list[float | None]":
    """The `price` at each row's `find_forward_indices()` result — see
    that function's docstring. Thin convenience wrapper kept for
    `compute_targets()`'s own use (it only ever needs the price, not the
    index)."""
    indices = find_forward_indices(rows, horizon_s)
    return [rows[j]["price"] if j is not None else None for j in indices]


def compute_targets(
    rows: "list[dict]",
    horizons_s: "tuple[int, ...]" = DEFAULT_TARGET_HORIZONS_S,
    thresholds_bps: "tuple[float, ...]" = DEFAULT_TARGET_THRESHOLDS_BPS,
) -> "list[dict]":
    """Section 28 target variables, computed forward from each row's own
    `price`/`timestamp`. Deliberately a separate function from
    `FeatureEngine.build_rows()` — see the module docstring's look-ahead
    section for why that separation is what keeps this safe: this is the
    only place forward-looking data is used, it only ever *adds* new
    `target_*` keys, and it's called once, after the full feature table
    already exists, so nothing it computes can feed back into a feature
    column for the same or an earlier row.

    `rows` must be sorted ascending by `timestamp` (as `build_rows()`
    already returns them). Returns new dicts — `rows` is not mutated.
    A horizon with no row far enough in the future yet (end of the
    available data) gets `None` targets rather than a silently wrong 0/1,
    since "no forward return observed" and "forward return was exactly
    flat" are not the same thing.
    """
    n = len(rows)
    out = [dict(r) for r in rows]
    for h in horizons_s:
        fwd_prices = find_forward_prices(rows, h)
        for i in range(n):
            base_price = rows[i]["price"]
            fwd_price = fwd_prices[i]
            if fwd_price is None or base_price is None or base_price == 0:
                forward_return_bps = None
            else:
                forward_return_bps = (fwd_price - base_price) / base_price * 10_000

            for b in thresholds_bps:
                up_key = f"target_{h}s_up_{b}bps"
                down_key = f"target_{h}s_down_{b}bps"
                if forward_return_bps is None:
                    out[i][up_key] = None
                    out[i][down_key] = None
                else:
                    out[i][up_key] = 1 if forward_return_bps >= b else 0
                    out[i][down_key] = 1 if forward_return_bps <= -b else 0
    return out
