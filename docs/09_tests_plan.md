# Tests Plan (Phase 1 baseline, growing every phase)

Current total: **641 tests**, all offline (no network — RSS fetch logic
is tested via `httpx.MockTransport`, WebSocket parsing via fixtures, real
DB/Parquet I/O against `tmp_path`, and `KrakenExecutor`'s real transport
against a mocked `httpx.post`).

## Phase 1 — what's tested today

`tests/test_config.py` (real, runs today):

- Every `config/*.yaml` file parses and contains the keys `main.py`
  actually depends on (`trading_mode.default`, enabled `assets`, etc.).
- `trading_mode.default` is `RESEARCH` — a regression here would silently
  change the system's safety posture, so it's asserted explicitly.
- `.env.example` exists and defines every variable `settings.py`/`main.py`
  reads from the environment, so a missing var fails fast in CI instead of
  at 3am on the Raspberry Pi.

`tests/test_hyperliquid_ws.py` (real, runs today, no network — schema
confirmed against Hyperliquid's live docs, Phase 2):

- `ReconnectPolicy.delay_for_attempt` respects `base_delay_s`/`max_delay_s`
  bounds; `heartbeat_timeout_s` stays above the collector's own ping
  interval (regression guard for a bug caught during Phase 2 testing).
- `parse_hyperliquid_message()` correctly parses `l2Book`/`trades`/
  `candle`/`subscriptionResponse`/`pong` fixtures matching the confirmed
  schema, and surfaces (without crashing on) unknown channels.
- `HyperliquidWebSocketCollector._handle_message` enqueues a parsed,
  timestamped event for well-formed JSON and drops malformed frames.

`tests/test_main_asset_state.py` (real, runs today, no network):

- `AssetState.mid`/`spread_bps` math and staleness detection.
- `market_state_consumer` updates the right asset from queued events and
  ignores events for unconfigured assets.

`tests/test_orderbook_imbalance_microprice.py`,
`tests/test_trade_flow.py`, `tests/test_liquidity.py`,
`tests/test_sweeps_absorption.py`, `tests/test_orderbook_engine.py` (real,
runs today, no network — Phase 3, 41 tests):

- OBI at multiple depths and the distance-weighted variant against
  hand-built order books with known imbalance.
- Microprice weighting direction and mid/microprice equivalence when sizes
  are symmetric; `MicropriceHistory` delta/acceleration arithmetic.
- Trade flow window stats (buy/sell volume, imbalance, velocity, avg size,
  large-trade frequency, volume acceleration) against synthetic trade
  sequences with hand-computed expected values, including window pruning.
- Liquidity wall lifecycle (detected -> persists -> cancelled),
  `wall_cancellation_rate`, `depth_change`, `liquidity_imbalance`, and
  `transient_liquidity_score` across a scripted snapshot sequence with a
  known-transient and a known-persistent wall.
- Sweep detection: burst grouping, breaks on side-change/time-gap,
  `min_volume` filtering, and `levels_crossed` against a fixture book.
- Absorption scores: high-volume/low-displacement scores higher than
  low-volume/high-displacement; zero-price-start doesn't crash.
- `OrderBookEngine.on_l2book`/`on_trades` end-to-end wiring into a single
  `metrics()` dict with all expected keys present.
- Regression test for a real bug caught while building Phase 4:
  `LiquidityTracker.update()`'s return value was computed but never merged
  into `metrics()`, so `liquidity_imbalance`/`transient_liquidity_score`
  silently stayed `None` in the live display. Fixed; test asserts these
  fields are populated after a snapshot.

`tests/test_parquet_store.py`, `tests/test_database_storage.py`,
`tests/test_main_storage_wiring.py` (real, runs today, no network —
Phase 4, 20 tests, all using `tmp_path` for isolation):

- `ParquetStore`: buffers below the flush threshold, auto-flushes at
  threshold, correct Hive-style partition paths
  (`<dataset>/asset=<SYM>/date=<YYYY-MM-DD>/part-NNNNN.parquet`), separate
  partitions per asset/dataset, part-file numbering, no-op flush with
  nothing pending.
- `SQLiteRecorder`/insert helpers: `init_db` creates all 9 tables and is
  idempotent; `record_market_data`/`record_orderbook_features`/
  `record_trade_features` round-trip correctly and coalesce `None` metric
  values to `0.0` instead of crashing; `write_asset_snapshot` commits one
  `market_data` + one `orderbook_features` row plus one `trade_features`
  row per configured window, in a single transaction.
- `main.py` wiring: `market_state_consumer` persists raw l2Book levels and
  trades to Parquet when given a store (and works unchanged when given
  `None`, so Phase 2/3 behavior isn't broken); `sqlite_snapshot_writer`
  skips assets with no book yet and writes once one exists.
- Also manually smoke-tested end-to-end outside pytest: a synthetic
  snapshot+trade sequence through the real queue consumer produced correct
  rows in both a real SQLite file and real, independently-readable Parquet
  files.

## Phase 5 — what's tested today (79 new tests, 165 total)

`tests/test_rss_collector.py` (real, runs today, no network —
`httpx.MockTransport` simulates every HTTP response):

- `normalize_entry()`: field extraction, stable/deterministic `event_uid`
  hashing, falls back to ingestion time when a feed entry has no parseable
  publish date.
- `BoundedSeenSet`: containment, oldest-first eviction past capacity.
- `RobotsCache`: allows when no `robots.txt` is served (404), disallows
  when `robots.txt` explicitly blocks the feed path, fails open (allows)
  if `robots.txt` itself is unreachable.
- `RSSCollector.poll_feed_once`: enqueues new items from a mocked feed
  response, skips already-seen items on a repeat call, returns the `-1`
  sentinel (and enqueues nothing) when `robots.txt` disallows the feed;
  `run()` with zero configured feeds returns immediately rather than
  hanging.

`tests/test_news_deduplication.py`, `test_news_scoring.py`,
`test_news_decay.py`, `test_news_pipeline.py` (real, runs today, no
network — sections 22-26):

- Title normalization/similarity; `deduplicate_events()` batch function
  collapses known-duplicate fixture articles into one canonical event and
  respects `time_window_s`; `DeduplicationTracker`'s streaming behavior
  matches batch semantics and its bounded history evicts oldest-first.
- `classify_event()` assigns the expected category for one labeled
  fixture per category (including the `OTHER` fallback) and resolves
  keyword-overlap priority correctly (e.g. "hack" beats "exchange").
- `score_sentiment()`: bullish fixture > 0, bearish fixture < 0, no-hit
  fixture == 0.0 exactly, always in [-1, 1].
- `score_relevance()`: zero relevance with no keyword hits, correct
  `affected_assets` extraction (including multi-asset), caps at 1.0.
- `score_credibility()`: known source (case-insensitive), unknown source
  and no-config both fall back to the documented default.
- `score_novelty()`: empty history == 1.0, near-duplicate history scores
  low, unrelated history stays high, accepts both dict and plain-string
  history items.
- `apply_decay()`: age 0 returns full impact, one half-life exactly halves
  it, an arbitrary age matches the closed-form `exp()` formula bit-for-bit
  (`pytest.approx`), does not mutate its input.
- `NewsPipeline.process()`: end-to-end field completeness, credibility
  pulled from configured feed list, irrelevant articles score
  relevance/importance at exactly 0.0, first-seen events are fully novel,
  a near-duplicate is flagged with the canonical's `event_uid` and scores
  low novelty, input dict is never mutated.

`tests/test_main_news_wiring.py` (real, runs today, no network):

- `news_consumer`: relevant items land in both `news_events` (SQLite) and
  `news_raw` (Parquet, `asset=None` partition); irrelevant items are
  persisted to Parquet only (not SQLite); works unchanged when
  `parquet_store=None`; a duplicate is stored in SQLite (linked via
  `duplicate_of`) but does not appear a second time in the live-display
  feed.
- `news_live_display`: doesn't crash on empty history; logs recent item
  titles at the configured interval.

`tests/test_parquet_store.py`, `tests/test_database_storage.py` (extended,
Phase 5 additions):

- `ParquetStore`: `asset=None` writes skip the `asset=` path segment
  entirely and coexist correctly with asset-keyed datasets in the same
  store.
- `record_news_event()` / `SQLiteRecorder.write_news_event()`: insert,
  idempotency on `event_uid` (no duplicate row), `duplicate_of` string
  UID correctly resolved to the canonical row's integer `id`, and graceful
  fallback to `duplicate_of=None` when the canonical UID doesn't exist in
  the table (e.g. it was filtered out before being written).

**Known sandbox limitation** (same pattern as Phase 2): live RSS fetches
were not validated from the cloud sandbox — its network proxy returns
`403 Forbidden` on outbound HTTPS to arbitrary hosts. Real feed content
was fetched and verified via targeted research calls during design (see
`docs/10_news_radar.md`), and every RSS/scoring code path is tested
offline via `httpx.MockTransport` fixtures — but confirming actual
articles land in `news_events` needs `python -m src.main` run on the
user's machine.

## Phase 6 — what's tested today (7 new tests, 172 total)

`tests/test_clock_discipline.py` (real, runs today, no network — real
SQLite I/O against `tmp_path`, same pattern as `test_database_storage.py`;
see `docs/11_news_market_sync.md`):

- `normalize_entry()`/`_parse_event_time()`: both `event_time` and
  `ingestion_time` are always tz-aware and equal to `UTC`.
- Hyperliquid's `_handle_message`: `ts_received` is plain epoch-
  nanoseconds (`time.time_ns()`), bracketed between two real calls to the
  same clock — inherently UTC, no tzinfo ambiguity possible.
- Regression test for the real bug found during this phase's audit: a
  `NewsEvent` round-tripped through a real SQLite file previously lost
  `tzinfo` on read-back (`DateTime(timezone=True)` isn't enforced by
  SQLite); the new `UTCDateTime` type keeps both `event_time` and
  `ingestion_time` tz-aware and bit-for-bit equal to the value written.
- `UTCDateTime.process_bind_param` rejects a naive datetime at insert
  time rather than silently accepting it.
- The no-look-ahead as-of-visibility property, against the real
  `NewsEvent` schema and `record_news_event()` insert path (not a mock):
  "visible as of time t" depends only on `ingestion_time`, never
  `event_time`, using synthetic events with deliberately disagreeing
  event_time/ingestion_time pairs — and is invariant to insertion order
  (same event set inserted in original, shuffled, and reversed order all
  produce the identical visible set at a fixed `as_of`).

This phase needed no live-run validation — the whole audit (every
timestamp's provenance, the SQLite bug, the join-safety property) is
reproducible offline.

## Phase 7 — what's tested today (45 new tests, 217 total)

`tests/test_parquet_reader.py` (real, runs today, no network — real
Parquet I/O against `tmp_path`):

- Round-trips rows written by `ParquetStore` back out via
  `read_partition`/`read_partitions`, including the `asset=None` (news)
  layout; missing partitions return an empty list, not an error;
  multi-date concatenation sorts correctly by `ts_received_ns`;
  `date_range()` inclusive bounds and rejects `end < start`.

`tests/test_replay.py` (real, runs today, no network):

- `build_market_event_stream()`: reconstructs one `l2book` snapshot event
  from per-level raw rows (re-sorted best-first regardless of write
  order), groups trades sharing the same `ts_received_ns` back into one
  batch (re-sorted by exchange timestamp), sorts the combined stream
  chronologically, and returns an empty stream when nothing was
  collected.
- `net_sweep_volume()`: nets buy/sell sweep volume within a lookback
  window, zero on empty history, ignores sweeps outside the window
  (including the defensive "sweep after now" case that can't arise from a
  real forward replay).

`tests/test_regime_detector.py` (real, runs today, no network):

- `detect_regime()`'s pure classification at each boundary; `None`
  volatility and invalid quantile ordering handled; cold-start stays
  `NORMAL_VOL` until `min_samples`; classifies a genuine outlier as
  `EXTREME_VOL` after warm-up; the current observation is never included
  in its own thresholds.
- Regression tests for the real bug found via smoke-testing (see
  `docs/12_feature_engine.md`): degenerate (zero-spread) thresholds stay
  `NORMAL_VOL` rather than misclassifying a flat market as `EXTREME_VOL`,
  both at the pure-function level and through 200 consecutive
  `RegimeDetector.update()` calls on constant input.

`tests/test_feature_engine.py` (real, runs today, no network — hand-built
synthetic tick/news sequences, sections 27-29):

- Grain: one row per sampling tick; `price` reflects the most recent
  snapshot at or before each tick; OBI/microprice/trade-flow columns
  populate correctly as ticks are fed in.
- Returns: `None` before enough history exists; a 5s return matches a
  hand-computed value once available; a return horizon shorter than the
  configured sampling interval is always `None` (grain-honesty guard —
  never a relabeled longer return).
- News join: columns stay zeroed before any relevant news; populate
  exactly at `ingestion_time` (not before); a news item's self-reported
  `event_time` is proven *not* to leak into the gate (a backdated
  `event_time` with a late `ingestion_time` still doesn't appear early);
  news tagged to a different asset is ignored; broad macro news with no
  asset tag but real relevance still applies market-wide; zero-relevance
  untagged news is ignored.
- **No-look-ahead property tests** (the ones docs/09 promised for this
  phase): feature rows are provably unaffected by later market events or
  later news events (build with vs. without a future event, assert
  identical rows up to the cutoff); shuffling the input event order
  reproduces identical output (defensive sort inside `build_rows()`).
- `compute_targets()`: matches a hand-computed forward-return example;
  `None` (not a fabricated 0) when a horizon reaches past the available
  data; never mutates its input rows; never alters any feature column;
  truncating the tail changes only the targets that needed the missing
  future rows, never any feature column on the surviving rows.

**No live-run validation needed this phase** (unlike Phases 2/4/5) — the
whole feature engine is deterministic replay over already-collected data,
fully reproducible offline. `scripts/build_feature_table.py` was
smoke-run end to end against synthetic data during development (not part
of the automated suite) specifically to catch integration issues unit
tests in isolation wouldn't — it's what caught the `RegimeDetector`
degenerate-threshold bug above, and a `ParquetStore.flush_every` sizing
issue (default would have written ~170 tiny part files/asset/day at 1s
sampling; the build script now uses a larger buffer, see
`docs/12_feature_engine.md`).

## Phase 8 — what's tested today (21 new tests, 238 total)

`tests/test_baseline_model.py` (real, runs today, no network — pure
computation over hand-built feature dicts):

- All 7 declared kinds present in `BASELINE_KINDS`.
- Momentum/OBI passthrough respects a configured horizon/level (not just
  the default); trade_imbalance/news passthroughs are exact.
- Missing input features collapse to `0.0` rather than crashing, for
  every kind including both combination baselines.
- `volatility_regime`: passes momentum through unchanged in
  LOW_VOL/NORMAL_VOL, sign-flips it in HIGH_VOL/EXTREME_VOL, and defaults
  to trend-following (passthrough) when `market_regime` is missing
  entirely.
- Both combination baselines (`order_flow_momentum`/`news_order_flow`)
  match the documented unweighted-sum formula exactly.
- An unknown `kind` raises `ValueError`.

`tests/test_baseline_evaluation.py` (real, runs today, no network):

- Flat-target rows and zero-signal rows are excluded from scoring (not
  silently counted as correct or incorrect).
- Missing target columns (`None`) are treated as "not yet resolvable",
  excluded rather than crashing; a row with both up and down targets set
  (which `compute_targets()` should never actually produce) raises
  rather than silently picking one.
- `evaluate_all_baselines()` covers every kind in `BASELINE_KINDS` order.
- **The two tests that actually matter**: a perfectly-correlated-by-
  construction baseline (100% accuracy) correctly rejects the coin-flip
  null (`beats_coin_flip is True`, `p_value < 0.001`); an exact-50/50-by-
  construction baseline (deliberately built, not randomly sampled, so
  this can't flake) correctly does **not** reject it
  (`beats_coin_flip is False`, `p_value > 0.05`) — a significance check
  that can only ever say "yes" isn't testing anything.

This phase's tests are entirely offline (hand-built synthetic feature/
target dicts) — no live-run or dedicated script needed, unlike Phase 7.
An ad hoc integration smoke test (`FeatureEngine` -> `compute_targets()`
-> `evaluate_all_baselines()` on synthetic tick data, not part of the
automated suite) confirmed the three phases' outputs actually wire
together correctly — column names, threshold-bps string formatting, and
all — before calling this done.

## Phase 9 — what's tested today (57 new tests, 295 total)

`tests/test_feature_matrix.py` (real, runs today, no network — pure
computation over hand-built row dicts):

- `feature_columns()` excludes `asset`/`timestamp`/`price` and any
  `target_*`-prefixed column, is the *union* of columns across all rows
  (not just the first row's keys), is sorted/deterministic, and returns
  `[]` for an empty row list.
- `rows_to_frame()` preserves the requested column order, fills a missing
  key with `NaN` rather than raising, and handles an empty row list.
- `extract_labels()` matches `evaluate.actual_direction()`'s
  up/down/flat/unresolved semantics exactly, is confirmed (via a source-
  inspection regression test) to actually import and call that shared
  function rather than carrying its own copy, and propagates the
  `ValueError` `actual_direction()` raises on a corrupt row with both
  up and down set.

`tests/test_logistic_model.py` / `tests/test_tree_model.py` (real, runs
today, no network — deterministically-constructed, not randomly sampled,
synthetic feature/target rows so accuracy assertions can't flake):

- `fit()` raises `ValueError` on a row set with no resolvable target;
  `predict_proba()` before `fit()` raises `RuntimeError`.
- Both models learn a trivially linearly-separable relationship
  (`obi > 0 -> up`, `obi < 0 -> down`) correctly.
- `predict_proba()` output: exactly `{"probability_down",
  "probability_neutral", "probability_up"}` keys, each in `[0, 1]`,
  summing to `1.0`.
- Missing numeric feature values don't crash either model — median
  imputation for logistic, native passthrough (no imputer at all,
  confirmed by inspecting the fitted `ColumnTransformer`'s `"numeric"`
  step) for the tree model.
- An unseen `market_regime` category at prediction time doesn't raise
  (`OneHotEncoder(handle_unknown="ignore")`).
- Rows with an unresolved/flat target are excluded from training, not
  errored on.
- The model's own recorded `feature_columns_` excludes identifiers and
  target columns, matching `feature_matrix.feature_columns()`.

`tests/test_calibration.py` (real, runs today, no network):

- `_multiclass_brier_score()`: exactly `0.0` for a perfect, fully
  confident forecast; strictly positive and reproduces the hand-worked
  value for a confidently wrong one.
- `calibration_curves()` returns exactly the three documented keys.
- **`_safe_roc_auc_macro()` / the sklearn 1.8 regression**: returns
  `None` (not `nan`) when a class is entirely absent from the labels —
  both a direct unit test and an end-to-end reproduction
  (`test_calibrate_roc_auc_macro_is_none_not_nan_when_validation_set_misses_a_class`)
  that trains a real model and evaluates it against a validation slice
  engineered to never produce a "down" target, the exact scenario
  smoke-testing found broken before this fix — and returns a real float
  in `[0, 1]` when all three classes are present.
- `calibrate()`: correct `n_scored`, non-negative Brier/log-loss, and
  raises `ValueError` on a validation set with no resolvable targets.
- `fit_calibrated()`: requires a fitted model (`RuntimeError` otherwise),
  works for both `"sigmoid"` and `"isotonic"`, returns a
  `CalibratedDirectionModel` whose `predict_proba()` matches the same
  three-key/sums-to-1 contract as the uncalibrated models, and that
  output can itself be fed back into `calibrate()` — calibrated and
  uncalibrated models are interchangeable to any caller.

`tests/test_model_registry.py` (real, runs today, no network — real
`tmp_path` JSONL files, not mocked I/O):

- Constructing a registry creates missing parent directories and the
  file itself.
- `register()` persists exactly what was passed, rejects a duplicate
  `model_version`, and defaults `calibration`/`notes` to `None` when
  omitted.
- `list_models()` filters correctly by any subset of
  `asset`/`horizon_s`/`threshold_bps`.
- `promote()` rejects an unregistered `model_version`, an empty/
  whitespace-only `reason`, and a `model_version` registered under a
  *different* `(asset, horizon_s, threshold_bps)` key than the one being
  promoted.
- `current_promoted()` is `None` before any promotion, returns the full
  `register()` record (not a bare version string) after one, and follows
  the most recent of multiple promotions for the same key.
- `list_promotions()` returns the full promotion history in order, not
  just the latest.
- Promoting never rewrites or removes the original `register()` line
  (append-only, verified by comparing file contents before/after).
- Two independent `ModelRegistry` instances pointed at the same file
  agree (no caching — every read re-parses from disk).

This phase's smoke-testing (900 rows of synthetic feature+target data,
both models, both calibration methods, train/validation/test split) is
what found the `roc_auc_macro`/`nan` bug covered above — see
`docs/14_statistical_models.md` for the full account of why a unit test
written before the bug was known would not have caught it.

## Phase 10 — what's tested today (69 new tests, 364 total)

`tests/test_feature_engine.py` gained 5 regression tests for the
Phase 10 extraction of `find_forward_indices()`/`find_forward_prices()`
out of `compute_targets()` (real, runs today, no network): the extracted
function's output matches the exact forward price
`test_compute_targets_matches_hand_computed_forward_return()` already
verified by hand, `None` at the tail when the horizon exceeds available
data, the forward index is monotonic non-decreasing across rows (the
two-pointer invariant), empty input handled, and a source-inspection
guard confirming `compute_targets()` actually delegates to
`find_forward_prices()` rather than carrying a second copy of the scan.

`tests/test_backtest_metrics.py` (real, runs today, no network — hand-
built trade dicts):

- Empty trade list returns zeros/`None`s, never raises.
- Aggregate sums (gross/net PnL, fees, slippage) match hand-computed
  totals; win rate's denominator excludes exact-breakeven trades; profit
  factor is `None` (not `inf`) with zero losing trades and matches a
  hand-computed ratio otherwise.
- Sharpe/Sortino are `None` with fewer than 2 trades, `None` on zero
  variance (would otherwise divide by zero), `None` for Sortino with
  fewer than 2 losing trades (not enough downside sample to speak of);
  `sharpe_annualized` differs from the raw per-trade `sharpe` once a
  real time span exists to derive an empirical trade frequency from.
- Max drawdown matches a hand-computed equity path (including the
  "new low after a partial recovery" case) both in absolute and
  percent-of-peak terms; average holding time matches hand-computed
  seconds.
- `bootstrap_confidence_intervals()`: raises on empty input, is
  deterministic given a `random_state`, and its resampled mean tracks
  the actual realized total within tolerance.
- `monte_carlo_reshuffle()`: raises on empty input, never reports a
  total/net PnL figure (order-invariant by construction — this is
  asserted directly, not just implied), `percentile_of_actual` is always
  in `[0, 100]`, and a hand-constructed provably-worst-case ordering
  (all losses, then all wins) shows an actual drawdown at or above the
  shuffled distribution's mean.
- `probability_of_ruin()`: raises on empty input, is exactly `0.0` for
  an all-small-wins trade sequence, and is `> 0.5` for a trade sequence
  engineered to breach the ruin threshold almost immediately.

`tests/test_event_backtester.py` (real, runs today, no network —
deterministic uptrend/downtrend synthetic price paths, never randomly
sampled, so PnL-sign assertions can't flake):

- `decisions_from_model()`: argmax over the three probability classes,
  a neutral call has no `expected_value_bps`, and the EV formula matches
  a hand-computed value. `decisions_from_baseline()`: direction matches
  the baseline signal's sign, probability/EV are always `None`.
- Long-only in a steady uptrend and short-only in a steady downtrend are
  both gross-profitable (the fundamental sanity check any backtester
  must pass); `net_pnl == gross_pnl - fees - slippage` exactly, with
  fees/slippage matching the documented entry+exit cost formula.
- Each entry-filter check blocks trades independently when it should
  (expected value below round-trip cost, probability below minimum,
  spread above maximum, disallowed regime) — and a row simply missing a
  filtered column (no `spread`/`market_regime` key at all) does **not**
  block entry, only an explicit violating value does.
- `max_open_positions=1` never produces overlapping trades;
  `max_open_positions=2` allows at least as many trades as `=1` on the
  same decisions.
- No trade at the tail where there isn't enough forward data to resolve
  an exit price; mismatched `rows`/`decisions` lengths raise `ValueError`;
  empty input returns an empty trade list, not a crash.

`tests/test_orderbook_backtester.py` (real, runs today, no network):

- `strip_news_columns()` removes every `news_*` key (bracket access
  raises `KeyError` afterward, not a silent `None`), preserves every
  non-news key exactly, never mutates its input, and handles an empty
  list.
- `OrderBookBacktester.run()` still produces gross-profitable trades from
  a news-bearing uptrend fixture once stripped, with the identical cost
  formula `EventBacktester` itself uses.

`tests/test_walk_forward.py` (real, runs today, no network):

- `classify_window_regime()`: `None`s on empty input; bull/bear/sideways
  correctly recovered from a window's realized return against the trend
  threshold; volatility majority-vote correctly collapses Phase 7's
  per-row `market_regime` to low/high; news-shock correctly flagged only
  above the configured absolute-score threshold.
- Constructor rejects non-positive window/step sizes.
- `build_windows()`: empty/too-short input returns `[]`; splits are
  strictly chronological with no overlap between slices; a full
  `horizon_s` is purged from the trailing edge of both `train` and
  `validation` (verified against the exact boundary timestamps, not just
  "some purging happened"); windows advance by `step_s`, not by a full
  window's width.
- `run()`: empty input returns zero windows; a `fit_fn` returning `None`
  (or an empty `train` slice) skips evaluation for that window without
  crashing and reports zero-trade metrics; an always-long decision
  function in a synthetic uptrend produces net-profitable out-of-sample
  results and a `summary.test_net_pnl_by_trend` breakdown that correctly
  attributes it to the `"bull"` label; `fit_fn` is called **exactly once
  per window**, not once per slice — the regression test that caught the
  double-fitting bug described in `docs/15_backtesting.md` (an early
  implementation fit the model separately for validation and test,
  caught by this test failing `assert 6 == 3` on a 3-window run, fixed
  to fit once and reuse the model for both).

A full end-to-end integration smoke test (a real fitted
`LogisticDirectionModel` -> `decisions_from_model()` ->
`EventBacktester.run()` -> `compute_metrics()`/
`bootstrap_confidence_intervals()`/`monte_carlo_reshuffle()`/
`probability_of_ruin()`, and separately a baseline through the same
pipeline, against ~2000 synthetic feature+target rows, not part of the
automated suite) confirmed every module's trade-dict/metrics-dict shape
composes with the next without a translation step, before calling this
phase done.

## Phase 11 — what's tested today (107 new tests, 471 total)

`tests/test_direction.py` (real, runs today, no network) —
`direction_from_probabilities()`, the shared argmax primitive:

- Up/down/neutral each win when strictly highest, returning the correct
  `(direction, probability)` pair.
- Tie-breaking matches `CLASS_ORDER = (1, -1, 0)` exactly: an exact
  three-way tie resolves `up`; a down/neutral tie resolves `down`; an
  up/down tie resolves `up`.
- A `proba` dict missing one of the three required keys raises
  `KeyError` rather than silently treating the missing class as
  probability `0`.

`tests/test_confidence.py` — `compute_confidence()`'s three
independently missing-tolerant components:

- A fully empty `signal` returns exactly the neutral `0.5`.
- Perfect calibration + zero data age + full three-column confirmation
  scores `~1.0`; a model *measured* at chance level (`roc_auc_macro=0.5`)
  scores strictly below a model with *no* calibration diagnostic at all
  (unmeasured is not penalized; measured-as-useless is).
- Calibration input is clamped before rescaling (an out-of-range
  `roc_auc_macro` above 1.0 can't push the overall score past 1.0).
- Freshness decays linearly with age (`1.0` at `age=0`, `0.5` at the
  midpoint, `0.0` at or past `max_data_age_s`) and defaults to neutral
  when `now` or `timestamp` is missing.
- Confirmation is neutral when the signal itself is neutral or no
  confirmation columns are available; a `0`-valued column casts no vote
  (neither confirms nor disconfirms); partial agreement is fractional.
- Weights need not sum to `1.0` (normalized by their own total); an
  all-zero weight dict raises `ValueError`; every result stays within
  `[0.0, 1.0]`.

`tests/test_scoring.py` — `classify_signal_quality()`'s A/B/C/NO_TRADE
boundaries:

- High/mid/weak conviction each grade correctly; below the `C` floor
  grades `NO_TRADE`.
- A neutral argmax is `NO_TRADE` even when `probability_neutral` is very
  high — quality grades directional conviction, never confidence in
  inaction.
- Every threshold boundary is exercised exactly at and just below the
  configured cutoff (inclusive lower bound).
- Custom thresholds override the defaults; the function works directly
  on a bare `predict_proba()` row, not just `SignalEngine`'s output
  shape.

`tests/test_signal_engine.py` — `SignalEngine`'s full section-33 payload:

- `build_signals()` batches through `model.predict_proba()` exactly
  once for N rows (not once per row); `build_signal()`'s single-row
  output matches `build_signals()`'s.
- `expected_move`'s sign matches the model's directional lean;
  `expected_value` is `None` for a neutral signal and, for a directional
  one, equals `abs(expected_move) - round_trip_cost_bps` exactly.
- `round_trip_cost_bps` matches `EventBacktester`'s identically-named
  property's formula.
- `confidence` is always populated and within `[0, 1]`;
  `features_snapshot` carries the full input row by reference; `asset`
  falls back to the engine's configured default when a row omits it;
  `model_version`/`horizon_s`/`threshold_bps` are stamped onto every
  signal.

`tests/test_entry.py` — `should_enter()`, the live counterpart to
`EventBacktester._passes_entry_filter()`:

- A neutral signal never enters regardless of every other check.
- `risk_state.blocked` (and its absence, treated as not-blocked) is
  respected.
- Probability/EV/spread/regime each block correctly when violated and
  are inclusive at their boundary; each is skipped (never blocks) when
  its underlying data is missing, and can be disabled outright by
  passing `None`.
- Freshness blocks a stale signal when `now` is given, passes a fresh
  one, and is skipped entirely (not a violation) when `now` isn't given
  — the backtest-replay case.
- Both long and short directional calls are evaluated correctly.

`tests/test_exit.py` — `should_exit()`'s 7 candidates in
`strategy.yaml: exit_candidates` config order:

- Each of `fixed_tp`, `fixed_sl`, `volatility_based_sl`,
  `probability_reversal`, `order_flow_reversal`, `liquidity_reversal`,
  `time_stop` fires correctly for both long and short positions where
  applicable, and each degrades to "no exit" (never "trigger by
  default") when its required data — price, volatility, a fresh
  model score merged into `features`, entry-time OBI/liquidity
  snapshots, `opened_at` — is missing.
- A position rigged so two candidates would independently fire (a
  `fixed_tp`-qualifying move that's also past `time_stop`) returns the
  earlier candidate in `CANDIDATE_ORDER`, confirming first-match-wins is
  real, not incidental.
- A position within every bound returns `None`.

`tests/test_strategy_engine.py` — `StrategyEngine`'s stateful
orchestration:

- Opens on a qualifying signal, stays flat on a neutral one, closes on
  a triggered exit reason.
- `max_open_positions` is respected across ticks when nothing closes;
  a position that closes mid-tick correctly frees room for a new one to
  open on that *same* `process_row()` call.
- A closed position's `net_pnl`/`fees`/`slippage` match calling
  `simulate_fill_costs()` directly with the same entry/exit/direction/
  quantity — the same shared formula, not a re-derivation.
- A `risk_state` dict blocks new entries; a callable `risk_state` is
  re-resolved fresh on every call (not cached from construction).
- A fake `executor` stub receives `place_order()` on both open and
  close with the correct opposing side when provided; the engine runs
  fully self-contained (no executor calls, no crash) when `executor` is
  `None`.
- `entry_kwargs`/`exit_kwargs` are forwarded through to
  `should_enter()`/`should_exit()`; short-position PnL sign is correct
  (profits when price falls).

`tests/test_event_backtester.py` gained 8 new regression tests for the
Phase 11 refactor: `decisions_from_model()`'s direction/probability
output is unchanged after switching to the shared
`direction_from_probabilities()` (including the three-way-tie case), and
the new public `simulate_fill_costs()` is tested directly — long/short
profit and loss, zero quantity, zero costs, and exact equivalence
against a real backtested trade's own `net_pnl`/`fees`/`slippage`
fields.

`StrategyEngine` was additionally smoke-tested end-to-end before the
formal suite was written (a scripted fake model wrapped in a real
`SignalEngine`, a short synthetic price sequence fed through repeated
`process_row()` calls, a fake `executor` stub) — not part of the
automated suite, but the same "run it against synthetic data before
trusting it" discipline that caught the Phase 10 walk-forward
double-fitting bug.

## Phase 12 — what's tested today (83 new tests, 554 total)

`tests/test_risk_manager.py` — `RiskManager`'s account-level state
tracking:

- Running equity updates with every `record_trade()` call; daily PnL
  accumulates within a UTC day and resets exactly on a day rollover
  (detected from the trade's own `closed_at`, not a wall-clock read) —
  a small loss that would have blocked under the old day's cumulative
  total no longer blocks once the day rolls over.
- Daily loss limit and consecutive-loss-streak breaches each block
  independently; a win *or* an exact breakeven resets the streak, only
  a strictly negative trade extends it.
- The kill switch blocks with its given reason (or the default
  `REASON_KILL_SWITCH` sentinel when none given), takes priority over
  every other simultaneous breach, and un-blocks on
  `disengage_kill_switch()`.
- `record_fill_quality()`'s slippage/latency breaches are sticky (a
  later good fill doesn't self-clear) until `reset_execution_breach()`.
- Missing `net_pnl`/`closed_at` on a `record_trade()` call degrades
  gracefully (defaults, no crash) rather than raising.

`tests/test_position_sizing.py` — `size_position()`'s formula and
multipliers, most cases run with an explicit high
`max_position_size_pct_equity` to isolate the multiplier math from the
equity ceiling (documented explicitly, with one dedicated test
confirming the ceiling *does* bind on the function's own plain
defaults):

- Zero/missing/negative equity and missing/zero stop distance both
  return `0.0`, never a guessed default.
- The base risk-based formula matches a hand-computed value; scales
  linearly with `risk_per_trade_pct`; a wider stop reduces size.
- Confidence/volatility/expected-value each scale size down
  proportionally, clamp/collapse correctly at their edges (confidence
  clamped to `[0, 1]`, non-positive EV collapses size to `0.0`), default
  to a neutral `1.0` multiplier when missing (never a boost), and
  compose multiplicatively when combined.
- The equity ceiling clamps the final result, with a custom override
  respected.

`tests/test_alert_manager.py` — `AlertManager.should_notify()`'s full
decision surface:

- An unknown `alert["type"]` or an unrecognized `escalation_reason`
  each raise `ValueError` (structural enforcement, not a silent
  fallback).
- Mode-based quality gating for all three modes, including a dedicated
  test for the flagged AGGRESSIVE/`required_signal_quality`
  inconsistency in both its literal (blocks "B") and disabled
  (`required_signal_quality=None`, allows "B") forms; SNIPER's
  exceptional-edge probability floor blocking a signal that clears the
  base `min_probability` but not SNIPER's own tightened one.
- A neutral signal, a missing `signal` payload, and a risk-blocked
  signal (with `block_on_risk_lock` both enabled and disabled) each
  correctly gate `HIGH_CONVICTION_SIGNAL` alerts; spread ceiling and
  data-freshness checks (skipped entirely when `now` isn't given, same
  convention as `entry.py`).
- The 4 non-signal alert types bypass the signal filter entirely.
- Cooldown and probability-delta dedup are independently exercised and
  shown to interact correctly: cooldown blocking a same-type/same-asset
  repeat, cooldown expiring after its window *combined with* the
  probability having moved enough to also clear dedup, dedup alone
  still blocking a near-identical probability well after cooldown has
  expired, escalation overriding cooldown but not dedup, and dedup
  disabled outright via `block_on_recent_duplicate=False`.
- `should_notify()` never mutates state on its own (repeated calls
  without `record_sent()` keep returning the same answer); different
  assets get independent cooldown clocks; an invalid `mode` at
  construction raises.

`tests/test_telegram_bot.py` — `TelegramBot`'s synchronous command core
and alert formatting:

- Every one of the 8 documented commands is handled without raising,
  including an unknown command (a helpful message, not a crash).
- A `None` provider degrades to an explicit "not available" message; a
  dict/list/empty-list provider result is formatted correctly (a
  regression test for a real bug — see below); all 6 data commands
  reach their own distinct provider.
- `/mode` with no args reports the current mode; a valid arg changes it
  and invokes `on_mode_change`; an invalid arg leaves the mode
  unchanged; the constructor itself rejects an invalid `mode`.
- `/stop` with no `on_stop` callback reports "not wired" and takes no
  action; with a callback, invokes it and confirms in its response text.
- `format_alert()` is tested standalone per alert type (signal, closed
  position, risk alert, and an unrecognized type that still formats
  without crashing); `send_alert()` always returns the formatted text
  and only calls the duck-typed `bot_client` when both it and `chat_id`
  are wired.

Two real bugs were caught by this suite before delivery, both fixed in
the implementation: `RiskManager.engage_kill_switch()`'s `reason`
parameter defaulted to the literal string `"manual"` instead of `None`,
which meant `state()`'s `reason or REASON_KILL_SWITCH` fallback could
never actually fire (a test asserting the exact sentinel value caught
it); and `telegram_bot.py`'s provider-formatting helper checked
`if not data` before branching on type, so an empty list was caught by
a generic "no data" message before it ever reached its own "none"
message (a dedicated empty-list test caught it).

## Phase 13 — what's tested today (45 new tests, 599 total)

`tests/test_execution_interface.py` — `ExecutionInterface`'s ABC
enforcement: direct instantiation raises `TypeError`; a subclass missing
even one of the 5 abstract methods also raises `TypeError`; a genuinely
complete subclass instantiates and its `place_order()` call works.

`tests/test_paper_executor.py` — `PaperExecutor`'s full simulated fill
path:

- Market buy/sell fill price correctly applies half the configured
  spread against mid in the unfavorable direction each way; zero spread
  fills exactly at mid.
- Fee matches `taker_fee_bps` (market) or `maker_fee_bps` (a crossing
  limit order) against the actual fill notional, not the requested one.
- Slippage stays within `[0, 2 * assumed_slippage_bps]` across repeated
  calls (not a fixed constant, by design); a maker fill carries zero
  simulated slippage. Latency stays within a configured range across
  repeated calls.
- Missing price data and below-min-notional (both the global default
  and a per-asset override) each reject with their own distinct reason;
  `reject_probability=1.0`/`0.0` deterministically always/never produce
  a simulated reject; `partial_fill_probability=1.0`/`0.0`
  deterministically always/never produce a partial fill within the
  configured fraction range.
- A limit order below/above the current market correctly stays
  `pending` vs. fills immediately at the better-of (mid, limit) price,
  each direction (buy/sell) tested independently; `cancel_order()`
  removes a pending order (and fails gracefully on an unknown id);
  `get_open_orders()` filters by asset; `try_fill_pending()` fills a
  previously-pending order once a fresh quote crosses it, and returns
  empty when nothing does.
- The position ledger: opening sets the average price to the fill
  price; adding in the same direction updates a real volume-weighted
  average; reducing realizes PnL into cash correctly; flipping through
  flat starts a fresh average price on the new side. Balance's `cash`
  decreases by fees on every fill; `unrealized_pnl` reflects the current
  `price_lookup` quote and degrades to `0.0` when no `price_lookup` is
  wired.
- Every decision — filled, rejected, pending, cancelled — lands in the
  append-only `order_log`.

`tests/test_performance.py` — `PerformanceTracker`'s windowing and its
Phase 10 `compute_metrics()` reuse:

- An empty tracker's snapshot has zero trades and `None` rankings, never
  a crash.
- Hourly/daily windows correctly exclude trades both older than the
  window *and* — the caught bug's dedicated regression test — trades
  closing *after* the snapshot's own `now`, not just before the window.
- `win_rate`/PnL/fees/slippage/`max_drawdown` in a snapshot match calling
  `compute_metrics()` directly on the same trade list bit-for-bit — this
  module is never allowed to silently drift from Phase 10's own math.
- `best_asset`/`best_setup`/`worst_setup` correctly rank by aggregate
  `net_pnl`; a trade missing the ranked dimension (`asset`/`exit_reason`)
  is excluded from that specific ranking while still counting toward
  the aggregate trade/PnL totals.
- `to_performance_snapshot_row()`'s output key set matches
  `PerformanceSnapshot`'s DB columns exactly, both with and without an
  explicit `timestamp`.

## Phase 14 — what's tested today (42 new tests, 641 total)

`tests/test_kraken_executor.py` — `KrakenExecutor` built as inert,
unactivated scaffolding (the user's explicit choice on 2026-08-19, in
lieu of waiting or skipping this phase entirely):

- ABC compliance: `KrakenExecutor` satisfies `ExecutionInterface`
  (`issubclass` and a zero-arg instantiation, both of which must not
  raise).
- **Gate coverage — the core safety property this phase exists to
  prove**: default construction is fully inert (`live_enabled is
  False`, all 3 `blocking_reasons` present); every one of the 6 ways to
  satisfy exactly two of the three gates (`dry_run=False` alone,
  `confirm_live_trading=True` alone, real keys alone, and each pair)
  stays inert; only all three together enables `live_enabled`. An
  inert `place_order()`/`cancel_order()`/`get_balance()`/`get_position()`/
  `get_open_orders()` call is proven to never reach the transport at all
  — the injected `transport` stub raises `AssertionError` if called,
  and every inert test passes specifically because it's never invoked.
  An inert `place_order()` is still appended to `order_log` (section
  43's "every decision is logged," including attempts blocked by the
  gates) and its response is marked `dry_run: True` with the exact
  `gate_reasons` that blocked it.
- Signing: `_sign()` is deterministic for identical inputs, changes
  with the nonce, and raises a clear `ValueError` (not an opaque
  `binascii` error) on a non-base64 secret. `_next_nonce()` is proven
  strictly increasing across 1000 rapid calls, covering the case where
  successive calls land within the same millisecond.
- `place_order()`'s live path (via a `ScriptedTransport` test double
  returning canned Kraken JSON in order): a market buy that fills fully;
  a `vol_exec < vol` closed order correctly reported `partial`, not
  `filled`; `open`/`pending` map to `pending`; `canceled`/`expired` map
  to `rejected`; **an order status Kraken doesn't document today is
  proven to fall back to `pending`, never guessed as `filled`, even
  when `vol_exec` happens to equal the requested quantity** — the
  regression test this phase's whole "never guess resolved" design
  choice exists to lock in; a response with no `txid` is reported
  `rejected` with a clear reason rather than raising; a limit order
  without a `price` raises `ValueError` before any network call; an
  unconfigured asset raises `ValueError` (with the asset's own name in
  the message) before any network call; a Kraken `error` array raises
  `KrakenAPIError` with the error text, never silently swallowed;
  `slippage_bps` computes correctly against an injected `price_lookup`
  and is `None` (never fabricated) when no `price_lookup` is wired.
- `cancel_order()`/`get_balance()`/`get_position()`/`get_open_orders()`'s
  live paths: cancel success/failure by Kraken's own `count` field;
  balance parses `ZUSD`/`XXBT` into `cash`/`balances` while preserving
  the untouched `raw` Kraken payload; `get_position()`'s `avg_price` is
  proven always `None` (the documented Kraken-spot-has-no-cost-basis
  gap, never silently fabricated) and returns `None` entirely when the
  asset has no recorded balance; `get_open_orders()` parses Kraken's
  `open` dict, reverse-resolves each order's pair back to an asset via
  `asset_config`, and filters by asset correctly.
- The real (default) `_httpx_transport` — the one code path none of the
  above (all of which inject `transport` directly) exercise — is proven
  correct against a mocked `httpx.post`: it signs the same way, measures
  a real non-negative latency, and propagates an HTTP error status as an
  exception rather than swallowing it. No test in this file ever reaches
  the actual Kraken API.

## Phase 15 — Live paper trading (47 new tests, 699 total; +20 addendum, 719 total; +1 addendum 2, 720 total; +5 addendum 3, 725 total; +1 addendum 5, 726 total; +6 addendum 6, 732 total; +9 addendum 7, 741 total; +5 addendum 8, 746 total; +7 addendum 10, 753 total)

- `test_feature_engine.py` (+4): the new incremental API
  (`on_market_event()`/`on_news_event()`/`build_row()`) produces
  byte-identical rows to the pre-existing batch `build_rows()` path when
  driven event-by-event over the same tick stream — the property this
  refactor exists to guarantee, verified directly rather than assumed
  from the extraction being "obviously equivalent." Plus a dedicated
  regression test for the `liquidity_imbalance` column now being
  present and sourced from `OrderBookEngine.metrics()`.
- `test_model_registry.py` (+2): the new optional `artifact_path` field
  is stored and returned by `current_promoted()`, and defaults to
  `None` when omitted (backward-compatible with pre-Phase-15 registry
  files).
- `test_model_persistence.py` (5, new file): `save_model()`/
  `load_model()` round-trip preserves predictions exactly for both
  `LogisticDirectionModel` and `TreeDirectionModel`; parent directories
  are created on save; a missing artifact raises `FileNotFoundError`; an
  `expected_class` mismatch raises a clear `TypeError`.
- `test_train_model.py` (10, new file): full CLI runs (`main(argv=...)`)
  against a real synthetic `features_v1` Parquet table built via
  `ParquetStore` under `tmp_path` (module globals `PROJECT_ROOT`/
  `CONFIG_DIR` monkeypatched so nothing touches the real repo) —
  registers without promoting by default; `--promote` without
  `--reason` raises `SystemExit`; `--promote --reason "..."` makes the
  model current; **the threshold_bps-normalization regression** (a
  whole-number `--threshold-bps 10.0` must still resolve real targets,
  not silently score zero rows); `--calibrate` saves a model that still
  predicts; `--model tree` is respected; no-rows-found and
  empty-validation-split both raise a clear `SystemExit`; registered
  `features` exclude every `target_*` column; two registrations a
  second apart get distinct `model_version`s (documents the real
  second-resolution-timestamp constraint, rather than assuming
  uniqueness is unconditional).
- `test_trading_loop.py` (15, new file): an asset with no promoted
  model is skipped, not fatal (including the all-assets-skipped case);
  `tick()` with no book snapshot yet returns empty actions, not a
  crash; **a full open → take-profit-exit cycle** against a real fitted
  `LogisticDirectionModel` and a synthetic imbalanced order book,
  confirming `RiskManager.record_trade()`/`PerformanceTracker.
  record_trade()` fire and `POSITION_OPENED`/`POSITION_CLOSED` alerts
  are sent; `HIGH_CONVICTION_SIGNAL` fires independently of whether a
  trade was actually made (the signal-filter/entry-filter are
  deliberately different questions); `RISK_ALERT` is edge-triggered —
  fires once on the blocked-state transition, never repeats while still
  blocked, and never fires if never blocked; news events broadcast to
  every asset's own `FeatureEngine` independently; every `TelegramBot`
  provider method (`status`/`open_positions`/`recent_signals`/
  `performance`/`today`/`risk`) returns correctly-shaped data;
  `load_promoted_model()` returns `None` (not a raise) when nothing's
  promoted or the promoted record has no `artifact_path`, and loads a
  genuinely usable model end-to-end when one exists.
- `test_database_storage.py` (+6): `record_position()`/
  `record_performance_snapshot()` round-trip correctly and ignore extra
  bookkeeping keys a closed-position dict carries but the `Position`
  table has no column for; `SQLiteRecorder.write_position()`/
  `write_performance_snapshot()` commit correctly; an end-to-end run
  through the real `PerformanceTracker.to_performance_snapshot_row()`
  shaping helper writes cleanly (confirms the two modules' contracts
  actually agree, not just each module's own fixture).
- `test_main_paper_trading_wiring.py` (16, new file): `paper_trading_
  enabled()`'s two-gate check across every `trading_mode`/env-var
  combination (parametrized); `build_risk_manager()`/
  `build_alert_manager()` map `strategy.yaml`/`telegram.yaml` onto their
  constructors correctly, verified against both hand-built fixtures and
  the real config files (catches config-key drift, not just a fixture
  that happens to match the code); `build_paper_trading_engine()` loads
  promoted models and skips the rest, with `telegram_bot=None`
  confirmed (not activated this phase); `market_state_consumer`/
  `news_consumer` forward the right events to an injected recording
  double, and behave exactly as before when `paper_trading_engine=None`
  (the default-off regression that matters most); the three new
  periodic tasks — `paper_trading_tick_loop` persists exactly the
  closed positions it should and **survives a `tick()` exception
  without dying** (a dedicated regression, not just the happy path),
  `paper_trading_performance_writer` writes periodic snapshots,
  `paper_trading_display` runs without raising.

Every new class was also manually smoke-tested end-to-end (a scripted
`PaperTradingEngine` run against a synthetic, strongly-imbalanced order
book — a real open → take-profit exit → risk/performance bookkeeping →
alert cycle, confirmed via direct script execution) before the formal
suite above was written, per this project's established discipline.

### Phase 15 addendum — Telegram live wiring (20 new tests, 719 total)

Added once a real Telegram bot token became available to configure
(`notifications/telegram_live.py`, `main.py: wire_live_telegram()`).

- `test_telegram_live.py` (15, new file): `build_live_telegram_bot()`
  returns `(None, None)` when `token`/`chat_id_raw` is missing or empty
  (three variants); a configured pair binds every provider to a fake
  `paper_trading_engine`'s methods correctly; numeric `chat_id` parses to
  `int`, a non-numeric one (e.g. a channel username) passes through
  unchanged; `register_handlers()` registers exactly one handler per
  `COMMANDS` entry; the chat-ID allowlist — an authorized chat gets a
  reply, an unauthorized chat and an update with no `effective_chat` are
  both silently ignored (no reply, no exception), verified for every
  registered command, not just one; `on_mode_change`/`on_stop` correctly
  mutate a fake `alert_manager.mode`/call a fake `risk_manager.
  engage_kill_switch("telegram_stop_command")`; `AsyncioTelegramBotClient.
  send_message()` schedules and executes an async send against a fake
  bot double, and swallows a send failure rather than letting it
  propagate. No real Telegram token or network access anywhere in this
  file — `ApplicationBuilder().token(...).build()` only validates a
  token's shape locally.
- `test_main_telegram_wiring.py` (5, new file): `wire_live_telegram()`
  itself — `paper_trading_engine=None` returns `None` and touches
  nothing; **the default-off regression** (neither env var set leaves
  the engine's `telegram_bot` and every unit's `telegram_bot` untouched,
  the case that matters most); only one of the two env vars set still
  stays inert; both set wires `paper_trading_engine.telegram_bot` and
  every `unit.telegram_bot` to the same instance, with providers bound
  correctly; the startup log line names the chat_id but never the token.

Both files were smoke-tested manually first (a real local `Application`
built via `ApplicationBuilder().token(fake_token).build()`, handlers
extracted from `application.handlers[0]` and invoked directly against
duck-typed fake `Update`/`Context` objects) before the formal suites
above were written, per this project's established discipline.

### Phase 15 addendum 2 — `artifact_path` regression test (1 new test, 720 total)

Added after a real bug surfaced on the first live end-to-end PAPER
launch (2026-08-25): `scripts/train_model.py` and `src/live/trading_loop.
py: load_promoted_model()` had disagreed for the whole of Phase 15 on
whether a promoted model's `artifact_path` is relative to `PROJECT_ROOT`
or to `data/models` — invisible until something actually exercised both
halves together, which no test had.

- `test_promoted_model_actually_loads_via_load_promoted_model()` (in
  `test_train_model.py`): runs the real `train_model.main([...,
  "--promote", "--reason", ...])`, then loads the result back through
  the real `load_promoted_model()` — the exact call `main.py` wires into
  `PaperTradingEngine` — and asserts it returns a usable model. No fakes
  on either side; this is deliberately the integration path the bug hid
  in, not another isolated unit test of either function.
- The two pre-existing assertions in this file that checked
  `artifact_path` against `PROJECT_ROOT` (and so passed despite the bug)
  were corrected to check it against `data/models`, matching the fixed
  convention.

### Phase 15 addendum 3 — `/signals` lean formatting (5 new tests, 725 total)

Added after `/signals` produced no reply at all on a real Telegram chat
where `/performance` worked fine right next to it. Root cause: a real
`SignalEngine` payload embeds `features_snapshot` (the full feature row)
and `model_calibration` (the promoted model's full calibration-curve
diagnostics), and the old formatter dumped every signal's full dict repr
into one message — measured at ~2733 characters for one asset, ~8200 for
three, comfortably past Telegram's ~4096-character limit.

- `test_signals_summary_excludes_the_oversized_fields()`: the new lean
  formatter's output contains neither `features_snapshot` nor
  `model_calibration`/`calibration_curve`, while still surfacing the
  fields that matter (asset, probability, regime).
- `test_signals_summary_stays_under_telegram_message_limit_for_three_assets()`:
  the exact regression case — three real-shaped signals (each built with
  a full features_snapshot + model_calibration, matching the real bug)
  render to a message under 4096 characters.
- `test_signals_summary_handles_missing_optional_fields_without_crashing()`:
  a minimal signal dict (just `asset`/`signal`) degrades every missing
  field to `"n/a"` rather than raising.
- `test_signals_empty_list_still_says_none()`: unchanged behavior,
  covered explicitly against the new code path.
- `test_signals_non_signal_shaped_data_falls_back_to_generic_formatting()`:
  a provider returning something that isn't a list of real signal dicts
  (e.g. the pre-existing generic `{"ok": True}` test double) still falls
  back to the original `_format_provider_result()` formatting, unchanged
  — the shape-detection fix doesn't narrow what a `signals_provider` is
  allowed to return.

### Phase 15 addendum 5 — target-threshold defaults extended (1 new test, 726 total)

Added after `scripts/sweep_entry_threshold.py` (built this addendum)
showed zero trades at every tested `probability_min`, traced to a
mathematical wall: `entry_filter.min_expected_value` can never be
cleared when `threshold_bps <= round_trip_cost_bps`, and real Kraken
Pro Tier 1 taker fees (0.80%, not the old 5bps placeholder) put
`round_trip_cost_bps` at ~166 — above every `threshold_bps` this
project has trained a model for so far (max 50). `feature_engine.py`'s
`DEFAULT_TARGET_THRESHOLDS_BPS` gates which `target_*_up/down_Xbps`
columns `build_feature_table.py` ever computes, so a threshold above
166 has to be added there before any model can even be trained at it.

- `test_default_target_thresholds_cover_above_real_kraken_round_trip_cost()`:
  `max(DEFAULT_TARGET_THRESHOLDS_BPS) > 166` (a value big enough to
  ever clear real costs is representable), and `{5, 10, 15, 25, 50}` (the
  original set) stays a subset — old features_v1 tables/tests stay
  comparable, this is additive only.

### Phase 15 addendum 6 — purged k-fold CV (6 new tests, 732 total)

Added after retraining BTC/SOL at a larger `threshold_bps` kept
reporting `accuracy=None (n=0)` even after extending the training
window from 4 to 6 days. Traced to a structural flaw in
`train_model.py`'s original single last-block chronological holdout: a
rare, calendar-clustered directional target (BTC had zero qualifying
moves at all in the original window; SOL had 677, but 80% concentrated
on one day) has a real chance of landing its validation slice on an
entirely quiet block — and since the holdout is always the *most
recent* chronological block, adding more (possibly also quiet) history
behind it doesn't fix this, it can make it worse.

- `test_calibrate_is_exactly_labels_and_proba_then_diagnostics_from_arrays()`
  (`test_calibration.py`): the `calibrate()` refactor into
  `labels_and_proba()` + `diagnostics_from_arrays()` produces bit-for-bit
  identical output to the pre-refactor single function.
- `test_diagnostics_from_arrays_pools_predictions_from_different_models()`
  (`test_calibration.py`): pooling `(y, proba)` from two different fitted
  models into one `diagnostics_from_arrays()` call scores the combined
  count, not either model's alone.
- `test_calibration_fraction_too_large_leaves_an_empty_side()`
  (`test_train_model.py`, renamed from the old `--validation-fraction`
  test): `--calibrate --calibration-fraction 0.999` still raises a clear
  `SystemExit` — the flag now only governs the `--calibrate` holdout.
- `test_final_model_is_fit_on_all_rows_when_not_calibrating()`: without
  `--calibrate`, `n_train_rows == n_rows_total` and `n_calibration_rows
  == 0` in the registered `training_period` — nothing is withheld from
  the model that actually gets saved/promoted.
- `test_cv_folds_flag_is_recorded_and_defaults_to_five()`: `--cv-folds`
  is stored in `training_period`, and at least one fold actually ran on
  the (now 800-row) synthetic fixture.
- `test_cv_pools_across_folds_so_a_quiet_last_block_still_gets_scored()`:
  the exact regression case — a synthetic table with every directional
  example in the first half only (a last-block holdout would score
  `n=0` on this table, always) — gets a nonzero pooled `n_scored` under
  purged k-fold CV.
- `test_purged_kfold_split_purges_both_edges_of_each_held_out_fold()`:
  every fold's train/validation timestamps are disjoint, and every
  train timestamp is at least `horizon_s` seconds from either edge of
  that fold's held-out validation block.

### Phase 15 addendum 7 — `NewsDirectionModel`, `--model news` (9 new tests, 741 total)

Built to test, under the same purged k-fold CV harness every other
model goes through, whether a classifier trained on just the five
news-derived `features_v1` columns (`news_score`, `news_importance`,
`news_novelty`, `news_age`, `news_credibility`) reproduces the edge an
ad hoc `sign(news_score)` comparison found on real data (BTC/SOL,
horizon_s=60/threshold_bps=10, accuracy 51.6-53.8%, p as low as 1e-11 on
8.6k-20.3k rows). It didn't: CV on the full row set gives `n_scored=0`
for all three assets; CV restricted to just the "news-active" rows the
ad hoc check used (news_age<900s, |news_score|>0.05 — 105k-133k rows per
asset) still gives `n_scored=0`, and `roc_auc_macro` comes back *below*
0.5 for all three (0.40-0.45) — the opposite of a confirmed edge. Likely
explanation, not yet proven: the ad hoc check's rows aren't independent
Bernoulli trials the way its binomial test assumed — a single news event
persists across up to ~900s of 1-second-sampled rows, so its "8,600
independent observations" were probably a much smaller number of actual
news events repeated many times over, which inflates apparent
significance without it surviving an honest out-of-fold test. Recorded
here as a real, evidence-based negative result, not a wasted effort —
exactly the kind of check purged CV exists to run before anything gets
promoted on a false positive.

- `src/models/logistic_model.py` — `fit()`'s column selection factored
  into an overridable `_feature_columns()` method (was inline
  `feature_columns(rows)`), so a subclass can restrict the feature set
  without duplicating the rest of `fit()`. Default behavior for
  `LogisticDirectionModel` itself is unchanged.
- `src/models/news_model.py` (new) — `NewsDirectionModel`, a thin
  `LogisticDirectionModel` subclass restricted to the 5 news columns;
  raises a clear `ValueError` naming the missing column(s) if a row
  lacks one, rather than silently imputing a whole feature away.
- `scripts/train_model.py` — `NewsDirectionModel` registered as
  `--model news` in `MODEL_CLASSES`, no other change (same CLI/CV/
  registry/promotion flow as `logistic`/`tree`).
- `tests/test_news_model.py` (new, 7 tests): missing-column error names
  the column; no-resolved-target error; predict-before-fit error;
  fits and predicts a trivially separable `news_score` relationship;
  `predict_proba()` output shape/keys; `feature_columns_` after fit is
  exactly the 5 news columns even though other columns (e.g. `obi`) are
  present on every row; `LogisticDirectionModel` itself is unaffected by
  the `_feature_columns()` refactor (still trains on every column,
  `obi` included).
- `tests/test_train_model.py` (2 new tests): `--model news` registers
  with `model_kind == "news"` and a nonzero pooled CV score on a
  synthetic table with a real news_score signal; on a table with no
  news_* columns at all, every fold fails to fit and `main()` hits the
  addendum 6 zero-folds-used `SystemExit` guard rather than registering
  a degenerate model.
- **741 tests total, all passing.**

Not yet done: aggregating to one row per distinct news *event* (rather
than one row per second of that event's decay window) before testing
again, which would give an honestly independent — but likely much
smaller — sample; revisiting whether `news_score` carries any
information at all once that's fixed.

### Phase 15 addendum 8 — event-level CV diagnostic, built into `train_model.py` (5 new tests, 746 total)

Addendum 7's news-model false positive turned out not to be a one-off.
Testing the full `LogisticDirectionModel` at `horizon_s=300,
threshold_bps=100` found the same pattern again for SOL: a CV result
that looked like the best of the day (`n=146`, accuracy=67.8%,
p=1.0e-05, `roc_auc_macro=0.560` — the first >0.5 of the session at
this threshold). An ad hoc audit of the pooled scored rows' timestamps
showed 120 of the 146 (82%) fell inside two tight windows on
2026-08-22, one spanning just 145 seconds — one real price move,
sampled once per second and counted as ~116 near-independent trials.
Two unrelated false positives from the same underlying mechanism in one
session (this one; addendum 7's news-score check) makes it a confirmed,
recurring failure mode of row-level CV scoring at 1-second sampling,
not a fluke — worth catching automatically rather than by ad hoc script
each time it happens to get noticed.

`scripts/train_model.py`'s `_cross_validate()` now also pools every
scored row's `(timestamp, correct)` detail across folds and runs
`_event_level_diagnostics()` on it: consecutive scored rows less than
`horizon_s` seconds apart are grouped into one cluster (the working
assumption being that's roughly how long one real market move stays
resolved at that horizon), each cluster becomes exactly one trial
(majority-vote correctness, exact ties count as half) for a second,
much more conservative binomial test — plus `top_cluster_share`, which
flags concentration in one dominant cluster even on a result that still
clears the event-level bar. This doesn't replace the row-level test;
it's a second, harder bar a real edge should also clear. `main()` now
logs both results and an explicit warning whenever a result clears the
row-level bar but not the event-level one — exactly the shape both
today's false positives had.

Retroactively run against the currently-promoted `horizon_s=60,
threshold_bps=10` `LogisticDirectionModel` (the one edge that has
survived every other check this session) on the full 2026-08-21..28
window: BTC (88 clusters across 6 days, 65.9% event-level accuracy,
p=0.0019) and ETH (169 clusters across 8 days, 62.1%, p=0.0010) both
clear the event-level bar too — this is real, distributed evidence, not
an artifact. SOL (231 clusters across 8 days, 55.0%, p=0.074) does not
clear 0.05 at the event level, despite a nominally-significant row-level
p=0.030 — weaker than BTC/ETH, should not be trusted with the same
confidence going forward.

- `scripts/train_model.py` — `_direction_score_counts()` refactored into
  a thin wrapper over new `_direction_score_details()` (same counts,
  now also returns per-row `(timestamp, correct)`); new
  `_event_level_diagnostics()`; `_cross_validate()` returns a new
  `"event_level"` key alongside `"accuracy"`/`"calibration"`; `main()`
  logs the event-level result and a warning on row/event disagreement;
  `event_level_accuracy` now stored in every registered model's
  `performance` dict.
- `tests/test_train_model.py` (5 new tests): the event-level dict is
  present with the right shape in registry `performance`; a single
  116-row burst (all one second apart) collapses to exactly 1 cluster
  and can never itself be "significant"; 20 widely-spaced (1h apart)
  events count as 20 separate trials with accuracy computed correctly;
  an exact tie within one cluster counts as half a win; the empty-input
  shape.
- **746 tests total, all passing.**

### Phase 15 addendum 10 — net-of-fees expected value, built into `train_model.py` (7 new tests, 753 total)

The event-level diagnostic (addendum 8) answers "does the sign beat a
coin flip, robustly." It doesn't answer "would trading this actually
make money after fees" — that needs magnitude, not just direction. New
`--round-trip-cost-bps` flag on `train_model.py` (purely informational:
logged and stored in registry `performance`, never affects promotion)
runs a proper net-expected-value check against any cost assumption.

`_forward_return_bps(rows, horizon_s)` reuses the existing
no-look-ahead-safe `find_forward_indices()` primitive (the same one
`compute_targets()` itself uses to build the binary threshold labels) to
compute the continuous point-in-time forward return in bps for every
row, `None`-safe when a row or its forward index has no price.

`_net_expected_value(rows, model_cls, horizon_s, threshold_bps, k,
round_trip_cost_bps)` reuses `_purged_kfold_split()`; per fold, fits a
fresh model, and for every validation row where the model's own
`direction_from_probabilities()` call is decisive (mirrors how
`EventBacktester.decisions_from_model()` already works — independent of
whether the *true* label was decisive), computes `net_pnl_bps =
predicted_direction * realized_forward_return_bps - round_trip_cost_bps`.
Pools `(timestamp, net_pnl_bps)` across folds, then reports both
row-level (every scored row as an independent trial) and event-level
(same clustering convention as addendum 8's diagnostic, each cluster's
*mean* net P&L as one trial) results via a one-sided `ttest_1samp`
against `popmean=0.0`. Each result is `{n, mean_net_pnl_bps, p_value,
beats_breakeven}`, with `p_value: None` and `beats_breakeven: False`
when `n` is 0, 1, or the sample has zero variance (can't run a t-test).

Used this addendum to validate whether the already-validated 10-25bps
directional edges from addenda 8-9 are economically tradeable at
Hyperliquid's much lower fee schedule (researched separately, see
`ROADMAP.md`'s addendum 10 for the fee sourcing and the full results
table) rather than Kraken's. Result: net EV stays negative to
essentially breakeven everywhere tested, never significantly positive —
closest is BTC 300s/25bps at Hyperliquid's maker rate, mean=-0.17bps,
p=0.59.

- `tests/test_train_model.py` (7 new tests): `_forward_return_bps`
  matches a 4-row hand-computed example exactly (including `None` for
  rows past the end of the data); net EV is significantly positive
  (`beats_breakeven=True`, mean > 20bps) on a synthetic fixture where a
  known 50bps edge clears a 5bps cost; net EV is significantly negative
  on the same fixture when cost is raised to 500bps; event-level trial
  count is strictly less than row-level trial count on the same data;
  rows with no `"price"` key produce the empty/all-`None` shape at both
  levels; the CLI flag wires its result into registry `performance`
  when passed, and leaves `performance["net_expected_value"]` as `None`
  when the flag is omitted.
- **753 tests total, all passing.**

## What's added per later phase (not yet written — tracked here so nothing
gets forgotten)

| Phase | New tests |
|---|---|
| 16 (Live trading, minimal size) | Not yet scoped — no Phase 1 stub file names this phase; flagged in the internal delivery log's "Full phase map" as unconfirmed against the original master prompt, and gated on a completed Phase 15 paper-trading validation cycle first (see `docs/21_paper_trading_methodology.md`) |

## Running tests

```bash
pytest -q
```

`pytest-asyncio` is configured (see `requirements.txt`) for the async
collector/queue tests.
