"""Phase 13 tests: execution/execution_interface.py — the exchange-
agnostic ABC every executor (PaperExecutor, and Phase 14's
KrakenExecutor) must implement in full to be instantiable at all.
"""
from __future__ import annotations

import pytest

from src.execution.execution_interface import ExecutionInterface


def test_cannot_instantiate_the_interface_directly():
    with pytest.raises(TypeError):
        ExecutionInterface()


def test_a_subclass_missing_a_method_cannot_be_instantiated():
    class Incomplete(ExecutionInterface):
        def place_order(self, *, asset, side, quantity, order_type="market", price=None):
            return {}
        def cancel_order(self, order_id):
            return {}
        def get_position(self, asset):
            return None
        def get_balance(self):
            return {}
        # get_open_orders deliberately omitted

    with pytest.raises(TypeError):
        Incomplete()


def test_a_complete_subclass_instantiates_fine():
    class Complete(ExecutionInterface):
        def place_order(self, *, asset, side, quantity, order_type="market", price=None):
            return {"order_id": 1, "status": "filled", "asset": asset, "side": side, "quantity": quantity}
        def cancel_order(self, order_id):
            return {"order_id": order_id, "status": "cancelled"}
        def get_position(self, asset):
            return None
        def get_balance(self):
            return {"cash": 0.0}
        def get_open_orders(self, asset=None):
            return []

    instance = Complete()
    result = instance.place_order(asset="BTC", side="buy", quantity=1.0)
    assert result["status"] == "filled"
