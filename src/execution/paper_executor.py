"""Realistic paper execution: simulated latency, spread, slippage,
maker/taker, partial fills, rejects, delayed execution. Every
decision is logged (section 43).

Roadmap: PHASE 13. Where `backtest/event_backtester.py`'s
`simulate_fill_costs()` (Phase 10-11) is a deliberately simple, fixed-bps
cost assumption shared for backtest/live PnL comparability,
`PaperExecutor` exists specifically to stress-test that assumption: fees
and slippage here are drawn with real variance (not a constant), latency
is sampled from a configurable range, and orders can be rejected or
partially filled — the gap between "what the fixed-cost model assumed"
and "what a realistic simulator produced" is the whole point of this
phase existing before Phase 14 risks real capital on Kraken.

## Price source: an injected `price_lookup`, not a global

`PaperExecutor` has no live market-data connection of its own — realistic
spread/slippage simulation needs a *current* two-sided quote for the
asset being traded, supplied via a duck-typed `price_lookup(asset) ->
{"price": float, "spread_bps": float}` callable (the same "accept a
narrow, duck-typed dependency" convention `StrategyEngine`'s `executor`
and `SignalEngine`'s `model` already use). Wiring it to the live
`OrderBookEngine`'s current best bid/ask (or a replayed row's own
`price`/`spread_bps` columns for offline paper-testing) is a `main.py`
integration concern, not this module's.

## Two independent kinds of "not filled"

A **rejected** order never executes at all (simulates an exchange-side
rejection — bad symbol, connectivity blip, risk check on the exchange's
own side) and carries no fee. A **pending** order is a limit order whose
price doesn't currently cross the market — it sits in `get_open_orders()`
until `cancel_order()`'s explicit cancellation, or a later
`try_fill_pending(asset, price_lookup=...)` call re-checks it against a
fresh quote. This is deliberately not a full matching engine (no
depth-aware partial crossing, no time-priority queue) — section 43 asks
for realistic *cost* simulation, not a second order book engine; that
would duplicate `orderbook/orderbook_engine.py`'s job for no benefit a
paper account needs.

## Position/balance ledger — deliberately a different model than `StrategyEngine`'s

`PaperExecutor` tracks its own running-inventory account state (net
quantity and volume-weighted average cost per asset, realized/unrealized
PnL, cash) across every fill it processes — independent of, and not a
replacement for, `StrategyEngine`'s own one-entry-one-exit trade-level
tracking (Phase 11). They serve different questions: this ledger answers
"what does the paper account actually hold and owe fees on right now,"
the same way a real exchange account would; `StrategyEngine`'s
`open_positions`/`closed_positions` answer "what trade did the strategy
attribute its PnL to." The two are expected to diverge in bookkeeping
style, not reconciled into one model here.
"""
from __future__ import annotations

import datetime as dt

import numpy as np

from src.execution.execution_interface import ExecutionInterface

DEFAULT_TAKER_FEE_BPS = 5              # strategy.yaml: costs.taker_fee_bps
DEFAULT_MAKER_FEE_BPS = 2              # strategy.yaml: costs.maker_fee_bps
DEFAULT_ASSUMED_SLIPPAGE_BPS = 3       # strategy.yaml: costs.assumed_slippage_bps
DEFAULT_LATENCY_MS_RANGE = (50.0, 150.0)
DEFAULT_REJECT_PROBABILITY = 0.0
DEFAULT_PARTIAL_FILL_PROBABILITY = 0.0
DEFAULT_MIN_PARTIAL_FILL_FRACTION = 0.5
DEFAULT_MIN_NOTIONAL_USD = 10.0        # assets.yaml: min_notional_usd (BTC/ETH/SOL all use 10 today)
DEFAULT_INITIAL_CASH = 10_000.0

STATUS_FILLED = "filled"
STATUS_PARTIAL = "partial"
STATUS_PENDING = "pending"
STATUS_REJECTED = "rejected"

REJECT_BELOW_MIN_NOTIONAL = "below_min_notional"
REJECT_NO_PRICE_AVAILABLE = "no_price_available"
REJECT_SIMULATED = "simulated_exchange_reject"


class PaperExecutor(ExecutionInterface):
    def __init__(
        self,
        *,
        price_lookup=None,
        initial_cash: float = DEFAULT_INITIAL_CASH,
        taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS,
        maker_fee_bps: float = DEFAULT_MAKER_FEE_BPS,
        assumed_slippage_bps: float = DEFAULT_ASSUMED_SLIPPAGE_BPS,
        latency_ms_range: "tuple[float, float]" = DEFAULT_LATENCY_MS_RANGE,
        reject_probability: float = DEFAULT_REJECT_PROBABILITY,
        partial_fill_probability: float = DEFAULT_PARTIAL_FILL_PROBABILITY,
        min_partial_fill_fraction: float = DEFAULT_MIN_PARTIAL_FILL_FRACTION,
        min_notional_usd: float = DEFAULT_MIN_NOTIONAL_USD,
        min_notional_usd_by_asset: "dict | None" = None,
        random_state: "int | np.random.RandomState | None" = None,
    ) -> None:
        self.price_lookup = price_lookup
        self.taker_fee_bps = taker_fee_bps
        self.maker_fee_bps = maker_fee_bps
        self.assumed_slippage_bps = assumed_slippage_bps
        self.latency_ms_range = latency_ms_range
        self.reject_probability = reject_probability
        self.partial_fill_probability = partial_fill_probability
        self.min_partial_fill_fraction = min_partial_fill_fraction
        self.min_notional_usd = min_notional_usd
        self.min_notional_usd_by_asset = min_notional_usd_by_asset or {}
        self._rng = random_state if isinstance(random_state, np.random.RandomState) else np.random.RandomState(random_state)

        self.cash = initial_cash
        self._next_id = 1
        self._positions: "dict[str, dict]" = {}   # asset -> {"quantity", "avg_price"}
        self._open_orders: "dict[int, dict]" = {}  # order_id -> order dict (pending limit orders)
        self.order_log: "list[dict]" = []          # append-only, every decision (section 43: "every decision is logged")

    # -- ExecutionInterface -------------------------------------------------

    def place_order(
        self,
        *,
        asset: str,
        side: str,
        quantity: float,
        order_type: str = "market",
        price: "float | None" = None,
    ) -> dict:
        order_id = self._next_id
        self._next_id += 1
        latency_ms = float(self._rng.uniform(*self.latency_ms_range))

        quote = self.price_lookup(asset) if self.price_lookup is not None else None
        if not quote or quote.get("price") is None:
            return self._log(self._reject(order_id, asset, side, quantity, order_type, latency_ms, REJECT_NO_PRICE_AVAILABLE))

        mid_price = quote["price"]
        spread_bps = quote.get("spread_bps", 0.0) or 0.0

        if order_type == "limit":
            fill_price = self._limit_crossing_price(side, price, mid_price)
            if fill_price is None:
                order = {
                    "order_id": order_id, "asset": asset, "side": side, "order_type": order_type,
                    "requested_quantity": quantity, "limit_price": price,
                }
                self._open_orders[order_id] = order
                return self._log({**order, "status": STATUS_PENDING, "filled_quantity": 0.0,
                                   "avg_fill_price": None, "fees": 0.0, "slippage_bps": 0.0,
                                   "latency_ms": latency_ms, "rejected_reason": None})
            is_taker = False
        else:
            fill_price = self._market_fill_price(side, mid_price, spread_bps)
            is_taker = True

        notional = fill_price * quantity
        min_notional = self.min_notional_usd_by_asset.get(asset, self.min_notional_usd)
        if notional < min_notional:
            return self._log(self._reject(order_id, asset, side, quantity, order_type, latency_ms, REJECT_BELOW_MIN_NOTIONAL))

        if self._rng.uniform(0.0, 1.0) < self.reject_probability:
            return self._log(self._reject(order_id, asset, side, quantity, order_type, latency_ms, REJECT_SIMULATED))

        filled_quantity = quantity
        status = STATUS_FILLED
        if self._rng.uniform(0.0, 1.0) < self.partial_fill_probability:
            fraction = float(self._rng.uniform(self.min_partial_fill_fraction, 1.0))
            filled_quantity = quantity * fraction
            status = STATUS_PARTIAL

        fee_bps = self.taker_fee_bps if is_taker else self.maker_fee_bps
        fees = fee_bps / 10_000 * fill_price * filled_quantity
        slippage_bps = float(self._rng.uniform(0.0, 2 * self.assumed_slippage_bps)) if is_taker else 0.0

        self._apply_fill(asset=asset, side=side, quantity=filled_quantity, fill_price=fill_price, fees=fees)

        result = {
            "order_id": order_id, "asset": asset, "side": side, "order_type": order_type,
            "requested_quantity": quantity, "status": status, "filled_quantity": filled_quantity,
            "avg_fill_price": fill_price, "fees": fees, "slippage_bps": slippage_bps,
            "latency_ms": latency_ms, "rejected_reason": None,
        }
        return self._log(result)

    def cancel_order(self, order_id) -> dict:
        order = self._open_orders.pop(order_id, None)
        if order is None:
            return self._log({"order_id": order_id, "status": "cancel_failed", "reason": "unknown_or_already_resolved_order_id"})
        return self._log({**order, "status": "cancelled"})

    def try_fill_pending(self, asset: str, *, price_lookup=None) -> "list[dict]":
        """Re-checks every pending limit order for `asset` against a
        fresh quote (from `price_lookup` if given, else the executor's
        own configured `price_lookup`) — the deliberately simple stand-in
        for "the market moved and a resting limit order now crosses,"
        per the module docstring's "not a full matching engine" scope.
        Returns the list of fill results for orders that filled this
        call (empty if none did)."""
        lookup = price_lookup or self.price_lookup
        if lookup is None:
            return []
        quote = lookup(asset)
        if not quote or quote.get("price") is None:
            return []
        mid_price = quote["price"]

        filled = []
        for order_id, order in list(self._open_orders.items()):
            if order["asset"] != asset:
                continue
            fill_price = self._limit_crossing_price(order["side"], order["limit_price"], mid_price)
            if fill_price is None:
                continue
            del self._open_orders[order_id]
            fees = self.maker_fee_bps / 10_000 * fill_price * order["requested_quantity"]
            self._apply_fill(asset=asset, side=order["side"], quantity=order["requested_quantity"], fill_price=fill_price, fees=fees)
            result = {
                "order_id": order_id, "asset": asset, "side": order["side"], "order_type": "limit",
                "requested_quantity": order["requested_quantity"], "status": STATUS_FILLED,
                "filled_quantity": order["requested_quantity"], "avg_fill_price": fill_price,
                "fees": fees, "slippage_bps": 0.0, "latency_ms": 0.0, "rejected_reason": None,
            }
            filled.append(self._log(result))
        return filled

    def get_position(self, asset: str) -> "dict | None":
        return self._positions.get(asset)

    def get_balance(self) -> dict:
        unrealized_pnl = 0.0
        for asset, position in self._positions.items():
            if self.price_lookup is None:
                continue
            quote = self.price_lookup(asset)
            if not quote or quote.get("price") is None:
                continue
            unrealized_pnl += (quote["price"] - position["avg_price"]) * position["quantity"]
        return {"cash": self.cash, "unrealized_pnl": unrealized_pnl, "equity": self.cash + unrealized_pnl}

    def get_open_orders(self, asset: "str | None" = None) -> "list[dict]":
        orders = list(self._open_orders.values())
        if asset is not None:
            orders = [o for o in orders if o["asset"] == asset]
        return orders

    # -- internals ------------------------------------------------------

    def _market_fill_price(self, side: str, mid_price: float, spread_bps: float) -> float:
        half_spread = mid_price * (spread_bps / 10_000) / 2
        return mid_price + half_spread if side == "buy" else mid_price - half_spread

    def _limit_crossing_price(self, side: str, limit_price: "float | None", mid_price: float) -> "float | None":
        if limit_price is None:
            return None
        if side == "buy" and limit_price >= mid_price:
            return min(limit_price, mid_price)
        if side == "sell" and limit_price <= mid_price:
            return max(limit_price, mid_price)
        return None

    def _apply_fill(self, *, asset: str, side: str, quantity: float, fill_price: float, fees: float) -> None:
        direction = 1 if side == "buy" else -1
        signed_quantity = direction * quantity
        self.cash -= fees

        position = self._positions.get(asset, {"quantity": 0.0, "avg_price": 0.0})
        existing_qty = position["quantity"]

        if existing_qty == 0 or (existing_qty > 0) == (signed_quantity > 0):
            # Opening or adding in the same direction: extend the
            # volume-weighted average cost basis, no PnL realized yet.
            new_qty = existing_qty + signed_quantity
            if new_qty != 0:
                position["avg_price"] = (position["avg_price"] * existing_qty + fill_price * signed_quantity) / new_qty
            position["quantity"] = new_qty
        else:
            # Reducing or flipping: realize PnL on the portion closed.
            closing_qty = min(abs(signed_quantity), abs(existing_qty))
            realized = (fill_price - position["avg_price"]) * closing_qty * (1 if existing_qty > 0 else -1)
            self.cash += realized

            remaining_signed = existing_qty + signed_quantity
            if abs(signed_quantity) > abs(existing_qty):
                # Flipped through flat into the opposite direction.
                position = {"quantity": remaining_signed, "avg_price": fill_price}
            else:
                position["quantity"] = remaining_signed
                if remaining_signed == 0:
                    position["avg_price"] = 0.0

        self._positions[asset] = position

    def _reject(self, order_id, asset, side, quantity, order_type, latency_ms, reason) -> dict:
        return {
            "order_id": order_id, "asset": asset, "side": side, "order_type": order_type,
            "requested_quantity": quantity, "status": STATUS_REJECTED, "filled_quantity": 0.0,
            "avg_fill_price": None, "fees": 0.0, "slippage_bps": 0.0,
            "latency_ms": latency_ms, "rejected_reason": reason,
        }

    def _log(self, decision: dict) -> dict:
        self.order_log.append(decision)
        return decision
