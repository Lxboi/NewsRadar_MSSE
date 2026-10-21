"""Phase 10 tests: backtest/event_backtester.py — deterministic synthetic
price paths (steady uptrend/downtrend, not randomly sampled) so PnL-sign
and cost assertions can't flake.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.backtest.event_backtester import (
    EventBacktester,
    decisions_from_baseline,
    decisions_from_model,
    simulate_fill_costs,
)
from src.models.direction import direction_from_probabilities

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def uptrend_rows(n=200, step=0.05, **overrides):
    price = 100.0
    rows = []
    for i in range(n):
        price += step
        row = {
            "asset": "BTC",
            "timestamp": BASE + dt.timedelta(seconds=i),
            "price": price,
            "spread": 2.0,
            "market_regime": "NORMAL_VOL",
        }
        row.update(overrides)
        rows.append(row)
    return rows


class ConfidentUpModel:
    def __init__(self, p_up=0.95):
        self.p_up = p_up

    def predict_proba(self, rows):
        return [
            {"probability_up": self.p_up, "probability_down": (1 - self.p_up) / 2, "probability_neutral": (1 - self.p_up) / 2}
            for _ in rows
        ]


class ConfidentDownModel:
    def __init__(self, p_down=0.95):
        self.p_down = p_down

    def predict_proba(self, rows):
        return [
            {"probability_down": self.p_down, "probability_up": (1 - self.p_down) / 2, "probability_neutral": (1 - self.p_down) / 2}
            for _ in rows
        ]


# -- decisions_from_model -----------------------------------------------


def test_decisions_from_model_argmax_picks_the_highest_probability_class():
    rows = uptrend_rows(3)
    decisions = decisions_from_model(ConfidentUpModel(0.9), rows, threshold_bps=50)
    assert all(d["direction"] == 1 for d in decisions)
    assert all(d["probability"] == pytest.approx(0.9) for d in decisions)


def test_decisions_from_model_neutral_direction_has_no_expected_value():
    class NeutralModel:
        def predict_proba(self, rows):
            return [{"probability_up": 0.2, "probability_down": 0.2, "probability_neutral": 0.6} for _ in rows]

    decisions = decisions_from_model(NeutralModel(), uptrend_rows(2), threshold_bps=10)
    assert all(d["direction"] == 0 for d in decisions)
    assert all(d["expected_value_bps"] is None for d in decisions)


def test_decisions_from_model_expected_value_formula():
    decisions = decisions_from_model(ConfidentUpModel(0.8), uptrend_rows(1), threshold_bps=20)
    # 0.8 * 20 - 0.2 * 20 = 12
    assert decisions[0]["expected_value_bps"] == pytest.approx(12.0)


# -- decisions_from_baseline ----------------------------------------------


def test_decisions_from_baseline_direction_matches_signal_sign():
    rows = [{"returns_30s": 0.01}, {"returns_30s": -0.01}, {"returns_30s": 0.0}]
    decisions = decisions_from_baseline(rows, "momentum", baseline_kwargs={"momentum_horizon_s": 30})
    assert [d["direction"] for d in decisions] == [1, -1, 0]
    assert all(d["probability"] is None and d["expected_value_bps"] is None for d in decisions)


# -- EventBacktester.run() -------------------------------------------------


def test_long_only_in_a_steady_uptrend_is_profitable_gross():
    rows = uptrend_rows(300, step=0.05)
    decisions = decisions_from_model(ConfidentUpModel(0.95), rows, threshold_bps=50)
    bt = EventBacktester()
    trades = bt.run(rows, decisions, horizon_s=30)
    assert len(trades) > 0
    assert all(t["gross_pnl"] > 0 for t in trades)
    assert all(t["side"] == "buy" for t in trades)


def test_short_only_in_a_steady_downtrend_is_profitable_gross():
    rows = uptrend_rows(200, step=-0.05)  # downtrend
    decisions = decisions_from_model(ConfidentDownModel(0.95), rows, threshold_bps=50)
    bt = EventBacktester()
    trades = bt.run(rows, decisions, horizon_s=20)
    assert len(trades) > 0
    assert all(t["gross_pnl"] > 0 for t in trades)
    assert all(t["side"] == "sell" for t in trades)


def test_net_pnl_is_gross_pnl_minus_fees_minus_slippage():
    rows = uptrend_rows(60, step=0.5)
    decisions = decisions_from_model(ConfidentUpModel(0.99), rows, threshold_bps=100)
    bt = EventBacktester(taker_fee_bps=5, assumed_slippage_bps=3)
    trades = bt.run(rows, decisions, horizon_s=20)
    assert len(trades) > 0
    for t in trades:
        assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["fees"] - t["slippage"])
        # fee/slippage each apply to entry+exit legs of a fixed notional
        assert t["fees"] == pytest.approx(2 * 5 / 10_000 * bt.position_size_quote)
        assert t["slippage"] == pytest.approx(2 * 3 / 10_000 * bt.position_size_quote)


def test_expected_value_below_round_trip_cost_blocks_every_entry():
    rows = uptrend_rows(100, step=0.05)
    # threshold_bps=5 with p=0.9 gives raw EV = 0.9*5-0.1*5=4bps, well
    # under the default round-trip cost (2*(5+3)=16bps).
    decisions = decisions_from_model(ConfidentUpModel(0.9), rows, threshold_bps=5)
    bt = EventBacktester()
    trades = bt.run(rows, decisions, horizon_s=20)
    assert trades == []


def test_probability_below_minimum_blocks_entry():
    rows = uptrend_rows(50, step=0.05)
    decisions = decisions_from_model(ConfidentUpModel(0.55), rows, threshold_bps=100)  # below default 0.62 min
    bt = EventBacktester()
    trades = bt.run(rows, decisions, horizon_s=20)
    assert trades == []


def test_spread_above_max_blocks_entry():
    rows = uptrend_rows(50, step=0.05, spread=1000.0)
    decisions = decisions_from_model(ConfidentUpModel(0.95), rows, threshold_bps=100)
    bt = EventBacktester()
    trades = bt.run(rows, decisions, horizon_s=20)
    assert trades == []


def test_disallowed_regime_blocks_entry():
    rows = uptrend_rows(50, step=0.05, market_regime="EXTREME_VOL")
    decisions = decisions_from_model(ConfidentUpModel(0.95), rows, threshold_bps=100)
    bt = EventBacktester()
    trades = bt.run(rows, decisions, horizon_s=20)
    assert trades == []


def test_missing_spread_or_regime_columns_do_not_block_entry():
    # A row without a spread/market_regime key at all should not be
    # treated as failing those filters — only an explicit value that
    # violates the threshold should block.
    rows = [
        {"asset": "BTC", "timestamp": BASE + dt.timedelta(seconds=i), "price": 100.0 + i * 0.1}
        for i in range(60)
    ]
    decisions = decisions_from_model(ConfidentUpModel(0.95), rows, threshold_bps=100)
    bt = EventBacktester()
    trades = bt.run(rows, decisions, horizon_s=20)
    assert len(trades) > 0


def test_max_open_positions_one_never_overlaps():
    rows = uptrend_rows(300, step=0.05)
    decisions = decisions_from_model(ConfidentUpModel(0.95), rows, threshold_bps=50)
    bt = EventBacktester(max_open_positions=1)
    trades = bt.run(rows, decisions, horizon_s=30)
    assert len(trades) > 1
    for a, b in zip(trades, trades[1:]):
        assert b["entry_index"] >= a["exit_index"]


def test_max_open_positions_two_allows_some_overlap():
    rows = uptrend_rows(300, step=0.05)
    decisions = decisions_from_model(ConfidentUpModel(0.95), rows, threshold_bps=50)
    bt_one = EventBacktester(max_open_positions=1)
    bt_two = EventBacktester(max_open_positions=2)
    trades_one = bt_one.run(rows, decisions, horizon_s=30)
    trades_two = bt_two.run(rows, decisions, horizon_s=30)
    assert len(trades_two) >= len(trades_one)


def test_no_forward_data_at_tail_produces_no_trade_for_those_rows():
    rows = uptrend_rows(20, step=0.05)
    decisions = decisions_from_model(ConfidentUpModel(0.95), rows, threshold_bps=50)
    bt = EventBacktester()
    trades = bt.run(rows, decisions, horizon_s=1000)  # horizon far exceeds the data span
    assert trades == []


def test_run_raises_on_mismatched_rows_and_decisions_length():
    rows = uptrend_rows(10)
    with pytest.raises(ValueError):
        EventBacktester().run(rows, [{"direction": 1}], horizon_s=10)


def test_empty_rows_returns_empty_trades():
    assert EventBacktester().run([], [], horizon_s=10) == []


def test_trade_dict_contains_expected_keys():
    rows = uptrend_rows(60, step=0.5)
    decisions = decisions_from_model(ConfidentUpModel(0.99), rows, threshold_bps=100)
    trades = EventBacktester().run(rows, decisions, horizon_s=20)
    assert trades
    expected_keys = {
        "asset", "side", "entry_price", "exit_price", "quantity", "opened_at", "closed_at",
        "exit_reason", "gross_pnl", "fees", "slippage", "net_pnl", "probability",
        "expected_value_bps", "horizon_s", "entry_index", "exit_index",
    }
    assert expected_keys <= set(trades[0].keys())


def test_round_trip_cost_bps_property():
    bt = EventBacktester(taker_fee_bps=5, assumed_slippage_bps=3)
    assert bt.round_trip_cost_bps == pytest.approx(16.0)


# -- Phase 11 regression: decisions_from_model() now delegates to the
# shared direction_from_probabilities() (src/models/direction.py) instead
# of its own inline argmax -- these confirm the refactor is behavior-
# preserving, matching direction_from_probabilities()'s own tie-break
# order (up > down > neutral) exactly.
# -------------------------------------------------------------------


def test_decisions_from_model_direction_matches_shared_primitive_directly():
    rows = uptrend_rows(5)
    decisions = decisions_from_model(ConfidentUpModel(0.9), rows, threshold_bps=10)
    for decision, row in zip(decisions, rows):
        proba = ConfidentUpModel(0.9).predict_proba([row])[0]
        direction, probability = direction_from_probabilities(proba)
        assert decision["direction"] == direction
        assert decision["probability"] == pytest.approx(probability)


def test_decisions_from_model_ties_break_via_class_order():
    class TiedModel:
        def predict_proba(self, rows):
            return [{"probability_up": 1 / 3, "probability_down": 1 / 3, "probability_neutral": 1 / 3} for _ in rows]

    rows = uptrend_rows(3)
    decisions = decisions_from_model(TiedModel(), rows, threshold_bps=10)
    # up(1) wins an exact three-way tie, per CLASS_ORDER.
    assert all(d["direction"] == 1 for d in decisions)


# -- simulate_fill_costs() ------------------------------------------------
# Extracted (Phase 11) out of EventBacktester._build_trade() so
# strategy/strategy_engine.py can reuse the exact same cost/PnL formula
# for live/paper fills -- tested directly here, independent of a full
# backtest run.


def test_simulate_fill_costs_long_profit():
    result = simulate_fill_costs(100.0, 101.0, 1, 10.0, taker_fee_bps=5, assumed_slippage_bps=3)
    # Both entry and exit legs are charged against the entry notional
    # (100.0 * 10.0), not re-notionalized at the exit price.
    entry_notional = 100.0 * 10.0
    assert result["gross_pnl"] == pytest.approx(10.0)
    assert result["fees"] == pytest.approx(2 * (5 / 10_000) * entry_notional)
    assert result["slippage"] == pytest.approx(2 * (3 / 10_000) * entry_notional)
    assert result["net_pnl"] == pytest.approx(result["gross_pnl"] - result["fees"] - result["slippage"])


def test_simulate_fill_costs_short_profit_when_price_falls():
    result = simulate_fill_costs(100.0, 99.0, -1, 10.0, taker_fee_bps=5, assumed_slippage_bps=3)
    assert result["gross_pnl"] == pytest.approx(10.0)
    assert result["net_pnl"] < result["gross_pnl"]


def test_simulate_fill_costs_long_loss_when_price_falls():
    result = simulate_fill_costs(100.0, 99.0, 1, 10.0, taker_fee_bps=5, assumed_slippage_bps=3)
    assert result["gross_pnl"] == pytest.approx(-10.0)


def test_simulate_fill_costs_zero_quantity_yields_zero_pnl_and_costs():
    result = simulate_fill_costs(100.0, 105.0, 1, 0.0, taker_fee_bps=5, assumed_slippage_bps=3)
    assert result["gross_pnl"] == 0.0
    assert result["fees"] == 0.0
    assert result["slippage"] == 0.0
    assert result["net_pnl"] == 0.0


def test_simulate_fill_costs_zero_costs_means_net_equals_gross():
    result = simulate_fill_costs(100.0, 102.0, 1, 5.0, taker_fee_bps=0, assumed_slippage_bps=0)
    assert result["net_pnl"] == pytest.approx(result["gross_pnl"])


def test_simulate_fill_costs_matches_backtester_trade_output():
    # EventBacktester._build_trade() now delegates to simulate_fill_costs()
    # -- confirm a real backtested trade's cost fields equal calling it
    # directly with the same entry/exit/direction/quantity.
    rows = uptrend_rows(60, step=0.5)
    decisions = decisions_from_model(ConfidentUpModel(0.99), rows, threshold_bps=100)
    trades = EventBacktester(taker_fee_bps=5, assumed_slippage_bps=3).run(rows, decisions, horizon_s=20)
    assert trades
    trade = trades[0]
    direction = 1 if trade["side"] == "buy" else -1
    expected = simulate_fill_costs(
        trade["entry_price"], trade["exit_price"], direction, trade["quantity"],
        taker_fee_bps=5, assumed_slippage_bps=3,
    )
    assert trade["net_pnl"] == pytest.approx(expected["net_pnl"])
    assert trade["fees"] == pytest.approx(expected["fees"])
    assert trade["slippage"] == pytest.approx(expected["slippage"])
