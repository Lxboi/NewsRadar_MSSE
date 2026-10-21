# Order Book Engine (sections 12-18, Phase 3)

`src/orderbook/orderbook_engine.py` is a thin orchestrator; every formula
lives in its own single-purpose module next to it so each is independently
testable (see `tests/test_orderbook_*.py`, `tests/test_trade_flow.py`,
`tests/test_liquidity.py`, `tests/test_sweeps_absorption.py` — 41 Phase 3
tests, all deterministic fixtures, no network). One `OrderBookEngine`
instance per asset, fed directly from the parsed events
`collectors/hyperliquid_ws.py` already produces (`l2Book` and `trades`).

## Spread & OBI (section 12) — `imbalance.py`

```text
spread_bps = (best_ask - best_bid) / mid * 10_000
OBI        = (BidVolume - AskVolume) / (BidVolume + AskVolume)
```

Computed at top 1/5/10/25 levels (`obi_1`...`obi_25`), plus a
distance-weighted variant (`obi_weighted`) that discounts levels by
`exp(-decay_per_bps * distance_from_mid_bps)` instead of a hard depth
cutoff, per section 12's "tester également des pondérations selon la
distance au mid-price." `decay_per_bps` is a starting-point constant
(0.01), not a researched value.

## Microprice (section 13) — `microprice.py`

```text
mid_price  = (best_bid + best_ask) / 2
microprice = (best_bid * best_ask_size + best_ask * best_bid_size)
             / (best_bid_size + best_ask_size)
```

Weights each side's *price* by the *opposite* side's size — more size
resting on the ask pulls microprice toward the ask. `MicropriceHistory`
keeps a short rolling window so `microprice_delta` (first difference) and
`microprice_acceleration` (second difference) are available every
snapshot. Comparing microprice's predictive power against last_price/mid
across the 10s-180s horizons (section 13) is exploratory-analysis work for
`notebooks/`, once Phase 4 (storage) is capturing history to analyze —
this module only computes the instantaneous values.

## Order flow (section 14) — `trade_flow.py`

`TradeFlowTracker` keeps a rolling deque of trades and computes, per
window in `{1, 5, 10, 30, 60, 180}` seconds: buy/sell volume, buy/sell
ratio, trade imbalance, signed volume, trade velocity (trades/sec), average
trade size, and large-trade frequency. "Large trade" is relative (average
trade size x a multiplier, default 5x, needs >= 20 samples to be
meaningful) rather than a hardcoded absolute size, so the same code works
across assets with very different price/size scales without per-asset
tuning. `volume_acceleration(short_s, long_s)` compares the short-window
rate against the long-window rate.

**Side convention**: Hyperliquid's `trades.side` is `"B"` (aggressor
bought) or `"A"` (aggressor sold) — cross-checked during Phase 2's live
run (a burst of "B" trades coincided with a rising mid price, consistent
with "B" = aggressive buying). Re-verify if Hyperliquid changes this.

## Liquidity & transient liquidity (sections 15-16) — `liquidity.py`

A "wall" is a book level whose size is >= `wall_size_multiplier` (default
3x) times the book's own current average level size — relative, not a
hardcoded size, for the same cross-asset reason as large-trade detection.
`LiquidityTracker` tracks each wall's lifetime across snapshots and reports:

- `wall_persistence` — average age (seconds) of currently-active walls
- `wall_cancellation_rate` — fraction of walls seen this update that
  disappeared since the last one
- `depth_change` — change in total depth within `near_mid_bps` (default
  50bps) of mid, snapshot to snapshot
- `liquidity_imbalance` — same OBI-style ratio, but over near-mid *depth*
  rather than raw top-of-book size
- `transient_liquidity_score` — fraction of the last 20 *completed* walls
  that persisted less than `transient_persistence_s` (default 2s) before
  vanishing

Per section 16, this is explicitly **not** a spoofing detector — it's a
descriptive score of how often recently-seen large liquidity turned out to
be short-lived. No claim about intent is made anywhere in this code.

## Sweep detection (section 17) — `sweeps.py`

A sweep is a burst of same-side trades arriving within `burst_window_ms`
(default 2000ms) of each other, with at least `min_trades` (default 3).
For each detected burst: volume, trade count, duration, price
displacement in bps, and — if a pre-burst book snapshot is available —
`levels_crossed` (how many resting price levels on the consumed side fall
within the burst's price range). Whether a sweep predicts continuation vs.
reversal ("réaction post-sweep") is Phase 8+ statistical work against
forward returns — this module only detects and describes the event.

## Absorption (section 18) — `absorption.py`

```text
buy_absorption_score  = buy_volume  / (price_displacement_bps + epsilon)
sell_absorption_score = sell_volume / (price_displacement_bps + epsilon)
```

High aggressive volume with low price displacement scores high — i.e.
"the book absorbed the aggression." `orderbook_engine.py` currently feeds
this from the last `ABSORPTION_WINDOW_S` (10s) of trade flow vs. the
current mid as a live approximation; Phase 7's `feature_engine.py` will
compute the exact windowed version once historical mids are stored
(Phase 4). Per section 18, this module does not decide whether absorption
means continuation, exhaustion, or reversal — "les données doivent
décider," tested empirically in Phase 8+.

## What Phase 3 does not do

No persistence (Phase 4), no cross-referencing with news (Phase 5-6), no
feature-table assembly with proper historical alignment (Phase 7, which is
where the no-look-ahead discipline from `docs/06_features_v1.md` gets
enforced against these exact metrics) — Phase 3 only computes correct,
tested, live-updating microstructure metrics from the raw Hyperliquid
stream and surfaces them in the console display.
