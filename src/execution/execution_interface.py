"""Exchange-agnostic execution interface: place_order, cancel_order,
get_position, get_balance, get_open_orders (section 41). The signal
engine and strategy engine only ever depend on this interface.

Roadmap: PHASE 13. An `abc.ABC` rather than a plain duck-typed contract
(unlike `strategy/strategy_engine.py`'s optional `executor`, which
deliberately stays duck-typed since it's *optional* — no executor at
all is a valid, fully-supported mode) — this interface is the thing
`PaperExecutor` (this phase) and `KrakenExecutor` (Phase 14) both
implement, so making it structurally impossible to instantiate a
subclass missing one of the five methods is worth the small extra
ceremony (the same "structural, not conventional" enforcement
`backtest/orderbook_backtester.py`/`notifications/alert_manager.py`
already use elsewhere in this codebase).

`place_order`'s keyword-only signature (`asset`, `side`, `quantity`,
plus optional `order_type`/`price`) matches exactly what
`strategy/strategy_engine.py: StrategyEngine._open_position()`/
`_close_position()` (Phase 11) already call with
(`executor.place_order(asset=, side=, quantity=)`) — those three kwargs
alone are a valid call against any real implementation, since
`order_type`/`price` default to a plain market order.

## A known integration gap, flagged rather than silently resolved

`StrategyEngine` calls `executor.place_order(...)` but never reads its
return value — it always uses its own `simulate_fill_costs()`-based
price/PnL math (the same shared formula `EventBacktester` uses), even
when a real executor is wired in. This was a deliberate Phase 11 choice
(the docstring there says the executor is called "additionally," not
"instead of" the simulated math) and is *not* changed here: a
`PaperExecutor`'s realistically-simulated fill price/rejection is fully
computed and logged by this phase, but `StrategyEngine` doesn't yet
consume it. Reconciling that — deciding whether a strategy's own PnL
tracking should defer to a real executor's actual fill, or stay on the
shared simulated formula for backtest/live comparability — is left as a
follow-up integration step for whoever wires `main.py`'s live loop, not
guessed at unilaterally here by changing already-delivered, already-
tested Phase 11 code.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class ExecutionInterface(ABC):
    """See module docstring. Every method is keyword-only where it takes
    more than one argument, matching the rest of this codebase's
    convention (`should_enter`, `should_exit`, `SignalEngine.build_signal`,
    ...)."""

    @abstractmethod
    def place_order(
        self,
        *,
        asset: str,
        side: str,
        quantity: float,
        order_type: str = "market",
        price: "float | None" = None,
    ) -> dict:
        """Returns a fill-result dict. Shape is implementation-defined
        (a live exchange and a paper simulator report different fields),
        but every implementation includes at least `order_id`, `status`
        (`"filled"`, `"partial"`, `"pending"`, or `"rejected"`), and
        `asset`/`side`/`quantity` echoing the request."""
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id) -> dict:
        raise NotImplementedError

    @abstractmethod
    def get_position(self, asset: str) -> "dict | None":
        raise NotImplementedError

    @abstractmethod
    def get_balance(self) -> dict:
        raise NotImplementedError

    @abstractmethod
    def get_open_orders(self, asset: "str | None" = None) -> "list[dict]":
        raise NotImplementedError
