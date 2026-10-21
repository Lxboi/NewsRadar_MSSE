# Backtesting — Implementation Notes (Phase 10)

Sections 44-46, restated in `docs/08_backtesting_strategy.md`'s "order of
operations": `baselines -> strategy hypotheses -> statistical models ->
walk-forward -> statistical validation`. This document is the
implementation record for the last two links in that chain —
`src/backtest/{metrics,event_backtester,orderbook_backtester,
walk_forward}.py` — plus the one refactor of Phase 7 code this phase
required.

## Why this doesn't call `signal_engine.py`/`strategy_engine.py`/`paper_executor.py`

Those don't exist yet — they're Phase 11 (signals/strategy) and Phase 13
(paper execution), both numbered *after* backtesting in the roadmap.
Section 44's backtest mechanics (chronological no-look-ahead replay,
realistic costs, PnL) don't actually require the live-deployable
orchestration classes those phases will build: they need *some*
per-row directional decision to backtest, which Phase 8's baselines and
Phase 9's models already produce. `event_backtester.py`'s
`decisions_from_model()`/`decisions_from_baseline()` are a deliberately
simple stand-in entry rule — probability/EV thresholding straight from
`config/strategy.yaml`'s `entry_filter` block — not a claim that this is
final production logic. When `SignalEngine`/`StrategyEngine` exist,
`EventBacktester.run()` can just as easily be handed a `decisions` list
built from their output instead: the interface (`direction`,
`probability`, `expected_value_bps` per row) doesn't change.

## The Phase 7 refactor: `find_forward_indices()` / `find_forward_prices()`

`features/feature_engine.py: compute_targets()` already had a two-pointer
O(n) scan to find "the row h seconds forward" for its up/down/flat
labels — but it only ever returned a boolean threshold-crossing flag, not
the continuous forward price a backtester needs to compute actual PnL.
Rather than have `event_backtester.py` re-derive that scan a second time
(and risk it silently drifting from what the target labels already
encode — the classic failure mode this codebase has repeatedly built
shared functions to avoid, e.g. `feature_matrix.py` reusing
`evaluation.actual_direction()`), the scan was extracted into
`find_forward_indices(rows, horizon_s)` (returns the row *index*, `None`
at the tail), with `find_forward_prices()` as a one-line convenience
wrapper for `compute_targets()`'s own price-only need.
`event_backtester.py` calls `find_forward_indices()` directly, since it
needs both the exit row's price *and* its timestamp (for `closed_at`) —
a `compute_targets()` regression test
(`test_compute_targets_uses_find_forward_prices_not_a_second_implementation`)
guards against this drifting back into two implementations later. All 21
pre-existing `test_feature_engine.py` tests still pass unchanged after
the refactor — it's a pure extraction, not a behavior change.

## `metrics.py` — aggregation, not simulation

Every function here is pure aggregation over an already-simulated trade
list (`event_backtester.py`'s job, not this module's) — nothing here
runs a strategy or reads market data. Trade dicts are shaped like
`storage/database.py: Position` (`net_pnl`, `gross_pnl`, `fees`,
`slippage`, `opened_at`, `closed_at` required; `asset`/`side`/
`entry_price`/`exit_price`/`quantity`/`exit_reason` optional, used only
for later reporting) — the same schema a live/paper trading run would
eventually persist, so this module doubles as the aggregation layer for
`monitoring/performance.py` (Phase 13) without a translation step.

Section 46's four extra tools each answer a genuinely different
question, not four variations on the same one:

- **`compute_metrics()`** — the core section-44 numbers, computed once
  over the trades exactly as they occurred.
- **`bootstrap_confidence_intervals()`** — resamples trades *with
  replacement* to ask whether the headline PnL/Sharpe is a stable
  estimate or a small-sample fluke.
- **`monte_carlo_reshuffle()`** — *reorders* (never resamples) the same
  exact set of trade outcomes to stress-test path-dependent risk
  (drawdown). Total net PnL is a sum, so it is mathematically identical
  under every permutation — this function deliberately does not report
  it, only `actual_max_drawdown` and where it falls in the shuffled
  distribution's percentile.
- **`probability_of_ruin()`** — bootstrap-resamples entire equity
  *paths* (not a single number) and reports how often a path built from
  this trade population would have breached a ruin threshold at any
  point, not just at the end.

**Sharpe/Sortino are per-trade by default, not daily-bar-annualized.**
This system's holding times are 10s-300s (section 39) and crypto trades
24/7 — the classic 252-trading-day equity-market annualization
convention doesn't apply. `sharpe_annualized`/`sortino_annualized` scale
the per-trade statistic by `sqrt(trades_per_year)`, where
`trades_per_year` is measured *empirically* from the actual span between
the first trade's open and the last trade's close in that exact backtest
run, not assumed from a calendar convention.

`win_rate`'s denominator excludes exact-breakeven trades (a trade with
`net_pnl == 0` counts toward neither wins nor losses) — there's no "right
answer" to score a breakeven against, same convention
`models/evaluation.py`'s baseline accuracy check already uses for flat
targets. `profit_factor` is `None` (not `inf`) when there are no losing
trades, matching this codebase's established "insufficient data is
`None`, never a fabricated number" rule (`compute_targets()`,
`calibration.py: roc_auc_macro`, etc.).

## `event_backtester.py` — the execution simulation

**Exit model: fixed-horizon only.** A position opened at row `i` closes
at whichever row is first at-or-after `rows[i]["timestamp"] + horizon_s`
— the exact same `find_forward_indices()` lookup `compute_targets()`
uses, so a trade's realized PnL is always consistent with what that
row's own `target_{h}s_*` columns already say about the same move.
`strategy.yaml`'s other `exit_candidates` (volatility-based SL,
probability reversal, order-flow reversal, liquidity reversal, time
stop) are section 39's domain — Phase 11's `strategy/exit.py` — and are
deliberately not implemented here.

**Decision adapters, and why baselines skip the probability filter.**
`decisions_from_model()` takes the argmax of a Phase 9 model's three
class probabilities (`1`=up, `-1`=down, `0`=neutral); a directional call's
`expected_value_bps` uses the model's own classification boundary
(`threshold_bps`) as the assumed payout size if the move happens —
`probability * threshold_bps - (1 - probability) * threshold_bps`. This
is a documented simplification (the model was trained to answer "did the
move clear threshold_bps", not "what was its exact size"), not a claim
of precision. `decisions_from_baseline()` always returns
`probability=None, expected_value_bps=None`, meaning every nonzero
baseline signal is taken as a trade — costs/spread/regime filters still
apply, but the probability/EV gate does not. This is deliberate:
`docs/08_backtesting_strategy.md` point 1 ("beats its baselines... on
the same data, same costs, same period") is fairest when baselines get
their best shot, gated only by the same execution realism a model's
trades face, not held to a probability threshold they were never
designed to produce.

**Costs are subtracted where the entry decision is made, not baked into
the model's raw EV.** `EventBacktester.round_trip_cost_bps` (`2 *
(taker_fee_bps + assumed_slippage_bps)` — entry and exit each pay a fee
and assumed slippage once) is subtracted from a decision's raw
`expected_value_bps` inside `_passes_entry_filter()`, so
`config/strategy.yaml`'s "EV must be strictly positive after costs"
(section 37) is actually enforced at the point of decision, not just
computed and ignored. Keeping "what the model implies" (in
`decisions_from_model()`) and "what it costs to act on it" (in
`EventBacktester`) as two separate, independently-inspectable numbers
was a deliberate separation of concerns.

**Position sizing is a fixed notional placeholder.**
`position_size_quote` (default $1000) converts to `quantity =
position_size_quote / entry_price` for every trade — section 40's real
position sizing is Phase 12's `strategy/position_sizing.py`. Every
trade's `quantity` is recorded, so a later phase can rescale post-hoc.

**`max_open_positions` defaults to 1**, matching
`config/strategy.yaml: risk.max_open_positions`. `EventBacktester.run()`
tracks currently-open positions by their (already-computed) exit index
and skips new entries once the limit is reached — a simple, generalizes-
to-N mechanism, not hardcoded to exactly one position.

`entry_filter` checks that read a row column (`spread`, `market_regime`)
only apply when that column is actually present and non-`None` on the
row — a row missing the column entirely is never treated as failing the
filter, only an explicit out-of-bounds value is.

## `orderbook_backtester.py` — enforcing the news-free ablation structurally

Section 44 explicitly calls for a way "to isolate order-flow edge" —
i.e., to check whether a strategy's edge survives when news access is
removed *by construction*, not just because a particular model/baseline
happens not to use it. `strip_news_columns()` removes every
`news_*`-prefixed key from the row dicts entirely (not zeroed, not
defaulted) — a `row["news_score"]` access after stripping raises
`KeyError` immediately, rather than silently returning a stale or
zeroed value a model could still learn something from.

**The important caveat, called out prominently in the module docstring
because it's easy to get backwards**: stripping has to happen *before* a
decision function ever scores the rows, not just before
`OrderBookBacktester.run()`. By the time `run()` is called, the
`decisions` list already exists — stripping columns out of `rows` at
that point can't retroactively un-train a model that already had news
access. `OrderBookBacktester.run()`'s own stripping is a second,
redundant safety net for the parts of `EventBacktester.run()` that read
`rows` directly (`spread`/`market_regime`/`price`/`timestamp`/`asset` —
never news), not a substitute for the caller stripping before scoring.
The correct sequence is documented with a worked example in the module's
own docstring.

## `walk_forward.py` — purged, chronological-only windows

**No random splits**, per section 45's explicit requirement: windows are
`train -> validation -> test`, strictly chronological, advancing by
`step_s` each iteration (an expanding-style walk-forward — later windows
reuse most of the data of earlier ones rather than discarding history).

**Purging.** A training row near the very end of the `train` window
still needs a price up to `horizon_s` seconds in its own future to
resolve its own label (`compute_targets()`'s job) — if that price falls
inside the nominal `validation` window's time range, that's not future-
peeking in the section-29 sense (the model never sees `validation`'s
*feature* columns, only reuses its realized prices to compute a
`train`-row's own label), but it does blur the boundary between what's
genuinely "seen" and "held out". `purge_s` (default: `horizon_s`) is
subtracted from the trailing edge of both the `train` and `validation`
slices before they're used, so every window's three slices are strictly
separated by at least one full label horizon — standard practice in
walk-forward validation with fixed-horizon labels, made explicit and
tested (`test_build_windows_purges_a_full_horizon_from_trailing_edge_of_train_and_validation`)
rather than left as an unstated assumption.

**Fit once per window, not once per slice.** An earlier draft called
`fit_fn(train_rows)` separately for the `validation` and `test`
evaluations of the same window — caught before delivery (a test
asserting `fit_fn` is called exactly once per window failed against the
first implementation, `assert 6 == 3` for a 3-window run). Fixed so the
model is fit once per window and the same fitted model scores both
slices; a real Phase 9 model fit is expensive enough that doubling it
for no reason would have been a real, if invisible, inefficiency.

**Regime labeling is a different granularity than Phase 7's
`market_regime`.** `classify_window_regime()` produces a *window-level*
summary — trend direction (bull/bear/sideways, from the window's total
return in bps against a configurable `trend_threshold_bps`, default 20),
a volatility majority-vote (collapsing Phase 7's own per-row LOW/NORMAL/
HIGH/EXTREME_VOL `market_regime` column into low/high — reused rather
than reinventing a second volatility classifier), and a `news_shock`
flag (any row's `news_score` magnitude exceeding
`news_shock_min_abs_score`, default 0.5, an explicitly illustrative
starting point, not empirically tuned — same "documented placeholder,
not a validated threshold" spirit as `strategy.yaml`'s own
`probability_up_min=0.62`). This directly answers
`docs/08_backtesting_strategy.md` point 3's question — does a strategy's
performance hold across regimes, or is it concentrated in one —
without needing a second bespoke classifier for the trend/news-shock
components that Phase 7's `RegimeDetector` never covered.

`run()`'s `summary.test_net_pnl_by_trend` breaks total out-of-sample net
PnL down by trend label specifically so "this only works in a bull
market" would be visible in the output, not buried in one aggregate
number — a strategy is not validated by one figure any more than by one
backtest (section 4).

## Smoke-testing

Following the established discipline: `metrics.py`'s four functions were
run against a hand-built synthetic trade list first (verifying bootstrap
CI mean tracks the actual total, Monte Carlo's actual-drawdown percentile
behaves sensibly, probability-of-ruin responds correctly to both a
zero-risk and a clearly-ruinous trade sequence) before any test was
written. `event_backtester.py` was smoke-tested against deterministic
uptrend/downtrend synthetic price series (verifying long-in-uptrend and
short-in-downtrend are both gross-profitable, non-overlapping positions
under `max_open_positions=1`, and that the EV/probability/spread/regime
filters each independently block entries when they should). A full
integration smoke test then wired a real fitted `LogisticDirectionModel`
through `decisions_from_model()` -> `EventBacktester.run()` ->
`compute_metrics()`/`bootstrap_confidence_intervals()`/
`monte_carlo_reshuffle()`/`probability_of_ruin()` end to end against
~2000 synthetic feature+target rows, confirming every module's output
shape composes with the next without a translation step — this is what
would have caught a shape mismatch between `event_backtester.py`'s trade
dicts and `metrics.py`'s expected input, had one existed. `walk_forward.py`
was smoke-tested against a three-segment synthetic series (bull, then
bear, then sideways, by construction) and confirmed
`classify_window_regime()` correctly recovered all three trend labels
from the window's realized returns.
