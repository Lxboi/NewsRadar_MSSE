# Paper Execution — Implementation Notes (Phase 13)

Sections 41, 43, 53-54, 70: an exchange-agnostic execution contract, a
realistic paper-trading simulator against it, and live/paper performance
tracking — `src/execution/{execution_interface,paper_executor}.py` and
`src/monitoring/performance.py`.

## `execution/execution_interface.py` — an ABC, not a duck-type

Unlike `strategy/strategy_engine.py`'s optional `executor` (deliberately
duck-typed, since "no executor at all" is a fully valid mode), this
interface is the contract `PaperExecutor` (this phase) and `KrakenExecutor`
(Phase 14) both implement — so it's an `abc.ABC` with five
`@abstractmethod`s (`place_order`, `cancel_order`, `get_position`,
`get_balance`, `get_open_orders`). A subclass missing even one method
can't be instantiated at all — the same "structural, not conventional"
enforcement `backtest/orderbook_backtester.py`/`notifications/
alert_manager.py` already use, applied here to a code-shape contract
instead of a data-shape one. `place_order`'s three required kwargs
(`asset`, `side`, `quantity`) match exactly what
`StrategyEngine._open_position()`/`_close_position()` (Phase 11) already
call it with.

**A known integration gap, flagged rather than silently patched**:
`StrategyEngine` calls `executor.place_order(...)` but has never read its
return value (Phase 11's docstring says the executor is called
"additionally," not "instead of" the shared `simulate_fill_costs()` math).
That's unchanged here — a `PaperExecutor`'s realistically-simulated fill
price, rejection, or partial fill is fully computed and logged by this
phase, but `StrategyEngine`'s own PnL bookkeeping doesn't yet consume it.
Deciding whether a strategy should defer to a real executor's actual
fill or stay on the shared simulated formula (for backtest/live
comparability) is a `main.py` integration decision, not one to guess at
by quietly changing already-delivered, already-tested Phase 11 code.

## `execution/paper_executor.py` — stress-testing the fixed-cost assumption

Where `EventBacktester`/`SignalEngine`/`StrategyEngine`'s shared
`simulate_fill_costs()` (Phase 10-11) is a deliberately simple, constant-
bps cost model (kept fixed specifically so backtested and live PnL stay
comparable), `PaperExecutor` exists to stress-test that assumption with
real variance: latency sampled from a configurable range, slippage drawn
uniformly on `[0, 2 * assumed_slippage_bps]` (same expected value as the
fixed assumption, but with spread), and configurable reject/partial-fill
probabilities. `random_state` follows the exact `int | np.random.
RandomState | None` convention `backtest/metrics.py`'s bootstrap/
Monte-Carlo functions already established (Phase 10), for reproducible
tests.

**Price source**: a duck-typed, injected `price_lookup(asset) ->
{"price": float, "spread_bps": float}` — `PaperExecutor` has no live
market-data connection of its own. Wiring it to the live
`OrderBookEngine`'s current quote (or a replayed row's own columns for
offline paper-testing) is a `main.py` concern.

**Two kinds of "not filled," deliberately distinct**: a **rejected**
order never executes (bad notional, a simulated exchange-side reject) and
carries no fee. A **pending** order is a limit order that doesn't
currently cross the market — it sits in `get_open_orders()` until
`cancel_order()` or a later `try_fill_pending()` call re-checks it
against a fresh quote. This is intentionally not a full matching engine
(no depth-aware partial crossing, no time-priority queue) — section 43
asks for realistic *cost* simulation, not a second order book engine,
which would duplicate `orderbook/orderbook_engine.py`'s job for no
benefit a paper account needs.

**Position/balance ledger**: `PaperExecutor` tracks its own running-
inventory account state (volume-weighted average cost per asset,
realized/unrealized PnL, cash) — deliberately a *different* bookkeeping
model than `StrategyEngine`'s one-entry-one-exit trade tracking (Phase
11), not a replacement for it. They answer different questions: this
ledger is "what does the paper account actually hold and owe fees on
right now" (the same way a real exchange account would, supporting
partial fills and multiple orders accumulating one position);
`StrategyEngine`'s `open_positions`/`closed_positions` answer "what
trade did the strategy attribute its PnL to." Divergence between the two
is expected, not a bug to reconcile here.

## `monitoring/performance.py` — reuses Phase 10's metrics, doesn't reinvent them

`PerformanceTracker.record_trade()` reads the exact same closed-position
dict shape `StrategyEngine._close_position()`/`EventBacktester`'s trade
dicts already produce. `hourly_snapshot()`/`daily_snapshot()`/
`all_time_snapshot()` each window the accumulated trade list and call
`backtest/metrics.py: compute_metrics()` (Phase 10) directly — this
module's whole job is answering the same win-rate/PnL/drawdown questions
Phase 10's backtest analysis already answers, just over live/paper trades
instead of a backtest run, so it was never going to reimplement that math
a third time.

Snapshots are shaped to match `storage/database.py: PerformanceSnapshot`'s
exact column set. `to_performance_snapshot_row()` is a pure, DB-agnostic
shaping helper — this module never imports SQLAlchemy directly, the same
"keep it DB-agnostic" convention `features/feature_engine.py` (Phase 7)
established for exactly this reason. These snapshot dicts are also the
natural real implementation behind `notifications/telegram_bot.py`'s
(Phase 12) `performance_provider`/`today_provider` injected callables.

**A real bug caught by the test suite, fixed before delivery**:
`trades_since()`'s original filter only checked a lower bound
(`closed_at >= cutoff`), with no upper bound — a trade that closed
*after* the snapshot's own `now` could leak into an earlier window
(confirmed directly via a smoke test: an hourly snapshot at `now =
BASE + 31min` incorrectly included a trade closing at `BASE + 2h`).
Fixed by requiring `cutoff <= closed_at <= now`, with a dedicated
regression test.

**"best_setup"/"worst_setup" — an interpretive choice**: the master
prompt doesn't define "setup," and closed-position dicts carry no
`strategy_id`-style field — the only categorical dimension available
besides `asset` is `exit_reason` (which of `strategy/exit.py`'s 7
candidates closed the trade). `best_setup`/`worst_setup` are computed as
the `exit_reason` with the highest/lowest aggregate `net_pnl` in the
window — flagged as an interpretive choice given the available data, the
same way every other interpretive default in this codebase is. There is
deliberately no `worst_asset`: `PerformanceSnapshot`'s own schema only
has a `best_asset` column.

## Testing

45 new tests: `test_execution_interface.py` (3 — the ABC blocks direct
instantiation and any incomplete subclass, a complete subclass
instantiates and works), `test_paper_executor.py` (29 — spread/fee/
slippage/latency math, every reject/partial-fill/pending path, limit-
order crossing in both directions, `cancel_order`/`try_fill_pending`,
opening/adding-to/reducing/flipping-through-flat a position with correct
realized-PnL accounting, balance cash/unrealized-PnL tracking including
graceful degradation with no `price_lookup`, and the append-only decision
log), `test_performance.py` (13 — window inclusion/exclusion at both
bounds including the caught bug's regression test, exact agreement with
`compute_metrics()` called directly, best/worst asset/setup ranking
including graceful exclusion of trades missing that dimension, the DB
row shaping helper's exact column set). 599 total, all passing.

## No live-run this phase

Same reasoning as Phases 8-12: no real Kraken/exchange connection is
wired up yet (that's Phase 14, explicitly gated on paper validation per
section 5's progression), and `PaperExecutor`/`PerformanceTracker` have
no live signal/strategy feed to run against until `main.py` wires
`StrategyEngine` through to them.
