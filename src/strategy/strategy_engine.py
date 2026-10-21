"""Orchestrates entry/exit/risk for the active research strategy
(sections 34-36, 67). Never talks to Kraken directly — only through
execution.execution_interface.

Roadmap: PHASE 11. `execution.execution_interface` (Phase 13) doesn't
exist yet, so `StrategyEngine` is built to the same contract that module
will eventually provide (`place_order`/`cancel_order`/`get_position`/
`get_balance`) via an **optional, duck-typed `executor`** — pass one when
Phase 13 exists and every position open/close will additionally call
`executor.place_order(...)`; pass `None` (the default) and
`StrategyEngine` still runs end-to-end as a self-contained research
orchestrator, tracking hypothetical positions and returning the actions
it *would* have taken. This lets Phase 11 be fully testable and directly
comparable against `backtest/event_backtester.py`'s trade-level output
today, without inventing a fake execution interface that Phase 13 would
just have to replace.

`StrategyEngine` never accounts for a fill any differently depending on
whether an `executor` is attached — closing a position always goes
through `backtest.event_backtester.simulate_fill_costs()`, the exact
same fee/slippage/PnL formula `EventBacktester` uses, so a backtested
trade and a live/paper one are never computed by two silently-diverging
implementations (`docs/08_backtesting_strategy.md`'s "backtest and live
code paths share logic", carried one layer further than the row schema
it originally referred to).

`risk_state` follows `strategy/entry.py`'s documented placeholder shape
(`{"blocked": bool, "reason": str | None}`) — `StrategyEngine` accepts
either a static dict or a zero-argument callable returning one (so a
real `RiskManager.state()` method, Phase 12, can be wired in without a
signature change here).
"""
from __future__ import annotations

import datetime as dt

from src.backtest.event_backtester import simulate_fill_costs
from src.strategy.entry import should_enter
from src.strategy.exit import should_exit

DEFAULT_POSITION_SIZE_QUOTE = 1000.0
DEFAULT_TAKER_FEE_BPS = 5
DEFAULT_ASSUMED_SLIPPAGE_BPS = 3
DEFAULT_MAX_OPEN_POSITIONS = 1  # strategy.yaml: risk.max_open_positions

ACTION_OPENED = "opened"
ACTION_CLOSED = "closed"


class StrategyEngine:
    """Stateful — tracks currently-open positions across successive
    `process_row()` calls, matching how a live tick-by-tick loop
    (`main.py`) or a streaming replay would actually call it. One
    instance per `(asset, horizon_s, threshold_bps)` question, same
    convention as `SignalEngine`/the Phase 9 models it wraps."""

    def __init__(
        self,
        signal_engine,
        *,
        risk_state=None,
        executor=None,
        max_open_positions: int = DEFAULT_MAX_OPEN_POSITIONS,
        position_size_quote: float = DEFAULT_POSITION_SIZE_QUOTE,
        taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS,
        assumed_slippage_bps: float = DEFAULT_ASSUMED_SLIPPAGE_BPS,
        entry_kwargs: "dict | None" = None,
        exit_kwargs: "dict | None" = None,
    ) -> None:
        self.signal_engine = signal_engine
        self._risk_state = risk_state if risk_state is not None else {}
        self.executor = executor
        self.max_open_positions = max_open_positions
        self.position_size_quote = position_size_quote
        self.taker_fee_bps = taker_fee_bps
        self.assumed_slippage_bps = assumed_slippage_bps
        self.entry_kwargs = entry_kwargs or {}
        self.exit_kwargs = exit_kwargs or {}
        self.open_positions: "list[dict]" = []
        self.closed_positions: "list[dict]" = []

    def risk_state(self) -> dict:
        """Resolves the constructor's `risk_state` — a static dict or a
        zero-arg callable — into the plain dict `should_enter()` expects.
        Re-resolved on every call so a callable form (e.g. a real
        `RiskManager.state`, Phase 12) is re-evaluated fresh each time,
        not cached from construction."""
        return self._risk_state() if callable(self._risk_state) else self._risk_state

    def process_row(self, row: dict, *, now: "dt.datetime | None" = None) -> "list[dict]":
        """Advances the strategy by one row/tick: first checks every
        currently-open position for an exit, then — if room remains
        under `max_open_positions` — scores `row` for a fresh signal and
        opens a position if `should_enter()` agrees. Returns the list of
        actions taken this call (`{"action": "opened"|"closed", ...}`),
        empty if nothing happened. `now` defaults to `row["timestamp"]`
        when not given — a feature row already represents "the current
        moment" for backtest/replay use; pass the real wall clock
        explicitly for live use if it can differ from the row's own
        timestamp (e.g. processing lag)."""
        now = now if now is not None else row.get("timestamp")
        actions = []

        still_open = []
        for position in self.open_positions:
            reason = should_exit(position, row, **self.exit_kwargs)
            if reason is None:
                still_open.append(position)
                continue
            actions.append(self._close_position(position, row, reason))
        self.open_positions = still_open

        if len(self.open_positions) < self.max_open_positions:
            signal = self.signal_engine.build_signal(row, now=now)
            if should_enter(signal, self.risk_state(), now=now, **self.entry_kwargs):
                actions.append(self._open_position(signal, row))

        return actions

    def _open_position(self, signal: dict, row: dict) -> dict:
        entry_price = row.get("price")
        direction = signal["signal"]
        quantity = self.position_size_quote / entry_price if entry_price else 0.0
        position = {
            "asset": signal.get("asset"),
            "side": "buy" if direction == 1 else "sell",
            "entry_price": entry_price,
            "quantity": quantity,
            "opened_at": row.get("timestamp"),
            "horizon_s": signal.get("horizon_s"),
            "threshold_bps": signal.get("threshold_bps"),
            "entry_probability": signal.get("probability_up") if direction == 1 else signal.get("probability_down"),
            "entry_confidence": signal.get("confidence"),
            "entry_signal_quality": signal.get("signal_quality"),
            # Entry-time snapshots for strategy/exit.py's reversal checks
            # — absent gracefully skips those checks, per exit.py's own
            # documented degradation, so these are opportunistic, not
            # required.
            "entry_obi": row.get("obi_10"),
            "entry_liquidity_imbalance": row.get("liquidity_imbalance"),
        }
        self.open_positions.append(position)
        if self.executor is not None:
            self.executor.place_order(asset=position["asset"], side=position["side"], quantity=quantity)
        return {"action": ACTION_OPENED, "position": position, "signal": signal}

    def _close_position(self, position: dict, row: dict, reason: str) -> dict:
        exit_price = row.get("price")
        direction = 1 if position["side"] == "buy" else -1
        costs = simulate_fill_costs(
            position["entry_price"],
            exit_price,
            direction,
            position["quantity"],
            taker_fee_bps=self.taker_fee_bps,
            assumed_slippage_bps=self.assumed_slippage_bps,
        )
        closed = {
            **position,
            "exit_price": exit_price,
            "closed_at": row.get("timestamp"),
            "exit_reason": reason,
            **costs,
        }
        self.closed_positions.append(closed)
        if self.executor is not None:
            self.executor.place_order(
                asset=position["asset"],
                side="sell" if position["side"] == "buy" else "buy",
                quantity=position["quantity"],
            )
        return {"action": ACTION_CLOSED, "position": closed}
