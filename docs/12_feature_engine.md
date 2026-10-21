# Feature Engine — Implementation Notes (Phase 7)

`docs/06_features_v1.md` is the spec (what the columns mean); this
document is the implementation record — the decisions Phase 7 had to make
to turn that spec into code, and why, so Phase 8+ doesn't have to
re-derive them from the source.

## Grain: raw-tick replay, not the 5s SQLite snapshot cadence

docs/06's grain rule ("one row per (asset, timestamp) on a fixed sampling
clock, e.g. every 1s") is implemented literally: `features/replay.py`
reconstructs the exact tick stream Phase 2-4 already captured to
`data/raw/hyperliquid_l2`/`hyperliquid_trades` Parquet (one row per book
level per snapshot, one row per trade), and `FeatureEngine.build_rows()`
replays it through the same `OrderBookEngine` (Phase 3) the live console
uses, walking a fixed UTC clock (default 1s, configurable) rather than
reusing Phase 4's coarser 5s SQLite snapshot cadence.

This was an explicit choice, not a default: the alternative (reuse the
already-stored 5s snapshots) would have shipped faster but made
`returns_1s` fake precision — a relabeled 5s return, not an independently
observed 1s one. The user chose the raw-tick-replay path when asked
(2026-08-18), on the reasoning that this replay capability is very likely
needed again for Phase 10's backtester, so building it once now avoids
duplicating it later.

**Mechanical consequence**: `FeatureEngine._compute_returns()` returns
`None` (not a computed number) for any horizon shorter than
`sampling_interval_s` — e.g. `returns_1s` is always `None` if you run the
engine at 5s sampling. This is enforced in code, not just documented.

## No-look-ahead enforcement (section 29)

`FeatureEngine.build_rows()` is a single forward pass: at each sample
timestamp `t`, it feeds every market/news event with
`ts_received_ns <= t_ns` / `ingestion_time <= t` into its internal state
(the `OrderBookEngine`, a `RegimeDetector`, a price history, and "most
recent relevant news so far"), then reads off the current row from that
state. No column is ever computed by looking at `market_events[j]` or
`news_events[j]` for a `j` whose timestamp is after the row being built —
structurally, not just by convention, since those events haven't been fed
to any tracker yet at that point in the loop.

Target variables (section 28) are the one deliberate exception — they
*are* forward-looking, by definition, since they're labels. They're
computed by `compute_targets()`, a **separate function** called only
after the full feature table already exists, and it only ever adds new
`target_*` keys — it has no way to feed a forward-looking value back into
a `price`/feature column for the same or an earlier row, because it never
touches the existing columns at all (verified by
`test_compute_targets_does_not_alter_feature_columns` and
`test_compute_targets_never_mutates_input_rows`).

`tests/test_feature_engine.py` has direct property tests for both halves
of this: `test_feature_rows_unaffected_by_later_market_events`/
`test_feature_rows_unaffected_by_later_news_events` (removing/adding a
future event never changes an earlier row), and
`test_compute_targets_truncating_the_tail_only_changes_targets_near_the_cut_not_features`
(truncating the tail changes only the targets that needed the missing
future rows, never any feature column, on the still-present rows).

## Column-by-column decisions not already pinned by docs/06

- **`price`**: mid-price (`(best_bid+best_ask)/2`) from the replayed
  order book, not last trade price — always available once a book
  exists, doesn't depend on trade cadence (a quiet asset with a live book
  but no recent trade still gets a `price`).
- **`spread`**: `spread_bps`, not the absolute spread — consistent with
  the rest of the codebase, which favors bps so BTC/ETH/SOL's very
  different price scales are comparable.
- **`volatility`**: population stdev (`statistics.pstdev`) of consecutive
  per-sample simple returns within a trailing `volatility_window_s`
  (default 60s) window. Backward-looking only, per rule 3.
- **`trade_imbalance`/`buy_volume`/`sell_volume`/`trade_velocity`**: from
  `TradeFlowTracker.window_stats()` (Phase 3) at a single representative
  window, `trade_flow_window_s` (default 10s) — chosen to match
  `orderbook_engine.py: ABSORPTION_WINDOW_S`, and because unlike
  `trade_features`' SQLite table (which stores every window 1-180s),
  docs/06 specifies these as single columns, not one per window.
- **`sweep_score`**: new column, not something Phase 3 already exposed as
  a scalar. Defined in `replay.py: net_sweep_volume()` as the signed sum
  of detected-sweep volumes (`sweeps.py: SweepEvent`, buy positive / sell
  negative) that completed within a trailing `sweep_lookback_s` (default
  30s) window — a narrower signal than `trade_imbalance` (every trade)
  since it only counts trades that formed a detected aggressive burst.
- **`absorption_score`**: single signed column
  (`buy_absorption_score - sell_absorption_score` from
  `orderbook/absorption.py`'s existing pure function), computed with
  `FeatureEngine`'s own price-history time series bracketing
  `trade_flow_window_s` — more precise than
  `OrderBookEngine._absorption_metrics()`'s live approximation (which
  uses the first trade in-window as a price proxy, per its own docstring,
  because the live engine doesn't retain a time-indexed price series;
  `FeatureEngine` does).
- **`liquidity_change`**: `orderbook_engine.py: metrics()["depth_change"]`
  under the spec's column name.
- **`market_regime`**: `signals/regime_detector.py`'s new
  `RegimeDetector`, one instance per asset, fed `volatility` once per
  row. Quantile-adaptive thresholds (25th/75th/95th percentile of its own
  rolling history) rather than a fixed bps cutoff — same
  self-relative-threshold convention `orderbook/liquidity.py` already
  uses for wall detection (section 4: no hardcoded per-asset constants).
  Cold-starts at `NORMAL_VOL` until `min_samples` (default 30)
  observations exist.

  **Bug found via a smoke-test run** (not a unit-test fixture — running
  the real `scripts/build_feature_table.py` end to end against a
  synthetic quiet order book): a long run of *identical* volatility
  observations (e.g. `0.0`, which is exactly what a book with no further
  price movement produces) collapses all three quantile thresholds to
  the same number. A naive `volatility >= extreme_threshold` chain then
  classifies every subsequent identical observation as `EXTREME_VOL` —
  backwards for what is definitionally the least volatile possible
  market state. Fixed in `detect_regime()`: only when thresholds are
  fully degenerate (no spread at all) *and* the new observation exactly
  equals that flat value does it resolve to `NORMAL_VOL`; a genuinely
  different observation against a degenerate history (a real outlier
  after a long flat run) still classifies normally, since the flat
  history is exactly what makes that deviation meaningful. Regression
  tests: `test_detect_regime_degenerate_thresholds_stay_normal_not_extreme`,
  `test_regime_detector_stays_normal_through_a_long_flat_volatility_run`.

## News columns: reconstructing from what's actually persisted

`src/news/pipeline.py` computes `initial_impact = importance * sentiment`
and attaches a `half_life_s` to every scored event — but
`storage/database.py: NewsEvent` never persisted those two fields (they
were prepared "for Phase 6/7's decay-weighted signal use," per that
module's own comment, and Phase 7 is where they're finally consumed).
Rather than add storage columns for values that are trivial to
recompute, `FeatureEngine._news_columns()` reconstructs `initial_impact`
with the exact same formula and applies `event_detector.py: apply_decay()`
directly (not a re-implementation) using a configurable
`news_half_life_s` (default `DEFAULT_HALF_LIFE_S` = 300s, same default
Phase 5 already used).

At each row, `FeatureEngine` tracks "the most recent canonical
(non-duplicate) news event relevant to this asset, ingested at or before
now" — a single event, not an aggregate across several, matching how
`main.py`'s live "recent relevant news" panel already presents this
(Phase 5). Columns:

- `news_score` = that event's decayed `current_impact` (importance ×
  sentiment, decayed by age since `ingestion_time`) — the blended signal.
- `news_importance` / `news_novelty` / `news_credibility` = that event's
  own (undecayed) scored values.
- `news_age` = seconds since that event's `ingestion_time` — resolves
  docs/06 section 4's open question (it listed two candidate
  definitions) by using the convention `event_detector.py: apply_decay()`
  already committed to in Phase 5/6: age from `ingestion_time`, never
  `event_time`, for the same no-look-ahead reason every other join in
  this system uses `ingestion_time`.

Before any relevant news has been ingested for an asset, all five columns
default to `0.0` / `None` (`news_age`) rather than an arbitrary
placeholder — `news_age=None` specifically distinguishes "no news
observed yet" from "news observed exactly now" (`news_age=0.0`), which
Phase 8+ model training should treat differently (e.g. an explicit
`has_recent_news` indicator derived from `news_age is not None`, rather
than silently imputing).

**Relevance/asset-tagging decision**: `NewsEvent.affected_assets` can be
empty even for a genuinely relevant item — e.g. a Fed/macro release
(MACRO/REGULATION categories, section 24) that isn't about any single
coin. `FeatureEngine._is_relevant()` treats an empty `affected_assets`
list combined with `market_relevance > 0` as *market-wide*, applying it
to every asset, rather than silently dropping it. Rationale: a Fed rate
decision moves BTC/ETH/SOL together — discarding it because it isn't
tagged to one symbol would throw away exactly the kind of news this
system's news+microstructure combination is supposed to catch.

## Target variables (section 28)

`compute_targets(rows, horizons_s, thresholds_bps)`: for each row and
horizon `h`, finds the first later row with `timestamp >= row.timestamp +
h` (nearest-at-or-after — this is a label, forward-looking is correct and
intended here), computes `forward_return_bps`, and sets
`target_{h}s_up_{b}bps` / `target_{h}s_down_{b}bps` for every configured
threshold `b`. Defaults match docs/06 exactly: horizons `{30, 60, 180,
300}`s, thresholds `{5, 10, 15, 25, 50}` bps. The 10s horizon docs/06
mentions for exploratory analysis (section 7) is deliberately not in the
default set — callers (e.g. a notebook) can pass `horizons_s=(10, ...)`
explicitly; the function takes the horizon/threshold sets as parameters
rather than hardcoding them.

When a horizon reaches past the end of the available data (no future row
exists yet), both target columns are `None` — never a fabricated `0`,
since "no forward return observed" and "forward return was exactly flat"
are different facts a model needs to be able to tell apart.

## What's not built this phase

- **Not wired into `main.py`'s live asyncio loop.** Feature-table
  building is a batch/research step over already-collected history
  (`scripts/build_feature_table.py`), run on demand — not a continuous
  process. `python -m src.main` still owns collecting the raw data and
  news this reads.
- **No Kraken/second-exchange replay.** Only Hyperliquid ticks exist to
  replay (Phase 2-4 only ever collected from Hyperliquid); this is a
  consequence of what's been built, not a new scope decision.
- **No per-asset feature parity check across BTC/ETH/SOL.** Each asset's
  `FeatureEngine` instance is independent by construction (mirrors
  `OrderBookEngine`'s one-per-asset convention) — there's nothing
  asset-specific hardcoded, but this phase didn't add a dedicated test
  running all three configured assets side by side. Worth adding once
  there's real multi-asset data to validate against, not before.
