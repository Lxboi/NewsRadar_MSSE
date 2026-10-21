# Feature Table v1 (sections 27-29)

Defines the synchronized feature table `feature_engine.py` will build in
Phase 7. This is a specification, not an implementation — Phase 7 is where
`src/features/feature_engine.py` actually computes these.

## Grain

One row per `(asset, timestamp)`, timestamp on a fixed sampling clock (e.g.
every 1s) so market, order-flow and news features are always aligned to the
same instant.

## Columns (section 27)

```text
timestamp
price
returns_1s, returns_5s, returns_10s, returns_30s, returns_60s
volatility
spread
obi_1, obi_5, obi_10, obi_25
microprice_delta
trade_imbalance
buy_volume, sell_volume
trade_velocity
sweep_score
absorption_score
liquidity_change
news_score, news_importance, news_novelty, news_age, news_credibility
market_regime
```

## Target variables (section 28)

For each horizon `h ∈ {30s, 60s, 180s, 300s}` and threshold `b ∈ {5, 10, 15,
25, 50} bps`:

```text
target_{h}_up_{b}bps    = 1 if forward return over h >= +b bps else 0
target_{h}_down_{b}bps  = 1 if forward return over h <= -b bps else 0
```

10s horizon is used for exploratory analysis (section 7) but is likely too
short for a usable target once costs are subtracted — validate empirically
rather than assuming.

## Look-ahead bias — mechanical rules (section 29)

1. Every feature at `timestamp = t` may only use data with
   `ingestion_time <= t`. News features in particular must join on
   `news_events.ingestion_time`, never `news_events.event_time`.
2. Target variables are computed by looking forward from `t` — they are
   labels, not features, and must never leak into the feature columns for
   the same or an earlier row.
3. Any rolling/window computation (volatility, trade_velocity, etc.) is
   backward-looking only (`[t - window, t]`), never centered.
4. `news_age = ingestion_time_of_most_recent_relevant_news - t` (or `t -
   event_time` if that's the definition validated in Phase 5 — decide once,
   document it, keep it consistent across research and live code paths).

## `market_regime`

Categorical, produced by `signals/regime_detector.py`:
`LOW_VOL | NORMAL_VOL | HIGH_VOL | EXTREME_VOL` (section 19). Included as a
feature (models may find regime-conditional edges) and also used
operationally to gate entries (`config/strategy.yaml: entry_filter.allowed_regimes`).

## Versioning

This is `features_v1`. If the column set changes in a way that breaks
comparability with existing backtests, bump to `features_v2` and keep `v1`
data/backtests intact rather than overwriting (see
`docs/05_parquet_schema.md`, schema evolution).
