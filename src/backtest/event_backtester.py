"""Chronological, no-look-ahead event backtester combining market data +
order book + trades + news (section 44).

Roadmap: PHASE 10. Operates directly on a Phase 7 `features_v1` row
table (`FeatureEngine.build_rows()` + `compute_targets()`'s output) —
`docs/08_backtesting_strategy.md`'s "same schema as processed/, so
backtest and live code paths share logic" is satisfied by reusing Phase
7's replay/feature machinery to build the row table in the first place,
rather than this module re-reading raw Parquet itself. This module's own
job, per section 44's mechanics list, is entirely about *execution*:
applying entry rules, exit timing, transaction costs, and turning that
into a chronological trade ledger — never about re-deriving what the
market did or what a model predicted.

## Why this doesn't call `signal_engine.py`/`strategy_engine.py`

Those are Phase 11 (formalizing signal generation and entry/exit into
live-deployable classes) and Phase 13 (paper execution). Phase 10 comes
before both in the roadmap, and section 44's backtest mechanics — no-
look-ahead replay, realistic costs, PnL — don't actually require them:
what they need is *some* per-row directional decision to backtest, which
Phase 8's baselines and Phase 9's models already produce. `decisions_from_model()`
and `decisions_from_baseline()` below are a deliberately simple stand-in
entry rule (probability/EV thresholding straight from `config/strategy.yaml`'s
`entry_filter` block) — not a claim that this is the final production
entry logic. Phase 11 formalizes a richer version of the same idea into
`SignalEngine`/`StrategyEngine`; when that exists, `EventBacktester.run()`
can just as easily be handed a `decisions` list built from
`SignalEngine` output instead, since the interface here (`direction`,
`probability`, `expected_value_bps` per row) doesn't change.

## Exit model

Exit is currently **fixed-horizon only**: a position opened at row `i`
closes at whichever row is first at-or-after `rows[i]["timestamp"] +
horizon_s` (via `features.feature_engine.find_forward_indices()` — the
exact same no-look-ahead-safe lookup `compute_targets()` uses to build
its labels, so a trade's realized PnL is always consistent with what
that row's `target_{h}s_*` columns already say about the same move).
`config/strategy.yaml`'s other `exit_candidates` (volatility-based SL,
probability reversal, order-flow reversal, liquidity reversal, time
stop) are section 39's domain — Phase 11's `strategy/exit.py` — and are
deliberately not implemented here; this backtester answers "does the
model's own classification boundary survive realistic costs and
walk-forward", not "what's the best possible exit rule".

## Position sizing

Fixed notional per trade (`position_size_quote`, default matches no
particular config value — section 40's real position sizing is Phase
12's `strategy/position_sizing.py`). This is an explicit placeholder,
not a claim about real position sizing; every trade's `quantity` is
still recorded, so a later phase can rescale post-hoc if needed.
"""
from __future__ import annotations

from src.features.feature_engine import find_forward_indices
from src.models.baseline_model import baseline_signal
from src.models.direction import direction_from_probabilities

# Defaults mirror config/strategy.yaml's own values, documented per key
# below rather than loaded from YAML directly — same convention
# baseline_model.py/evaluation.py already use (this module has no config
# I/O of its own; a caller wires in real config values as constructor
# kwargs).
DEFAULT_TAKER_FEE_BPS = 5              # strategy.yaml: costs.taker_fee_bps
DEFAULT_ASSUMED_SLIPPAGE_BPS = 3       # strategy.yaml: costs.assumed_slippage_bps
DEFAULT_PROBABILITY_MIN = 0.62         # strategy.yaml: entry_filter.probability_up_min
DEFAULT_MIN_EXPECTED_VALUE_BPS = 0.0   # strategy.yaml: entry_filter.min_expected_value
DEFAULT_MAX_SPREAD_BPS = 5             # strategy.yaml: entry_filter.max_spread_bps
DEFAULT_ALLOWED_REGIMES = ("LOW_VOL", "NORMAL_VOL")  # strategy.yaml: entry_filter.allowed_regimes
DEFAULT_MAX_OPEN_POSITIONS = 1         # strategy.yaml: risk.max_open_positions
DEFAULT_POSITION_SIZE_QUOTE = 1000.0
DEFAULT_INITIAL_EQUITY = 10_000.0


def decisions_from_model(model, rows: "list[dict]", *, threshold_bps: float) -> "list[dict]":
    """Adapts a Phase 9 model (`LogisticDirectionModel`/`TreeDirectionModel`/
    `CalibratedDirectionModel` — anything exposing `predict_proba(rows)`)
    into the `{"direction", "probability", "expected_value_bps"}` decision
    shape `EventBacktester.run()` consumes. `direction` is the argmax
    class (`1`=up, `-1`=down, `0`=neutral) — a genuinely-neutral call is
    `direction=0` with `expected_value_bps=None` (nothing to compute an
    edge for). For a directional call, `expected_value_bps` uses the
    model's own classification boundary (`threshold_bps`) as the assumed
    payout size if the move happens: `probability * threshold_bps - (1 -
    probability) * threshold_bps`. This is a deliberate simplification
    (the model was trained to answer "did the move clear threshold_bps",
    not "what was the move's exact size"), not a precise expected-move
    estimate — flagged here rather than silently presented as more
    precise than it is. Transaction costs are **not** subtracted here;
    `EventBacktester` does that itself against its own cost assumptions,
    keeping "what the model implies" and "what it costs to act on it"
    separate and independently inspectable."""
    proba = model.predict_proba(rows)
    decisions = []
    for p in proba:
        direction, probability = direction_from_probabilities(p)
        if direction == 0:
            decisions.append({"direction": 0, "probability": probability, "expected_value_bps": None})
            continue
        expected_value_bps = probability * threshold_bps - (1 - probability) * threshold_bps
        decisions.append({"direction": direction, "probability": probability, "expected_value_bps": expected_value_bps})
    return decisions


def decisions_from_baseline(rows: "list[dict]", kind: str, *, baseline_kwargs: "dict | None" = None) -> "list[dict]":
    """Adapts a Phase 8 baseline (`baseline_model.baseline_signal()`) into
    the same decision shape. Baselines output a raw signed score, not a
    probability, so `probability`/`expected_value_bps` are always `None`
    — meaning `EventBacktester`'s probability/EV entry-filter checks are
    skipped for baseline decisions (a `None` bypasses that specific
    check, see `EventBacktester._passes_entry_filter()`), and every
    nonzero baseline signal is taken as a trade. This is a deliberate,
    documented methodological choice: `docs/08_backtesting_strategy.md`'s
    point 1 ("beats its baselines... on the same data, same costs, same
    period") is fairest when baselines get their best shot — gated only
    by cost/spread/regime, the same as any trade actually taken — rather
    than being held to a probability threshold they were never designed
    to produce."""
    kwargs = baseline_kwargs or {}
    decisions = []
    for row in rows:
        signal = baseline_signal(row, kind, **kwargs)
        direction = 1 if signal > 0 else (-1 if signal < 0 else 0)
        decisions.append({"direction": direction, "probability": None, "expected_value_bps": None})
    return decisions


def simulate_fill_costs(
    entry_price: float,
    exit_price: float,
    direction: int,
    quantity: float,
    *,
    taker_fee_bps: float,
    assumed_slippage_bps: float,
) -> dict:
    """Fee/slippage/PnL math for one round-trip trade — entry and exit
    each pay `taker_fee_bps` and `assumed_slippage_bps` once, against a
    notional of `entry_price * quantity` for the entry leg (the exit leg
    is charged against the same `quantity`, i.e. no re-notionalizing at
    the exit price, matching how a fixed-size order actually settles).
    `direction` is `1` (long: profits when `exit_price > entry_price`) or
    `-1` (short: profits when `exit_price < entry_price`).

    Public (Phase 11) specifically so `strategy/strategy_engine.py`'s
    live paper-trading-shaped position closing can share this exact
    formula with `EventBacktester._build_trade()` — the same reasoning
    `docs/08_backtesting_strategy.md` gives for backtest and live sharing
    a row schema applies just as much to sharing the cost arithmetic:
    a live PnL number and a backtested one should never disagree about
    what "the same trade" would have cost, because they're computed by
    two different, silently-drifting implementations.

    Returns `{"gross_pnl", "fees", "slippage", "net_pnl"}`."""
    notional = entry_price * quantity
    entry_fee = taker_fee_bps / 10_000 * notional
    exit_fee = taker_fee_bps / 10_000 * notional
    entry_slippage = assumed_slippage_bps / 10_000 * notional
    exit_slippage = assumed_slippage_bps / 10_000 * notional
    fees = entry_fee + exit_fee
    slippage = entry_slippage + exit_slippage

    gross_pnl = (exit_price - entry_price) * quantity * direction
    net_pnl = gross_pnl - fees - slippage

    return {"gross_pnl": gross_pnl, "fees": fees, "slippage": slippage, "net_pnl": net_pnl}


class EventBacktester:
    """See module docstring. One instance can be reused across multiple
    `run()` calls (e.g. `walk_forward.py` sweeping windows) since it
    holds no per-run state — every `run()` call is independent."""

    def __init__(
        self,
        *,
        taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS,
        assumed_slippage_bps: float = DEFAULT_ASSUMED_SLIPPAGE_BPS,
        probability_min: "float | None" = DEFAULT_PROBABILITY_MIN,
        min_expected_value_bps: "float | None" = DEFAULT_MIN_EXPECTED_VALUE_BPS,
        max_spread_bps: "float | None" = DEFAULT_MAX_SPREAD_BPS,
        allowed_regimes: "tuple[str, ...] | None" = DEFAULT_ALLOWED_REGIMES,
        max_open_positions: int = DEFAULT_MAX_OPEN_POSITIONS,
        position_size_quote: float = DEFAULT_POSITION_SIZE_QUOTE,
    ) -> None:
        self.taker_fee_bps = taker_fee_bps
        self.assumed_slippage_bps = assumed_slippage_bps
        self.probability_min = probability_min
        self.min_expected_value_bps = min_expected_value_bps
        self.max_spread_bps = max_spread_bps
        self.allowed_regimes = allowed_regimes
        self.max_open_positions = max_open_positions
        self.position_size_quote = position_size_quote

    @property
    def round_trip_cost_bps(self) -> float:
        """Entry + exit, each paying fee + assumed slippage once — the
        cost figure `_passes_entry_filter()` subtracts from a decision's
        raw `expected_value_bps` before comparing against
        `min_expected_value_bps`, so "EV must be positive after costs"
        (section 37) is actually enforced, not just a raw model-implied
        number."""
        return 2 * (self.taker_fee_bps + self.assumed_slippage_bps)

    def run(self, rows: "list[dict]", decisions: "list[dict]", *, horizon_s: int) -> "list[dict]":
        """`rows` and `decisions` must be the same length and index-
        aligned (`decisions[i]` is the decision made *at* `rows[i]`,
        using only information available at or before `rows[i]`'s
        timestamp — that discipline is `decisions_from_model()`/
        `decisions_from_baseline()`'s responsibility, since both only
        ever look at `rows[i]` itself). Returns a chronological list of
        `Position`-shaped trade dicts (see module docstring / `metrics.py`
        for the exact shape) — empty if nothing ever qualified to trade,
        never raising for "no trades", since a parameter combination that
        never trades is a normal (if uninteresting) backtest outcome."""
        if len(rows) != len(decisions):
            raise ValueError(f"rows and decisions must be the same length, got {len(rows)} vs {len(decisions)}")

        exit_indices = find_forward_indices(rows, horizon_s)
        trades: "list[dict]" = []
        open_exits: "list[int]" = []  # exit indices of currently-open positions

        for i, (row, decision) in enumerate(zip(rows, decisions)):
            open_exits = [j for j in open_exits if j > i]  # drop positions that have since closed

            direction = decision.get("direction", 0)
            if direction == 0:
                continue
            if len(open_exits) >= self.max_open_positions:
                continue

            exit_index = exit_indices[i]
            if exit_index is None:
                continue  # not enough forward data in this table to know the exit price yet

            entry_price = row.get("price")
            exit_price = rows[exit_index].get("price")
            if entry_price is None or exit_price is None or entry_price == 0:
                continue

            if not self._passes_entry_filter(row, decision):
                continue

            trade = self._build_trade(row, rows[exit_index], direction, entry_price, exit_price, decision, horizon_s, i, exit_index)
            trades.append(trade)
            open_exits.append(exit_index)

        return trades

    def _passes_entry_filter(self, row: dict, decision: dict) -> bool:
        probability = decision.get("probability")
        if probability is not None and self.probability_min is not None and probability < self.probability_min:
            return False

        raw_ev_bps = decision.get("expected_value_bps")
        if raw_ev_bps is not None and self.min_expected_value_bps is not None:
            net_ev_bps = raw_ev_bps - self.round_trip_cost_bps
            if net_ev_bps < self.min_expected_value_bps:
                return False

        spread = row.get("spread")
        if spread is not None and self.max_spread_bps is not None and spread > self.max_spread_bps:
            return False

        regime = row.get("market_regime")
        if regime is not None and self.allowed_regimes is not None and regime not in self.allowed_regimes:
            return False

        return True

    def _build_trade(
        self,
        entry_row: dict,
        exit_row: dict,
        direction: int,
        entry_price: float,
        exit_price: float,
        decision: dict,
        horizon_s: int,
        entry_index: int,
        exit_index: int,
    ) -> dict:
        quantity = self.position_size_quote / entry_price
        costs = simulate_fill_costs(
            entry_price,
            exit_price,
            direction,
            quantity,
            taker_fee_bps=self.taker_fee_bps,
            assumed_slippage_bps=self.assumed_slippage_bps,
        )

        return {
            "asset": entry_row.get("asset"),
            "side": "buy" if direction == 1 else "sell",
            "entry_price": entry_price,
            "exit_price": exit_price,
            "quantity": quantity,
            "opened_at": entry_row["timestamp"],
            "closed_at": exit_row["timestamp"],
            "exit_reason": "horizon_exit",
            "gross_pnl": costs["gross_pnl"],
            "fees": costs["fees"],
            "slippage": costs["slippage"],
            "net_pnl": costs["net_pnl"],
            "probability": decision.get("probability"),
            "expected_value_bps": decision.get("expected_value_bps"),
            "horizon_s": horizon_s,
            "entry_index": entry_index,
            "exit_index": exit_index,
        }
