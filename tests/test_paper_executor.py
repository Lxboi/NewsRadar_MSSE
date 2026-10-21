"""Phase 13 tests: execution/paper_executor.py — realistic paper
execution (latency, spread, slippage, maker/taker, partial fills,
rejects, an append-only decision log) against a duck-typed, injected
price_lookup.
"""
from __future__ import annotations

import pytest

from src.execution.paper_executor import (
    REJECT_BELOW_MIN_NOTIONAL,
    REJECT_NO_PRICE_AVAILABLE,
    REJECT_SIMULATED,
    STATUS_FILLED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    STATUS_REJECTED,
    PaperExecutor,
)


def make_price_lookup(prices):
    def lookup(asset):
        return prices.get(asset)
    return lookup


def flat_quote(price=50_000.0, spread_bps=0.0):
    return {"price": price, "spread_bps": spread_bps}


def test_market_buy_pays_a_premium_over_mid_with_a_spread():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0, spread_bps=4.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0)
    result = ex.place_order(asset="BTC", side="buy", quantity=1.0)
    assert result["status"] == STATUS_FILLED
    assert result["avg_fill_price"] == pytest.approx(50_000.0 + 50_000.0 * 0.0004 / 2)


def test_market_sell_receives_a_discount_under_mid_with_a_spread():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0, spread_bps=4.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0)
    result = ex.place_order(asset="BTC", side="sell", quantity=1.0)
    assert result["avg_fill_price"] == pytest.approx(50_000.0 - 50_000.0 * 0.0004 / 2)


def test_zero_spread_fills_exactly_at_mid():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0, spread_bps=0.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0)
    result = ex.place_order(asset="BTC", side="buy", quantity=1.0)
    assert result["avg_fill_price"] == pytest.approx(50_000.0)


def test_market_order_fee_matches_taker_bps_against_notional():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0, spread_bps=0.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0, taker_fee_bps=10)
    result = ex.place_order(asset="BTC", side="buy", quantity=2.0)
    expected_fee = 10 / 10_000 * result["avg_fill_price"] * result["filled_quantity"]
    assert result["fees"] == pytest.approx(expected_fee)


def test_slippage_bps_is_within_the_configured_range_for_a_market_order():
    lookup = make_price_lookup({"BTC": flat_quote()})
    ex = PaperExecutor(price_lookup=lookup, random_state=7, assumed_slippage_bps=5)
    for _ in range(20):
        result = ex.place_order(asset="BTC", side="buy", quantity=1.0)
        assert 0.0 <= result["slippage_bps"] <= 10.0


def test_latency_ms_is_within_the_configured_range():
    lookup = make_price_lookup({"BTC": flat_quote()})
    ex = PaperExecutor(price_lookup=lookup, random_state=3, latency_ms_range=(20.0, 40.0))
    for _ in range(20):
        result = ex.place_order(asset="BTC", side="buy", quantity=1.0)
        assert 20.0 <= result["latency_ms"] <= 40.0


def test_no_price_available_rejects_without_touching_cash():
    ex = PaperExecutor(price_lookup=make_price_lookup({}), random_state=0)
    result = ex.place_order(asset="BTC", side="buy", quantity=1.0)
    assert result["status"] == STATUS_REJECTED
    assert result["rejected_reason"] == REJECT_NO_PRICE_AVAILABLE
    assert result["fees"] == 0.0


def test_below_min_notional_rejects():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0, min_notional_usd=1000.0)
    result = ex.place_order(asset="BTC", side="buy", quantity=0.00001)
    assert result["status"] == STATUS_REJECTED
    assert result["rejected_reason"] == REJECT_BELOW_MIN_NOTIONAL


def test_per_asset_min_notional_override():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0)})
    ex = PaperExecutor(
        price_lookup=lookup, random_state=0, min_notional_usd=10.0,
        min_notional_usd_by_asset={"BTC": 100_000.0},
    )
    result = ex.place_order(asset="BTC", side="buy", quantity=0.1)  # notional=5000 < 100_000 override
    assert result["status"] == STATUS_REJECTED
    assert result["rejected_reason"] == REJECT_BELOW_MIN_NOTIONAL


def test_reject_probability_one_always_rejects_with_simulated_reason():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0, reject_probability=1.0)
    result = ex.place_order(asset="BTC", side="buy", quantity=1.0)
    assert result["status"] == STATUS_REJECTED
    assert result["rejected_reason"] == REJECT_SIMULATED


def test_reject_probability_zero_never_rejects_for_this_reason():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0, reject_probability=0.0)
    for _ in range(20):
        result = ex.place_order(asset="BTC", side="buy", quantity=1.0)
        assert result["rejected_reason"] != REJECT_SIMULATED


def test_partial_fill_probability_one_always_partially_fills():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=5, partial_fill_probability=1.0, min_partial_fill_fraction=0.5)
    result = ex.place_order(asset="BTC", side="buy", quantity=1.0)
    assert result["status"] == STATUS_PARTIAL
    assert 0.5 <= result["filled_quantity"] < 1.0


def test_partial_fill_probability_zero_never_partially_fills():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=5, partial_fill_probability=0.0)
    for _ in range(20):
        result = ex.place_order(asset="BTC", side="buy", quantity=1.0)
        assert result["status"] == STATUS_FILLED
        assert result["filled_quantity"] == 1.0


def test_limit_buy_below_market_stays_pending():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0)
    result = ex.place_order(asset="BTC", side="buy", quantity=1.0, order_type="limit", price=40_000.0)
    assert result["status"] == STATUS_PENDING
    assert result["fees"] == 0.0
    assert len(ex.get_open_orders()) == 1


def test_limit_buy_at_or_above_market_fills_immediately_as_maker():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0, maker_fee_bps=1, taker_fee_bps=10)
    result = ex.place_order(asset="BTC", side="buy", quantity=1.0, order_type="limit", price=51_000.0)
    assert result["status"] == STATUS_FILLED
    assert result["avg_fill_price"] == pytest.approx(50_000.0)  # better-or-equal price, not the limit itself
    expected_fee = 1 / 10_000 * 50_000.0 * 1.0
    assert result["fees"] == pytest.approx(expected_fee)
    assert result["slippage_bps"] == 0.0  # maker fills are not subject to simulated slippage


def test_limit_sell_at_or_below_market_fills_immediately():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0)
    result = ex.place_order(asset="BTC", side="sell", quantity=1.0, order_type="limit", price=49_000.0)
    assert result["status"] == STATUS_FILLED
    assert result["avg_fill_price"] == pytest.approx(50_000.0)


def test_cancel_order_removes_a_pending_order():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0)
    pending = ex.place_order(asset="BTC", side="buy", quantity=1.0, order_type="limit", price=40_000.0)
    cancelled = ex.cancel_order(pending["order_id"])
    assert cancelled["status"] == "cancelled"
    assert ex.get_open_orders() == []


def test_cancel_unknown_order_id_fails_gracefully():
    ex = PaperExecutor(price_lookup=make_price_lookup({}), random_state=0)
    result = ex.cancel_order(9999)
    assert result["status"] == "cancel_failed"


def test_get_open_orders_filters_by_asset():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0), "ETH": flat_quote(3_000.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0)
    ex.place_order(asset="BTC", side="buy", quantity=1.0, order_type="limit", price=40_000.0)
    ex.place_order(asset="ETH", side="buy", quantity=1.0, order_type="limit", price=2_000.0)
    assert len(ex.get_open_orders()) == 2
    assert len(ex.get_open_orders(asset="BTC")) == 1


def test_try_fill_pending_fills_once_the_market_moves():
    prices = {"BTC": flat_quote(50_000.0)}
    lookup = make_price_lookup(prices)
    ex = PaperExecutor(price_lookup=lookup, random_state=0)
    pending = ex.place_order(asset="BTC", side="buy", quantity=1.0, order_type="limit", price=40_000.0)
    assert pending["status"] == STATUS_PENDING

    prices["BTC"] = flat_quote(39_000.0)  # market drops through the limit price
    filled = ex.try_fill_pending("BTC")
    assert len(filled) == 1
    assert filled[0]["status"] == STATUS_FILLED
    assert ex.get_open_orders() == []


def test_try_fill_pending_returns_empty_when_nothing_crosses():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0)
    ex.place_order(asset="BTC", side="buy", quantity=1.0, order_type="limit", price=40_000.0)
    filled = ex.try_fill_pending("BTC")
    assert filled == []


def test_opening_a_position_sets_avg_price_to_fill_price():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0)
    ex.place_order(asset="BTC", side="buy", quantity=1.0)
    position = ex.get_position("BTC")
    assert position["quantity"] == pytest.approx(1.0)
    assert position["avg_price"] == pytest.approx(50_000.0)


def test_adding_to_a_position_updates_volume_weighted_average_price():
    prices = {"BTC": flat_quote(50_000.0)}
    lookup = make_price_lookup(prices)
    ex = PaperExecutor(price_lookup=lookup, random_state=0)
    ex.place_order(asset="BTC", side="buy", quantity=1.0)  # avg=50_000
    prices["BTC"] = flat_quote(60_000.0)
    ex.place_order(asset="BTC", side="buy", quantity=1.0)  # avg should become 55_000
    position = ex.get_position("BTC")
    assert position["quantity"] == pytest.approx(2.0)
    assert position["avg_price"] == pytest.approx(55_000.0)


def test_reducing_a_position_realizes_pnl_into_cash():
    prices = {"BTC": flat_quote(50_000.0)}
    lookup = make_price_lookup(prices)
    ex = PaperExecutor(price_lookup=lookup, random_state=0, initial_cash=100_000.0, taker_fee_bps=0)
    ex.place_order(asset="BTC", side="buy", quantity=1.0)
    cash_before_close = ex.cash
    prices["BTC"] = flat_quote(55_000.0)
    ex.place_order(asset="BTC", side="sell", quantity=1.0)
    # Realized PnL = (55_000 - 50_000) * 1.0 = 5_000 (fees are 0 here).
    assert ex.cash == pytest.approx(cash_before_close + 5_000.0)
    assert ex.get_position("BTC")["quantity"] == pytest.approx(0.0)


def test_flipping_through_flat_starts_a_fresh_avg_price_on_the_opposite_side():
    prices = {"BTC": flat_quote(50_000.0)}
    lookup = make_price_lookup(prices)
    ex = PaperExecutor(price_lookup=lookup, random_state=0, taker_fee_bps=0)
    ex.place_order(asset="BTC", side="buy", quantity=1.0)  # long 1.0 @ 50_000
    prices["BTC"] = flat_quote(60_000.0)
    ex.place_order(asset="BTC", side="sell", quantity=2.5)  # closes the long, opens a 1.5 short
    position = ex.get_position("BTC")
    assert position["quantity"] == pytest.approx(-1.5)
    assert position["avg_price"] == pytest.approx(60_000.0)


def test_balance_cash_decreases_by_fees_on_every_fill():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0, initial_cash=100_000.0, taker_fee_bps=10)
    ex.place_order(asset="BTC", side="buy", quantity=1.0)
    assert ex.get_balance()["cash"] < 100_000.0


def test_balance_unrealized_pnl_reflects_current_price():
    prices = {"BTC": flat_quote(50_000.0)}
    lookup = make_price_lookup(prices)
    ex = PaperExecutor(price_lookup=lookup, random_state=0, taker_fee_bps=0)
    ex.place_order(asset="BTC", side="buy", quantity=1.0)
    prices["BTC"] = flat_quote(51_000.0)
    balance = ex.get_balance()
    assert balance["unrealized_pnl"] == pytest.approx(1_000.0)


def test_balance_with_no_price_lookup_degrades_to_zero_unrealized():
    ex = PaperExecutor(price_lookup=None, random_state=0)
    balance = ex.get_balance()
    assert balance["unrealized_pnl"] == 0.0


def test_every_decision_is_logged():
    lookup = make_price_lookup({"BTC": flat_quote(50_000.0)})
    ex = PaperExecutor(price_lookup=lookup, random_state=0)
    ex.place_order(asset="BTC", side="buy", quantity=1.0)
    ex.place_order(asset="BTC", side="buy", quantity=0.00000001)  # rejected
    pending = ex.place_order(asset="BTC", side="buy", quantity=1.0, order_type="limit", price=1.0)
    ex.cancel_order(pending["order_id"])
    assert len(ex.order_log) == 4
    statuses = [entry["status"] for entry in ex.order_log]
    assert STATUS_FILLED in statuses
    assert STATUS_REJECTED in statuses
    assert STATUS_PENDING in statuses
    assert "cancelled" in statuses
