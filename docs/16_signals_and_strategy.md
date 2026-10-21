# Signal Generation + Strategy Orchestration — Implementation Notes (Phase 11)

Sections 26, 33-39. This is the live/paper counterpart to Phase 10's
backtest: turning a fitted Phase 9 model's raw probabilities into a full
signal payload, deciding whether to act on one, and managing the
resulting position through to exit — `src/signals/{confidence,scoring,
signal_engine}.py` and `src/strategy/{entry,exit,strategy_engine}.py`.

## DRY foundations laid before writing any of the six modules

Two shared primitives were extracted *first*, specifically so backtested
and live/paper code never independently decide the same question twice
and silently drift apart — the exact failure mode `docs/08_backtesting_
strategy.md` already warns about at the row-schema level, carried one
layer deeper into decision logic and cost math:

- **`src/models/direction.py: direction_from_probabilities(proba) ->
  (direction, probability)`** — the argmax over `probability_up`/
  `probability_down`/`probability_neutral`, tie-broken `up > down >
  neutral` (an exact three-way tie resolves `up`, matching no particular
  market intuition — just a documented, deterministic, testable rule).
  Previously `backtest/event_backtester.py: decisions_from_model()` had
  its own inline argmax; it now calls this function instead, and
  `signal_engine.py`/`scoring.py`/`strategy/exit.py`'s
  `probability_reversal` check all call the same one. A regression test
  (`test_decisions_from_model_direction_matches_shared_primitive_
  directly`) confirms the refactor didn't change `decisions_from_model()`'s
  output.
- **`backtest/event_backtester.py: simulate_fill_costs(entry_price,
  exit_price, direction, quantity, *, taker_fee_bps, assumed_slippage_bps)
  -> {"gross_pnl","fees","slippage","net_pnl"}`** — extracted out of
  `EventBacktester._build_trade()`'s inline cost math into a public
  function, now also called by `strategy/strategy_engine.py:
  StrategyEngine._close_position()`. A backtested trade and a live/paper
  one are computed by the literal same formula, not two implementations
  that happen to agree today. Both legs (entry and exit) are charged
  fee + slippage against the *entry* notional — no re-notionalizing at
  the exit price, matching how a fixed-size order actually settles.

## `signals/confidence.py` — three independently missing-tolerant components

`compute_confidence(signal, *, now=None, max_data_age_s=3.0, weights=None)
-> float`, always in `[0, 1]`. Combines by weighted average (defaults
`calibration=0.4, freshness=0.2, confirmation=0.4` — un-tuned starting
points per section 26, same spirit as every other un-tuned threshold in
this codebase):

1. **Calibration quality** — `signal["model_calibration"]["roc_auc_macro"]`
   rescaled `[0.5, 1.0] -> [0, 1]`. Missing/`None` defaults to a
   **neutral 0.5**, not a penalty — an unmeasured model isn't evidence of
   a bad one. A model *measured* at chance level (`roc_auc_macro=0.5`)
   correctly scores `0.0` on this component, strictly below the "we don't
   know" default.
2. **Data freshness** — linear decay of `(now - signal["timestamp"])`
   against `max_data_age_s`. Missing `now` or `timestamp` (e.g. offline/
   backtest use) defaults to neutral 0.5, not 0.0 or 1.0 — staleness is
   unanswerable without a reference clock, not automatically fine or
   automatically bad.
3. **News/order-flow confirmation** — of `{"obi_10", "trade_imbalance",
   "news_score"}` present in `signal["features_snapshot"]`, what fraction
   agree in sign with the signal's direction. A `0`-valued column casts no
   vote (neither confirms nor disconfirms — the sign comparison would be
   arbitrary). Neutral 0.5 when the signal itself is neutral or no columns
   are available.

## `signals/scoring.py` — deliberately probability-only, not confidence-aware

`classify_signal_quality(signal, ...) -> "A"|"B"|"C"|"NO_TRADE"` reads
only `probability_up`/`probability_down`/`probability_neutral` — nothing
from `compute_confidence()`. This is a choice, not an oversight:
`config/strategy.yaml: signal_engine`'s threshold block
(`quality_A_min_probability=0.68`, `quality_B=0.58`, `quality_C=0.52`) is
literally named and scoped for probability alone. `NO_TRADE` covers two
distinct cases that both mean "nothing to grade a direction on": a
neutral argmax (never graded `"C"` just because `probability_neutral`
happens to be high — quality is about directional conviction, not
confidence in inaction), and a directional call that doesn't clear even
the lowest bar. `SignalEngine` is free to combine `signal_quality` and
`confidence` however a downstream consumer wants; this function only
ever answers the question its own config keys are named for.

## `signals/signal_engine.py` — the richer, storage-shaped payload

`SignalEngine(model, *, asset, horizon_s, threshold_bps, model_version,
...)`. One instance per `(asset, horizon_s, threshold_bps)` question,
same convention as `LogisticDirectionModel`/`FeatureEngine`/
`RegimeDetector`. `build_signals(rows, *, now=None)` batches
`model.predict_proba(rows)` once (`build_signal(row, *, now=None)` is a
single-row convenience wrapper — batch when scoring more than one row,
since that's what the underlying pipeline is actually efficient at).

Formalizes what `backtest/event_backtester.py: decisions_from_model()`
did as a deliberately simple backtest-only stand-in into the full
section-33 payload, shaped to match `storage/database.py: Signal`'s
column set so a later phase persisting a signal needs no translation
step. Two numbers worth being precise about:

- `expected_move` — the full probability-weighted signed bps expectation
  (`p_up * threshold_bps - p_down * threshold_bps`), independent of which
  direction is ultimately chosen. A market-move estimate, not a
  strategy-specific one.
- `expected_value` — `abs(expected_move) - round_trip_cost_bps` when
  `direction != 0`, else `None`. `round_trip_cost_bps` is a same-named,
  same-formula property as `EventBacktester`'s, so a caller reading
  either engine's cost assumption never has to remember two different
  attribute names.

## `strategy/entry.py` — the live counterpart to `_passes_entry_filter()`

`should_enter(signal, risk_state, *, probability_min=0.62,
min_expected_value=0.0, max_spread_bps=5, max_data_age_s=3,
allowed_regimes=("LOW_VOL","NORMAL_VOL"), now=None) -> bool`. Same checks
as `EventBacktester._passes_entry_filter()` (probability, EV-net-of-costs,
spread, regime), same `strategy.yaml: entry_filter` defaults, plus one
check the backtester structurally cannot perform on historical data:
freshness — skipped entirely (not "fails by default") when `now` isn't
given, since a backtest replay has no live wall clock to be stale
relative to. Every check degrades to "not a violation" when its input is
missing, matching every other optional-data convention in this codebase.

`risk_state`'s shape (`{"blocked": bool, "reason": str|None}`) is a
documented placeholder: Phase 12's `RiskManager` doesn't exist yet, so
`should_enter()` only ever reads `.get("blocked", False)` — a caller with
no real risk manager can pass `{}`. The natural Phase 12 integration
point is a `RiskManager.state()` method returning this same shape
(extended with more diagnostic keys), so this function's signature never
has to change.

Kept as a plain function, not folded into `SignalEngine` or
`StrategyEngine`, so a research notebook can ask "would this signal have
passed the entry filter" without constructing a whole orchestrator —
matching the Phase 1 stub's own plain-function signature.

## `strategy/exit.py` — 7 candidates, config order, first match wins

`should_exit(position, features, ...) -> str|None` checks every
`config/strategy.yaml: exit_candidates` entry **in that exact order**
(`fixed_tp, fixed_sl, volatility_based_sl, probability_reversal,
order_flow_reversal, liquidity_reversal, time_stop`) and returns the
first that fires. Which reason wins when several would fire
simultaneously is a real methodological choice — made explicit as
"config order" rather than left as an accident of dict iteration (a test,
`test_candidates_checked_in_config_order_first_match_wins`, rigs a
position where both `fixed_tp` and `time_stop` would independently fire
and confirms `fixed_tp` — the earlier candidate — wins).

Data requirements and graceful degradation, per candidate:

- `fixed_tp` / `fixed_sl` — need `position.entry_price` and
  `features.price`; skip (never trigger) if either is missing.
- `volatility_based_sl` — additionally needs `features.volatility`; skips
  if absent, so it never silently falls back to acting like `fixed_sl`.
- `probability_reversal` — needs a **fresh model score** merged into
  `features` (`probability_up`/`probability_down`/`probability_neutral`)
  by the caller, e.g. from a fresh `SignalEngine.build_signal()` call — a
  bare feature row doesn't carry this on its own, since `predict_proba()`
  is a model operation, not a feature-engine one. Skipped, not an error,
  when absent.
- `order_flow_reversal` / `liquidity_reversal` — need entry-time snapshot
  keys on `position` (`entry_obi`, `entry_liquidity_imbalance`) that the
  caller populates when opening the position; skipped if absent.
- `time_stop` — `features["timestamp"]` doubles as "now" (not a separate
  wall-clock read), keeping this function as deterministic/testable as
  everything upstream of it.

## `strategy/strategy_engine.py` — stateful orchestration, optional executor

`StrategyEngine(signal_engine, *, risk_state=None, executor=None,
max_open_positions=1, position_size_quote=1000.0, ...)`. Stateful across
successive `process_row()` calls — `self.open_positions`,
`self.closed_positions` — matching how a live tick-by-tick loop or a
streaming replay actually calls it.

`process_row(row, *, now=None)` checks every open position for an exit
first (`should_exit()`), then — if room remains under
`max_open_positions` — builds a fresh signal (`signal_engine.build_signal
(row, now=now)`) and opens a position if `should_enter()` agrees. Because
exit is checked before entry within the *same* call, a position that
closes mid-tick can free up room for a new one to open on that same row —
this is intentional (a live tick-by-tick loop calling `process_row()`
once per row shouldn't have to wait an extra tick just because a slot
freed up), and is exercised directly by
`test_room_for_new_position_after_a_close_same_tick`.

`executor` is an **optional, duck-typed** stand-in for Phase 13's
`execution.execution_interface` (doesn't exist yet): pass one exposing
`place_order(asset=, side=, quantity=)` and every open/close additionally
calls it; pass `None` (the default) and `StrategyEngine` runs as a fully
self-contained research orchestrator, returning the actions it *would*
have taken. This makes Phase 11 fully testable today, directly comparable
against `backtest/event_backtester.py`'s trade-level output, without
inventing a fake execution interface Phase 13 would just have to replace.

`risk_state` follows `entry.py`'s placeholder contract exactly — a static
dict or a zero-argument callable, re-resolved fresh on every
`process_row()` call (not cached from construction), so a real
`RiskManager.state` method (Phase 12) can be wired in with no signature
change.

## Testing

99 new tests across the six Phase 11 modules
(`test_direction.py`, `test_confidence.py`, `test_scoring.py`,
`test_signal_engine.py`, `test_entry.py`, `test_exit.py`,
`test_strategy_engine.py`), plus 8 new regression tests in
`test_event_backtester.py` covering the `direction_from_probabilities()`
refactor (decision output unchanged, three-way ties break `up`) and
`simulate_fill_costs()` directly (long/short profit and loss, zero
quantity, zero costs, and equivalence against a real backtested trade's
own cost fields). 471 total, all passing.

`StrategyEngine` was additionally smoke-tested end-to-end before the
formal suite was written: a scripted fake model wrapped in a real
`SignalEngine`, fed a short synthetic price sequence through repeated
`process_row()` calls, confirmed opening on a qualifying signal, closing
on `fixed_tp`, a same-tick reopen after a close, `max_open_positions`
being respected, and a fake `executor` stub receiving the expected
`place_order()` calls on both open and close.

## No live-run or dedicated CLI script this phase

Same reasoning as Phases 8-10 — wiring `SignalEngine`/`StrategyEngine`
into `main.py`'s live loop against real collected feature data is a
research-side activation step, not something this phase's scope covers;
Phase 12 (risk management) and Phase 13 (paper execution) still need to
land first before a live-shaped run makes sense.
