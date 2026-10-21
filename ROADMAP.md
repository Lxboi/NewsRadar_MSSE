# Roadmap

> **Final status (2026-08-29): research concluded, project halted at this
> baseline.** Every avenue tested (threshold scan 10-300bps, purged k-fold
> CV, event-level audit, news-only model, additive and tree-based
> news+microstructure models, cheaper execution venues) found at best a
> small directional edge on very small moves, 5-10x smaller than
> realistic round-trip trading costs. No configuration produced a
> positive expected value net of fees. The Raspberry Pi collector was
> decommissioned; Phases 16-17 will not be started. See
> ["Final status"](#final-status--project-halted-2026-08-29) at the end of
> this file and the README for the summary.

Mandatory progression (section 5 of the master prompt) — never skip ahead
to live trading. Default mode is always `RESEARCH` until each gate below is
explicitly validated by you.

| # | Phase | Status | Deliverable |
|---|---|---|---|
| 1 | Python architecture | **DONE — validated by you** | repo skeleton, config, logging setup, `.env.example`, tests, this roadmap |
| 2 | Hyperliquid WebSocket | **DONE — validated live by you (2026-08-18)** | real l2Book/trades/candle parsing, reconnect/heartbeat/ping, live console display for BTC/ETH/SOL — confirmed working against real prices via `python -m src.main` |
| 3 | Order Book Engine | **DONE — implemented & unit-tested** | spread, OBI (1/5/10/25 + weighted), microprice(+delta/accel), trade flow (1-180s windows), liquidity walls + transient score, sweep detection, absorption scores — live in the console display |
| 4 | Storage | **DONE — validated live by you (2026-08-18)** | Parquet buffering/partitioning for raw l2Book levels + trades; periodic SQLite snapshot (market_data, orderbook_features, trade_features) every 5s — confirmed real data landing in both `data/raw/*.parquet` and `crypto_radar.db` |
| 5 | News Radar | **DONE — validated live by you (2026-08-18)** | 24 RSS feeds polled + scored (classification, sentiment, relevance, credibility, novelty, dedup, decay) — raw items to Parquet, relevant items to `news_events`, recent items in the live display. Google/social deferred (user decision, not hardware) |
| 6 | News + market sync | **DONE — audited & unit-tested (2026-08-18)** | confirmed shared UTC clock end to end; found & fixed a real SQLite tz-round-trip bug; `tests/test_clock_discipline.py` locks in the no-look-ahead join property |
| 7 | Feature engineering | **DONE — implemented & unit-tested (2026-08-18)** | `feature_engine.py` (raw-tick replay, not the coarser 5s snapshot cadence), `signals/regime_detector.py`, `scripts/build_feature_table.py` batch builder — writes `features_v1` to Parquet |
| 8 | Baseline strategies | **DONE — implemented & unit-tested (2026-08-18)** | 7 non-ML baselines (`baseline_model.py`) + a "beats a flip of a coin" directional-accuracy check (`evaluation.py`), section 31 |
| 9 | Statistical models | **DONE — implemented & unit-tested (2026-08-18)** | logistic + gradient boosting direction models, calibration diagnostics/recalibration, an append-only manual-promotion-only `model_registry` |
| 10 | Backtesting | **DONE — implemented & unit-tested (2026-08-18)** | chronological no-look-ahead `event_backtester.py`, order-flow-only ablation, `metrics.py` (PnL/Sharpe/Sortino/drawdown + bootstrap/Monte-Carlo/ruin), purged `walk_forward.py` |
| 11 | Signal engine | **DONE — implemented & unit-tested (2026-08-18)** | probability/EV/confidence/signal_quality output (A/B/C/NO_TRADE), live `should_enter()`/`should_exit()`, stateful `StrategyEngine` with optional duck-typed executor |
| 12 | Telegram SNIPER | **DONE — implemented & unit-tested (2026-08-19)** | account-level `RiskManager` + kill switch, `size_position()`, anti-spam `AlertManager`, synchronous `telegram_bot.py` command/alert core |
| 13 | Paper trading | **DONE — implemented & unit-tested (2026-08-19)** | `execution_interface.py` ABC, `paper_executor.py` with realistic latency/slippage/partial fills, `monitoring/performance.py` |
| 14 | Kraken integration | **DONE — implemented & unit-tested, delivered inert/unactivated (2026-08-19)** | `kraken_executor.py` (spot only; 3-gate safety design keeps it inactive until the user supplies real credentials) — spot-vs-futures comparison (section 42) deferred to a follow-up |
| 15 | Live paper trading | **DONE — implemented & unit-tested (2026-08-19)** | `live/trading_loop.py` wires every prior phase into a running pipeline gated behind PAPER mode (section 65); `scripts/train_model.py`; `docs/21_paper_trading_methodology.md` — how to actually run and evaluate a test |
| 16 | Live trading (minimal) | **Cancelled** — no tradeable edge found, see "Final status" below | smallest possible size, explicit startup confirmation required |
| 17 | Raspberry Pi 24/7 | **Cancelled** — collector decommissioned 2026-08-29 | systemd service, watchdog, log rotation, low-memory mode |

## Phase 1 — what shipped

- Full folder tree from section 8, every module stubbed with a docstring
  (purpose + owning phase) so imports and tests work today.
- Real config: `config/{settings,assets,sources,strategy,telegram}.yaml`,
  `.env.example`, `requirements.txt`.
- Real SQLite schema (`src/storage/database.py`, 10 tables) +
  `docs/05_sqlite_schema.md`.
- Parquet layout spec (`docs/05_parquet_schema.md`).
- Feature table v1 spec incl. targets and no-look-ahead rules
  (`docs/06_features_v1.md`).
- Working asyncio skeleton: `src/main.py` wires queues, signal handling,
  and a health heartbeat; `src/collectors/hyperliquid_ws.py` implements a
  real connect/reconnect/heartbeat/backoff loop (message *parsing* is
  Phase 2's first task, since it needs the live HL schema confirmed).
- Collection strategy (`docs/07_collection_strategy.md`) and backtesting/
  validation strategy (`docs/08_backtesting_strategy.md`) written up front
  so later phases have a bar to clear, not just code to write.
- `tests/` — 10 passing tests covering config integrity and the collector's
  offline logic (`docs/09_tests_plan.md` lists what every later phase adds).

## Phase 2 — what shipped (2026-08-18)

- Confirmed Hyperliquid's live WS schema against their current docs
  (`docs/03_hyperliquid_ws_architecture.md` has the details + sources):
  connect URL, subscribe format, and the exact `l2Book`/`trades`/`candle`/
  `subscriptionResponse`/`pong` envelope shapes.
- `src/collectors/hyperliquid_ws.py`: real parsing via the pure function
  `parse_hyperliquid_message()`, a keepalive ping every 20s (server disconnects
  idle sockets after 60s), and a socket-level heartbeat watchdog tuned to sit
  above the ping interval (30s) so it doesn't fight its own keepalive.
- `src/main.py`: tracks best bid/ask/mid/spread/last trade per asset and
  prints a live snapshot every 2s — the "afficher les données en temps
  réel" deliverable from section 72.
- `config/settings.yaml`: added `asyncio.ws_heartbeat_timeout_s`, kept
  distinct from `stale_data_timeout_s` (that one's the future system-level
  "no data -> STOP TRADING" switch, Phase 12).
- 14 new tests (24 total): parser fixtures matching the confirmed schema,
  `AssetState` mid/spread math, and `market_state_consumer` wiring — all
  pass with no network access.
- **Validated 2026-08-18**: `python -m src.main` run on the user's Windows
  machine (CMD, `.venv`, `.env` copied from `.env.example` with API keys
  left blank — Hyperliquid market data needs no auth) connected
  successfully and streamed real, sane-looking BTC/ETH/SOL prices into the
  live display every 2s. Phase 2 is closed.

## Phase 3 — what shipped (2026-08-18)

New modules, each independently tested against hand-built fixtures (see
`docs/04_orderbook_engine.md` for the full formulas and design notes):

- `src/orderbook/imbalance.py` — OBI at 1/5/10/25 levels + a
  distance-weighted variant.
- `src/orderbook/microprice.py` — mid, microprice, and a
  `MicropriceHistory` tracker for delta/acceleration.
- `src/orderbook/trade_flow.py` — `TradeFlowTracker`: buy/sell volume,
  imbalance, velocity, avg size, large-trade frequency (relative
  threshold, not hardcoded), volume acceleration, across 1/5/10/30/60/180s
  windows.
- `src/orderbook/liquidity.py` — `LiquidityTracker`: wall detection
  (relative threshold), persistence, cancellation rate, depth_change,
  liquidity_imbalance, `transient_liquidity_score` (explicitly not a
  spoofing detector, per section 16).
- `src/orderbook/sweeps.py` — `SweepDetector`: same-side trade bursts ->
  volume/duration/displacement/levels_crossed.
- `src/orderbook/absorption.py` — buy/sell absorption scores from volume
  vs. price displacement.
- `src/orderbook/orderbook_engine.py` — per-asset orchestrator
  (`on_l2book`/`on_trades` -> one `metrics()` dict); wired into
  `src/main.py`, which now prints a second live-display line per asset
  with all of the above.
- 41 new tests (65 total), all deterministic fixtures, no network needed —
  this phase's correctness doesn't depend on live market conditions the
  way Phase 2's connectivity did.

**Not yet done at the time**: nothing was persisted, and the absorption
score used a live approximation (current trade-flow window vs. current
mid) rather than the exact historical-mid version `feature_engine.py`
will use once history is actually stored — Phase 4 below fixes the first
half of that.

## Phase 4 — what shipped (2026-08-18)

- Bug fix caught while wiring this phase: `OrderBookEngine.on_l2book()`
  called `LiquidityTracker.update()` but discarded its return value, so
  `liquidity_imbalance`/`wall_persistence`/`transient_liquidity_score`
  silently stayed `None` everywhere (live display included) since Phase 3.
  Fixed, with a regression test.
- `src/storage/parquet_store.py` — real `ParquetStore`: buffers rows per
  `(dataset, asset, date)`, auto-flushes at a configurable row count,
  Hive-style partitioning matching `docs/05_parquet_schema.md`.
- `src/storage/database.py` — added `record_market_data`/
  `record_orderbook_features`/`record_trade_features` insert helpers and a
  `SQLiteRecorder` convenience wrapper (one transaction per snapshot, not
  one per row) on top of the Phase-1 schema.
- `src/main.py`: raw l2Book levels and trades are persisted to Parquet as
  they arrive (`market_state_consumer` now takes an optional
  `ParquetStore`); a new `sqlite_snapshot_writer` task commits one
  SQLite snapshot per asset every `sqlite_snapshot_interval_s` (default
  5s, configurable in `settings.yaml`); pending Parquet buffers are
  flushed on shutdown so nothing is silently lost on Ctrl+C.
- `docs/05_parquet_schema.md` corrected against reality: the Phase-1
  speculative `sequence` column doesn't exist in Hyperliquid's feed
  (confirmed Phase 2) — replaced with `n_orders`, which Hyperliquid does
  send.
- 20 new tests (86 total): `ParquetStore` buffering/flushing/partitioning,
  SQLite insert helpers and schema creation, and `main.py`'s storage
  wiring — all using `tmp_path`, no network. Also manually smoke-tested
  end-to-end: a synthetic snapshot+trade sequence through the real queue
  consumer produced correct, independently-readable rows in both a real
  SQLite file and real Parquet files.

## Phase 5 — what shipped (2026-08-18)

Source list expanded from the Phase-1 placeholder (3 feeds) to 24, per the
user's explicit request to maximize coverage within Pi 5 4GB hardware
limits — research concluded feed count isn't the real constraint (see
`docs/10_news_radar.md`). X/Twitter and Reddit deferred by user decision
(no free X tier in 2026; Reddit gated behind a June-2026 approval
process), not implemented this phase. Google News RSS kept disabled — its
unofficial endpoint's `robots.txt` disallows automated fetching.

- `config/sources.yaml`: 16 crypto-media + 8 official SEC/Fed feeds, plus
  a new `rss_polling` block (staggered polling, concurrency cap, timeout,
  user agent) tuned for the Pi.
- `src/collectors/rss_collector.py` — real `RSSCollector`: one polling
  task per feed (fault isolation), semaphore-capped concurrent fetches,
  actual `robots.txt` enforcement (`RobotsCache`, fails open), per-feed
  reconnect/backoff on fetch errors, bounded per-feed "seen" set.
- `src/collectors/news_collector.py` — thin `NewsCollector` fan-out
  wrapper (RSS only today; ready for Google/social later without a
  `main.py` wiring change).
- `src/news/{classifier,sentiment,relevance,credibility,novelty,
  deduplication,event_detector}.py` — real implementations: category
  classification, lexicon-based sentiment (deliberately not a transformer
  model, section 63), keyword relevance + affected-asset extraction,
  per-source credibility lookup, title-similarity novelty/dedup (batch +
  streaming), exponential decay.
- `src/news/pipeline.py` — new `NewsPipeline` orchestrator composing all
  of the above into one call per raw item.
- `src/storage/parquet_store.py` — additive change: `asset=None` support
  for non-per-asset datasets (news isn't tied to one symbol).
- `src/storage/database.py` — `record_news_event()` / `SQLiteRecorder.
  write_news_event()`, idempotent on `event_uid`.
- `src/main.py` — `news_consumer` (raw→Parquet, scored/relevant→SQLite)
  and `news_live_display` (rolling "recent relevant news" panel with
  decayed impact) wired alongside the existing market-data tasks.
- 79 new tests (165 total) — all offline; RSS fetch logic tested via
  `httpx.MockTransport`, no real network calls anywhere in the suite.

**Validated live 2026-08-18**: user ran `python -m src.main`, confirmed
24 feeds polling, robots.txt correctly blocking a disallowed feed
(AMBCrypto), and real headlines landing in the console and `news_events`.
The run also surfaced two real bugs, both fixed the same day:

- CoinDesk/U.Today feeds failed every poll — httpx doesn't follow
  redirects by default, and both URLs redirect. Fixed:
  `follow_redirects=True` on the collector's HTTP client.
- Naive substring keyword matching produced false positives — an SEC
  litigation release against an unrelated party was scored relevant, and
  another was incorrectly tagged `affected_assets: ["SOL"]`, both because
  short tickers/abbreviations (sec, eth, sol, ...) matched *inside*
  unrelated words, and the SEC's own institutional name trivially
  satisfied the "exchange" keyword. Fixed: word-boundary-aware matching
  (`relevance.py: keyword_matches()`) plus boilerplate stripping, shared
  by both `score_relevance()` and `classify_event()`. Regression tests use
  the exact real examples from the log. See `docs/10_news_radar.md`.

CryptoSlate returns 403 on both its `robots.txt` and its feed — left
as-is (looks like anti-bot blocking a non-browser User-Agent; spoofing a
browser UA to get past it would violate section 20's
`never_bypass_anti_bot` rule). It backs off and contributes zero items;
the other 23 feeds are unaffected.

## Gate to Phase 6

Per section 73: **do not proceed automatically.** Before Phase 6 (News +
market sync — confirm shared UTC clock, `ingestion_time` discipline end to
end):

1. ~~Run `python -m src.main` locally and confirm real news items appear
   in the console and land in `news_events`~~ — done, validated
   2026-08-18; also caught and fixed two real bugs (redirects, false-
   positive relevance matching — see above).
2. ~~Re-run to confirm the fixes hold against live feeds~~ — done,
   validated 2026-08-18: CoinDesk (308) and U.Today (301) both now follow
   their redirect and land items correctly; the Federal Reserve feed's
   relevant-rate dropped from near-100% to 1/20 in this run, confirming
   the false-positive fix is working against real content, not just the
   regression fixtures.
3. ~~Explicitly say "go" for Phase 6~~ — done, 2026-08-18.

**Phase 5 closed.**

## Phase 6 — what shipped (2026-08-18)

Scoped as an audit/verification phase per `docs/07_collection_strategy.md`
("confirm news `ingestion_time` and market data timestamps are on a
common clock... before any feature work begins") — not a new subsystem;
Phase 7's `feature_engine.py` is what actually joins news and market data.

- `docs/11_news_market_sync.md` — new audit document: every timestamp
  field in the system traced to its source and confirmed UTC/tz-aware
  (§1), the no-look-ahead join contract restated and now mechanically
  tested rather than just asserted (§2), OS-clock-accuracy/NTP explicitly
  scoped OUT to Phase 17's `SystemMonitor` so it isn't half-built twice
  (§3), and the bug found + fix (§4, below).
- **Real bug found and fixed**: `NewsEvent.event_time`/`ingestion_time`
  (and every other `DateTime(timezone=True)` column in the schema) lost
  `tzinfo` on read-back from SQLite — confirmed via a round-trip script
  showing `tzinfo: None` after writing a tz-aware value. SQLite has no
  native datetime type and doesn't enforce tz-preservation the way
  Postgres does. Fixed with a new `UTCDateTime` `TypeDecorator` in
  `src/storage/database.py` (converts to UTC and strips tzinfo on write,
  re-attaches `tzinfo=UTC` on read, raises `ValueError` on a naive
  datetime input) — applied to all 11 timestamp columns across
  `NewsEvent`, `MarketData`, `OrderbookFeatures`, `TradeFeatures`,
  `Signal`, `Order`, `Fill`, `Position`, `PerformanceSnapshot`.
- `tests/test_clock_discipline.py` — 7 new tests: RSS `normalize_entry`
  and `_parse_event_time` produce tz-aware UTC timestamps; Hyperliquid's
  `ts_received` is confirmed to be plain epoch-nanoseconds (inherently
  UTC, no tzinfo ambiguity possible); the SQLite round-trip bug above has
  a dedicated regression test; a naive datetime is rejected at insert
  time; and the core no-look-ahead property — "visible as of t" depends
  only on `ingestion_time`, never `event_time`, and is invariant to
  insertion order — is tested against the real `NewsEvent` schema and
  `record_news_event()` insert path (not a mock), using synthetic events
  with deliberately shuffled insertion order and event_times that
  disagree with ingestion_times.
- 7 new tests (172 total). Full suite re-run after the `UTCDateTime`
  change confirmed no regressions — no existing code constructs `Order`/
  `Fill`/`Position`/`PerformanceSnapshot`/`Signal` rows with naive
  datetimes, so the new strict-tz-aware guard doesn't break anything that
  was already working.

This phase's audit is entirely reproducible offline (real SQLite I/O
against `tmp_path`, no network) — no live-run validation needed, unlike
Phases 2/4/5.

## Gate to Phase 7

Per section 73: **do not proceed automatically.** Before Phase 7 (Feature
engineering — `feature_engine.py`, per the spec in `docs/06_features_v1.md`):

1. Review this delivery (docs/11_news_market_sync.md, the `UTCDateTime`
   fix in `src/storage/database.py`, `tests/test_clock_discipline.py`).
2. Explicitly say "go" for Phase 7.

**Phase 6 closed, validated by you — "Go" (2026-08-18).**

## Phase 7 — what shipped (2026-08-18)

Before implementing, the user was asked one scoping question with real
cost/rigor tradeoffs: docs/06_features_v1.md's grain ("one row per
(asset, timestamp), e.g. every 1s") isn't what's actually stored on a
fixed clock today — Phase 4's SQLite snapshots commit every 5s. Reusing
them would ship faster but make `returns_1s` fake precision (a relabeled
5s return). Building a proper raw-tick replay engine off the Parquet data
already captured since Phase 4 would match the spec exactly and give a
real `returns_1s`, at the cost of more new code this phase — and is very
likely needed again for Phase 10's backtester anyway. User chose the
raw-tick replay engine.

- `src/storage/parquet_reader.py` — new read-back counterpart to Phase
  4's `ParquetStore` writer (`read_partition`/`read_partitions`/
  `date_range`), kept separate so the already-validated writer doesn't
  need touching.
- `src/features/replay.py` — reconstructs the exact tick stream Phase 2-4
  captured (one row per book level per snapshot, one row per trade) back
  into `l2Book`/`trades` events shaped exactly like
  `collectors.hyperliquid_ws.parse_hyperliquid_message()`'s output, so
  they replay through the same `OrderBookEngine` (Phase 3) the live
  console uses. Also `net_sweep_volume()` — a new `sweep_score` signal
  (signed volume of detected sweep bursts within a trailing window), a
  narrower measure than `trade_imbalance` since it only counts trades
  that formed a detected burst.
- `src/signals/regime_detector.py` — implemented (was a Phase 1 stub):
  `RegimeDetector`, one per asset, classifies `volatility` into
  LOW/NORMAL/HIGH/EXTREME_VOL against quantile boundaries computed from
  its own rolling history (same self-relative-threshold convention
  `orderbook/liquidity.py` uses for wall detection — no hardcoded
  per-asset constants).
- `src/features/feature_engine.py` — `FeatureEngine` (one per asset,
  mirrors `OrderBookEngine`'s convention) replays raw ticks + canonical
  news events through a fixed sampling clock (default 1s) to produce
  every docs/06 column; `compute_targets()`, a structurally separate
  function for section 28's forward-looking labels, called only after
  the full feature table exists so nothing it computes can leak back
  into a feature column. See `docs/12_feature_engine.md` for every
  column-by-column decision docs/06 didn't already pin down (which
  trade-flow window, how `sweep_score`/`absorption_score` are defined,
  how the news columns reconstruct fields Phase 5 never persisted, the
  broad-macro-news-with-no-asset-tag decision, `news_age`'s
  `ingestion_time`-only definition).
- `src/storage/database.py` — `list_canonical_news_events()`, a read-only
  query helper so `FeatureEngine` stays DB-agnostic (fed plain dicts, not
  a live session).
- `scripts/build_feature_table.py` — new batch CLI wiring all of the
  above together: reads raw Parquet + `news_events` SQLite, builds the
  feature table, writes `features_v1` to `data/features/`. Deliberately
  not wired into `main.py`'s live loop — this is a research/batch step
  over already-collected history, run on demand.
- 45 new tests (217 total): `test_parquet_reader.py`,
  `test_replay.py`, `test_regime_detector.py`, `test_feature_engine.py`
  (including the no-look-ahead property tests docs/09 promised for this
  phase: shuffling/removing future market or news events never changes
  an earlier row's feature columns; target computation matches a
  hand-computed forward-return example and never mutates or reads back
  through the feature columns it's attached to).

**Real bug found via a smoke-test run** (not a fixture — running
`scripts/build_feature_table.py` end to end against synthetic raw data):
a long run of identical volatility observations (a book that stops
moving) collapsed `RegimeDetector`'s quantile thresholds to a single
value, and the naive `>=` classification chain then flagged every
subsequent identical (i.e. calm) observation as `EXTREME_VOL` — backwards
for what is definitionally the least volatile state. Fixed in
`detect_regime()` with a narrow degenerate-threshold guard (only
suppresses to `NORMAL_VOL` when the new observation is indistinguishable
from a fully flat history; a genuine outlier against a flat history still
classifies normally). Two regression tests added. Also right-sized
`ParquetStore`'s `flush_every` in the build script (default 500 would
have written ~170 tiny part files per asset per day at 1s sampling;
raised to 20,000 to stay closer to docs/05's "roughly 64-256MB per part
file" target) — caught the same way, by actually running the script
before calling this done.

This phase's tests are entirely offline (hand-built synthetic tick/news
sequences, real Parquet/SQLite I/O against `tmp_path`) — no live network
needed, same as Phase 6. `scripts/build_feature_table.py` was smoke-run
end to end against synthetic data during development (not shipped) to
catch exactly the regime-detector and file-count issues above; a full
live-collected-data run is still worth doing once enough raw history has
accumulated on your machine, but isn't a gate condition the way Phases 2/
4/5's live validation was — this phase's correctness claims are all
mechanically testable offline.

## Gate to Phase 8

Per section 73: **do not proceed automatically.** Before Phase 8
(Baseline strategies — momentum/OBI/trade-imbalance/news-only baselines,
section 31):

1. Review this delivery (`docs/12_feature_engine.md`,
   `src/features/feature_engine.py`, `scripts/build_feature_table.py`,
   the `RegimeDetector` fix).
2. Optionally run `python -m scripts.build_feature_table --start <date>
   --end <date>` against real collected data once enough has accumulated,
   to see actual feature rows rather than the synthetic smoke-test
   values above.
3. Explicitly say "go" for Phase 8.

**Phase 7 closed, validated by you — "Go" (2026-08-18).**

## Phase 8 — what shipped (2026-08-18)

Baseline strategies (section 31, `docs/08_backtesting_strategy.md`'s
"order of operations": `baselines -> strategy hypotheses -> statistical
models -> walk-forward -> statistical validation`). Scope taken directly
from `baseline_model.py`'s own Phase-1 docstring (momentum, OBI, trade
imbalance, news score, volatility regime, order flow + momentum, news +
order flow — 7 kinds) plus docs/09_tests_plan.md's placeholder for this
phase ("baselines beat a flip of a coin reference").

- `src/models/baseline_model.py` — implemented (was a Phase 1 stub):
  `baseline_signal(features, kind, **params)`, one line of arithmetic per
  kind over Phase 7's feature columns. Four are direct passthroughs
  (momentum/OBI/trade_imbalance/news); `volatility_regime` is
  regime-conditional momentum (trend-following in LOW/NORMAL_VOL,
  sign-flipped in HIGH/EXTREME_VOL — `market_regime` alone has no
  direction); the two combination baselines
  (`order_flow_momentum`/`news_order_flow`) are **plain unweighted
  addition**, deliberately not normalized or tuned, since any weighting
  scheme would itself need the kind of empirical justification a
  baseline exists to be measured against, not have baked in. See
  `docs/13_baseline_strategies.md` for the full reasoning, including a
  flagged (not silently resolved) discrepancy: `config/strategy.yaml`'s
  `strategies_under_test` only benchmarks 5 of these 7 in the eventual
  Phase 10 comparison sweep.
- `src/models/evaluation.py` — new file (not a Phase 1 stub):
  `evaluate_direction_accuracy()`, a one-sided exact binomial test
  (`scipy.stats.binomtest`) of a baseline's sign against Phase 7's
  `compute_targets()` labels — "beats a coin flip on direction," nothing
  about profitability after costs (that stays Phase 10's job) or
  calibration (Phase 9's). `evaluate_all_baselines()` runs all 7 kinds at
  once.
- 21 new tests (238 total): `test_baseline_model.py` (all 7 kinds, both
  volatility-regime branches, missing-data handling, unknown-kind
  rejection) and `test_baseline_evaluation.py` — including two
  deliberately-constructed-not-randomly-sampled tests so the significance
  check is verified in both directions: a perfectly correlated baseline
  must reject the coin-flip null, and an exact-50/50-by-construction
  baseline must **not** — the second is the one that actually matters,
  since a check that only ever says "yes, significant" isn't testing
  anything.

No live-run or dedicated CLI script needed this phase (unlike Phase 7's
`build_feature_table.py`) — running the baselines against real collected
data is a two-line job once a feature table exists (shown in
`docs/13_baseline_strategies.md`); a dedicated script is deliberately
deferred until there's enough real history collected to make running one
more than a smoke test.

## Gate to Phase 9

Per section 73: **do not proceed automatically.** Before Phase 9
(Statistical models — logistic regression -> gradient boosting,
calibration, `model_registry`, section 30):

1. Review this delivery (`docs/13_baseline_strategies.md`,
   `src/models/baseline_model.py`, `src/models/evaluation.py`).
2. Optionally run the baseline evaluation against real collected feature
   data once enough has accumulated.
3. Explicitly say "go" for Phase 9.

**Phase 8 closed, validated by you — "Go" (2026-08-18).**

## Phase 9 — what shipped (2026-08-18)

Statistical models (section 30): logistic regression, gradient boosting,
probability calibration, and a model registry, all sharing the row-based
`fit(rows)`/`predict_proba(rows)` contract every Phase 7-9 module already
uses. Full reasoning in `docs/14_statistical_models.md`.

- `src/models/feature_matrix.py` — new file (not a Phase 1 stub): shared
  `feature_columns()`/`rows_to_frame()`/`extract_labels()` so the two
  models can never select features or extract labels differently.
  `extract_labels()` reuses `models/evaluation.py: actual_direction()`
  (promoted from private `_actual_direction` to public specifically for
  this reuse), so baseline evaluation (Phase 8) and model training labels
  (Phase 9) can't silently disagree on what up/down/flat means.
- `src/models/logistic_model.py` — implemented (was a Phase 1 stub):
  `LogisticDirectionModel`, one instance per
  `(asset, horizon_s, threshold_bps)`. Median-impute + standard-scale
  numeric columns, one-hot `market_regime`, `LogisticRegression`.
- `src/models/tree_model.py` — implemented (was a Phase 1 stub):
  `TreeDirectionModel`, same contract, `HistGradientBoostingClassifier`
  (native NaN tolerance — numeric columns passed through unimputed,
  unlike the logistic model — and the lighter histogram-binned
  implementation, matching section 63's "keep heavy compute light"
  concern even though training itself is offline).
- `src/models/calibration.py` — implemented (was a Phase 1 stub):
  `calibrate()` (diagnostics: multiclass Brier score, log loss,
  macro-averaged one-vs-rest ROC-AUC, per-class reliability curves —
  never modifies the model) and `fit_calibrated()` (actual Platt/isotonic
  recalibration via `CalibratedClassifierCV(FrozenEstimator(...))` on a
  held-out validation set). Required bumping `requirements.txt` to
  `scikit-learn>=1.6` for `sklearn.frozen.FrozenEstimator` — the
  installed scikit-learn 1.8 has fully removed the old
  `CalibratedClassifierCV(estimator, cv="prefit")` pattern. Smoke-testing
  this module before delivery caught a second, subtler scikit-learn 1.8
  behavior change: `roc_auc_score` no longer raises when a class is
  absent from validation labels, it silently returns `nan` — fixed with
  an explicit class-presence check (`_safe_roc_auc_macro()`) rather than
  trusting an exception that scikit-learn no longer raises. Full account
  in `docs/14_statistical_models.md`.
- `src/models/model_registry.py` — implemented (was a Phase 1 stub):
  `ModelRegistry`, an append-only JSONL audit log (`register()` /
  `promote()` / `list_models()` / `list_promotions()` /
  `current_promoted()`). Promotion requires a non-empty `reason`, refuses
  to promote a `model_version` never registered for the exact
  `(asset, horizon_s, threshold_bps)` key being promoted, and is never
  called automatically by anything in this codebase — section 59's "no
  model is auto-deployed" enforced in code, not just documented.
- 57 new tests (295 total): `test_feature_matrix.py`,
  `test_logistic_model.py`, `test_tree_model.py`, `test_calibration.py`
  (including a regression test reproducing the `roc_auc_macro`/`nan` bug
  above), `test_model_registry.py`.

No live-run or dedicated CLI script this phase (same reasoning as
Phase 8) — training/calibrating/registering a model against real
collected feature data is a research-side job once enough history has
accumulated, not something to wire into `main.py`'s live loop yet.

## Gate to Phase 10 — and a change in how gates work for the rest of this session

Per section 73: **do not proceed automatically** is still the rule
between sessions/reviews. Within *this* session, the user explicitly
authorized continuing through subsequent phases without a per-phase
"go" ("continue les phases jusqu'à épuisement de la session actuelle...
comme déjà eu au phases précédente" — 2026-08-18), after being told the
remaining phase count and scope up front. That blanket authorization is
what Phase 10 (and any phase after it, this session) proceeds under —
recorded here explicitly rather than silently treated as equivalent to
the phase-by-phase "Go" messages Phases 6-9 each received individually,
since it is a materially different kind of approval and the difference
should stay visible in this log. A genuine cost/rigor tradeoff decision
(the kind Phase 5's source-coverage question or Phase 7's replay-grain
question raised) still gets an explicit `AskUserQuestion` regardless of
this authorization — the blanket "go" covers *pacing* between phases,
not skipping decisions that are genuinely the user's to make.

**Phase 9 closed, validated by you — "Go" (2026-08-18).**

## Phase 10 — what shipped (2026-08-18)

Backtesting (sections 44-46): a chronological, no-look-ahead event
backtester, an order-flow-only ablation variant, realistic-cost PnL/risk
metrics including section 46's bootstrap/Monte-Carlo/ruin stress tests,
and purged walk-forward validation. Full reasoning in
`docs/15_backtesting.md`.

- `src/features/feature_engine.py` — refactored (not a new stub):
  `compute_targets()`'s internal two-pointer forward-price scan extracted
  into public `find_forward_indices()`/`find_forward_prices()`, so
  `event_backtester.py` can reuse the exact same no-look-ahead-safe
  lookup for trade exit simulation instead of re-deriving it. Pure
  extraction — all 21 pre-existing tests pass unchanged; 5 new regression
  tests added.
- `src/backtest/metrics.py` — implemented (was a Phase 1 stub):
  `compute_metrics()` (PnL, fees, win rate, expectancy, profit factor,
  per-trade and empirically-annualized Sharpe/Sortino, max drawdown,
  holding time), `bootstrap_confidence_intervals()`, `monte_carlo_reshuffle()`
  (reorders, never resamples — stress-tests drawdown/path risk, not PnL,
  since total PnL is order-invariant by construction), `probability_of_ruin()`
  (bootstrap-resampled equity *paths*, not a single statistic).
- `src/backtest/event_backtester.py` — implemented (was a Phase 1 stub):
  `EventBacktester` (fixed-horizon exit via `find_forward_indices()`,
  entry gated by probability/EV-net-of-costs/spread/regime from
  `config/strategy.yaml`'s `entry_filter`, `max_open_positions`-aware),
  plus `decisions_from_model()`/`decisions_from_baseline()` adapting
  Phase 8 baselines and Phase 9 models into one shared decision shape
  (`SignalEngine`/`StrategyEngine`, Phase 11, don't exist yet — this is a
  deliberately simple stand-in entry rule, not final production logic).
- `src/backtest/orderbook_backtester.py` — implemented (was a Phase 1
  stub): `strip_news_columns()` + `OrderBookBacktester`, enforcing a
  news-free ablation *structurally* (news columns removed, `KeyError` on
  access, not zeroed) rather than by convention — with a documented
  caveat that stripping must happen before a decision function scores
  the rows, not just before `run()`.
- `src/backtest/walk_forward.py` — implemented (was a Phase 1 stub):
  `WalkForwardValidator` (chronological-only `train -> validation ->
  test` windows, no random splits, purged by a full label horizon at
  each boundary) and `classify_window_regime()` (window-level
  bull/bear/sideways trend + a low/high volatility majority-vote reusing
  Phase 7's own `market_regime` column + a news-shock flag) — directly
  answering `docs/08_backtesting_strategy.md` point 3's "does this only
  work in one regime" question. An early implementation fit the model
  twice per window (once per slice) — caught by a test asserting
  `fit_fn` is called once per window, fixed to fit once and reuse the
  model for both evaluations.
- 69 new tests (364 total): `test_backtest_metrics.py`,
  `test_event_backtester.py`, `test_orderbook_backtester.py`,
  `test_walk_forward.py`, plus 5 new regression tests in
  `test_feature_engine.py` for the `find_forward_indices`/
  `find_forward_prices` extraction.

No live-run or dedicated CLI script this phase — running a real
backtest against real collected feature data is a research-side job
once there's enough real history, same reasoning as Phases 7-9.

## Phase 11 — what shipped (2026-08-18)

Signal generation + strategy orchestration (sections 26, 33-39): turning
a fitted Phase 9 model's raw probabilities into a full signal payload
(confidence, quality, expected value), deciding whether to act on one,
and managing the resulting position through to exit. Full reasoning in
`docs/16_signals_and_strategy.md`.

- `src/models/direction.py` — new (DRY extraction, not a Phase 1 stub):
  `direction_from_probabilities(proba) -> (direction, probability)`, the
  shared argmax primitive now used by `backtest/event_backtester.py:
  decisions_from_model()`, `signals/signal_engine.py`,
  `signals/scoring.py`, and `strategy/exit.py`'s `probability_reversal`
  check, so "which direction did the model pick" is never computed two
  different ways depending on whether it's backtested or live.
- `src/backtest/event_backtester.py` — refactored: `decisions_from_model()`
  now calls the shared primitive above (behavior-preserving — a
  regression test confirms it); new public `simulate_fill_costs()`
  extracted from `_build_trade()`'s inline cost math, now also reused by
  `strategy_engine.py`, so a backtested trade and a live/paper one are
  never computed by two silently-diverging implementations.
- `src/signals/confidence.py` — implemented (was a Phase 1 stub):
  `compute_confidence()` — calibration quality + data freshness +
  news/order-flow confirmation, weighted average, each component
  independently missing-tolerant (degrades to neutral, never to a
  penalty or a false positive).
- `src/signals/scoring.py` — implemented (was a Phase 1 stub):
  `classify_signal_quality()` — deliberately probability-only
  (`A`/`B`/`C`/`NO_TRADE`), kept separate from `confidence.py` on
  purpose since `strategy.yaml: signal_engine`'s config block is scoped
  for probability specifically.
- `src/signals/signal_engine.py` — implemented (was a Phase 1 stub):
  `SignalEngine` — batches `model.predict_proba()`, builds the full
  section-33 payload shaped to match `storage/database.py: Signal`'s
  column set.
- `src/strategy/entry.py` — implemented (was a Phase 1 stub):
  `should_enter()` — live counterpart to `EventBacktester.
  _passes_entry_filter()`, plus a freshness check the backtester can't
  perform on historical data. `risk_state`'s shape is a documented
  placeholder pending Phase 12's `RiskManager`.
- `src/strategy/exit.py` — implemented (was a Phase 1 stub):
  `should_exit()` — checks all 7 `strategy.yaml: exit_candidates` in
  config order, first match wins; each candidate degrades to "skip" (not
  "trigger") when its required data is missing.
- `src/strategy/strategy_engine.py` — implemented (was a Phase 1 stub):
  `StrategyEngine` — stateful open/closed position tracking across
  `process_row()` calls, an optional duck-typed `executor` standing in
  for Phase 13's execution interface, reuses `simulate_fill_costs()` for
  every close.
- 107 new tests (471 total): `test_direction.py`, `test_confidence.py`,
  `test_scoring.py`, `test_signal_engine.py`, `test_entry.py`,
  `test_exit.py`, `test_strategy_engine.py`, plus 8 new regression tests
  in `test_event_backtester.py`. `StrategyEngine` was additionally
  smoke-tested end-to-end (scripted fake model + real `SignalEngine`,
  synthetic price sequence, fake `executor` stub) before the formal
  suite was written.

No live-run or dedicated CLI script this phase — same reasoning as
Phases 8-10; wiring this into `main.py`'s live loop is a research-side
activation step that still needs Phase 12 (risk) and Phase 13 (paper
execution) to land first.

## Phase 12 — what shipped (2026-08-19)

Risk management + Telegram notifications (sections 40, 47-55, 66):
account-level hard limits with a manual kill switch, pure risk-based
position sizing, an anti-spam notification gate, and a synchronous,
network-free Telegram command/alert-formatting core. Built under this
session's standing authorization — no per-phase "go" requested; no
genuine cost/rigor tradeoff decision arose that warranted an
`AskUserQuestion` (two interpretive/config-inconsistency points were
documented rather than guessed at silently — see below). Full reasoning
in `docs/17_risk_and_telegram.md`.

- `src/strategy/risk_manager.py` — implemented (was a Phase 1 stub):
  `RiskManager` — tracks running equity/daily PnL/consecutive-loss streak
  across `record_trade()` calls (reading the exact same closed-position
  dict shape `StrategyEngine`/`EventBacktester` already produce), a
  manual `engage_kill_switch()`/`disengage_kill_switch()`, and a sticky
  `record_fill_quality()` breach for slippage/latency (no real caller
  yet — Phase 13/14). `state()` returns `strategy/entry.py`'s
  already-documented `{"blocked": bool, "reason": str|None}` shape
  exactly, so `StrategyEngine(risk_state=risk_manager.state)` needs no
  change on either side. Deliberately does not re-check
  `max_open_positions` (already structural in `StrategyEngine`); its
  `max_spread_bps`/`max_slippage_bps` (8/8) are a looser, hard
  account-level backstop layered *under* `entry_filter`'s tighter (5/5)
  tunable filter — flagged as intentional layering, not a bug.
- `src/strategy/position_sizing.py` — implemented (was a Phase 1 stub):
  `size_position()` — fixed-fractional risk-based base size, scaled only
  ever *down* by confidence/volatility/expected-value (each defaulting
  to a neutral no-op multiplier when missing, never a boost), clamped to
  an equity ceiling. No stop distance -> `0.0`, never a guessed default.
- `src/notifications/alert_manager.py` — implemented (was a Phase 1
  stub): `AlertManager.should_notify()` — a pure decision function (same
  "decide, never mutate" convention as `should_enter()`/`should_exit()`;
  `record_sent()` is an explicit separate mutation step). Structurally
  enforces section 51's 5-item `ALERT_TYPES` allowlist (`ValueError` on
  anything else), applies a stricter `signal_filter` than trading's own
  `entry_filter`, and layers independent cooldown/probability-delta-dedup
  checks with escalation overriding cooldown only. Two spec points
  flagged rather than silently resolved: `AGGRESSIVE` mode's stated "B"
  quality allowance is currently unreachable under the global
  `required_signal_quality: "A"` filter as literally written (a real
  config inconsistency); SNIPER's `require_exceptional_edge` is an
  interpretive choice (a further-tightened probability floor), not a
  spec-given number.
- `src/notifications/telegram_bot.py` — implemented (was a Phase 1
  stub): `TelegramBot.handle_command()` — a fully synchronous, plain
  string-in/string-out core for all 8 section-55 commands, independent
  of `python-telegram-bot`'s `Update`/`Context`/`Application` objects, so
  it's genuinely unit-tested with no network or credentials. Live
  `Application`/`CommandHandler` wiring deferred — no real Telegram bot
  token exists in this sandbox to smoke-test against, same reasoning as
  every other "no live-run this phase" call. `send_alert()` is
  synchronous and duck-typed (`bot_client.send_message(...)`), matching
  `strategy_engine.py`'s optional `executor` pattern.
- 83 new tests (554 total): `test_risk_manager.py`,
  `test_position_sizing.py`, `test_alert_manager.py`,
  `test_telegram_bot.py`. Two real bugs caught by the suite before
  delivery and fixed in the implementation:
  `RiskManager.engage_kill_switch()`'s `reason` parameter defaulted to
  the literal string `"manual"` instead of `None`, silently preventing
  `state()`'s `REASON_KILL_SWITCH` fallback from ever firing; and
  `telegram_bot.py`'s provider-formatting helper checked emptiness
  before branching on type, so an empty list was caught by a generic
  "no data" message before ever reaching its own "none" message.

No live-run this phase — same reasoning as Phases 8-11; no real
Telegram bot token exists in this sandbox, and there's no live
account/position feed yet since `main.py` isn't wired to `StrategyEngine`.

## Phase 13 — what shipped (2026-08-19)

Paper execution (sections 41, 43, 53-54, 70): an exchange-agnostic
execution ABC, a realistic paper-trading simulator against it, and
live/paper performance tracking that reuses Phase 10's backtest metrics
rather than reimplementing them. Built under this session's standing
authorization — no per-phase "go" requested; no genuine cost/rigor
tradeoff decision arose that warranted an `AskUserQuestion`. Full
reasoning in `docs/18_paper_execution.md`.

- `src/execution/execution_interface.py` — implemented (was a Phase 1
  stub): `ExecutionInterface`, an `abc.ABC` with 5 `@abstractmethod`s
  (`place_order`/`cancel_order`/`get_position`/`get_balance`/
  `get_open_orders`) — structurally impossible to instantiate an
  incomplete subclass, the same enforcement style
  `orderbook_backtester.py`/`alert_manager.py` already use. Flags a
  known integration gap rather than silently patching it:
  `StrategyEngine` (Phase 11) calls `executor.place_order(...)` but has
  never consumed its return value, so a real executor's simulated
  fill/rejection doesn't yet feed back into a strategy's own PnL
  tracking — left for a `main.py` integration step, not guessed at by
  changing already-tested Phase 11 code.
- `src/execution/paper_executor.py` — implemented (was a Phase 1 stub):
  `PaperExecutor(ExecutionInterface)` — simulated latency (sampled from
  a configurable range), spread-aware market fills, slippage drawn with
  real variance (not the fixed constant `simulate_fill_costs()` uses,
  by design — this module exists to stress-test that fixed-cost
  assumption), maker/taker fee distinction, configurable reject/partial-
  fill probabilities, limit orders that either cross immediately or sit
  `pending` until `cancel_order()`/`try_fill_pending()`, and its own
  running-inventory position/balance ledger (volume-weighted average
  cost, realized/unrealized PnL) — deliberately a different bookkeeping
  model than `StrategyEngine`'s trade-level tracking, not a replacement
  for it. Every decision is logged to an append-only `order_log`.
- `src/monitoring/performance.py` — implemented (was a Phase 1 stub):
  `PerformanceTracker` — accumulates closed-position dicts (same shape
  `StrategyEngine`/`EventBacktester` already produce) and windows them
  through `backtest/metrics.py: compute_metrics()` (Phase 10) for
  hourly/daily/all-time snapshots shaped to match `PerformanceSnapshot`'s
  exact DB columns; `to_performance_snapshot_row()` is a pure,
  DB-agnostic shaping helper. `best_setup`/`worst_setup` (aggregated by
  `exit_reason`, the only categorical dimension a closed-position dict
  carries besides `asset`) is flagged as an interpretive reading of an
  undefined spec term, not a given definition.
- 45 new tests (599 total): `test_execution_interface.py`,
  `test_paper_executor.py`, `test_performance.py`. One real bug caught
  by the suite before delivery and fixed in the implementation:
  `PerformanceTracker.trades_since()`'s original filter had no upper
  bound, so a trade closing *after* a snapshot's own `now` could leak
  into an earlier window — confirmed directly via a smoke test, fixed to
  require `cutoff <= closed_at <= now`, with a dedicated regression
  test.

No live-run this phase — same reasoning as Phases 8-12; no real
Kraken/exchange connection exists yet (Phase 14, explicitly gated on
paper validation), and there's no live signal/strategy feed to run
`PaperExecutor`/`PerformanceTracker` against until `main.py` wires
`StrategyEngine` through to them.

## Phase 14 — what shipped (2026-08-19) — delivered as inert, unactivated scaffolding

After Phase 13, the standing session authorization was deliberately
paused rather than continuing straight into live Kraken execution:
section 5 gates it on paper-trading validation, which hasn't happened
yet, and that's a materially different kind of step than Phases 10-13
(real capital risk, not more research/simulation code). The user was
asked directly how to handle it — wait for real paper-trading history,
build `KrakenExecutor` now as inert/unactivated code, or something else
— and chose the second option. This phase is that choice. Full reasoning
in `docs/19_kraken_executor.md`.

- `src/execution/kraken_executor.py` — implemented (was a Phase 1 stub):
  `KrakenExecutor(ExecutionInterface)` against Kraken's Spot REST API
  (`AddOrder`+`QueryOrders` for placement/confirmation, `CancelOrder`,
  `Balance`, `OpenOrders`) — **spot only**, not futures/margin; the
  "spot-vs-futures comparison (section 42)" this table's Phase 14 row
  names is left as an explicit follow-up once there's paper-trading
  history to inform it, not guessed at now.
- **Three independent gates, all defaulting to "off"**, the same
  defense-in-depth convention `RiskManager`'s account-level backstop
  already uses (Phase 12): `dry_run=False`, `confirm_live_trading=True`,
  and real `api_key`/`api_secret` must *all* be explicitly set before
  any method can reach Kraken's servers. Verified directly: every one of
  the 6 ways to satisfy exactly two of the three gates is tested and
  proven to stay inert. When inert, every method still returns a
  structurally valid, clearly `dry_run`-marked response and still logs
  to `order_log` (section 43) — so the full wiring path is safely
  exercisable today with zero real-money risk.
- Kraken's own HMAC-SHA512 request signing and strictly-increasing
  nonce; one follow-up `QueryOrders` call after `AddOrder` to confirm
  status (never a reconciliation loop, matching `PaperExecutor`'s
  "not a full matching engine" scope call); an unrecognized Kraken order
  status always falls back to `pending`, never guessed as `filled` or
  `rejected` — the same "missing information should never look more
  favorable/certain than it is" principle `position_sizing.py` (Phase
  12) already applies to missing multipliers, applied here to order
  state. `get_position()`'s `avg_price` is always `None` (Kraken spot
  has no cost-basis endpoint — flagged, not fabricated).
- 42 new tests (641 total): `test_kraken_executor.py` — every gate
  combination, signing/nonce correctness, every order-status mapping
  including the "never guess resolved" regression, and the real default
  HTTP transport verified against a mocked `httpx.post` (no test ever
  reaches Kraken's actual API).

No live-run this phase, and — deliberately — **no `main.py` wiring**:
nothing anywhere in this codebase constructs a `KrakenExecutor`. No real
Kraken API key exists in this sandbox to test against. Supplying real
credentials, flipping the three gates, and wiring one into
`StrategyEngine`'s `executor` slot is left entirely to the user, once
there's real paper-trading history to justify it.

## Phase 15 — delivered 2026-08-19, implemented & unit-tested

Live paper trading (sections 26, 33-43, 47-55, 65): the real execution
phase — every prior phase's built-but-never-connected pipeline (Phase
7's `FeatureEngine`, Phase 9's trained models, Phase 11's
`SignalEngine`/`StrategyEngine`, Phase 12's `RiskManager`/
`AlertManager`/`TelegramBot`, Phase 13's `PaperExecutor`/
`PerformanceTracker`) wired into one pipeline that actually runs,
gated behind PAPER mode, plus a step-by-step methodology for running
and evaluating a test. Requested directly by the user rather than
paced through like the phases before it. No Phase 1 stub file named
this phase (`grep -rn "Roadmap: PHASE" src/` found nothing for 15 —
flagged honestly at the time in the internal delivery log's "Full
phase map"); its scope was defined by the user's explicit request, not
guessed at. Full reasoning in `docs/20_live_paper_trading.md`.

- `src/features/feature_engine.py` — refactored (not a new stub):
  `build_rows()`'s offline batch loop extracted into public
  `on_market_event()`/`on_news_event()`/`build_row()`, so
  `live/trading_loop.py`'s live path and Phase 7's offline batch path
  share exactly one feature-computation implementation — verified by a
  dedicated equivalence test, not just asserted. Also fixed a real,
  previously-silent gap found while wiring this phase:
  `liquidity_imbalance` was never populated on feature rows, even
  though `StrategyEngine`/`strategy/exit.py`'s `liquidity_reversal`
  exit (Phase 11, already tested) read it — meaning that exit candidate
  has been permanently inert since Phase 11. Fixed, with a regression
  test.
- `src/models/model_persistence.py` — new: `save_model()`/`load_model()`
  via `joblib`, filling the one real gap in Phase 9's deliberately
  metadata-only `ModelRegistry` (an audit trail was the right Phase 9
  scope; actually loading a fitted model back in a different process is
  a genuine Phase 15 need). `model_registry.py` gained a matching
  optional, backward-compatible `artifact_path` field.
- `scripts/train_model.py` — new CLI: chronological, purged
  train/validation split (never random — section 45), fits/evaluates/
  saves/registers one `(asset, horizon_s, threshold_bps)` candidate;
  `--promote` requires `--reason`, matching `ModelRegistry.promote()`'s
  own requirement — no model is ever auto-deployed (section 59). Caught
  and fixed a real bug via its own smoke-testing: an unnormalized float
  `--threshold-bps` silently matched zero target columns
  (`compute_targets()` always writes plain-int-keyed columns) —
  normalized to `int` when whole, with a regression test.
- `src/live/trading_loop.py` — new: `AssetTradingUnit` (one asset's
  full live pipeline) and `PaperTradingEngine` (owns the shared
  account-level `RiskManager`/`AlertManager`/`TelegramBot`/
  `PerformanceTracker`, one `AssetTradingUnit` per asset with a
  promoted model — an asset with none is skipped, not fatal).
  `model_loader` is duck-typed, not a hard `ModelRegistry` coupling;
  `load_promoted_model()` is the real implementation `main.py` wires
  in. Alerts: `HIGH_CONVICTION_SIGNAL` every tick, `POSITION_OPENED`/
  `POSITION_CLOSED` off `StrategyEngine`'s own action list,
  edge-triggered `RISK_ALERT` (fires once on the blocked-state
  transition, not every subsequent blocked tick).
- `src/storage/database.py` — `positions`/`performance` tables have
  existed in the schema since Phase 4 with no write helper; gained
  `record_position()`/`record_performance_snapshot()` and
  `SQLiteRecorder.write_position()`/`write_performance_snapshot()`,
  following the exact same conventions every earlier `record_*`/
  `write_*` helper in that module already uses.
- `src/main.py` — wired, gated behind `paper_trading_enabled()`'s
  section-65 two-gate check (`trading_mode.default == "PAPER"` in
  settings.yaml AND `PAPER_TRADING=true` in .env — settings.yaml has
  documented this requirement since Phase 1; this phase is what
  actually enforces it in code). With either gate closed (the default),
  every addition this phase made is inert and `main.py`'s behavior is
  byte-for-byte unchanged. When both are open: builds `RiskManager`/
  `AlertManager` explicitly mapped from `strategy.yaml`/`telegram.yaml`
  (not just relying on matching `DEFAULT_*` constants), forwards the
  same live l2Book/trades/news events the existing consumers already
  receive into the engine, and runs three new periodic tasks
  (`paper_trading_tick_loop`, `paper_trading_performance_writer`,
  `paper_trading_display`). `build_paper_trading_engine()` still starts
  every engine with `telegram_bot=None` — Phase 12's `TelegramBot` is
  real, tested logic, the same "built, not activated" call already made
  for Phase 14's `KrakenExecutor` — but `main.py` now also calls
  `wire_live_telegram()` (Phase 15 addendum, see below), which replaces
  that `None` with a real, live-polling bot the moment
  `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are both set in `.env`; with
  neither set, alerts still fall back to structured console logging,
  unchanged.
- `config/settings.yaml` — new `paper_trading:` block (`tick_interval_s`,
  `horizon_s`/`threshold_bps`, `initial_cash`/`account_equity`,
  `performance_snapshot_interval_s`).
- `docs/20_live_paper_trading.md` — every implementation decision above
  in full. `docs/21_paper_trading_methodology.md` — the requested
  step-by-step guide to actually running and evaluating a PAPER-mode
  test, including what to look for before even considering Phase 14's
  live-Kraken gate.
- 47 new tests (699 total): `test_feature_engine.py` (+4),
  `test_model_registry.py` (+2), `test_model_persistence.py` (5, new),
  `test_train_model.py` (10, new — full CLI runs against a real
  synthetic `features_v1` table under `tmp_path`), `test_trading_loop.py`
  (15, new — a full open→close→alert→risk→performance cycle against a
  real fitted model, edge-triggered `RISK_ALERT`, `load_promoted_model()`
  against a real registry/artifact), `test_database_storage.py` (+6),
  `test_main_paper_trading_wiring.py` (16, new — the two-gate check,
  config-to-constructor mapping against the real YAML files, event
  forwarding, and the three new periodic tasks including a
  tick-exception-doesn't-kill-the-loop regression).

Every new class was also manually smoke-tested end-to-end (a scripted
`PaperTradingEngine` run against a synthetic, strongly-imbalanced order
book — a real open → take-profit exit → risk/performance bookkeeping →
alert cycle) before the formal suite was written, matching this
project's established discipline. **699 tests total, all offline** — no
live-run in this sandbox (no live Hyperliquid connection, no trained
model here to promote), same reasoning as most prior phases; running an
actual live-paper session now needs to happen on the user's own
machine, per `docs/21_paper_trading_methodology.md`.

### Phase 15 addendum — Telegram live wiring, delivered 2026-08-19

Added the same day, once the user configured a real
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in `.env` — the exact trigger
`telegram_bot.py`'s own Phase 12 docstring had named as the reason
`register_handlers()` was deferred.

- `src/notifications/telegram_live.py` (new file) — `AsyncioTelegramBotClient`
  (a sync-signature shim over the async-only `telegram.Bot.send_message`,
  scheduling via `asyncio.create_task()`, exceptions caught and logged
  inside the task rather than left to propagate), `register_handlers()`
  (one `CommandHandler` per `telegram_bot.py: COMMANDS` entry, each
  enforcing a chat-ID allowlist that silently ignores anything but the
  configured `chat_id`), and `build_live_telegram_bot()` (returns
  `(None, None)` when not configured, otherwise a real `Application` +
  `TelegramBot` pair with every provider bound to `PaperTradingEngine`'s
  own methods).
- `src/main.py` — new `wire_live_telegram()` function, called right after
  `build_paper_trading_engine()`; on success, mutates
  `paper_trading_engine.telegram_bot` and every `unit.telegram_bot` in
  place (both plain instance attributes — no `trading_loop.py` change
  needed) and returns the `Application` for `run()` to drive: manual
  `initialize()`/`start()`/`updater.start_polling()` right before the
  existing `await stop_event.wait()`, and `updater.stop()`/`stop()`/
  `shutdown()` in the same `finally` block every other task is cancelled
  in — deliberately not `run_polling()`, which would take over the event
  loop `main.py` already shares with a dozen other tasks. Not configured
  (either `.env` var missing) leaves everything byte-for-byte unchanged.
- `docs/20_live_paper_trading.md` — new "Telegram live wiring" section
  with the full design rationale. `requirements.txt`'s
  `python-telegram-bot>=21.0` (declared since Phase 1, unused until now)
  annotated as actually consumed, tested against 22.8.
- 20 new tests (719 total): `test_telegram_live.py` (15, new — the
  adapter layer in isolation, no real token/network) and
  `test_main_telegram_wiring.py` (5, new — `wire_live_telegram()` itself,
  with the default-off regression as the case that matters most).

Both smoke-tested manually first (a real local `Application`, handlers
invoked directly against duck-typed fakes) before the formal suites were
written, matching this project's established discipline. **719 tests
total, all offline** — no real Telegram token or network access in any
test. Phase 15 (base + addendum) closed, delivered to the user's machine
2026-08-19.

### Phase 15 addendum 2 — `artifact_path` bug fix + first live PAPER session, 2026-08-25

Found the hard way, on the very first real end-to-end PAPER launch (the
two-machine split from `docs/22_raspberry_pi_data_collection.md`: models
trained on the PC, `python -m src.main` running PAPER mode on the Pi).
`scripts/train_model.py` registered a promoted model's `artifact_path`
relative to `PROJECT_ROOT` (`data/models/<file>.joblib`), but
`src/live/trading_loop.py: load_promoted_model()` — the function
`main.py` actually wires into `PaperTradingEngine` — joins it as
`model_registry_root / artifact_path`, where `model_registry_root` is
already `data/models`. The two conventions disagreed from the start; it
just went uncaught because `train_model.py` and `load_promoted_model()`
were only ever tested in isolation, never through the real path a
promoted model actually takes into a running engine. On Windows the
doubled segment was additionally backslash-separated (`Path.relative_to()`
str's with `\` on Windows), which is inert on POSIX (not a separator, just
a literal character) — so the failure mode when reading a
Windows-trained registry from the Pi was `data/models/data\models\<file>`,
not found.

- `scripts/train_model.py` — `artifact_path` is now stored relative to
  `artifacts_dir` (== `model_registry_root`) via `.relative_to(artifacts_dir)`,
  and `.as_posix()`'d for cross-platform portability.
- `tests/test_train_model.py` — updated the two assertions that
  (accidentally) encoded the old, buggy convention; added
  `test_promoted_model_actually_loads_via_load_promoted_model()`, a real
  end-to-end regression test that runs `train_model.py --promote` and
  then loads the result back through the actual `load_promoted_model()`
  — the exact integration this bug fell through the gap of.
- The already-corrupted `registry.jsonl` entries on the Pi (13, spanning
  earlier BTC/ETH/SOL training runs) were repaired in place with a
  one-off script rather than regenerated, to preserve the run history.
- **720 tests total, all passing.**

Immediately after: first successful live PAPER session, all three assets
(`BTC`, `ETH`, `SOL`) with promoted models at `horizon_s=60,
threshold_bps=10`, confirmed via `PAPER TRADING ENABLED ... live
assets: ['BTC', 'ETH', 'SOL']` and `TELEGRAM LIVE-WIRED: ...` in the
Pi's `journalctl` output, running as `crypto-radar.service`. BTC's
promoted model: accuracy=60.5% (n=751, p=5.6e-09) on 2026-08-21..24
data — the same `(horizon_s, threshold_bps)` pair validated across two
separate training runs (753→113 rows on 3 days, then 751 rows on 4 days)
rather than a config found by searching multiple combinations, per the
multiple-comparisons caution raised when it was first selected. ETH and
SOL trained on the same window and promoted at the same config (p=2.9e-05
and p=0.0067 respectively) without any additional threshold search.

### Phase 15 addendum 4 — `/signals` message-too-long fix, 2026-08-25

Found the same way as addendum 3's bug: the user reported `/signals`
"does nothing" on Telegram, right after `/performance` worked fine on
the same chat/bot. Root cause, confirmed numerically before fixing
anything: `signals/signal_engine.py: SignalEngine._build_one()` embeds
`features_snapshot` (the entire raw feature row, ~26+ columns) and
`model_calibration` (the promoted model's full calibration-curve
diagnostics — a deeply nested per-class reliability-bucket dict) in
every signal payload, and `telegram_bot.py`'s old `_handle_signals()`
dumped each signal's *full* dict repr into one message via the generic
`_format_provider_result()`. Measured directly against a real signal
shape built from this session's own promoted-model calibration data:
**one asset's formatted line = 2733 characters; three assets = ~8200
characters**, roughly double Telegram's ~4096-character message limit —
`Message.reply_text()` rejects that outright, and `python-telegram-
bot`'s default error handling only logs the exception, never surfaces
it back to the chat, so the user saw nothing at all. `/performance`
worked because `PerformanceTracker`'s snapshot dict is 8 short scalar
fields — nowhere near the limit.

- `src/notifications/telegram_bot.py` — `_handle_signals()` now renders
  a lean, one-line-per-asset summary (`_format_signal_summary()`):
  asset, direction, `signal_quality`, `probability_up`/`probability_down`,
  `expected_value`, `market_regime`, `timestamp` — deliberately excluding
  `features_snapshot`/`model_calibration` (still fully available to any
  caller reading the dict directly, e.g. a notebook or `PerformanceTracker`
  — this is a Telegram-rendering fix only, not a payload change).
  A shape check (list of dicts each with `asset`+`signal` keys) falls
  back to the pre-existing generic formatter for anything else, so a
  test double or a future caller with a different provider shape still
  works unchanged.
- `tests/test_telegram_bot.py` — 5 new tests: the lean summary excludes
  both oversized fields; three real-shaped signals stay under 4096
  characters (the exact regression case); missing optional fields
  degrade to `"n/a"` rather than crashing; an empty list still says
  "none"; a non-signal-shaped provider result still falls back to the
  generic formatter untouched.
- **725 tests total, all passing.**

Delivered alongside this fix: confirmation that the earlier
`entry_filter.probability_up_min: 0.62` / `telegram.yaml:
signal_filter.min_probability: 0.68` diagnosis (zero PAPER trades in
~20h) is very likely the real, separate root cause of *why* there was
nothing to show `/signals` in the first place — the promoted BTC/ETH/
SOL models' own calibration curves (already in `registry.jsonl`) never
predict an individual up/down probability above roughly 0.30, so these
thresholds — set generically in Phase 11, before any real model
existed, and explicitly commented as "must be re-optimized" — reject
essentially every tick. Not yet changed in config; the user was pointed
at `/signals` (now actually usable) to confirm live before any
threshold is touched.

### Phase 15 addendum 5 — real Kraken fees expose a structural zero-trades wall, 2026-08-26

Follow-through on addendum 4's closing note. Built `scripts/
sweep_entry_threshold.py` (reuses `EventBacktester`/`decisions_from_
model()`/`compute_metrics()`, read-only, never touches live config) to
test whether lowering `entry_filter.probability_up_min` below 0.62
would unblock trades. Along the way, two smaller bugs surfaced and were
fixed before the sweep could even run:

- The PC's own `data/models/registry.jsonl` still had addendum 2's
  `artifact_path` bug (`PROJECT_ROOT`-relative, not `artifacts_dir`-
  relative) for all 10 `register` entries — only the Pi's copy had been
  repaired, over SSH, in addendum 2. Repaired the PC's copy the same
  way (10 entries fixed).
- Confusion over which `(horizon_s, threshold_bps)` is actually live:
  `settings.yaml`'s `paper_trading.horizon_s: 30` is stale — the only
  models ever promoted (2026-08-25, all three assets) are
  `horizon_s=60, threshold_bps=10`. `settings.yaml` was never updated
  after that decision; not fixed here since the sweep script takes
  `--horizon-s`/`--threshold-bps` directly and doesn't read this key,
  but worth reconciling before the next `main.py` restart on the Pi.

With that resolved, the sweep ran cleanly but returned **zero trades at
every tested `probability_min`, including 0.30** — well below the
0.62 in `strategy.yaml`. A direct diagnostic (`predict_proba()` +
`direction_from_probabilities()` over 172,800 BTC rows) showed why:
99.68% of rows have `neutral` as the argmax class; only 554 rows
(0.32%) call up/down at all. But the real blocker turned out to be
mathematical, not a calibration/class-imbalance issue: `entry.py`'s and
`event_backtester.py`'s expected-value formula is
`|prob_up*threshold_bps - prob_down*threshold_bps| - round_trip_cost_
bps`, and with `threshold_bps=10`, the maximum possible value of the
first term is 10 (at `prob_up=1`, never observed) — so with the *old*
cost placeholders (`round_trip_cost_bps=16`), `expected_value` could
never reach `entry_filter.min_expected_value: 0.0` at **any**
probability. No sweep of `probability_min` could ever have found a
trade.

Checking the placeholders against reality made this much worse, not
better: Kraken Pro's real Tier 1 (< $50k/30-day volume) API fees are
**maker 0.40% / taker 0.80%** (`kraken.com/features/fee-schedule`,
checked 2026-08-26) — the old `taker_fee_bps: 5` / `maker_fee_bps: 2`
placeholders were ~16x too low, not too high. Real
`round_trip_cost_bps` for taker orders at this tier is ~166, not 16.
No `threshold_bps` at or below 50 (the whole range explored so far)
can ever clear that regardless of model confidence — sub-minute
microstructure scalping via market orders is structurally unprofitable
at this fee tier, independent of model quality.

Presented both fee-tier options (maker/limit-order redesign — needs
fill-risk modeling, not built yet — vs. targeting much larger moves
with the existing taker-order architecture) to the user; decided to
keep the current architecture and retrain toward larger moves
(`threshold_bps` in the 150-300bps range, longer `horizon_s`) rather
than redesign around maker orders for now.

- `config/strategy.yaml` — `costs.taker_fee_bps: 5→80`,
  `costs.maker_fee_bps: 2→40` (real Kraken Pro Tier 1, sourced/dated in
  comment); `move_thresholds_bps` extended with `100, 150, 200, 250,
  300` (kept the old `<=50` values for before/after comparison);
  `entry_filter.min_expected_value` comment updated to spell out the
  `threshold_bps > round_trip_cost_bps` requirement.
- `src/features/feature_engine.py` — `DEFAULT_TARGET_THRESHOLDS_BPS`
  extended the same way, so `build_feature_table.py` actually computes
  `target_*_up/down_{100,150,200,250,300}bps` columns on a rebuild —
  without this, no threshold above 50bps is even representable in a
  features_v1 table, silently reproducing the same wall.
- `tests/test_feature_engine.py` — 1 new regression test:
  `DEFAULT_TARGET_THRESHOLDS_BPS` must include a value above the real
  `round_trip_cost_bps` (~166), and must still include the original
  `{5, 10, 15, 25, 50}` set.
- **726 tests total, all passing.**

Not yet done: rebuilding `features_v1` with the new target columns (on
the PC's original 2026-08-21..24 training window, and optionally on the
Pi's 2026-08-25..26 live-collection window for a genuinely out-of-
sample check), training/registering candidate models at the new
`threshold_bps` values, and re-running `sweep_entry_threshold.py`
per-candidate (via `--model-version`, since nothing here is promoted
yet) before promoting anything to replace the current live models.

### Phase 15 addendum 6 — purged k-fold CV replaces the single last-block holdout, 2026-08-26

Direct follow-through on addendum 5's retraining attempts. Trying to
retrain BTC/SOL at `threshold_bps=200, horizon_s=300` (to clear the real
~166bps round-trip cost from addendum 5) kept hitting `accuracy=None
(n=0)` in validation, or an outright `ValueError: This solver needs
samples of at least 2 classes`. Root-caused by pulling the real
`features_v1` rows into a dataframe and counting directional target
rows per calendar day directly, rather than guessing: BTC never once
moved ≥150bps in 300s across the 4-day training window (0 examples, not
just few); SOL had 677 examples but 540 of them (80%) fell on a single
day (2026-08-22), with the other days at 95/42/0/0/0 — and
`train_model.py`'s original `_chronological_split()` always holds out
the chronologically *last* block. Extending the window from 4 to 6 days
(hoping more history would help) made it worse, not better: both newly
added days (25-26, the live PAPER period itself) turned out to have
*zero* SOL examples at this threshold, so the last-block holdout kept
landing on an empty slice no matter how much quiet data was appended
behind it — a structural flaw in the split method, not a data-volume
problem.

- `src/models/calibration.py` — `calibrate()` factored into two public
  halves: `labels_and_proba(model, rows, horizon_s, threshold_bps)`
  (the existing `_labels_and_proba_matrix()`, now public) and
  `diagnostics_from_arrays(y, proba)` (brier/log-loss/roc-auc/
  calibration-curve computed directly from arrays). `calibrate()`
  itself is now a two-line composition of both and behaves identically
  (pinned by a new equality test) — the split exists so pooled CV can
  concatenate `(y, proba)` across several folds' different models and
  run diagnostics once, instead of once per fold.
- `scripts/train_model.py` — the evaluation step is now purged k-fold
  CV (`_purged_kfold_split()`, default `--cv-folds 5`): the whole row
  range is split into `k` contiguous chronological folds, each held out
  once with a fresh model fit on the other `k-1` folds' rows (purged
  `horizon_s` seconds at both edges of the held-out fold, generalizing
  `_chronological_split()`'s single-boundary purge to `k-1`
  boundaries). Every row gets scored in exactly one fold's validation
  set, so a rare, calendar-clustered target no longer depends on
  whether it happened to land in the *last* block. Score counts and
  `(y, proba)` are pooled across folds for one binomial test and one
  set of calibration diagnostics — not averaged per-fold, which would
  double-count folds with more resolved rows. A fold that can't fit
  (empty split side, or a `ValueError` from a single-class training
  set) is skipped with a warning, not fatal; if *every* fold is
  skipped, the script now refuses to register a model with zero
  evaluation (`SystemExit`) rather than silently proceeding. The old
  `--validation-fraction` flag is renamed `--calibration-fraction` and
  now only reserves a holdout for `--calibrate`'s Platt-scaling step
  (which genuinely needs data the final model never trained on) — the
  **final saved/registered model is refit on effectively all rows**
  when `--calibrate` isn't passed, since CV already produced an honest
  evaluation number without needing to withhold anything from the
  model that actually gets promoted.
- `tests/test_calibration.py` — 2 new tests: `calibrate()` is exactly
  `labels_and_proba()` + `diagnostics_from_arrays()` composed (pins the
  refactor's behavior); `diagnostics_from_arrays()` correctly pools
  predictions from two different fitted models.
- `tests/test_train_model.py` — synthetic fixture bumped from 200 to
  800 rows (200 was too short for 5-fold CV purging at `horizon_s=30`
  to leave any fold usable); renamed the old validation-fraction test;
  4 new tests: the final model is fit on all rows when not calibrating;
  `--cv-folds` is recorded and at least one fold runs by default; the
  exact regression case — a synthetic table with every directional
  example in the first half only, which a last-block holdout would
  always score `n=0` on — gets a nonzero pooled score under CV; the
  k-fold splitter purges both edges of every held-out fold, not just
  one.
- **732 tests total, all passing.**

Not yet done: actually re-running `scripts.train_model` for BTC/SOL at
`threshold_bps=200, horizon_s=300` with the new CV — addendum 5's
BTC-has-zero-examples finding still stands regardless of split method
(no amount of resampling manufactures events that never happened), but
SOL should now get a real pooled evaluation instead of `n=0`.

### Phase 15 addendum 7 — `NewsDirectionModel` tested and found not to hold up under CV, 2026-08-28

Follow-through on addendum 6's retrain: `threshold_bps=200` still gave
`n=0`/`None` for BTC/ETH/SOL even after collecting through 2026-08-28
(one direct day-count check found every real ≥200bps/300s move across
all three assets falls in 2026-08-21..23, with 2026-08-24 onward
entirely quiet for all three — not a data-volume problem, a real quiet
period). Re-running CV on just the three volatile days (isolating the
signal from the dilution) made things *worse*, not better — SOL's
pooled `n` dropped from 16 (6-day window) to 9 (3-day window), BTC/ETH
stayed at 0 — evidence this isn't a calendar problem at all.

Before abandoning the "wider threshold" direction, an ad hoc check
comparing `sign(news_score)` (an already-computed `features_v1` column,
untested as a standalone signal until now) against real 60s/10bps
direction found a real-looking, highly significant edge for BTC/SOL:
51.6-53.8% accuracy, p as low as 1e-11, on 8.6k-20.3k rows. Built
`NewsDirectionModel` (`src/models/news_model.py`) — a
`LogisticDirectionModel` subclass restricted to just the 5 news
columns, via a new `_feature_columns()` override point — and wired it
into `train_model.py` as `--model news`, to test that signal properly
under purged k-fold CV rather than trust the ad hoc comparison.

**It didn't hold up.** CV on the full row set: `n_scored=0` for all
three assets. CV restricted to just the "news-active" rows the ad hoc
check used (`news_age<900s`, `|news_score|>0.05`): still `n_scored=0`
for all three, and `roc_auc_macro` comes back *below* 0.5 (0.40-0.45) —
the model's out-of-fold ranking is anti-correlated with the real
outcome, the opposite of a confirmed edge. Most likely explanation
(flagged, not yet proven): the ad hoc check's rows weren't independent
observations the way its binomial test assumed — a single news event's
`news_age`/`news_score` persist across up to ~900s of 1-second-sampled
rows, so "8,600 independent samples" was probably a much smaller number
of actual news events, each repeated hundreds of times, which inflates
apparent significance without it surviving an honest out-of-fold test.
This is exactly the kind of false positive purged CV exists to catch —
recorded as a real negative result, not a wasted afternoon.

- `src/models/logistic_model.py` — `fit()`'s feature-column selection
  factored into an overridable `_feature_columns()` method (was inline).
  `LogisticDirectionModel`'s own default behavior is unchanged.
- `src/models/news_model.py` (new) — `NewsDirectionModel`.
- `scripts/train_model.py` — registered as `--model news`.
- `tests/test_news_model.py` (new, 7 tests), `tests/test_train_model.py`
  (2 new tests).
- **741 tests total, all passing.**

Not yet done: re-testing with one row per distinct news *event* (not
per second of its decay window) to get an honestly independent sample —
likely a much smaller one, may not be enough data yet to conclude
either way. Given this result, the more promising immediate next step
is testing the *full* `LogisticDirectionModel` (kitchen-sink features,
not news-isolated) at a moderate threshold a maker/limit redesign would
make economically viable (~50-100bps, well below the untested
150-300bps range and well above the already-validated-but-too-cheap
10bps) — the one edge that has actually survived scrutiny all day is
still the original promoted 60s/10bps model (60.5% accuracy,
p=5.6e-09, n=751); the open question is whether that same architecture
holds up anywhere between 10bps and 150bps, which hasn't been tested.

### Phase 15 addendum 8 — 100bps/300s tested and rejected; retroactive event-level audit of the 10bps edge, 2026-08-28

Tested the full `LogisticDirectionModel` (kitchen-sink features) at
`horizon_s=300, threshold_bps=100` on the complete 2026-08-21..28
window, per the closing question of addendum 7 — is there a
maker-viable threshold anywhere between 10bps and 150bps that holds up?

- **BTC**: n=23, accuracy=78.3%, p=0.0053, but `roc_auc_macro=0.418`
  (below 0.5) — same false-positive signature already seen twice today
  (ETH/150bps/n=5, the news model). Rejected.
- **ETH**: n=22, accuracy=59.1%, p=0.262 (not significant),
  `roc_auc_macro=0.251`. Rejected.
- **SOL**: n=146, accuracy=67.8%, p=1.0e-05, `roc_auc_macro=0.560` (the
  first >0.5 roc_auc of the day at this threshold) — looked like the
  best candidate. **It wasn't.** Per-fold breakdown showed one fold
  alone (validation window 2026-08-21/22) carrying 120 of the 146
  scored rows (82%); those 120 rows' timestamps cluster into just 2
  contiguous windows, one spanning 145 seconds. `n=146` was actually
  **one real ~2.5-minute price move**, sampled once per second and
  counted as 116 near-independent Bernoulli trials in the binomial
  test. Rejected — same autocorrelation mechanism as the news-model
  false positive, now confirmed on a second, independent case.

**This is now a confirmed, recurring failure mode, not a one-off**:
`train_model.py`'s CV counts *decisive rows*, but real market moves
persist for seconds to minutes at 1-second sampling, so a small
`n_scored` after a large threshold filter is often actually a handful
of real events, heavily repeated — which can produce an
arbitrarily-small nominal p-value with zero real evidence behind it.
`roc_auc_macro < 0.5` was the tell each time so far, but that's a
lucky correlate, not a guarantee — a instructive ad hoc audit (cluster
consecutive scored rows within a fold using a `> horizon_s` gap as the
event boundary, then re-run the binomial test with one trial per
cluster, majority-vote correctness) was written to check directly
rather than rely on the roc_auc proxy.

**Applied that same audit retroactively to the currently-promoted
60s/10bps edge**, to check whether *it* was ever real, now that the
tooling exists to ask properly:

| asset | row-level n / acc / p | event-level clusters / acc / p | days spanned | top cluster share |
|---|---|---|---|---|
| BTC | 4149 / 56.9% / 1.7e-19 | 88 / 65.9% / **0.0019** | 6 of 6 available | 18% |
| ETH | 6747 / 54.9% / 6.6e-16 | 169 / 62.1% / **0.0010** | 8 of 8 | 11% |
| SOL | 12755 / 50.8% / 0.030 | 231 / 55.0% / **0.074** | 8 of 8 | 16% |

BTC and ETH's 10bps edge **survives** the much stricter event-level
test — significant even when every real market move counts as exactly
one trial, spread across 6-8 different days with no single cluster
dominating. SOL's does not clear 0.05 event-level (p=0.074) — weaker,
should be treated as unconfirmed, not promoted with the same
confidence as BTC/ETH.

**The honest state of the project after today's full investigation**:
BTC and ETH have a real, event-level-validated directional edge at
`horizon_s=60, threshold_bps=10` (~10bps average move captured, 56-66%
directional accuracy depending on how it's counted). Real Kraken costs
put the round-trip floor at ~166bps taker / ~86-126bps maker (Tier 1,
`config/strategy.yaml`) — a 10bps edge cannot cover any of those,
maker or taker; execution-style optimization cannot rescue it, because
the edge itself is smaller than the cheapest possible round trip. No
threshold tested today or in prior sessions between 10bps and 300bps
has produced a validated edge for any asset (100bps: rejected above;
150-300bps: addendum 6/this segment's earlier maker-feasibility check,
all rejected or `n=0`). The news-score signal track (addendum 7):
rejected. Both of the two parallel tracks the user authorized
(maker/limit execution redesign, news-driven signal) have now been
tested and neither produces a tradeable edge as currently built.

**Done, same session**: formalized the event-clustering diagnostic as a
permanent part of `train_model.py`'s CV output (`_event_level_diagnostics()`,
5 new tests, 746 total) — every future run now reports both the
row-level and event-level result automatically, with an explicit
warning when a result clears the row-level bar but not the event-level
one, instead of needing an ad hoc script reached for after the fact.
See `docs/09_tests_plan.md`'s "Phase 15 addendum 8" section for the
implementation writeup.

### Phase 15 addendum 9 — systematic threshold scan, 15-90bps, event-level audited, 2026-08-28

With the event-level diagnostic now built in, ran the full
`LogisticDirectionModel` at `horizon_s=300` across every threshold not
yet tested (15, 25, 50, 75, 90bps — filling the gap between the
validated-but-too-cheap 10bps and the already-rejected 100-300bps
range) on the complete 2026-08-21..28 window, all three assets:

| asset | 15bps | 25bps | 50bps | 75bps | 90bps |
|---|---|---|---|---|---|
| BTC | **event p=0.0018** (64 clusters, 68.0%) | **event p=0.0083** (35 clusters, 71.4%) | no edge (row p=0.84) | 0/5 folds fit | 0/5 folds fit |
| ETH | **event p=0.0029** (162 clusters, 61.1%) | row-only false positive (event p=0.50) | row-only false positive (event p=0.073) | 0/5 folds fit | 0/5 folds fit |
| SOL | no edge (row p=1.0) | row-only false positive (event p=0.63) | row-only false positive (event p=0.18) | 0/5 folds fit | 0/5 folds fit |

("row-only false positive" = the new addendum 8 warning fired: row-level
`beats_coin_flip=True` but event-level does not — the same
autocorrelation artifact as SOL/100bps and the news model, now caught
automatically instead of requiring an ad hoc script each time.)

**Conclusion — the threshold axis has now been exhaustively scanned
from 10 to 300bps, event-level audited at every point tested this
session**: a real, event-level-validated edge exists only at the very
low end (10bps confirmed in addendum 8's retroactive audit; 15bps now
confirmed for both BTC and ETH; 25bps confirmed for BTC only). Nothing
at 50bps or above survives the event-level test for any asset, and
nothing at 75-90bps has enough decisive training data to even fit a
model in any single CV fold (separately already true at 100-300bps per
addenda 6-8). This low-end edge is real but the round-trip cost floor
(~166bps taker / ~86-126bps maker, `config/strategy.yaml` Tier 1) is
5-10x larger than the 10-25bps edge itself — execution-style
optimization cannot close that gap, because the edge is smaller than
even the cheapest possible round trip, not just smaller than the
current (taker) one.

**Net result of everything tested this session, both of the two
parallel tracks the user authorized**: maker/limit execution redesign
— doesn't help, the edge that exists is too small for any execution
style to make profitable. News-driven signal — doesn't hold up under
CV (addendum 7). Threshold scan 10-300bps — no economically-viable
point found. The "predict a move past a threshold, in this feature
set, at these horizons" approach has been thoroughly tested and, as
currently built, does not produce a tradeable strategy. Next step is a
genuine strategic decision for the user, not another variant of the
same test.

### Phase 15 addendum 10 — net-of-fees expected value; Hyperliquid execution validated and rejected, 2026-08-28

User question: could routing execution through a non-custodial wallet
(Phantom) and a Solana DEX/swap platform get around Kraken's fee floor?
Researched real, current (Aug 2026) all-in costs rather than assuming:
Jupiter platform fee (0-0.5%, typically ~0.02-0.1%) + Solana network fee
(~$0.0005-0.02) + underlying Raydium/Orca pool fees (25-30bps) +
realistic slippage on major pools (~50bps even for small size) +
MEV/sandwich risk (15-25bps even with Jito protection) → realistic
all-in round trip ~100-150bps, similar to or worse than Kraken, and the
slippage/MEV components get *worse* exactly during the volatility
windows this strategy needs to trade. Wrapped BTC/ETH on Solana go
through the Wormhole bridge (cited the 2022 $326M Wormhole hack as a
concrete illustration of bridge risk) with much thinner liquidity than
Kraken spot. Conclusion: DEX/swap does not solve the cost problem and
adds a new venue/bridge-risk/feed-mismatch validation burden. Rejected.

User then asked to have AMM liquidity provision (earn the spread instead
of paying it) and OBI-based active market-making developed in enough
depth to choose between them. Also measured this session's own real
`spread` column (already stored in `features_v1`, in bps) against
Kraken's round-trip maker cost: BTC median 0.129bps, ETH median
0.407bps, SOL median 0.107-0.938bps — 196-746x smaller than Kraken's
86bps round-trip maker fee. Naive passive market-making (post both
sides, capture the raw spread) is structurally dead at Kraken's fee
tier regardless of signal quality. AMM LP (Raydium/Orca CLMM) earns the
25-30bps pool fee instead but shifts the risk to impermanent loss —
sources are explicit that concentrated-liquidity LPing "is not passive
yield, closer to active market-making," with no reliable historical
profitability data for volatile pairs, and this project has no on-chain
data source or IL/fee backtest engine at all yet — the most speculative
and expensive-to-validate of the three options.

The one new fact that changed the picture: this project's market data
already comes from **Hyperliquid**, and Hyperliquid's own perpetuals fee
schedule is dramatically cheaper than Kraken's — base tier maker
0.015% (1.5bps) / taker 0.045% (4.5bps), i.e. round-trip maker ~3bps
(~9bps with the same 3bps/side slippage assumption used throughout this
project's testing) vs Kraken's 86bps, and round-trip taker ~9bps
(~15bps with slippage) vs Kraken's 166bps — 10-27x cheaper on the same
venue the signal was built from, no new feed/bridge risk, and a real
Python SDK (`hyperliquid-python-sdk`) for order placement. User chose to
validate this properly rather than assume it works.

The existing quick `EventBacktester` sanity check only classifies
correct/incorrect, so a proper tool was built: `_forward_return_bps()`
(uses the existing no-look-ahead-safe `find_forward_indices()` primitive
— confirmed by reading `compute_targets()` that this is exactly the same
point-in-time forward return the binary threshold labels are already
derived from, no methodological mismatch) and `_net_expected_value()`,
wired into `train_model.py` via a new `--round-trip-cost-bps` flag
(purely informational — logged and stored in registry `performance`,
never affects promotion). For every row where the model's own call is
decisive, computes `net_pnl_bps = predicted_direction *
realized_forward_return_bps - round_trip_cost_bps`, tested both
row-level (all scored rows as independent trials) and event-level (same
clustering as addendum 8's diagnostic, one trial per cluster using its
mean net P&L) via a one-sided `ttest_1samp` against 0. 7 new tests (753
total), including hand-computed exact-bps fixtures.

Ran the real validation on the complete 2026-08-21..28 window, across
every (asset, horizon_s, threshold_bps) config that has actually cleared
the event-level bar in addenda 8-9, at both Kraken's costs (for
reference) and Hyperliquid's:

| config | Kraken taker 166bps | Kraken maker 86bps | Hyperliquid taker ~15bps | Hyperliquid maker ~9bps |
|---|---|---|---|---|
| BTC 60s/10bps (n=7194) | row mean=-164.5bps p=1.0 | row mean=-84.5bps p=1.0 | row mean=-13.5bps p=1.0 | row mean=-7.5bps p=1.0 |
| BTC 300s/15bps (n=13370) | — | — | row mean=-9.2bps p=1.0 | row mean=-3.2bps p=1.0 |
| BTC 300s/25bps (n=4605) | — | — | row mean=-6.2bps p≈1.0 | **row mean=-0.17bps p=0.59** |
| ETH 60s/10bps (n=11664) | — | — | row mean=-13.9bps p=1.0 | row mean=-7.9bps p=1.0 |
| ETH 300s/15bps (n=23754) | — | — | row mean=-12.5bps p=1.0 | row mean=-6.5bps p=1.0 |

(Event-level results track the row-level results directionally in every
config — smaller n, same sign, same conclusion — omitted from the table
for space; full numbers in the addendum-10 test run log.)

**Conclusion: even at Hyperliquid's 10-27x lower fees, net expected
value is negative to essentially breakeven in every config tested, and
never significantly positive anywhere.** The closest result — BTC
300s/25bps at Hyperliquid's maker rate — has a mean net P&L of
-0.17bps, indistinguishable from zero (p=0.59, nowhere near the 0.05
bar this project has held all session). Every other config is clearly
and often heavily net-negative even after the fee cut. The reason:
these edges were sized against a *directional-accuracy* bar (beats a
coin flip on sign), not a *magnitude* bar — the correct calls are
correct by a small margin, and the incorrect calls are wrong by enough
to erase it, so cutting round-trip cost by 10-20x closes most but not
quite all of the gap at the one edge (25bps) large enough to get close,
and doesn't come close anywhere else.

This directly answers the question the user set at the top of this
track: **"si ce n'est pas réglé nous en resterons a la base du projet"**
— it is not resolved. Both AMM LP and OBI market-making remain
undeveloped/untested (the user asked for them to be explained, not
built, pending this Hyperliquid result), and building either would mean
building an entirely new data source and backtest engine for a
speculative payoff, which is a materially larger commitment than the
one-flag validation this addendum just ran. Reported to the user for a
decision rather than assumed.

- `scripts/train_model.py` — new `--round-trip-cost-bps` CLI flag; new
  `_forward_return_bps()` (forward point-in-time return in bps, reusing
  `find_forward_indices()`, `None`-safe on missing price data); new
  `_net_expected_value()` (row-level + event-level net P&L vs. a cost
  assumption, one-sided t-test against 0); `net_expected_value` now
  stored in every registered model's `performance` dict (`None` when
  the flag isn't passed).
- `tests/test_train_model.py` (7 new tests): hand-computed forward
  return matches an exact worked example; net EV is significantly
  positive when a synthetic edge clears a low cost and significantly
  negative when cost exceeds it; event-level trial count is strictly
  fewer than row-level; empty/no-price-data input returns the all-None
  shape; the CLI flag wires into registry `performance` when passed and
  leaves it `None` when omitted.
- **753 tests total, all passing.**

### Phase 15 addendum 11 — tree model tested as a cheap proxy for a news→microstructure interaction; no better economics, one new false positive caught, 2026-08-28

User question, following addendum 10's result: instead of blending news
and microstructure features additively (what `LogisticDirectionModel`
does), could news act as a *gate* — news identifies a candidate
direction/window, and order-book microstructure (OBI etc.) confirms and
times the actual entry? This is a genuinely different mechanism from
both prior tests (`LogisticDirectionModel`'s flat additive blend, and
`NewsDirectionModel`'s news-only, addendum 7) — it proposes an
interaction effect (microstructure's predictive value conditional on a
recent news signal), which a linear model can't represent without it
being hand-engineered.

Cheapest test available: `TreeDirectionModel`
(`HistGradientBoostingClassifier`, built since Phase 9, never applied to
this session's threshold-scan/event-level work) captures interactions
natively — if a news-conditional microstructure effect exists in the
data, a tree ensemble should find some of it without any gating logic
being hand-written. Ran it on the same 5 already-validated configs from
addendum 10, same window, event-level diagnostic + `--round-trip-cost-
bps 9.0` (Hyperliquid maker):

| config | event-level accuracy | event-level p | net EV (event, Hyperliquid maker) |
|---|---|---|---|
| BTC 60s/10bps | 59.4% (n=240 clusters) | 0.0027 | -7.64bps, p=1.0 |
| BTC 300s/15bps | 51.8% (n=140 clusters) | 0.40 — **fails event-level** | -7.11bps, p≈1.0 |
| BTC 300s/25bps | 65.4% (n=52 clusters) | 0.018 | -6.66bps, p≈1.0 |
| ETH 60s/10bps | 59.2% (n=333 clusters) | 0.00049 | -7.82bps, p=1.0 |
| ETH 300s/15bps | 61.6% (n=255 clusters) | 0.00013 | -6.67bps, p≈1.0 |

Two findings, both negative for the gating hypothesis:

1. **The tree model's classification signal is often much stronger than
   the logistic model's on the same data** (e.g. BTC 60s/10bps
   `roc_auc_macro=0.706` vs. the logistic model's much lower figure;
   ETH 60s/10bps similarly at 0.700) — meaning it likely *is* picking
   up some real interaction structure, plausibly including a
   news-conditional microstructure effect, that the additive logistic
   model can't represent. **But this extra classification power does
   not translate into better net economics anywhere** — net EV at
   Hyperliquid's cheapest tier is negative in all 5 configs, and no
   better (in 4/5 cases meaningfully worse) than the logistic model's
   own net EV at the same configs from addendum 10. BTC 300s/25bps is
   the clearest case: this was logistic's closest-to-breakeven result
   (row mean=-0.17bps, p=0.59); the tree model's version of the same
   config is solidly negative (event mean=-6.66bps). A model that is
   *better* at classification is not automatically *better* at
   money — accuracy on direction says nothing about whether the
   magnitude of correct calls exceeds the magnitude of incorrect ones,
   net of cost, and here it doesn't.
2. **BTC 300s/15bps flips from a validated edge (logistic, addendum 9:
   64 clusters, 68.0%, p=0.0018) to a caught false positive under the
   tree model** (140 clusters, 51.8%, p=0.40, `_direction_score_counts`
   row-level p was significant but event-level was not — the
   addendum-8 warning fired). The same underlying market data, scored
   by a different, more flexible model, produces a result the event-
   level diagnostic correctly rejects — a useful, if unplanned,
   confirmation that the diagnostic isn't tied to one model's quirks.

**Conclusion**: a strictly more general model than a hand-built gate
(a tree ensemble subsumes "predict off microstructure only when a news
condition holds" as one of many interactions it's free to discover) was
given the same data and found nothing that clears net EV. This doesn't
*prove* no news-conditional gating rule could ever work — a hand-built
gate imposes a specific, theory-motivated structure a generic tree might
weight differently — but it is real evidence against it, from the
cheapest test available, and raises the bar for justifying the more
expensive, more sample-starved explicit-gating build (addendum 10's own
math: gating shrinks the eligible-trade count further, on a dataset
where n=16-52 clusters is already the realistic range at the wider
thresholds). Reported to the user; explicit gating not yet built,
pending their decision on whether this result changes the calculus.

- No new source files — reused `TreeDirectionModel` (Phase 9) and the
  existing `--model tree` CLI flag, `--round-trip-cost-bps` (addendum
  10) unchanged. No test-suite changes; this addendum is a research run,
  not a code change. 5 new (unpromoted) model artifacts registered in
  `data/models/registry.jsonl` for reference.
- **753 tests total, all passing** (unchanged from addendum 10).

## Final status — project halted (2026-08-29)

After addendum 11, the decision point was explicit: stop at the current,
honestly-tested baseline, or invest in one of the two remaining unbuilt
and speculative directions (AMM liquidity provision, or an explicitly
built news-gated order-book execution rule). **The project was stopped
at this baseline.** This was a deliberate decision, not a dead end
reached by exhausting options still wanted.

What was established:

- A **directional** edge that survives the event-level audit exists only
  on very small moves: BTC and ETH at 10bps/60s and 15bps/300s, BTC at
  25bps/300s. Nothing at 50bps or above survives; 75bps and above cannot
  even fit a model for lack of examples.
- That edge is 5-10x smaller than realistic round-trip costs (~86-166bps
  on Kraken). Even at Hyperliquid's much lower fees (~9-15bps), net
  expected value is negative in every configuration tested and never
  significantly positive (best case: BTC 300s/25bps at maker fees,
  -0.17bps, p=0.59).
- Neither a news-only model (out-of-fold ROC-AUC 0.40-0.45) nor a tree
  model free to discover news×microstructure interactions (better
  classification, worse economics) changed that conclusion.

Final state: code, tests (753 passing) and documentation are complete
for Phases 1-15. The Raspberry Pi collector service was stopped and
removed. Phases 16 (live trading) and 17 (24/7 deployment) are cancelled.
Known open points left as-is: the `performance.win_rate` NOT-NULL schema
issue, and the two Phase 12 Telegram config interpretations noted below.

## Phase 16 — not started, scope unconfirmed

No Phase 1 stub file names this phase (`grep -rn "Roadmap: PHASE" src/`
finds nothing for 16) — flagged honestly in
the internal delivery log's "Full phase map" as a best guess
(micro-capital live validation checkpoints, not new code) rather than a
confirmed scope. Per this session's own gate discipline, this — like
the Phase 14 pause before it — is a genuine decision point (real
capital risk), not just inter-phase pacing, so it isn't started without
the user weighing in on what this phase actually means, and without a
completed paper-trading validation cycle (Phase 15, now available via
`docs/21_paper_trading_methodology.md`) to inform it first.
