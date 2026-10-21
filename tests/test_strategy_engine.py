"""Phase 11 tests: strategy/strategy_engine.py — stateful orchestrator
tying signals/signal_engine.py + strategy/entry.py + strategy/exit.py
together, reusing backtest/event_backtester.py: simulate_fill_costs() so
backtested and live/paper PnL accounting never silently diverge.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.backtest.event_backtester import simulate_fill_costs
from src.signals.signal_engine import SignalEngine
from src.strategy.strategy_engine import ACTION_CLOSED, ACTION_OPENED, StrategyEngine

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


class ScriptedModel:
    """Returns a pre-scripted sequence of probability dicts, one per
    predict_proba() call (batched call -> as many dicts as rows)."""

    def __init__(self, script):
        self._script = list(script)

    def predict_proba(self, rows):
        out = []
        for _ in rows:
            out.append(self._script.pop(0))
        return out


class FakeExecutor:
    def __init__(self):
        self.orders = []

    def place_order(self, *, asset, side, quantity):
        self.orders.append({"asset": asset, "side": side, "quantity": quantity})


UP = {"probability_up": 0.90, "probability_down": 0.05, "probability_neutral": 0.05}
DOWN = {"probability_up": 0.05, "probability_down": 0.90, "probability_neutral": 0.05}
NEUTRAL = {"probability_up": 0.30, "probability_down": 0.30, "probability_neutral": 0.40}


def make_row(price, seconds_offset=0, **overrides):
    row = {
        "asset": "BTC/USD",
        "timestamp": BASE + dt.timedelta(seconds=seconds_offset),
        "price": price,
        "market_regime": "NORMAL_VOL",
        "spread": 1.0,
        "obi_10": 0.5,
        "liquidity_imbalance": 0.5,
        "volatility": 0.001,
    }
    row.update(overrides)
    return row


def make_engine(script, **engine_kwargs):
    model = ScriptedModel(script)
    signal_engine = SignalEngine(model, asset="BTC/USD", horizon_s=60, threshold_bps=25.0, model_version="test-v0")
    defaults = dict(
        entry_kwargs={"probability_min": 0.6, "min_expected_value": None, "max_data_age_s": None},
        exit_kwargs={"take_profit_bps": 15.0, "stop_loss_bps": 50.0, "time_stop_s": 3600},
    )
    defaults.update(engine_kwargs)
    return StrategyEngine(signal_engine, **defaults)


def test_opens_a_position_on_a_qualifying_signal():
    engine = make_engine([UP])
    actions = engine.process_row(make_row(100.0))
    assert [a["action"] for a in actions] == [ACTION_OPENED]
    assert len(engine.open_positions) == 1
    assert engine.open_positions[0]["side"] == "buy"


def test_no_position_opened_on_neutral_signal():
    engine = make_engine([NEUTRAL])
    actions = engine.process_row(make_row(100.0))
    assert actions == []
    assert engine.open_positions == []


def test_position_closes_when_take_profit_hit():
    # Second script entry is NEUTRAL, not UP: once the first position
    # closes mid-tick, room reopens and process_row evaluates a fresh
    # entry on the SAME row before returning -- a NEUTRAL second score
    # keeps this test isolated to "does closing work", not "does the
    # same-tick reopen also work" (that's covered separately below).
    engine = make_engine([UP, NEUTRAL])
    engine.process_row(make_row(100.0, seconds_offset=0))
    actions = engine.process_row(make_row(100.20, seconds_offset=5))  # +20bps >= 15bps TP
    closed = [a for a in actions if a["action"] == ACTION_CLOSED]
    assert len(closed) == 1
    assert closed[0]["position"]["exit_reason"] == "fixed_tp"
    assert engine.open_positions == []
    assert len(engine.closed_positions) == 1


def test_max_open_positions_respected_across_ticks():
    # Two UP signals in a row, both well within TP/SL bounds so neither
    # closes -- with max_open_positions=1, only the first should open.
    engine = make_engine([UP, UP], max_open_positions=1)
    engine.process_row(make_row(100.0, seconds_offset=0))
    actions = engine.process_row(make_row(100.02, seconds_offset=5))
    assert actions == []
    assert len(engine.open_positions) == 1


def test_room_for_new_position_after_a_close_same_tick():
    # fixed_sl set very tight so the very next tick both closes the first
    # position AND (since room reopened) evaluates entry for a fresh one.
    engine = make_engine([UP, DOWN], max_open_positions=1, exit_kwargs={"stop_loss_bps": 1.0, "take_profit_bps": 1000.0})
    engine.process_row(make_row(100.0, seconds_offset=0))
    actions = engine.process_row(make_row(99.90, seconds_offset=5))  # -10bps triggers fixed_sl
    kinds = [a["action"] for a in actions]
    assert ACTION_CLOSED in kinds
    assert ACTION_OPENED in kinds
    assert len(engine.open_positions) == 1
    assert engine.open_positions[0]["side"] == "sell"


def test_closed_position_pnl_matches_simulate_fill_costs_directly():
    engine = make_engine([UP, UP], taker_fee_bps=5, assumed_slippage_bps=3)
    engine.process_row(make_row(100.0, seconds_offset=0))
    actions = engine.process_row(make_row(100.20, seconds_offset=5))
    closed_position = [a for a in actions if a["action"] == ACTION_CLOSED][0]["position"]
    expected = simulate_fill_costs(
        100.0, 100.20, 1, closed_position["quantity"], taker_fee_bps=5, assumed_slippage_bps=3
    )
    assert closed_position["net_pnl"] == pytest.approx(expected["net_pnl"])
    assert closed_position["fees"] == pytest.approx(expected["fees"])
    assert closed_position["slippage"] == pytest.approx(expected["slippage"])


def test_blocked_risk_state_prevents_new_entries():
    engine = make_engine([UP], risk_state={"blocked": True, "reason": "daily loss limit"})
    actions = engine.process_row(make_row(100.0))
    assert actions == []
    assert engine.open_positions == []


def test_callable_risk_state_is_re_resolved_on_every_call():
    calls = {"n": 0}

    def risk_state():
        calls["n"] += 1
        # Blocked on the first call only.
        return {"blocked": calls["n"] == 1}

    engine = make_engine([UP, UP], risk_state=risk_state)
    first = engine.process_row(make_row(100.0, seconds_offset=0))
    second = engine.process_row(make_row(100.01, seconds_offset=5))
    assert first == []  # blocked
    assert [a["action"] for a in second] == [ACTION_OPENED]  # unblocked on 2nd resolve
    assert calls["n"] == 2


def test_executor_is_called_on_open_and_close_when_provided():
    # Second script entry is NEUTRAL so the same-tick reopen after closing
    # doesn't add a third order -- see comment on the take-profit test.
    executor = FakeExecutor()
    engine = make_engine([UP, NEUTRAL], executor=executor)
    engine.process_row(make_row(100.0, seconds_offset=0))
    engine.process_row(make_row(100.20, seconds_offset=5))
    sides = [order["side"] for order in executor.orders]
    assert sides == ["buy", "sell"]  # open long, then close (sell)


def test_runs_fully_self_contained_without_an_executor():
    engine = make_engine([UP, UP], executor=None)
    engine.process_row(make_row(100.0, seconds_offset=0))
    actions = engine.process_row(make_row(100.20, seconds_offset=5))
    assert any(a["action"] == ACTION_CLOSED for a in actions)


def test_entry_and_exit_kwargs_are_forwarded_to_should_enter_and_should_exit():
    # probability_min set unreachably high -> even a 0.90 UP signal must
    # not open a position.
    engine = make_engine([UP], entry_kwargs={"probability_min": 0.99})
    actions = engine.process_row(make_row(100.0))
    assert actions == []


def test_short_position_pnl_direction_is_correct():
    engine = make_engine([DOWN, DOWN], exit_kwargs={"take_profit_bps": 15.0, "stop_loss_bps": 1000.0, "time_stop_s": 3600})
    engine.process_row(make_row(100.0, seconds_offset=0))
    actions = engine.process_row(make_row(99.80, seconds_offset=5))  # price fell 20bps -> short profits
    closed = [a for a in actions if a["action"] == ACTION_CLOSED][0]["position"]
    assert closed["side"] == "sell"
    assert closed["net_pnl"] > 0
